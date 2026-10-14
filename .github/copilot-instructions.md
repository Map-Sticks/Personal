# Instructions for AI assistants working in this repo

This repo is a pyRevit extension (`CustomPyRevit.extension`) written in **IronPython 2.7** for Revit 2023-2025.
You cannot run Revit here, so correctness comes from following verified patterns, not guessing.

Before writing or changing any button, read:

1. `docs/pyrevit-patterns.md` - transactions, modeless WPF windows, ExternalEvent, XAML wiring, bundle layout.
2. `docs/pyrevit-versions.md` - engine/version matrix and the IronPython 2.7 compatibility rules.
3. `docs/revit-api-references.md` - where the authoritative API sources are.

## Hard rules

- Write IronPython 2.7-compatible code: no f-strings, type hints, `pathlib`, or pip packages. It must run on
  pyRevit 4.8.x through 6.x.
- Verify every Revit API type/member you are unsure about: `python tools/revit_api_lookup.py <name> [--ui] [--year N]`.
  Do not invent API names or signatures. If it cannot be verified, say so.
- Any model change is inside a transaction (`with revit.Transaction("name"):`). Read-only tools never open one.
- **Modeless windows**: start from `Custom Tools.tab/Templates.panel/Modeless Window.pushbutton`. Always set
  `__persistentengine__ = True`, always do model work in an `IExternalEventHandler.Execute` via `ExternalEvent.Raise()`,
  create the event in `__init__`, never call the Revit API from click handlers directly.
- Prefer pyRevit built-ins (`forms.alert`, `forms.SelectFromList`, ...) over custom XAML when they are enough.
- New button = new `<Name>.pushbutton` folder with `script.py` + `bundle.yaml` (+ `window.xaml` if needed). Update the
  README button list.
- Put pure logic in plain functions and add a mock-based unit test in `tests/` (run with
  `python -m unittest discover -s tests -v`). Run `python -m py_compile` on new scripts.
- When finished, tell the user exactly how to test in Revit (reload pyRevit, click which button, expected result), and
  which parts only Revit can confirm.
- If the target Revit year or engine is unclear, ask; default is IronPython 2.7 and Revit 2023-2025.
