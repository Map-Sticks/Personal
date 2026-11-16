# -*- coding: utf-8 -*-
"""Modeless, searchable browser for Space and Room parameters and the elements inside them.

Modeless rules (see docs/pyrevit-patterns.md):
  * __persistentengine__ keeps the engine alive so the window's handlers keep working.
  * The window only works on cached plain-Python data. Every Revit read (refresh, loading
    extra parameter columns) and every selection change runs in ExternalEvent.Execute.
  * The tool never modifies the model, so it never opens a transaction.
"""
__title__ = "Parameter\nBrowser"
__doc__ = "Modeless searchable parameter browser for Spaces and Rooms, listing the elements in each."
__persistentengine__ = True

import os
import json
import time
import traceback
from functools import wraps

import clr
clr.AddReference("System.Data")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
from System import Int32, Int64
from System.Collections.Generic import List
from System.Data import DataTable
from System.Windows import Visibility
from System.Windows.Controls import (DataGridTextColumn, DataGridLength, DataGridRow, ItemsControl,
                                     TreeViewItem)
from System.Windows.Data import Binding

from pyrevit import DB, UI, HOST_APP, forms, revit, script

import parameter_browser_logic as logic

logger = script.get_logger()
XAML_PATH = script.get_bundle_file("UI.xaml")
USE_64_BIT_IDS = int(HOST_APP.version) >= 2024
SOURCE_CATEGORIES = {
    "Spaces": DB.BuiltInCategory.OST_MEPSpaces,
    "Rooms": DB.BuiltInCategory.OST_Rooms,
}
CONTENT_BOUNDS_PADDING = 1.0  # Revit internal feet; include nearby hosted devices.
SETTINGS_FILE = os.path.join(
    os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming"),
    "pyRevit", "ParameterBrowser.settings.json")

WINDOW = globals().get("WINDOW")


class UserError(Exception):
    pass


def load_settings():
    try:
        with open(SETTINGS_FILE, "r") as fp:
            return json.load(fp)
    except Exception:
        return {}


def save_settings(data):
    try:
        folder = os.path.dirname(SETTINGS_FILE)
        if not os.path.exists(folder):
            os.makedirs(folder)
        with open(SETTINGS_FILE, "w") as fp:
            json.dump(data, fp, indent=2)
    except Exception:
        logger.debug("Could not save settings: {}".format(traceback.format_exc()))


def as_eid(value):
    return DB.ElementId(Int64(value) if USE_64_BIT_IDS else Int32(value))


def read_levels(doc):
    level_ids = {}
    names = []
    for level in DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements():
        name = logic.to_text(level.Name)
        if name:
            level_ids[name] = level.Id
            names.append(name)
    return level_ids, sorted(set(names), key=lambda x: x.lower())


def ui_handler(func):
    """Never let an exception escape a WPF event handler of a modeless window."""
    @wraps(func)
    def wrapper(self, *args):
        try:
            return func(self, *args)
        except Exception as ex:
            logger.error(traceback.format_exc())
            self.set_status("ERROR: {}".format(ex))
    return wrapper


# --- Revit reads: only call these from the command itself or ExternalEvent.Execute ----------
def element_name(doc, element_id):
    try:
        target = doc.GetElement(element_id)
        return target.Name if target is not None else None
    except Exception:
        return None


def read_values(doc, element, rec, fields):
    for field in fields:
        if field in logic.PSEUDO_FIELDS:
            continue
        value = u""
        if element is not None:
            try:
                value = logic.format_parameter_value(
                    element.LookupParameter(field), lambda eid: element_name(doc, eid))
            except Exception:
                value = u""
        rec["values"][field] = value


def collect_source_elements(doc, category, scope="Whole project", view_id=None, level_id=None):
    collector = DB.FilteredElementCollector(doc)
    if scope == "Current view":
        if view_id is None:
            raise UserError("There is no active view to scan.")
        try:
            collector = DB.FilteredElementCollector(doc, view_id)
        except Exception as ex:
            raise UserError("The active view cannot be scanned: {}".format(ex))
    collector = collector.OfCategory(category).WhereElementIsNotElementType()
    if scope == "Level":
        if level_id is None:
            raise UserError("Select a level before refreshing selected.")
        collector = collector.WherePasses(DB.ElementLevelFilter(level_id))
    try:
        return list(collector.ToElements())
    except Exception as ex:
        if scope == "Current view":
            raise UserError("The active view cannot be scanned: {}".format(ex))
        raise


def read_source(doc, category, wanted_fields, scope="Whole project", view_id=None, level_id=None):
    """Return records, parameter names, and the spatial elements read for one scope."""
    elements = collect_source_elements(doc, category, scope, view_id, level_id)
    visible = set()
    if scope == "Current view":
        visible = set([logic.element_id_value(e.Id) for e in elements])
    elif view_id is not None:
        try:
            for e in (DB.FilteredElementCollector(doc, view_id).OfCategory(category)
                      .WhereElementIsNotElementType().ToElementIds()):
                visible.add(logic.element_id_value(e))
        except Exception:
            # Non-graphical views do not expose view-scoped element collection.
            pass

    names = set(logic.PSEUDO_FIELDS)
    for e in elements[:20]:
        for p in e.Parameters:
            try:
                if p.Definition is not None and p.Definition.Name:
                    names.add(logic.to_text(p.Definition.Name))
            except Exception:
                pass
    names = sorted(names, key=lambda x: x.lower())
    fields = logic.resolve_fields(wanted_fields, names)

    level_names = {}
    records = []
    for e in elements:
        eid = logic.element_id_value(e.Id)
        level_key = logic.element_id_value(e.LevelId)
        if level_key not in level_names:
            level_names[level_key] = logic.to_text(element_name(doc, e.LevelId) or u"")
        try:
            category_name = logic.to_text(e.Category.Name)
        except Exception:
            category_name = u""
        rec = {"id": eid, "level": level_names[level_key], "visible": eid in visible,
               "category": category_name, "label": spatial_label(e, eid), "values": {}, "counts": {}}
        read_values(doc, e, rec, fields)
        records.append(rec)
    return records, names, elements


def spatial_label(element, eid):
    number = name = u""
    try:
        number = logic.to_text(element.Number)
    except Exception:
        pass
    try:
        name = logic.format_parameter_value(element.get_Parameter(DB.BuiltInParameter.ROOM_NAME))
    except Exception:
        pass
    return u" ".join([x for x in (number, name) if x]) or u"Id {}".format(eid)


def spatial_phases(doc, elements):
    """Phases that the supplied Spaces/Rooms are placed in, latest phase first."""
    found = {}
    for e in elements:
        phase_id = None
        try:
            param = e.get_Parameter(DB.BuiltInParameter.ROOM_PHASE)
            if param is not None and param.HasValue:
                phase_id = param.AsElementId()
        except Exception:
            phase_id = None
        if phase_id is None or logic.element_id_value(phase_id) < 0:
            phase_id = e.CreatedPhaseId
        key = logic.element_id_value(phase_id)
        if key >= 0 and key not in found:
            found[key] = phase_id
    sequence = [logic.element_id_value(p.Id) for p in doc.Phases]
    phases = []
    for key in logic.order_phase_ids(list(found.keys()), sequence):
        phase = doc.GetElement(found[key])
        if phase is not None:
            phases.append(phase)
    return phases


class SpatialLookup(object):
    """FamilyInstance -> Space/Room id using get_Space(phase)/get_Room(phase) with explicit phases.

    The phase-less .Space/.Room properties are ambiguous in IronPython and only use the project's
    last phase, so they are not used.
    """

    def __init__(self, method_name, phases):
        self.method_name = method_name
        self.property_name = method_name[len("get_"):]
        self.phases = phases
        self.found = 0
        self.errors = 0
        self.first_error = None

    def lookup(self, element):
        for phase in self.phases:
            try:
                target = getattr(element, self.method_name)(phase)
            except Exception as ex:
                try:
                    target = getattr(element, self.property_name)[phase]
                except Exception:
                    self.errors += 1
                    if self.first_error is None:
                        self.first_error = logic.to_text(ex)
                    continue
            if target is not None:
                self.found += 1
                return logic.element_id_value(target.Id)
        return None

    def summary(self, noun):
        if not self.phases:
            return u"No {} found in this model.".format(noun)
        text = u"{} matched in phase: {}".format(
            noun.capitalize(), u", ".join([logic.to_text(p.Name) for p in self.phases]))
        if self.errors:
            text += u" ({} lookup error(s), first: {})".format(self.errors, self.first_error)
        return text


def content_category_map():
    category_labels = {}
    category_ids = List[DB.BuiltInCategory]()
    for label, bic_name in logic.CONTENT_CATEGORIES:
        bic = getattr(DB.BuiltInCategory, bic_name, None)
        if bic is not None:
            category_ids.Add(bic)
            category_labels[bic] = label
    return category_ids, category_labels


def read_contents(doc, space_lookup, room_lookup):
    """Family instances of CONTENT_CATEGORIES with the Space/Room each one is in."""
    items = []
    level_names = {}
    category_ids, category_labels = content_category_map()
    if not category_ids.Count:
        return items
    elements = (DB.FilteredElementCollector(doc).WherePasses(
        DB.ElementMulticategoryFilter(category_ids)).WhereElementIsNotElementType().ToElements())
    for e in elements:
        if not isinstance(e, DB.FamilyInstance):
            continue
        try:
            label = category_labels[e.Category.BuiltInCategory]
        except Exception:
            continue
        level_key = logic.element_id_value(e.LevelId)
        if level_key not in level_names:
            level_names[level_key] = logic.to_text(element_name(doc, e.LevelId) or u"")
        item = content_item(doc, e, label, level_names[level_key])
        item["space"] = space_lookup.lookup(e)
        item["room"] = room_lookup.lookup(e)
        items.append(item)
    return items


def content_item(doc, element, label, level=None):
    if level is None:
        level = logic.to_text(element_name(doc, element.LevelId) or u"")
    try:
        family = logic.to_text(element.Symbol.FamilyName)
    except Exception:
        family = u""
    try:
        mark = logic.format_parameter_value(element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK))
    except Exception:
        mark = u""
    return {
        "id": logic.element_id_value(element.Id),
        "category": label,
        "family": family,
        "type": logic.to_text(element.Name),
        "mark": mark,
        "level": level,
        "space": None,
        "room": None,
    }


def all_project_phases(doc):
    phases = list(doc.Phases)
    phases.reverse()
    return phases


def read_scoped_contents(doc, source, spatial_elements, lookup, previous_index, affected_ids):
    """Recheck only content near refreshed spaces plus items previously assigned to them."""
    key = "space" if source == "Spaces" else "room"
    cached_by_id = {}
    candidates = {}
    for indexed_items in previous_index.values():
        for item in indexed_items:
            if item.get(key) in affected_ids:
                cached_by_id[item["id"]] = item
                element = doc.GetElement(as_eid(item["id"]))
                if element is not None and isinstance(element, DB.FamilyInstance):
                    candidates[item["id"]] = element

    bounds = []
    box_errors = []
    for element in spatial_elements:
        try:
            box = element.get_BoundingBox(None)
            if box is not None:
                for x in (box.Min.X, box.Max.X):
                    for y in (box.Min.Y, box.Max.Y):
                        for z in (box.Min.Z, box.Max.Z):
                            point = box.Transform.OfPoint(DB.XYZ(x, y, z))
                            bounds.append((point.X, point.Y, point.Z))
        except Exception as ex:
            box_errors.append(logic.to_text(ex))

    if box_errors:
        raise UserError("Could not read {} space/room bounding box(es): {}"
                        .format(len(box_errors), box_errors[0]))
    if spatial_elements and not bounds:
        raise UserError("Could not read bounding boxes for the selected spaces/rooms. Use Refresh All.")

    category_ids, category_labels = content_category_map()

    if bounds and category_ids.Count:
        min_x = min([point[0] for point in bounds]) - CONTENT_BOUNDS_PADDING
        min_y = min([point[1] for point in bounds]) - CONTENT_BOUNDS_PADDING
        min_z = min([point[2] for point in bounds]) - CONTENT_BOUNDS_PADDING
        max_x = max([point[0] for point in bounds]) + CONTENT_BOUNDS_PADDING
        max_y = max([point[1] for point in bounds]) + CONTENT_BOUNDS_PADDING
        max_z = max([point[2] for point in bounds]) + CONTENT_BOUNDS_PADDING
        outline = DB.Outline(DB.XYZ(min_x, min_y, min_z), DB.XYZ(max_x, max_y, max_z))
        category_filter = DB.ElementMulticategoryFilter(category_ids)
        bounds_filter = DB.BoundingBoxIntersectsFilter(outline)
        elements = (DB.FilteredElementCollector(doc).WherePasses(category_filter)
                    .WherePasses(bounds_filter).WhereElementIsNotElementType().ToElements())
        for element in elements:
            if isinstance(element, DB.FamilyInstance):
                try:
                    label = category_labels[element.Category.BuiltInCategory]
                except Exception:
                    continue
                candidates[logic.element_id_value(element.Id)] = element

    refreshed = []
    for eid, element in candidates.items():
        previous = cached_by_id.get(eid)
        item = dict(previous) if previous is not None else content_item(doc, element, u"")
        if previous is None:
            try:
                item["category"] = category_labels[element.Category.BuiltInCategory]
            except Exception:
                continue
        item[key] = lookup.lookup(element)
        refreshed.append(item)
    return refreshed, set(candidates.keys())


class BrowserEventHandler(UI.IExternalEventHandler):
    def __init__(self, window):
        self.window = window

    def Execute(self, uiapp):
        try:
            self.window.run_queued(uiapp)
        except Exception:
            logger.error(traceback.format_exc())

    def GetName(self):
        return "Parameter Browser"


class ParameterBrowser(forms.WPFWindow):
    def __init__(self, doc, uidoc):
        self.initialising = True
        self.closed = False
        forms.WPFWindow.__init__(self, XAML_PATH, handle_esc=False)
        self.queue = logic.ActionQueue()
        self.handler = BrowserEventHandler(self)
        # Must be created while the command still has a valid API context.
        self.ext_event = UI.ExternalEvent.Create(self.handler)

        self.doc = doc
        self.doc_title = logic.to_text(doc.Title)
        view = uidoc.ActiveView
        self.view_name = logic.to_text(view.Name) if view is not None else u""
        self.view_id = view.Id if view is not None else None
        self.level_ids, self.level_names = read_levels(doc)
        self.records = {}
        self.param_names = {}
        self.pending_fields = set()
        self.table = None
        self.contents = {"Spaces": {}, "Rooms": {}}
        self.unassigned_contents = {"Spaces": [], "Rooms": []}
        self.unassigned_scanned = False
        self.unassigned_stale = False
        self.unassigned_scanned_at = None
        self.content_count = 0
        self.contents_ids = []
        self.phase_notes = {}
        settings = load_settings()
        source, scope, self.fields = logic.normalize_settings(settings)
        self.content_categories = logic.normalize_content_categories(
            settings.get("content_categories") if isinstance(settings, dict) else None)

        for name in logic.SOURCES:
            self.cmbSource.Items.Add(name)
        for name in logic.SCOPES:
            self.cmbScope.Items.Add(name)
        self.cmbSource.SelectedItem = source
        self.cmbScope.SelectedItem = scope
        self.Closed += self.on_closed

        self.initialising = False
        self.populate_levels()
        self.set_status("Choose a scope and click Refresh Selected, or click Refresh All.")
        self.rebuild_view()

    # --- state helpers ----------------------------------------------------------------------
    def current_source(self):
        return str(self.cmbSource.SelectedItem or logic.SOURCES[0])

    def current_scope(self):
        return str(self.cmbScope.SelectedItem or logic.SCOPES[0])

    def current_level(self):
        return logic.to_text(self.cmbLevel.SelectedItem) if self.cmbLevel.SelectedItem is not None else None

    def set_status(self, text):
        try:
            self.lblStatus.Text = text
        except Exception:
            pass

    def persist_settings(self):
        save_settings({"source": self.current_source(), "scope": self.current_scope(),
                       "fields": list(self.fields), "content_categories": list(self.content_categories)})

    # --- ExternalEvent plumbing -------------------------------------------------------------
    def run_in_revit(self, label, action):
        """Queue action(uiapp); it runs in Revit's API context once Revit is idle."""
        if self.closed:
            return False
        self.queue.add(label, action)
        result = self.ext_event.Raise()
        if result not in (UI.ExternalEventRequest.Accepted, UI.ExternalEventRequest.Pending):
            self.queue.clear()
            self.pending_fields = set()
            self.set_status("Revit could not queue '{}' ({}). Try again.".format(label, result))
            return False
        return True

    def run_queued(self, uiapp):
        if self.closed:
            self.queue.clear()
            return
        self.queue.drain(uiapp, self.on_action_error)

    def on_action_error(self, label, ex):
        if isinstance(ex, UserError):
            self.set_status(logic.to_text(ex))
            return
        logger.error("{} failed:\n{}".format(label, traceback.format_exc()))
        self.set_status("ERROR in {}: {}".format(label, ex))

    def cached_doc(self):
        if self.doc is None or not self.doc.IsValidObject:
            raise UserError("The project this list was read from is closed. Click Refresh All.")
        return self.doc

    def active_uidoc(self, uiapp):
        doc = self.cached_doc()
        uidoc = uiapp.ActiveUIDocument
        if uidoc is None or not doc.Equals(uidoc.Document):
            raise UserError("Switch back to '{}' or click Refresh All to load the active project."
                            .format(self.doc_title))
        return uidoc

    # --- Model reads (API context only) -----------------------------------------------------
    def capture_model(self, doc, uidoc):
        view = uidoc.ActiveView
        view_id = view.Id if view is not None else None
        source_data = {}
        source_elements = {}
        for source, category in SOURCE_CATEGORIES.items():
            records, names, elements = read_source(
                doc, category, self.fields, view_id=view_id)
            source_data[source] = (records, names)
            source_elements[source] = elements
        lookups = {
            "Spaces": SpatialLookup("get_Space", spatial_phases(doc, source_elements["Spaces"])),
            "Rooms": SpatialLookup("get_Room", spatial_phases(doc, source_elements["Rooms"])),
        }
        items = read_contents(doc, lookups["Spaces"], lookups["Rooms"])
        self.content_count = len(items)
        self.phase_notes = dict((k, v.summary(k.lower())) for k, v in lookups.items())
        for source in logic.SOURCES:
            records, names = source_data[source]
            index = logic.index_contents(items, "space" if source == "Spaces" else "room")
            for rec in records:
                rec["counts"] = logic.category_counts(index.get(rec["id"], []))
            self.records[source] = records
            self.param_names[source] = sorted(names + logic.count_fields(), key=lambda x: x.lower())
            self.contents[source] = index
            self.unassigned_contents[source] = list(index.get(None, []))
        self.unassigned_scanned = True
        self.unassigned_stale = False
        self.unassigned_scanned_at = time.strftime("%Y-%m-%d %H:%M")
        self.doc = doc
        self.doc_title = logic.to_text(doc.Title)
        self.view_name = logic.to_text(view.Name) if view is not None else u""
        self.view_id = view.Id if view is not None else None
        self.level_ids, self.level_names = read_levels(doc)
        self.pending_fields = set()

    def refresh_from_revit(self, uiapp):
        uidoc = uiapp.ActiveUIDocument
        if uidoc is None or uidoc.Document.IsFamilyDocument:
            raise UserError("Open a project (not a family) and click Refresh All again.")
        started = time.time()
        self.capture_model(uidoc.Document, uidoc)
        self.populate_levels()
        self.rebuild_view()
        total = sum([len(self.records.get(source, [])) for source in logic.SOURCES])
        self.set_status("Refresh All read {} spaces/rooms and {} content elements in {:.1f}s."
                        .format(total, self.content_count, time.time() - started))

    def refresh_selected_from_revit(self, uiapp):
        uidoc = self.active_uidoc(uiapp)
        doc = uidoc.Document
        source = self.current_source()
        scope = self.current_scope()
        level_name = self.current_level()
        level_id = self.level_ids.get(level_name) if scope == "Level" else None
        started = time.time()
        view = uidoc.ActiveView
        view_id = view.Id if view is not None else None
        records, names, elements = read_source(
            doc, SOURCE_CATEGORIES[source], self.fields, scope, view_id, level_id)

        old_records = self.records.get(source, [])
        if scope == "Level":
            affected_ids = set([rec["id"] for rec in old_records if rec.get("level") == level_name])
        elif scope == "Current view":
            affected_ids = set([rec["id"] for rec in old_records if rec.get("visible")])
        else:
            affected_ids = set([rec["id"] for rec in old_records])

        lookup = SpatialLookup("get_Space" if source == "Spaces" else "get_Room",
                               all_project_phases(doc))
        refreshed_items, candidate_ids = read_scoped_contents(
            doc, source, elements, lookup, self.contents.get(source, {}), affected_ids)
        self.phase_notes[source] = lookup.summary(source.lower())

        self.records[source] = logic.merge_scoped_records(old_records, records, scope, level_name)
        key = "space" if source == "Spaces" else "room"
        index = {}
        for indexed_items in self.contents.get(source, {}).values():
            for item in indexed_items:
                if item.get(key) not in affected_ids and item["id"] not in candidate_ids:
                    index.setdefault(item.get(key), []).append(item)
        for item in refreshed_items:
            if item.get(key) is not None:
                index.setdefault(item[key], []).append(item)
        self.contents[source] = index
        for rec in self.records[source]:
            rec["counts"] = logic.category_counts(index.get(rec["id"], []))
        if self.unassigned_scanned:
            self.unassigned_stale = True

        self.param_names[source] = sorted(
            set(self.param_names.get(source, []) + names + logic.count_fields()),
            key=lambda x: x.lower())
        self.view_name = logic.to_text(view.Name) if view is not None else u""
        self.view_id = view_id
        self.populate_levels()
        self.rebuild_view()
        self.set_status("Refresh Selected read {} {} and checked {} nearby elements in {:.1f}s. "
                        "Unassigned results are unchanged; use Scan Unassigned to update them."
                        .format(len(records), source.lower(), len(candidate_ids), time.time() - started))

    def scan_unassigned_from_revit(self, uiapp):
        uidoc = self.active_uidoc(uiapp)
        doc = uidoc.Document
        started = time.time()
        source_elements = {}
        lookups = {}
        for source, category in SOURCE_CATEGORIES.items():
            source_elements[source] = collect_source_elements(doc, category)
            lookups[source] = SpatialLookup(
                "get_Space" if source == "Spaces" else "get_Room",
                spatial_phases(doc, source_elements[source]))
        items = read_contents(doc, lookups["Spaces"], lookups["Rooms"])
        self.content_count = len(items)
        for source in logic.SOURCES:
            key = "space" if source == "Spaces" else "room"
            self.unassigned_contents[source] = list(logic.index_contents(items, key).get(None, []))
            self.phase_notes[source] = lookups[source].summary(source.lower())
        self.unassigned_scanned = True
        self.unassigned_stale = False
        self.unassigned_scanned_at = time.strftime("%Y-%m-%d %H:%M")
        self.update_contents()
        self.set_status("Scan Unassigned checked {} content elements in {:.1f}s."
                        .format(len(items), time.time() - started))

    def load_fields_from_revit(self, uiapp, source, fields):
        try:
            doc = self.cached_doc()
            for rec in self.records.get(source, []):
                read_values(doc, doc.GetElement(as_eid(rec["id"])), rec, fields)
        finally:
            self.pending_fields.difference_update([(source, f) for f in fields])
        self.rebuild_view()

    def select_in_revit(self, uiapp, element_ids, show):
        uidoc = self.active_uidoc(uiapp)
        doc = uidoc.Document
        ids = List[DB.ElementId]()
        for value in element_ids:
            element = doc.GetElement(as_eid(value))
            if element is not None:
                ids.Add(element.Id)
        if ids.Count == 0:
            raise UserError("Those elements no longer exist in the model. Click Refresh Selected or Refresh All.")
        uidoc.Selection.SetElementIds(ids)
        if show:
            try:
                uidoc.ShowElements(ids)
            except Exception as ex:
                self.set_status("{} selected, but Revit could not show them: {}".format(ids.Count, ex))
                return
        self.set_status("{} element(s) {} in Revit".format(ids.Count, "shown" if show else "selected"))

    # --- Cached-data UI (no Revit API) ------------------------------------------------------
    def populate_levels(self):
        old = self.current_level()
        was_initialising = self.initialising
        self.initialising = True
        try:
            self.cmbLevel.Items.Clear()
            for name in self.level_names:
                self.cmbLevel.Items.Add(name)
            if old in self.level_names:
                self.cmbLevel.SelectedItem = old
            elif self.level_names:
                self.cmbLevel.SelectedIndex = 0
        finally:
            self.initialising = was_initialising
        self.update_level_visibility()

    def update_level_visibility(self):
        state = Visibility.Visible if self.current_scope() == "Level" else Visibility.Collapsed
        self.lblLevel.Visibility = state
        self.cmbLevel.Visibility = state

    def rebuild_view(self):
        source = self.current_source()
        all_records = self.records.get(source, [])
        fields = logic.resolve_fields(self.fields, self.param_names.get(source, []))

        missing = [f for f in logic.missing_fields(all_records, fields)
                   if (source, f) not in self.pending_fields]
        if missing:
            self.pending_fields.update([(source, f) for f in missing])
            if self.run_in_revit("Load parameters",
                                 lambda uiapp: self.load_fields_from_revit(uiapp, source, missing)):
                self.set_status("Reading {} from the model...".format(", ".join(missing)))
            return
        if any((source, f) in self.pending_fields for f in fields):
            return

        records = logic.filter_records(all_records, self.current_scope(), self.current_level())
        table = DataTable("Rows")
        table.Columns.Add(logic.ID_COLUMN)
        table.Columns.Add(logic.SEARCH_COLUMN)
        for i, field in enumerate(fields):
            if logic.is_count_field(field):
                # Typed column so counts sort numerically.
                table.Columns.Add("P{}".format(i), clr.GetClrType(Int32))
            else:
                table.Columns.Add("P{}".format(i))
        table.BeginLoadData()
        for rec in records:
            values = [logic.record_value(rec, f) for f in fields]
            row = table.NewRow()
            row[logic.ID_COLUMN] = logic.to_text(rec["id"])
            row[logic.SEARCH_COLUMN] = logic.search_text(values)
            for i, value in enumerate(values):
                row["P{}".format(i)] = value
            table.Rows.Add(row)
        table.EndLoadData()

        self.grid.ItemsSource = None
        self.grid.Columns.Clear()
        for i, field in enumerate(fields):
            column = DataGridTextColumn()
            column.Header = field
            column.Binding = Binding("P{}".format(i))
            column.SortMemberPath = "P{}".format(i)
            column.IsReadOnly = True
            column.MinWidth = 60.0
            column.Width = DataGridLength(160.0)
            self.grid.Columns.Add(column)
        self.table = table
        self.grid.ItemsSource = table.DefaultView
        self.apply_search()
        self.update_contents()

        if source not in self.records:
            self.set_status("Choose a scope and click Refresh Selected, or click Refresh All.")
        else:
            scope = self.current_scope()
            if scope == "Current view":
                scope = u"view '{}' (as of last refresh)".format(self.view_name)
            elif scope == "Level":
                scope = u"level '{}'".format(self.current_level() or u"-")
            else:
                scope = u"whole project"
            self.set_status(u"{} {} in {} - {}".format(len(records), source.lower(), scope, self.doc_title))
        self.persist_settings()

    def apply_search(self):
        if self.table is None:
            return
        view = self.table.DefaultView
        view.RowFilter = logic.build_row_filter(self.txtSearch.Text or u"")
        self.lblCount.Text = "{} shown / {} loaded".format(view.Count, self.table.Rows.Count)

    def selected_element_ids(self):
        ids = []
        for item in self.grid.SelectedItems:
            try:
                ids.append(int(item.Row[logic.ID_COLUMN]))
            except Exception:
                pass
        return ids

    def request_selection(self, show):
        ids = self.selected_element_ids()
        if not ids:
            self.set_status("Select one or more rows first.")
            return
        if self.run_in_revit("Show elements" if show else "Select elements",
                             lambda uiapp: self.select_in_revit(uiapp, ids, show)):
            self.set_status("Sending {} element(s) to Revit...".format(len(ids)))

    # --- Contents side panel (cached data only) ---------------------------------------------
    def singular(self):
        return "space" if self.current_source() == "Spaces" else "room"

    def update_contents(self):
        source = self.current_source()
        index = self.contents.get(source, {})
        noun = self.singular()
        if self.chkUnassigned.IsChecked:
            items = self.unassigned_contents.get(source, [])
            if self.unassigned_scanned:
                scanned_at = self.unassigned_scanned_at or u"unknown time"
                state = (u"; last scan {} (stale after scoped refresh)".format(scanned_at)
                         if self.unassigned_stale else u"; whole-project scan {}".format(scanned_at))
                header = u"Not in any {} - {} element(s){}".format(
                    noun, len(items), state)
            else:
                header = u"Unassigned results have not been scanned. Click Scan Unassigned."
        else:
            ids = self.selected_element_ids()
            items = []
            for eid in ids:
                items.extend(index.get(eid, []))
            if not ids:
                header = u"Select a {} to list the elements in it.".format(noun)
            elif len(ids) == 1:
                rec = [r for r in self.records.get(source, []) if r["id"] == ids[0]]
                header = u"{} - {} element(s)".format(rec[0]["label"] if rec else ids[0], len(items))
            else:
                header = u"{} {}s selected - {} element(s)".format(len(ids), noun, len(items))
        groups = logic.group_contents(items, self.content_categories)
        self.contents_ids = [i for _, ids_, _ in groups for i in ids_]
        if len(self.contents_ids) != len(items):
            header += u" ({} hidden by Categories)".format(len(items) - len(self.contents_ids))
        note = self.phase_notes.get(source)
        if self.chkUnassigned.IsChecked and not self.unassigned_scanned:
            note = None
        self.lblContents.Text = header + (u"\n" + note if note else u"")

        self.treeContents.Items.Clear()
        for category, cat_ids, types in groups:
            cat_node = self.tree_node(u"{} ({})".format(category, len(cat_ids)), cat_ids)
            cat_node.IsExpanded = True
            for type_name, type_ids, instances in types:
                type_node = self.tree_node(u"{} ({})".format(type_name, len(type_ids)), type_ids)
                for label, iid in instances:
                    type_node.Items.Add(self.tree_node(label, [iid]))
                cat_node.Items.Add(type_node)
            self.treeContents.Items.Add(cat_node)

    @staticmethod
    def tree_node(header, ids):
        node = TreeViewItem()
        node.Header = header
        node.Tag = list(ids)
        return node

    def contents_selection(self):
        """Ids of the highlighted tree node, or everything listed when nothing is highlighted."""
        node = self.treeContents.SelectedItem
        if node is not None and node.Tag:
            return list(node.Tag)
        return list(self.contents_ids)

    def request_contents(self, show):
        ids = self.contents_selection()
        if not ids:
            self.set_status("No elements listed.")
            return
        if self.run_in_revit("Show elements" if show else "Select elements",
                             lambda uiapp: self.select_in_revit(uiapp, ids, show)):
            self.set_status("Sending {} element(s) to Revit...".format(len(ids)))

    # --- WPF event handlers (names referenced from UI.xaml) ---------------------------------
    @ui_handler
    def grid_selection_changed(self, sender, args):
        if not self.initialising and not self.chkUnassigned.IsChecked:
            self.update_contents()

    @ui_handler
    def unassigned_toggled(self, sender, args):
        if not self.initialising:
            self.update_contents()

    @ui_handler
    def choose_categories(self, sender, args):
        items = [forms.TemplateListItem(c, checked=(c in self.content_categories)) for c in logic.CONTENT_LABELS]
        chosen = forms.SelectFromList.show(items, title="Categories listed in the panel", multiselect=True,
                                           button_name="Apply")
        if chosen is None:
            return
        self.content_categories = logic.normalize_content_categories([logic.to_text(c) for c in chosen])
        self.persist_settings()
        self.update_contents()

    @ui_handler
    def contents_select_clicked(self, sender, args):
        self.request_contents(False)

    @ui_handler
    def contents_show_clicked(self, sender, args):
        self.request_contents(True)

    @ui_handler
    def source_changed(self, sender, args):
        if self.initialising:
            return
        self.populate_levels()
        self.rebuild_view()

    @ui_handler
    def scope_changed(self, sender, args):
        if self.initialising:
            return
        self.update_level_visibility()
        self.rebuild_view()

    @ui_handler
    def level_changed(self, sender, args):
        if self.initialising or self.current_scope() != "Level":
            return
        self.rebuild_view()

    @ui_handler
    def search_changed(self, sender, args):
        if not self.initialising:
            self.apply_search()

    @ui_handler
    def clear_clicked(self, sender, args):
        self.txtSearch.Text = ""

    @ui_handler
    def choose_columns(self, sender, args):
        names = self.param_names.get(self.current_source(), [])
        current = logic.resolve_fields(self.fields, names)
        items = [forms.TemplateListItem(n, checked=(n in current))
                 for n in logic.column_choices(names, current)]
        chosen = forms.SelectFromList.show(items, title="Choose Parameters", multiselect=True,
                                           button_name="Apply Columns")
        if chosen is None:
            return
        if not chosen:
            self.set_status("Select at least one parameter.")
            return
        self.fields = [logic.to_text(x) for x in chosen]
        self.rebuild_view()

    @ui_handler
    def refresh_selected_clicked(self, sender, args):
        if self.run_in_revit("Refresh selected", self.refresh_selected_from_revit):
            scope = self.current_scope()
            label = self.current_level() if scope == "Level" else scope.lower()
            self.set_status("Reading {} {}...".format(label or "selected", self.current_source().lower()))

    @ui_handler
    def refresh_all_clicked(self, sender, args):
        if self.run_in_revit("Refresh all", self.refresh_from_revit):
            self.set_status("Reading the whole model...")

    @ui_handler
    def scan_unassigned_clicked(self, sender, args):
        if self.run_in_revit("Scan unassigned", self.scan_unassigned_from_revit):
            self.set_status("Scanning unassigned elements across the whole model...")

    @ui_handler
    def select_clicked(self, sender, args):
        self.request_selection(False)

    @ui_handler
    def show_clicked(self, sender, args):
        self.request_selection(True)

    @ui_handler
    def row_double_clicked(self, sender, args):
        # Ignore double-clicks on headers and scrollbars.
        if isinstance(ItemsControl.ContainerFromElement(self.grid, args.OriginalSource), DataGridRow):
            self.request_selection(True)

    @ui_handler
    def close_clicked(self, sender, args):
        self.Close()

    def on_closed(self, sender, args):
        global WINDOW
        self.closed = True
        self.queue.clear()
        self.persist_settings()
        try:
            self.ext_event.Dispose()
        except Exception:
            pass
        if WINDOW is self:
            WINDOW = None


def main():
    global WINDOW
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Open a Revit project before running Parameter Browser.", title="Parameter Browser")
        return
    if WINDOW is not None and not WINDOW.closed:
        try:
            WINDOW.Close()
        except Exception:
            pass
    WINDOW = ParameterBrowser(doc, revit.uidoc)
    WINDOW.show(modal=False)


main()
