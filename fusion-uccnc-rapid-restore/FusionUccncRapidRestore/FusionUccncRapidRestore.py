"""Fusion 360 Add-In: UCCNC Rapid Move Restorer.

Adds a 'UCCNC Rapid Restore' button to Manufacture -> Actions and
Design -> Utilities -> Add-Ins. Operates solely on posted .nc/.tap G-code
files and never modifies the active Fusion document.
"""

import os
import sys
import traceback

import adsk.core

_ADDIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _ADDIN_DIR not in sys.path:
    sys.path.insert(0, _ADDIN_DIR)

import rapid_restore_core as core  # noqa: E402


CMD_ID = "MapSticks_UccncRapidRestore_Cmd"
CMD_NAME = "UCCNC Rapid Restore"
CMD_TOOLTIP = (
    "Restore conservative G0 rapid moves (Safe-Z XY moves and upward Z retracts) "
    "in posted Fusion Personal UCCNC G-code files."
)

# Target Fusion UI panels (Manufacture Actions + Utilities Add-Ins)
TARGET_PANELS = (
    ("CAMEnvironment", "CAMActionPanel"),
    ("FusionSolidEnvironment", "SolidScriptsAddinsPanel"),
)

# Input IDs
INPUT_BROWSE_BTN = "browse_gcode_file"
INPUT_FILE_PATH = "gcode_file_path"
INPUT_FILE_INFO = "gcode_file_info"
INPUT_SAFE_Z = "safe_z_value"
INPUT_STRICT_RETRACT = "strict_retract_safe_z"
INPUT_DRY_RUN = "dry_run_only"
INPUT_OVERWRITE = "overwrite_original"

_handlers = []
_added_controls = []


def _get_app_ui():
    app = adsk.core.Application.get()
    return app, app.userInterface


def _inspect_and_populate_inputs(inputs, file_path):
    """Read G-code file metadata and update the dialog info and suggested Safe Z."""
    info_box = adsk.core.TextBoxCommandInput.cast(inputs.itemById(INPUT_FILE_INFO))
    safe_z_in = adsk.core.StringValueCommandInput.cast(inputs.itemById(INPUT_SAFE_Z))

    if not file_path or not os.path.isfile(file_path):
        info_box.text = "Select a posted .nc or .tap G-code file to inspect."
        return

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    summary = core.inspect_gcode_text(text)
    z_range = (
        f"{summary.min_z:g} to {summary.max_z:g} {summary.units}"
        if summary.min_z is not None and summary.max_z is not None
        else "Unknown"
    )
    g43_str = (
        ", ".join(f"{z:g}" for z in summary.g43_z_values)
        if summary.g43_z_values
        else "None detected"
    )
    info_box.text = (
        f"Lines: {summary.line_count} | Units: {summary.units}\n"
        f"Z range: {z_range}\n"
        f"G43 Z heights: {g43_str}\n"
        f"Suggested Safe Z: {summary.suggested_safe_z:g} {summary.units} "
        f"(adjust to your Retract Height if lower)"
    )
    safe_z_in.value = f"{summary.suggested_safe_z:g}"


class _CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = adsk.core.Command.cast(args.command)
            cmd.isExecutedWhenPreEmpted = False
            inputs = cmd.commandInputs

            browse_btn = inputs.addBoolValueInput(
                INPUT_BROWSE_BTN, "Select G-code File...", False, "", False
            )
            browse_btn.tooltip = "Choose a posted UCCNC .nc or .tap file"

            file_in = inputs.addStringValueInput(INPUT_FILE_PATH, "G-code File", "")
            file_in.tooltip = "Full path to the posted UCCNC G-code file"

            info_in = inputs.addTextBoxCommandInput(
                INPUT_FILE_INFO,
                "File Summary",
                "Select a posted .nc or .tap G-code file to inspect.",
                4,
                True,
            )
            info_in.isFullWidth = True

            safe_z_in = inputs.addStringValueInput(
                INPUT_SAFE_Z, "Safe Z (file units)", "5.0"
            )
            safe_z_in.tooltip = (
                "Only XY moves at or above this Z height (in the G-code file's "
                "own mm/in units) will be converted to G0 rapids."
            )

            strict_in = inputs.addBoolValueInput(
                INPUT_STRICT_RETRACT,
                "Z retracts must reach Safe Z",
                True,
                "",
                True,
            )
            strict_in.tooltip = (
                "Conservative safety: only convert pure upward Z retracts whose "
                "target Z is at or above Safe Z (protects undercut/T-slot cuts)."
            )

            dry_run_in = inputs.addBoolValueInput(
                INPUT_DRY_RUN,
                "Dry run (preview stats only)",
                True,
                "",
                False,
            )
            dry_run_in.tooltip = "Analyze the file and report changes without writing."

            overwrite_in = inputs.addBoolValueInput(
                INPUT_OVERWRITE,
                "Overwrite original file",
                True,
                "",
                False,
            )
            overwrite_in.tooltip = (
                "Unchecked (recommended): saves a new <name>_rapids<ext> file."
            )

            on_changed = _InputChangedHandler()
            cmd.inputChanged.add(on_changed)
            _handlers.append(on_changed)

            on_validate = _ValidateInputsHandler()
            cmd.validateInputs.add(on_validate)
            _handlers.append(on_validate)

            on_execute = _CommandExecuteHandler()
            cmd.execute.add(on_execute)
            _handlers.append(on_execute)
        except Exception:
            _, ui = _get_app_ui()
            if ui:
                ui.messageBox(
                    f"Failed to open dialog:\n{traceback.format_exc()}",
                    CMD_NAME,
                )


class _InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            changed = args.input
            inputs = args.inputs

            if changed.id == INPUT_BROWSE_BTN:
                _, ui = _get_app_ui()
                dlg = ui.createFileDialog()
                dlg.isMultiSelectEnabled = False
                dlg.title = "Select Posted UCCNC G-code File"
                dlg.filter = (
                    "UCCNC G-code (*.nc;*.tap;*.gcode;*.ngc);;All Files (*.*)"
                )
                if dlg.showOpen() == adsk.core.DialogResults.DialogOK:
                    path = dlg.filename
                    file_in = adsk.core.StringValueCommandInput.cast(
                        inputs.itemById(INPUT_FILE_PATH)
                    )
                    file_in.value = path
                    _inspect_and_populate_inputs(inputs, path)
            elif changed.id == INPUT_FILE_PATH:
                file_in = adsk.core.StringValueCommandInput.cast(changed)
                _inspect_and_populate_inputs(inputs, file_in.value.strip())
        except Exception:
            _, ui = _get_app_ui()
            if ui:
                ui.messageBox(
                    f"Error inspecting G-code file:\n{traceback.format_exc()}",
                    CMD_NAME,
                )


class _ValidateInputsHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        try:
            inputs = args.inputs
            file_path = adsk.core.StringValueCommandInput.cast(
                inputs.itemById(INPUT_FILE_PATH)
            ).value.strip()
            safe_z_str = adsk.core.StringValueCommandInput.cast(
                inputs.itemById(INPUT_SAFE_Z)
            ).value.strip()

            if not file_path or not os.path.isfile(file_path):
                args.areInputsValid = False
                return

            float(safe_z_str)
            args.areInputsValid = True
        except Exception:
            args.areInputsValid = False


class _CommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        _, ui = _get_app_ui()
        try:
            inputs = args.command.commandInputs
            file_path = adsk.core.StringValueCommandInput.cast(
                inputs.itemById(INPUT_FILE_PATH)
            ).value.strip()
            safe_z = float(
                adsk.core.StringValueCommandInput.cast(
                    inputs.itemById(INPUT_SAFE_Z)
                ).value.strip()
            )
            strict_retract = adsk.core.BoolValueCommandInput.cast(
                inputs.itemById(INPUT_STRICT_RETRACT)
            ).value
            dry_run = adsk.core.BoolValueCommandInput.cast(
                inputs.itemById(INPUT_DRY_RUN)
            ).value
            overwrite = adsk.core.BoolValueCommandInput.cast(
                inputs.itemById(INPUT_OVERWRITE)
            ).value

            options = core.RestoreOptions(
                safe_z=safe_z,
                retract_must_reach_safe_z=strict_retract,
            )

            if dry_run:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                _, stats = core.restore_rapids_text(text, options=options)
                report = core.format_stats_report(
                    file_path, None, options, stats, dry_run=True
                )
            else:
                out_path = file_path if overwrite else None
                saved_path, stats = core.restore_rapids_file(
                    file_path, output_path=out_path, options=options
                )
                report = core.format_stats_report(
                    file_path, saved_path, options, stats, dry_run=False
                )

            ui.messageBox(report, CMD_NAME)
        except Exception:
            if ui:
                ui.messageBox(
                    f"Rapid restoration failed:\n{traceback.format_exc()}",
                    CMD_NAME,
                )


def run(_context):
    _, ui = _get_app_ui()
    try:
        existing_def = ui.commandDefinitions.itemById(CMD_ID)
        if existing_def:
            existing_def.deleteMe()

        cmd_def = ui.commandDefinitions.addButtonDefinition(
            CMD_ID, CMD_NAME, CMD_TOOLTIP, ""
        )

        on_created = _CommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        for ws_id, panel_id in TARGET_PANELS:
            ws = ui.workspaces.itemById(ws_id)
            if not ws:
                continue
            panel = ws.toolbarPanels.itemById(panel_id)
            if not panel:
                continue
            existing_ctrl = panel.controls.itemById(CMD_ID)
            if existing_ctrl:
                existing_ctrl.deleteMe()
            ctrl = panel.controls.addCommand(cmd_def)
            if ctrl:
                ctrl.isPromoted = True
                _added_controls.append((ws_id, panel_id, CMD_ID))
    except Exception:
        if ui:
            ui.messageBox(
                f"Failed to start {CMD_NAME}:\n{traceback.format_exc()}",
                CMD_NAME,
            )


def stop(_context):
    _, ui = _get_app_ui()
    try:
        for ws_id, panel_id, ctrl_id in _added_controls:
            ws = ui.workspaces.itemById(ws_id)
            if not ws:
                continue
            panel = ws.toolbarPanels.itemById(panel_id)
            if not panel:
                continue
            ctrl = panel.controls.itemById(ctrl_id)
            if ctrl:
                ctrl.deleteMe()
        _added_controls.clear()

        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()

        _handlers.clear()
    except Exception:
        if ui:
            ui.messageBox(
                f"Failed to stop {CMD_NAME}:\n{traceback.format_exc()}",
                CMD_NAME,
            )
