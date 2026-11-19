# -*- coding: utf-8 -*-
"""Snap selected smoke detector + speaker pairs into ceiling tiles (XY only)."""
from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI.Selection import ObjectSnapTypes
from Autodesk.Revit.Exceptions import OperationCanceledException
import tile_snap_logic as logic

MM_PER_FT = 304.8
TITLE = "Snap Fire Pairs"


def element_key(element_id):
    value = getattr(element_id, "Value", None)
    return value if value is not None else element_id.IntegerValue


def collect_devices(elements):
    devices, by_key, ignored = [], {}, []
    for el in elements:
        if not isinstance(el, DB.FamilyInstance) or el.SuperComponent is not None:
            continue
        if not isinstance(el.Location, DB.LocationPoint):
            ignored.append((el, "not point-based"))
            continue
        type_name = revit.query.get_name(el.Symbol)
        family_name = revit.query.get_name(el.Symbol.Family)
        kind = logic.classify(type_name, family_name)
        if kind is None:
            ignored.append((el, "ignored type: {0}".format(type_name)))
            continue
        point = el.Location.Point
        key = element_key(el.Id)
        devices.append(logic.Device(key, kind, point.X * MM_PER_FT, point.Y * MM_PER_FT))
        by_key[key] = el
    return devices, by_key, ignored


def pick_tile():
    snaps = ObjectSnapTypes.Intersections | ObjectSnapTypes.Endpoints
    first = revit.uidoc.Selection.PickPoint(snaps, "Snap Fire Pairs: click one CORNER of a ceiling tile")
    second = revit.uidoc.Selection.PickPoint(snaps, "Snap Fire Pairs: click the OPPOSITE corner of the same tile")
    return ((first.X * MM_PER_FT, first.Y * MM_PER_FT), (second.X * MM_PER_FT, second.Y * MM_PER_FT))


def main():
    view = revit.active_view
    if not isinstance(view, DB.ViewPlan):
        forms.alert("Open a reflected ceiling plan (or floor plan) and run {0} there.".format(TITLE),
                    title=TITLE)
        return

    selection = revit.get_selection().elements
    devices, by_key, ignored = collect_devices(selection)
    if not devices:
        forms.alert("Select the surface smoke detectors and speakers in ONE ceiling area "
                    "(a room or corridor), then run {0} again.".format(TITLE), title=TITLE)
        return

    forms.alert("Next: click two opposite corners of ONE ceiling tile in this area.\n\n"
                "Zoom in and snap to the grid line intersections. The tile size is rounded "
                "to 300 mm, so 600 x 1200 and its direction come from your clicks.",
                title=TITLE)
    try:
        corners = pick_tile()
    except OperationCanceledException:
        return
    except Exception as err:
        forms.alert("Could not pick points: {0}\n\nIf Revit says there is no work plane, set one "
                    "for this view (Architecture > Work Plane > Set) and try again.".format(err),
                    title=TITLE)
        return
    try:
        grid = logic.grid_from_corners(corners[0], corners[1])
    except ValueError as err:
        forms.alert(str(err), title=TITLE)
        return

    moves, warnings = logic.plan_moves(grid, devices)
    to_move = [m for m in moves if m.distance >= 1.0]

    output = script.get_output()
    output.print_md("## {0} - preview".format(TITLE))
    output.print_md("Tile {0:.0f} (E-W) x {1:.0f} (N-S) mm, corner at ({2:.0f}, {3:.0f}).".format(
        grid.sx, grid.sy, grid.ox, grid.oy))
    rows = []
    for m in moves:
        rows.append([output.linkify(by_key[m.device.id].Id), m.device.kind,
                     "{0:.0f}".format(m.distance), "; ".join(m.notes)])
    output.print_table(rows, columns=["Element", "Kind", "Move (mm)", "Notes"])
    for el, reason in ignored:
        output.print_md("- Ignored {0}: {1}".format(output.linkify(el.Id), reason))

    if not to_move:
        forms.alert("Everything is already in place.", title=TITLE)
        return
    question = "Move {0} device(s) in X/Y only (height unchanged)?".format(len(to_move))
    if warnings:
        question += "\n\nWarnings:\n- " + "\n- ".join(warnings[:8])
    if not forms.alert(question, title=TITLE, yes=True, no=True):
        return

    moved, skipped = 0, []
    with revit.Transaction("Snap fire pairs to tiles"):
        for m in to_move:
            el = by_key[m.device.id]
            if el.Pinned:
                skipped.append(el)
                continue
            offset = DB.XYZ((m.x - m.device.x) / MM_PER_FT, (m.y - m.device.y) / MM_PER_FT, 0)
            DB.ElementTransformUtils.MoveElement(revit.doc, el.Id, offset)
            moved += 1
    output.print_md("**Moved {0} device(s).** Skipped pinned: {1}".format(
        moved, ", ".join(output.linkify(e.Id) for e in skipped) or "none"))


if __name__ == "__main__":
    main()
