# pyRevit button patterns & gotchas

## Basic imports

```python
from pyrevit import revit, DB, UI, forms, script
doc = revit.doc          # active Document   (only valid in the main script run, not in modeless callbacks)
uidoc = revit.uidoc
selection = revit.get_selection()
```

## Transactions

- Every model change needs a transaction. Reads do not.
- Preferred: `with revit.Transaction("Descriptive name"):` (auto commit; rolls back on exception).
- Several steps as one undo item: `with revit.TransactionGroup("Name"):`.
- Never leave a transaction open; never start one inside another `DB.Transaction` (use `SubTransaction`;
  pyRevit's wrapper does this automatically when `doc.IsModifiable`).
- Pass `doc=` explicitly in handlers and event callbacks: `revit.Transaction("x", doc=uiapp.ActiveUIDocument.Document)`.
- Read-only buttons: do not open a transaction; "does not modify the model" must be actually true.

## Modeless WPF window = the #1 source of "dead buttons"

Rules (all required):

1. `__persistentengine__ = True` at the top of `script.py` (pyRevit reads this module variable). Without it the
   engine is torn down when the script ends and click handlers silently fail.
2. A modeless window runs outside Revit's API context. **Any API call that reads or writes the model from a
   click handler can throw "Cannot access a disposed object" or "not in valid API context".** Route model work
   through `UI.ExternalEvent` + `UI.IExternalEventHandler`.
3. Create the `ExternalEvent` while still inside the original command (in the window's `__init__`), not later.
4. Pattern: click handler stores a callable on the handler and calls `ext_event.Raise()`; `Execute(uiapp)` runs it
   and opens the transaction there. `Raise()` is asynchronous: it runs after the click handler returns, and only
   when Revit is idle. Multiple quick `Raise()` calls can coalesce, so use a queue if each request matters.
5. Keep a module-level reference to the window; call `window.show(modal=False)` (`forms.WPFWindow.show`).
6. Do not call `window.Close()` from `Execute` and then keep using the window. Do not use `time.sleep` or loops on the UI thread.
7. Selection inside the window: read selection via `uidoc.Selection.GetElementIds()` in `Execute`, or read it in
   the click handler only if you accept it may be stale. To react to selection changes use
   `UI.UIApplication.SelectionChanged` (2023+) with an event handler, not polling.
8. If you need the user to pick an element, hide the window, do the pick in an ExternalEvent
   (`uidoc.Selection.PickObject`), then show it again.

A complete, working, commented implementation is in
`CustomPyRevit.extension/Custom Tools.tab/Templates.panel/Modeless Window.pushbutton/`. **Copy that folder for
every new modeless tool** and change only the XAML and the model-work methods.

## Modal windows / simple UI

Use pyRevit built-ins first, they need no XAML: `forms.alert`, `forms.ask_for_string`, `forms.SelectFromList.show`,
`forms.CommandSwitchWindow.show`, `forms.pick_file`, `forms.ProgressBar`, `script.get_output()`.
Modal `window.show_dialog()` runs inside the API context, so API calls and transactions are safe there.

## XAML wiring in pyRevit

- Load with `forms.WPFWindow.__init__(self, xaml_path)`. Elements with `x:Name="foo"` become `self.foo`.
- Event handlers named in XAML (`Click="apply_click"`) must be methods of the window class with signature
  `(self, sender, args)`.
- Plain `Window` root, no `x:Class`, no code-behind, no external resource dictionaries without full paths.

## Bundle layout and metadata

- `Tab.tab/Panel.panel/Name.pushbutton/` with `script.py`, optional `bundle.yaml` (`title`, `tooltip`, `author`),
  `icon.png` (96x96 recommended; `icon.dark.png` for dark theme), shared code in `lib/`.
- Reload pyRevit after any bundle/metadata change. Edits to a persistent-engine
  script need the old window closed first.
- Useful module variables: `__title__`, `__doc__` (tooltip), `__author__`, `__persistentengine__`, `__min_revit_ver__`,
  `__max_revit_ver__`, `__context__` (e.g. `'doc-project'`, `'selection'`).

## Debugging

- `script.get_output()`: `output = script.get_output(); output.print_md("**text**")`; `script.get_logger()`.
- Enable "Debug Mode" in pyRevit Settings to see detailed logs/tracebacks. Most "dead button" reports are swallowed exceptions: wrap handlers and log them
  (the template's `Execute` does this).
- Make sure the output of `python -m py_compile script.py` is clean, but remember this only checks syntax.

## Testing without Revit

`tests/` uses mock `pyrevit`/Revit modules (see `tests/test_list_sheets.py`). Keep logic that doesn't need the API in
plain functions so it can be unit tested; keep Revit-touching code thin. Anything touching the API or WPF must still
be tried in Revit by the user.
