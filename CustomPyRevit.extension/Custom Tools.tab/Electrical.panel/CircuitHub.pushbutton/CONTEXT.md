# CircuitHub - context and roadmap

Status: v11.3 script, UI, and bundle metadata imported as-is from `Circuit Test`, superseding v8.
The review findings below are not fixed by this replacement. Revit validation is still required.
Read the repo rules first (`.github/copilot-instructions.md`, `docs/*.md`).

## What it does today

Modeless WPF window (`script.py` + `ui.xaml`) listing Panel > Circuit > Fixture rows with level
filter and search. Actions: move circuits to another panel (bulk, button or Alt-drag), reassign /
add / remove fixtures to circuits (from Revit selection or highlighted rows), create a new circuit
from the Revit selection (optionally assigned to a highlighted panel), rename a circuit's Load
Name, create a panel schedule, undo, and optional auto-refresh on document change (off by default).
Also includes configurable category filtering, inline add/remove actions, natural circuit-number
sorting, optional empty panels, unassigned circuits grouped by fixture level, and panel picking
in the view. Model-changing actions use ExternalEvent. Add/reassign actions have heuristic phase
pre-checks; known Revit validation failures have friendly messages.

## Author's wishes for the next version (priority)

1. **Virtual / "invisible" panels.** Move away from having to place a physical panel family
   in the model to host circuits. Create a panel by simply giving it a **name** (no modelled
   geometry). Feasibility is NOT yet verified: Revit circuits need a `BaseEquipment` that is an
   Electrical Equipment element, so a virtual panel probably means an unplaced/hidden
   proxy equipment instance (e.g. on a "virtual" workset / hidden category / in a non-printing
   location) created and managed by the tool. Verify with `tools/revit_api_lookup.py` and test in
   Revit before promising anything.
2. **Create panels** from within the tool (name, type, level, supply/distribution system).
3. **Rename panels** (Panel Name parameter, `RBS_ELEC_PANEL_NAME`) and bulk rename.
4. **Change a panel's level** (and other panel properties) from the tool.
5. Do all of the above in the tool instead of clunky Revit schedules.

## Review notes (v11.3 imported without fixes)

Worth fixing / checking when we next edit `script.py`:

- `CACHE.circuits` is stale-safe only via refresh; `find_circuits_containing` scans every circuit's
  `Elements` for each lookup (O(circuits x elements)), repeated per selected element. Build a
  one-off element-id -> circuits map in `Cache.build` for big models.
- Auto-refresh is off by default, so external edits/Undo can leave circuit memberships stale
  until Refresh. When enabled, document-change refresh can duplicate the action's own refresh.
- `Cache.build` `except` branch appends circuits whose `SystemType` could not be read (non-power
  circuits can slip in).
- `ElementId.IntegerValue` is deprecated in Revit 2024 and removed in 2026 (use `.Value`,
  with a fallback for 2023-2025). Same for storing ids as ints throughout.
- Model-changing actions now use ExternalEvent, but model reads, selection changes, and
  dispatcher refreshes still occur outside it. Global `doc`/`uidoc` remain bound to the document
  active when the button opened; guard against project switching/closing.
- Show Target Only and Locate Selection still expand `None` for unassigned circuits rather than
  the new `("unassigned", level)` group key.
- Selection filtering can raise `UserFacingError` before the protected action runner.
- `run_transaction` does not check the status returned by `Commit()`.
- `element_level_name(None)` calls its nested helper before defining it.
- Inline fixture removal removes all cached circuit memberships, not just the displayed row's.
- `on_drop` etc. swallow exceptions with bare `except: pass` in places, hiding real failures.
- Settings now use `%APPDATA%/CircuitHub/circuit_hub_settings.json`; save failures are reported.
- `panel_level_name` guesses level via several parameters; confirm it matches the intended level for
  your families.
- `phase_class` guesses phase from explicit parameters or poles, not voltage. Pole count is not
  sufficient to establish phase compatibility. Unknown phase passes validation; circuit creation
  no longer has v8's explicit mixed-phase pre-check.
- Not unit-tested; pure helpers (phase inference, label formatting) could be split into a module
  without Revit imports and tested in `tests/`.

## How to develop

IronPython 2.7, Revit 2023-2025, follow `docs/pyrevit-patterns.md`. Verify any new API with
`python tools/revit_api_lookup.py <name> [--year N]`. Keep one transaction per user action.
Always give Revit test steps (reload pyRevit, open CircuitHub, which button, expected result).
