"""Rename selected sheet names or sheet numbers in batch."""

from pyrevit import revit, DB
from pyrevit import forms


TITLE = "Rename Selected Sheets"

FIELD_NAME = "Sheet Name"
FIELD_NUMBER = "Sheet Number"
RENAME_FIELDS = [FIELD_NAME, FIELD_NUMBER]

RENAME_MODES = [
    "Find & Replace",
    "Add Prefix",
    "Add Suffix",
    "to UPPERCASE",
    "to lowercase",
]


def _field_param(field):
    if field == FIELD_NUMBER:
        return DB.BuiltInParameter.SHEET_NUMBER
    return DB.BuiltInParameter.SHEET_NAME


def _get_sheet_value(sheet, field):
    return sheet.Parameter[_field_param(field)].AsString() or ""


def _set_sheet_value(sheet, field, value):
    sheet.Parameter[_field_param(field)].Set(value)


def _sheet_label(sheet, field):
    """Identify a sheet by the field that is NOT being renamed."""
    other = FIELD_NAME if field == FIELD_NUMBER else FIELD_NUMBER
    return _get_sheet_value(sheet, other)


def _collect_rename_inputs(mode, field):
    noun = field.lower() + "s"
    if mode == "Find & Replace":
        find_txt = forms.ask_for_string(
            prompt="Find text in current {}:".format(noun), title=TITLE
        )
        if find_txt is None:
            return None
        if find_txt == "":
            forms.alert("Find text cannot be empty.")
            return None
        replace_txt = forms.ask_for_string(
            default="", prompt="Replace with:", title=TITLE
        )
        if replace_txt is None:
            return None
        return {"find": find_txt, "replace": replace_txt}

    if mode in ("Add Prefix", "Add Suffix"):
        kind = "Prefix" if mode == "Add Prefix" else "Suffix"
        txt = forms.ask_for_string(
            default="",
            prompt="{} to add to selected {}:".format(kind, noun),
            title=TITLE,
        )
        if txt is None:
            return None
        if txt == "":
            forms.alert("{} cannot be empty.".format(kind))
            return None
        return {kind.lower(): txt}

    return {}


def _build_new_name(old_name, mode, data):
    if mode == "Find & Replace":
        return old_name.replace(data["find"], data["replace"])
    if mode == "Add Prefix":
        return "{}{}".format(data["prefix"], old_name)
    if mode == "Add Suffix":
        return "{}{}".format(old_name, data["suffix"])
    if mode == "to UPPERCASE":
        return old_name.upper()
    if mode == "to lowercase":
        return old_name.lower()
    return old_name


def _conflict(pair, reason):
    return {
        "label": pair["label"],
        "old_name": pair["old_name"],
        "new_name": pair["new_name"],
        "reason": reason,
    }


def _resolve_conflicts(rename_pairs, all_values):
    """Split pairs into (valid, conflicts).

    all_values maps sheet id -> current value of the renamed field for every
    sheet in the model. Targets must be non-empty, unique within the selection,
    and must not collide with a sheet that keeps its current value.
    """
    conflicts = []
    valid_pairs = []
    target_names = {}
    order = []
    for pair in rename_pairs:
        if not pair["new_name"].strip():
            conflicts.append(_conflict(pair, "Target value is empty"))
            continue
        if pair["new_name"] not in target_names:
            order.append(pair["new_name"])
        target_names.setdefault(pair["new_name"], []).append(pair)

    for tname in order:
        pairs = target_names[tname]
        if len(pairs) > 1:
            for pair in pairs:
                conflicts.append(
                    _conflict(pair, "Target value is repeated in selected sheets")
                )
        else:
            valid_pairs.append(pairs[0])

    # Removing a pair keeps its sheet on the old value, which can block others.
    unique_pairs = list(valid_pairs)
    while True:
        moving_ids = set(x["sheet_id"] for x in unique_pairs)
        existing = set(
            value for sid, value in all_values.items() if sid not in moving_ids
        )
        next_unique_pairs = []
        removed_any = False
        for pair in unique_pairs:
            if pair["new_name"] in existing:
                conflicts.append(
                    _conflict(pair, "Target value already exists in model")
                )
                removed_any = True
            else:
                next_unique_pairs.append(pair)
        unique_pairs = next_unique_pairs
        if not removed_any:
            break

    return unique_pairs, conflicts


def _print_preview(rename_pairs, field):
    print("{} rename preview:".format(field))
    for idx, pair in enumerate(rename_pairs):
        if idx >= 25:
            print("... and {} more".format(len(rename_pairs) - 25))
            break
        print("[{}] {} -> {}".format(pair["label"], pair["old_name"], pair["new_name"]))


def _report_results(field, renamed, unchanged, conflicts, failed):
    print("Rename Selected Sheets results ({})".format(field))
    print("  Renamed: {}".format(len(renamed)))
    print("  Unchanged: {}".format(len(unchanged)))
    print("  Conflicts skipped: {}".format(len(conflicts)))
    print("  Failed: {}".format(len(failed)))
    if renamed:
        print("\nRenamed sheets:")
        for item in renamed:
            print("  [{}] {} -> {}".format(
                item["label"], item["old_name"], item["new_name"]))
    if conflicts:
        print("\nSkipped because target value was not valid or unique:")
        for item in conflicts:
            print("  [{}] {} -> {} ({})".format(
                item["label"], item["old_name"], item["new_name"], item["reason"]))
    if failed:
        print("\nFailed to rename:")
        for item in failed:
            print("  [{}] {} -> {} ({})".format(
                item["label"], item["old_name"], item["new_name"], item["reason"]))


def _execute_renames(rename_pairs, field):
    """Two-pass rename via temporary values so values can be swapped safely."""
    renamed = []
    failed = []
    temp_name_map = {}

    with revit.Transaction("Rename Selected Sheets ({})".format(field)):
        for idx, pair in enumerate(rename_pairs):
            sheet = pair["sheet"]
            temp_name = "__pyrevit_temp_sheet_value_{}_{}__".format(
                pair["sheet_id"], idx
            )
            try:
                _set_sheet_value(sheet, field, temp_name)
                temp_name_map[pair["sheet_id"]] = temp_name
            except Exception as err:
                failed.append(_conflict(
                    pair, "Could not assign temporary value: {}".format(err)))

        for pair in rename_pairs:
            sheet = pair["sheet"]
            if pair["sheet_id"] not in temp_name_map:
                continue
            try:
                _set_sheet_value(sheet, field, pair["new_name"])
                renamed.append({
                    "label": pair["label"],
                    "old_name": pair["old_name"],
                    "new_name": pair["new_name"],
                })
            except Exception as err:
                restore_reason = ""
                try:
                    _set_sheet_value(sheet, field, pair["old_name"])
                    restore_reason = " Restored original value."
                except Exception as restore_err:
                    temp_name = temp_name_map[pair["sheet_id"]]
                    try:
                        _set_sheet_value(sheet, field, temp_name)
                        restore_reason = (
                            " Could not restore original value: {}. "
                            "Sheet was kept on temporary value: {}.".format(
                                restore_err, temp_name))
                    except Exception as temp_restore_err:
                        restore_reason = (
                            " Could not restore original value: {}. "
                            "Could not restore temporary value {} either: {}.".format(
                                restore_err, temp_name, temp_restore_err))
                failed.append(_conflict(
                    pair, "Could not assign target value: {}.{}".format(
                        err, restore_reason)))

    return renamed, failed


def main():
    selected_sheets = forms.select_sheets(title="Select Sheets", use_selection=True)
    if not selected_sheets:
        return

    field = forms.CommandSwitchWindow.show(
        RENAME_FIELDS, message="What do you want to rename?"
    )
    if not field:
        return

    selected_option, switches = forms.CommandSwitchWindow.show(
        RENAME_MODES,
        switches=["Show Report"],
        message="Select rename option for {}:".format(field.lower()),
    )
    if not selected_option:
        return

    rename_inputs = _collect_rename_inputs(selected_option, field)
    if rename_inputs is None:
        return

    all_values = {}
    all_sheets = (
        DB.FilteredElementCollector(revit.doc)
        .OfClass(DB.ViewSheet)
        .WhereElementIsNotElementType()
        .ToElements()
    )
    for sheet in all_sheets:
        all_values[sheet.Id.IntegerValue] = _get_sheet_value(sheet, field)

    rename_pairs = []
    unchanged = []
    for sheet in selected_sheets:
        old_name = _get_sheet_value(sheet, field)
        new_name = _build_new_name(old_name, selected_option, rename_inputs)
        pair = {
            "sheet": sheet,
            "sheet_id": sheet.Id.IntegerValue,
            "label": _sheet_label(sheet, field),
            "old_name": old_name,
            "new_name": new_name,
        }
        if old_name == new_name:
            unchanged.append(pair)
        else:
            rename_pairs.append(pair)

    if not rename_pairs:
        forms.alert("No {}s changed with selected option.".format(field.lower()))
        if switches["Show Report"]:
            _report_results(field, [], unchanged, [], [])
        return

    unique_pairs, conflicts = _resolve_conflicts(rename_pairs, all_values)

    if not unique_pairs:
        forms.alert("No valid unique target values. Nothing renamed.")
        _report_results(field, [], unchanged, conflicts, [])
        return

    _print_preview(unique_pairs, field)
    proceed = forms.alert(
        "Change the {} of {} selected sheets?\n"
        "{} sheets are ready to rename.\n"
        "{} sheets will be skipped because of conflicts.\n"
        "See output window for preview.".format(
            field.lower(), len(selected_sheets), len(unique_pairs), len(conflicts)
        ),
        title=TITLE,
        yes=True,
        no=True,
        ok=False,
    )
    if not proceed:
        return

    renamed, failed = _execute_renames(unique_pairs, field)

    if switches["Show Report"] or conflicts or failed:
        _report_results(field, renamed, unchanged, conflicts, failed)
    forms.alert(
        "Done ({}).\nRenamed: {}\nSkipped (unchanged/conflicts): {}\nFailed: {}".format(
            field, len(renamed), len(unchanged) + len(conflicts), len(failed)
        ),
        title=TITLE,
    )


if __name__ == "__main__":
    main()
