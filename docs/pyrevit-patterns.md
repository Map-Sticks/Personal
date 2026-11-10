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
- pyRevit transaction/group wrappers **dispose their native objects when the `with` block exits**.
  Do not read `.status` afterward: that calls `GetStatus()` on a disposed object and can raise
  "The managed object is not valid". For a transaction held open across native picks, follow the
  guarded `DB.Transaction` session pattern in Draw Switchlines / Detail Line Fillets: inspect
  the `Commit()` return value before `Dispose()`, and roll back unfinished work in `finally`.

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
`CustomPyRevit.extension/ETC PyTools.tab/Templates.panel/Modeless Window.pushbutton/`. **Copy that folder for
every new modeless tool** and change only the XAML and the model-work methods.

## HTML / WebView2 tools (pyRevit 7+)

pyRevit 7's output window is WebView2 (Chromium). For an HTML tool, **use the output window; do not host your
own `Microsoft.Web.WebView2` control in a WPF window** (assembly loading, user-data folder and async init make
that fragile under IronPython). Reference: `ParameterBrowser WebView.pushbutton` and `WebviewExample/`.

- Read the model in the command (valid API context), then `output = script.get_output()`,
  `output.add_style(css)` and `output.inject_script(data_js + app_js, body=True)`. No persistent engine,
  no ExternalEvent: no Python runs after the command ends.
- Pass data with `inject_script` (`window.X = <json>;`, `ensure_ascii=True`, element ids as strings).
  `print_html` rewrites `:name:` emoji codes and newlines, which corrupts data embedded in HTML.
- JS -> Revit only through pyRevit's link handler (`ScriptConsoleUtils.ProcessUrl`):
  `revit://outputhelpers?command=select&element[]=1,2,3&show=true|false` (ids may be comma-joined) and
  `revit://outputhelpers?command=button&controlid=<EXEC_PARAMS.command_controlid>` to re-run a button
  (call `output.close_others()` at the start so the re-run replaces the old window). Navigate with an
  `<a href>` click or `location.href = url`. Selection acts on the *active* document.
- `localStorage` persists (per Revit version WebView2 profile); there is no other way to save from JS.
- Detect pyRevit 7: `pyrevit.VERSION_MAJOR >= 7`, or `output.renderer` raising `NotImplementedError`.

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

- `Tab.tab/Panel.panel/Name.pushbutton/` with `script.py`, `bundle.yaml` (`title`, `tooltip`, `author`),
  and a unique, purpose-made `icon.png` (96x96 recommended; `icon.dark.png` for dark theme), shared code in `lib/`.
  Do not reuse a generic icon from another button.
- The ETC PyTools tab and panels have explicit `bundle.yaml` layouts: add new panel or button folder
  names without their suffixes to the appropriate list, or pyRevit hides them. Panel layout directives
  such as `'Name[title:Short\nLabel]'` change the ribbon label without changing the bundle path.
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
