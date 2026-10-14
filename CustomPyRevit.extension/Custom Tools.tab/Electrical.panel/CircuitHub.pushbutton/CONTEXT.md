# CircuitHub - context and roadmap

Status: working tool imported as-is from the author's own copy (v8). Functionality is good and is
not to be broken. Read the repo rules first (`.github/copilot-instructions.md`, `docs/*.md`).

## What it does today

Modeless WPF window (`script.py` + `ui.xaml`) listing Panel > Circuit > Fixture rows with level
filter and search. Actions: move circuits to another panel (bulk, button or Alt-drag), reassign /
add / remove fixtures to circuits (from Revit selection or highlighted rows), create a new circuit
from the Revit selection, rename a circuit's Load Name, create a panel schedule, undo, auto-refresh
on document change. Pre-checks for phase mismatch; friendly messages for Revit "circular
connection" errors.

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

## Review notes (no code changed yet)

Worth fixing / checking when we next edit `script.py`:

- `CACHE.circuits` is stale-safe only via refresh; `find_circuits_containing` scans every circuit's
  `Elements` for each lookup (O(circuits x elements)), repeated per selected element. Build a
  one-off element-id -> circuits map in `Cache.build` for big models.
- `DocumentChanged` fires on every change (including the tool's own commits), then does a full
  rebuild; combined with `self.refresh()` in `run_direct` this rebuilds twice per action.
- `Cache.build` `except` branch appends circuits whose `SystemType` could not be read (non-power
  circuits can slip in).
- `ElementId.IntegerValue` is deprecated in Revit 2024 and removed in 2026 (use `.Value`,
  with a fallback for 2023-2025). Same for storing ids as ints throughout.
- Revit API is called from WPF click handlers inside `run_transaction`. Works while the window is
  opened via a normal command, but repo rules say to use an `ExternalEvent` for modeless windows;
  moving to one is the safe fix (e.g. for Alt-drag / handlers firing outside a valid API context).
- Alt-drag drop handler duplicates the logic of the button handlers (reassign fixtures / move
  circuits); factor into shared functions.
- `on_drop` etc. swallow exceptions with bare `except: pass` in places, hiding real failures.
- `save_settings` writes next to `script.py` (inside the extension folder, may be read-only or
  synced); prefer a user config path. `circuit_hub_settings.json` is git-ignored.
- `panel_level_name` guesses level via several parameters; confirm it matches the intended level for
  your families.
- `phase_class` guesses phase from poles/voltage with hard-coded voltage bands (100-140 -> 1,
  190-215 -> 2, 360-500 -> 3); treated as best effort. Unknown phase silently passes validation.
- Title is "Circuit Hub v8" in the docstring; no version/changelog.
- Not unit-tested; pure helpers (phase inference, label formatting) could be split into a module
  without Revit imports and tested in `tests/`.

## How to develop

IronPython 2.7, Revit 2023-2025, follow `docs/pyrevit-patterns.md`. Verify any new API with
`python tools/revit_api_lookup.py <name> [--year N]`. Keep one transaction per user action.
Always give Revit test steps (reload pyRevit, open CircuitHub, which button, expected result).
