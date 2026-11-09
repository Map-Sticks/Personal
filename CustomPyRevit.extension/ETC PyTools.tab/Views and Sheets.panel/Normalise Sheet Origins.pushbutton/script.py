# -*- coding: utf-8 -*-
"""Move everything on misaligned sheets so the title block's bottom-left corner
matches the reference sheet's title block bottom-left corner."""
__title__ = "Normalise Sheet\nOrigins"
__author__ = "Map-Sticks"
__doc__ = ("Align sheets to a reference sheet: moves the title block and all sheet content "
           "together so the title block bottom-left corner matches the reference.")

from pyrevit import DB, forms, revit, script

import sheet_origins_logic as logic

TITLE = "Normalise Sheet Origins"
TOLERANCE_MM = 0.01


def id_value(element_id):
    if element_id is None:
        return -1
    value = getattr(element_id, "Value", None)
    if value is None:
        value = getattr(element_id, "IntegerValue", -1)
    return int(value)


def to_mm(value):
    return DB.UnitUtils.ConvertFromInternalUnits(value, DB.UnitTypeId.Millimeters)


def from_mm(value):
    return DB.UnitUtils.ConvertToInternalUnits(value, DB.UnitTypeId.Millimeters)


def fmt_xy(xy):
    return "({:.3f}, {:.3f})".format(to_mm(xy[0]), to_mm(xy[1]))


def is_nested(element):
    return isinstance(element, DB.FamilyInstance) and element.SuperComponent is not None


def sheet_titleblocks(doc):
    """Map sheet id -> list of top-level (non-nested) title blocks on that sheet."""
    result = {}
    blocks = (
        DB.FilteredElementCollector(doc)
        .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
        .WhereElementIsNotElementType()
        .ToElements()
    )
    for block in blocks:
        if is_nested(block):
            continue
        result.setdefault(id_value(block.OwnerViewId), []).append(block)
    return result


def bottom_left(titleblock, sheet):
    box = titleblock.get_BoundingBox(sheet)
    if box is None:
        return None
    return (box.Min.X, box.Min.Y)


def has_sheet_graphics(element, sheet):
    try:
        return element.get_BoundingBox(sheet) is not None
    except Exception:
        return False


def movement_roots(doc, sheet):
    """Sheet elements to pass to MoveElements; anything that follows a host is left out."""
    elements = (
        DB.FilteredElementCollector(doc)
        .WherePasses(DB.ElementOwnerViewFilter(sheet.Id))
        .WhereElementIsNotElementType()
        .ToElements()
    )
    candidates = {}
    for element in elements:
        is_schedule = isinstance(element, DB.ScheduleSheetInstance)
        is_placed_view = is_schedule or isinstance(element, DB.Viewport)
        info = {
            "is_view": isinstance(element, (DB.View, DB.Sketch, DB.SketchPlane)),
            "has_category": element.Category is not None,
            "in_group": id_value(element.GroupId) != -1,
            "is_nested": is_nested(element),
            "is_revision_schedule": is_schedule and element.IsTitleblockRevisionSchedule,
            "is_viewport_or_schedule": is_placed_view,
            "has_bbox": is_placed_view or has_sheet_graphics(element, sheet),
        }
        if logic.is_movement_root(info):
            candidates[id_value(element.Id)] = element
    dependents = {}
    for key, element in candidates.items():
        try:
            dependents[key] = set(id_value(item) for item in element.GetDependentElements(None))
        except Exception:
            dependents[key] = set()
    keys = logic.drop_followers(sorted(candidates), dependents)
    return [candidates[key] for key in keys]


def move_sheet(doc, sheet, titleblock, reference_corner, tolerance):
    """Move one sheet's content in its own transaction. Returns a result dict."""
    from System.Collections.Generic import List

    before = bottom_left(titleblock, sheet)
    result = {"before": before, "after": None, "shift": None, "moved": 0, "status": ""}
    if before is None:
        result["status"] = "Skipped: title block has no bounding box"
        return result
    shift = logic.corner_shift(reference_corner, before)
    result["shift"] = shift
    if not logic.needs_shift(shift, tolerance):
        result["status"] = "Already aligned"
        result["after"] = before
        return result

    roots = movement_roots(doc, sheet)
    ids = List[DB.ElementId]()
    for element in roots:
        ids.Add(element.Id)
    pinned = [element for element in roots if element.Pinned]
    transaction = DB.Transaction(doc, "Normalise sheet {}".format(sheet.SheetNumber))
    transaction.Start()
    try:
        for element in pinned:
            element.Pinned = False
        DB.ElementTransformUtils.MoveElements(doc, ids, DB.XYZ(shift[0], shift[1], 0.0))
        for element in pinned:
            element.Pinned = True
        doc.Regenerate()
        after = bottom_left(titleblock, sheet)
        result["after"] = after
        if after is None or logic.needs_shift(logic.corner_shift(reference_corner, after), tolerance):
            transaction.RollBack()
            result["status"] = "FAILED: title block did not reach the reference corner; rolled back"
            return result
        if transaction.Commit() != DB.TransactionStatus.Committed:
            result["status"] = "FAILED: Revit did not commit (see Revit warnings); rolled back"
            return result
    except Exception as ex:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
        result["status"] = "FAILED: {}; rolled back".format(ex)
        return result
    finally:
        transaction.Dispose()
    result["moved"] = len(roots)
    result["status"] = "Moved"
    return result


def sheet_label(sheet):
    return u"{} - {}".format(sheet.SheetNumber, sheet.Name)


def fmt_mm(value):
    return "{:.3f}".format(to_mm(value))


def audit_sheets(sheets, blocks_by_sheet, reference, reference_corner, tolerance, progress=None):
    """One row per sheet: offset of its title block corner from the reference corner.

    Returns (rows, completed); completed is False if the user cancelled.
    """
    rows = []
    for index, sheet in enumerate(sheets):
        if progress is not None:
            if progress.cancelled:
                return rows, False
            progress.update_progress(index + 1, len(sheets))
        row = {"sheet": sheet, "titleblock": None, "offset": None, "status": ""}
        blocks = blocks_by_sheet.get(id_value(sheet.Id), [])
        if id_value(sheet.Id) == id_value(reference.Id):
            row["status"] = "Reference"
        elif not blocks:
            row["status"] = "No title block"
        elif len(blocks) > 1:
            row["status"] = "Multiple title blocks"
        else:
            corner = bottom_left(blocks[0], sheet)
            if corner is None:
                row["status"] = "Title block has no bounding box"
            else:
                row["titleblock"] = blocks[0]
                row["offset"] = (corner[0] - reference_corner[0], corner[1] - reference_corner[1])
                row["status"] = logic.audit_status(row["offset"], tolerance)
        row["action"] = ""
        rows.append(row)
    return rows, True


def align_sheets(doc, to_move, reference_corner, tolerance, progress=None):
    """Move each row's sheet in its own transaction; stops between sheets on cancel.

    Returns True if all rows were processed.
    """
    group = DB.TransactionGroup(doc, TITLE)
    group.Start()
    completed = True
    try:
        for index, row in enumerate(to_move):
            if progress is not None:
                if progress.cancelled:
                    completed = False
                    break
                progress.update_progress(index + 1, len(to_move))
            result = move_sheet(doc, row["sheet"], row["titleblock"], reference_corner, tolerance)
            row["action"] = result["status"]
            if result["status"] == "Moved":
                row["action"] = "Moved ({} elements)".format(result["moved"])
        group.Assimilate()
    except Exception:
        group.RollBack()
        raise
    if not completed:
        for row in to_move:
            if row["action"] == "Not moved":
                row["action"] = "Not moved (cancelled)"
    return completed


def offset_cells(row):
    if row["offset"] is None:
        return ["-", "-"]
    return [fmt_mm(row["offset"][0]), fmt_mm(row["offset"][1])]


def print_audit(output, rows, title):
    output.print_md("### {}".format(title))
    output.print_table(
        [[output.linkify(row["sheet"].Id, sheet_label(row["sheet"]))] + offset_cells(row)
         + [row["status"], row["action"]] for row in rows],
        columns=["Sheet", "Offset X (mm)", "Offset Y (mm)", "Audit", "Action"],
    )


def save_record(doc, rows, reference, reference_corner):
    import datetime
    import io

    stamp = datetime.datetime.now()
    path = forms.save_file(
        file_ext="csv",
        default_name="Sheet origin audit {}".format(stamp.strftime("%Y-%m-%d %H%M")),
    )
    if not path:
        return None
    lines = [
        logic.csv_row(["Project", doc.Title]),
        logic.csv_row(["Date", stamp.strftime("%Y-%m-%d %H:%M")]),
        logic.csv_row(["Reference sheet", sheet_label(reference)]),
        logic.csv_row(["Reference corner X (mm)", fmt_mm(reference_corner[0]),
                       "Y (mm)", fmt_mm(reference_corner[1])]),
        "",
        logic.csv_row(["Sheet number", "Sheet name", "Offset X (mm)", "Offset Y (mm)",
                       "Audit", "Action"]),
    ]
    for row in rows:
        lines.append(logic.csv_row(
            [row["sheet"].SheetNumber, row["sheet"].Name] + offset_cells(row)
            + [row["status"], row["action"]]
        ))
    with io.open(path, "w", encoding="utf-8-sig") as handle:
        handle.write(u"\n".join(lines) + u"\n")
    return path


def main():
    doc = revit.doc
    if doc.IsFamilyDocument:
        forms.alert("Open a project document.", title=TITLE)
        return
    blocks_by_sheet = sheet_titleblocks(doc)
    sheets = [
        sheet for sheet in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet).ToElements()
        if not sheet.IsPlaceholder
    ]
    sheets.sort(key=lambda sheet: logic.natural_sort_key(sheet.SheetNumber))
    candidates = [s for s in sheets if len(blocks_by_sheet.get(id_value(s.Id), [])) == 1]
    if len(candidates) < 2:
        forms.alert("Need at least two sheets that each have exactly one title block.", title=TITLE)
        return
    by_label = dict((sheet_label(sheet), sheet) for sheet in candidates)

    reference_label = forms.SelectFromList.show(
        [sheet_label(sheet) for sheet in candidates],
        title="Pick the REFERENCE sheet (correctly placed)",
        multiselect=False, button_name="Use as reference",
    )
    if not reference_label:
        return
    reference = by_label[reference_label]
    reference_corner = bottom_left(blocks_by_sheet[id_value(reference.Id)][0], reference)
    if reference_corner is None:
        forms.alert("The reference title block has no bounding box on its sheet.", title=TITLE)
        return

    all_option = "Audit all {} sheets".format(len(sheets))
    choose_option = "Choose sheets to audit..."
    scope = forms.CommandSwitchWindow.show(
        [all_option, choose_option],
        message="Reference: {}. Which sheets should be audited? (Esc to cancel)".format(
            reference.SheetNumber),
    )
    if not scope:
        return
    if scope == choose_option:
        others = [sheet for sheet in sheets if id_value(sheet.Id) != id_value(reference.Id)]
        picked = forms.SelectFromList.show(
            [sheet_label(sheet) for sheet in others],
            title="Pick the sheets to audit against {}".format(reference.SheetNumber),
            multiselect=True, button_name="Audit selected sheets",
        )
        if not picked:
            return
        picked = set(picked)
        sheets = [reference] + [sheet for sheet in others if sheet_label(sheet) in picked]

    tolerance = from_mm(TOLERANCE_MM)
    with forms.ProgressBar(
        title="Auditing sheets {value} of {max_value} (Cancel to stop)", cancellable=True
    ) as progress:
        rows, audit_complete = audit_sheets(
            sheets, blocks_by_sheet, reference, reference_corner, tolerance, progress
        )
    misaligned = [row for row in rows if row["status"] == logic.STATUS_MISALIGNED]
    aligned = sum(1 for row in rows if row["status"] == logic.STATUS_ALIGNED)
    skipped = sum(1 for row in rows if row["status"] not in (
        logic.STATUS_MISALIGNED, logic.STATUS_ALIGNED, "Reference"))

    output = script.get_output()
    output.print_md("## {}".format(TITLE))
    output.print_md("Reference **{}**: title block bottom-left at **{} mm**. Tolerance {} mm.".format(
        reference_label, fmt_xy(reference_corner), TOLERANCE_MM))
    summary = "{} misaligned, {} aligned, {} skipped (no/multiple title blocks).".format(
        len(misaligned), aligned, skipped)
    if not audit_complete:
        summary = "AUDIT CANCELLED after {} of {} sheets: {}".format(len(rows), len(sheets), summary)
    output.print_md("**Audit:** " + summary)

    if misaligned:
        align_all = "Align all {} misaligned sheets".format(len(misaligned))
        choose = "Choose which sheets to align"
        audit_only = "Audit only (move nothing)"
        choice = forms.alert(
            "Audit: {}\n\nMisaligned:\n{}".format(
                summary,
                "\n".join("  {}  (X {} mm, Y {} mm)".format(
                    row["sheet"].SheetNumber, *offset_cells(row)) for row in misaligned[:15])
                + ("\n  ... and {} more".format(len(misaligned) - 15) if len(misaligned) > 15 else ""),
            ),
            title=TITLE, options=[align_all, choose, audit_only],
        )
        if choice == choose:
            picked = forms.SelectFromList.show(
                [sheet_label(row["sheet"]) for row in misaligned],
                title="Pick the sheets to align to {}".format(reference.SheetNumber),
                multiselect=True, button_name="Align selected sheets",
            ) or []
            to_move = [row for row in misaligned if sheet_label(row["sheet"]) in picked]
        elif choice == align_all:
            to_move = misaligned
        else:
            to_move = []
    else:
        forms.alert("Audit: {}\nNothing needs moving.".format(summary), title=TITLE)
        to_move = []

    for row in misaligned:
        row["action"] = "Not moved"
    if to_move and not forms.alert(
        "Move {} sheet(s) to match {}?\n\nEach sheet's title block and content move together. "
        "You can cancel from the progress bar; sheets already moved can be undone with Undo.".format(
            len(to_move), reference.SheetNumber),
        title=TITLE, yes=True, no=True,
    ):
        to_move = []
    if to_move and doc.IsReadOnly:
        forms.alert("The document is read-only; nothing was moved.", title=TITLE)
        to_move = []
    if to_move:
        with forms.ProgressBar(
            title="Aligning sheets {value} of {max_value} (Cancel stops after the current sheet)",
            cancellable=True,
        ) as progress:
            align_complete = align_sheets(doc, to_move, reference_corner, tolerance, progress)
        if not align_complete:
            output.print_md("**Alignment cancelled.** Sheets already moved stay moved (Undo reverses them).")

    print_audit(output, rows, "Sheets")
    moved = sum(1 for row in rows if row["action"].startswith("Moved"))
    failed = sum(1 for row in rows if row["action"].startswith("FAILED"))
    result_text = "{} moved, {} failed.".format(moved, failed) if to_move else "Nothing moved."
    output.print_md("**Result:** {} Undo in Revit reverses the whole run.".format(result_text))

    if forms.alert(
        "{}\n{}\n\nSave a CSV record of this audit?".format(summary, result_text),
        title=TITLE, yes=True, no=True,
    ):
        path = save_record(doc, rows, reference, reference_corner)
        if path:
            output.print_md("Record saved: `{}`".format(path))


if __name__ == "__main__":
    main()
