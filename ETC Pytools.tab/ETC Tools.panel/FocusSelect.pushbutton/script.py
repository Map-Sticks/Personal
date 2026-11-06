# -*- coding: utf-8 -*-
"""Focus Select - select elements of one or more categories only.

HOW IT WORKS
- Preselected elements: all of their categories become the filter.
- Nothing preselected: pick an example element first to set the category.
- Each round: click and/or drag (window or crossing), then FINISH.
    * Round contains new elements      -> they are ADDED (locked, shown orange).
      Locked elements caught in the same round are ignored (never removed).
    * Round contains ONLY locked items -> they are REMOVED.
- FINISH with nothing picked, or Esc -> tool ends, locked elements stay selected.
  (Esc discards only the round in progress - Revit does not return those picks.)
"""

from Autodesk.Revit.DB import (ElementId, Transaction, TransactionGroup,
                               TransactionStatus, OverrideGraphicSettings,
                               Color, FilteredElementCollector,
                               FillPatternElement)
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException
from System.Collections.Generic import List

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document
view = uidoc.ActiveView

HIGHLIGHT = Color(255, 128, 0)      # orange
MAX_LABEL = 60


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def id_int(eid):
    """ElementId -> int. Revit 2024+ has .Value (IntegerValue removed in 2026)."""
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def cat_int(el):
    try:
        cat = el.Category if el is not None else None
        return id_int(cat.Id) if cat is not None else None
    except Exception:
        return None


def to_id_list(ids):
    out = List[ElementId]()
    for i in ids:
        out.Add(i)
    return out


class CategoryFilter(ISelectionFilter):
    """Allow only elements whose category is in `keys`.
    Must never raise - an exception here silently breaks the pick."""
    def __init__(self, keys):
        self.keys = keys

    def AllowElement(self, el):
        try:
            return cat_int(el) in self.keys
        except Exception:
            return False

    def AllowReference(self, ref, pt):
        return False


class AnyCategoryFilter(ISelectionFilter):
    def AllowElement(self, el):
        try:
            return cat_int(el) is not None
        except Exception:
            return False

    def AllowReference(self, ref, pt):
        return False


# --------------------------------------------------------------------------
# Temporary highlight (view element overrides inside a TransactionGroup that
# is rolled back at the end - nothing is left in the model or Undo list)
# --------------------------------------------------------------------------
class Highlighter(object):
    def __init__(self):
        self.enabled = self._can_override()
        self.ogs = self._make_ogs() if self.enabled else None
        self.original = {}          # int id -> original OverrideGraphicSettings

    @staticmethod
    def _can_override():
        try:
            if doc.IsReadOnly or view is None or view.IsTemplate:
                return False
            return view.AreGraphicsOverridesAllowed()
        except Exception:
            return False

    @staticmethod
    def _make_ogs():
        ogs = OverrideGraphicSettings()
        ogs.SetProjectionLineColor(HIGHLIGHT)
        ogs.SetCutLineColor(HIGHLIGHT)
        solid = None
        for fp in FilteredElementCollector(doc).OfClass(FillPatternElement):
            try:
                if fp.GetFillPattern().IsSolidFill:
                    solid = fp.Id
                    break
            except Exception:
                pass
        if solid is not None:
            try:                                    # Revit 2019+
                ogs.SetSurfaceForegroundPatternId(solid)
                ogs.SetSurfaceForegroundPatternColor(HIGHLIGHT)
            except Exception:
                try:                                # Revit 2018 and older
                    ogs.SetProjectionFillPatternId(solid)
                    ogs.SetProjectionFillColor(HIGHLIGHT)
                except Exception:
                    pass
        return ogs

    def update(self, add_ids, remove_ids):
        if not self.enabled or (not add_ids and not remove_ids):
            return
        t = Transaction(doc, "Focus Select highlight")
        try:
            t.Start()
            for eid in add_ids:
                try:
                    k = id_int(eid)
                    if k not in self.original:
                        self.original[k] = view.GetElementOverrides(eid)
                    view.SetElementOverrides(eid, self.ogs)
                except Exception:
                    pass
            for eid in remove_ids:
                try:
                    orig = self.original.pop(id_int(eid), None)
                    view.SetElementOverrides(eid, orig or OverrideGraphicSettings())
                except Exception:
                    pass
            if t.Commit() != TransactionStatus.Committed:
                self.enabled = False
        except Exception:
            # e.g. view owned by another user in a workshared model
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
            self.enabled = False
        try:
            uidoc.RefreshActiveView()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def get_start_state():
    """Returns (locked dict, category int set, category names) or None."""
    locked, keys, names = {}, set(), set()
    for eid in uidoc.Selection.GetElementIds():
        el = doc.GetElement(eid)
        k = cat_int(el)
        if k is None:
            continue
        locked[id_int(eid)] = eid
        keys.add(k)
        names.add(el.Category.Name)

    if keys:
        return locked, keys, names

    try:
        ref = uidoc.Selection.PickObject(
            ObjectType.Element, AnyCategoryFilter(),
            "FOCUS SELECT: Pick an example element to set the category (Esc to cancel)")
    except OperationCanceledException:
        return None
    el = doc.GetElement(ref.ElementId)
    if cat_int(el) is None:
        return None
    locked[id_int(el.Id)] = el.Id
    keys.add(cat_int(el))
    names.add(el.Category.Name)
    return locked, keys, names


def run():
    if view is None:
        return
    start = get_start_state()
    if start is None:
        return
    locked, keys, names = start

    label = ", ".join(sorted(names))
    if len(label) > MAX_LABEL:
        label = "{} categories".format(len(names))

    filt = CategoryFilter(keys)
    hl = Highlighter()

    # Clear Revit's selection so locked elements are not also blue-selected
    # while picking (avoids confusion with the pick-session highlight).
    uidoc.Selection.SetElementIds(List[ElementId]())

    tg = TransactionGroup(doc, "Focus Select")
    tg_started = False
    if hl.enabled:
        try:
            tg_started = tg.Start() == TransactionStatus.Started
        except Exception:
            hl.enabled = False
        if not tg_started:
            hl.enabled = False

    try:
        hl.update(list(locked.values()), [])

        while True:
            prompt = ("FOCUS SELECT [{}] {} locked | Click/drag then FINISH to add | "
                      "Round of only orange items = remove | Finish empty or Esc = done"
                      ).format(label, len(locked))
            try:
                refs = uidoc.Selection.PickObjects(ObjectType.Element, filt, prompt)
            except OperationCanceledException:
                break

            round_ids = {}
            for r in refs:
                round_ids[id_int(r.ElementId)] = r.ElementId
            if not round_ids:
                break                                   # empty Finish = done

            new = [k for k in round_ids if k not in locked]
            if new:                                     # ADD round
                added = []
                for k in new:
                    locked[k] = round_ids[k]
                    added.append(round_ids[k])
                hl.update(added, [])
            else:                                       # REMOVE round
                removed = [locked.pop(k) for k in round_ids]
                hl.update([], removed)
    finally:
        if tg_started:
            try:
                tg.RollBack()                           # drops all overrides
            except Exception:
                pass
        final = [eid for eid in locked.values() if doc.GetElement(eid) is not None]
        uidoc.Selection.SetElementIds(to_id_list(final))


run()
