PARAMETER BROWSER WEBVIEW (pyRevit 7+, WebView2 output window, read-only)

Same tool as ParameterBrowser.pushbutton, rendered as HTML in pyRevit 7's output window
(which is WebView2/Chromium). Built on the same pattern as the working WebView example: no
custom WPF window, no WebView2 control of our own, no persistent engine, no ExternalEvent.

Files:
- script.py                            reads the model once, then injects app.css + data + app.js
- app.js                               the browser UI (pure helpers exported as window.PBWVCore)
- app.css                              styles, scoped to .pb
- lib/parameter_browser_logic.py       Revit-free logic, same file as ParameterBrowser.pushbutton
- lib/parameter_browser_webview_data.py  Revit-free payload/JSON helpers
Tests: tests/test_parameter_browser_webview.py (Python mocks + node for app.js).

How it works:
- The command runs in a valid API context: it reads Spaces, Rooms (all their parameter values)
  and the family instances of the content categories, with the Space/Room each one is in
  (FamilyInstance.get_Space(phase)/get_Room(phase) using the phases the Spaces/Rooms are in).
- The data is passed with output.inject_script (window.PBWV_DATA = {...}). print_html is NOT
  used for data because pyRevit rewrites ":name:" emoji codes and newlines in printed HTML.
- After that, no Python runs. Everything in the window works on the cached data:
  source, scope (whole project / current view as of the read / level), search (every
  space-separated word must appear in a visible column), sorting (click a header), column
  chooser with order (Columns...), drag headers to reorder, drag header edges to resize,
  drag the splitter to resize the side panel, Copy (tab separated, pastes into Excel),
  Export CSV (Downloads folder).
- Select in Revit / Show / Zoom / double-click / Enter navigate to
  revit://outputhelpers?command=select&element[]=<ids>&show=true|false. pyRevit handles that
  link itself and selects/shows the elements in the ACTIVE project. If you switched projects,
  switch back (or refresh) first.
- Refresh model navigates to revit://outputhelpers?command=button&controlid=<this button>,
  which re-runs this button; the new run closes the old window. Search, selection and the
  "not in any Space/Room" toggle are carried over.
- Elements in selection (side panel): Category > Family : Type > element, for the selected rows,
  or the elements not in any Space/Room. Select/Show act on the highlighted node, or on
  everything listed if none is highlighted. Categories... hides categories in the panel only.
- "Count: <category>" columns (via Columns...) show per-row counts and sort numerically.
- Not included: linear elements (conduit, tray, duct, pipe), elements in linked models, and
  Rooms in linked models.
- Settings (source, scope, level, columns, categories, sort, widths) are kept in the WebView2
  profile's localStorage. The first run starts from the classic button's settings file
  %APPDATA%\pyRevit\ParameterBrowser.settings.json if it exists.

Differences from the WPF version:
- All parameter values are read up front (the page cannot ask Revit for more later), so opening
  takes a little longer on very large models.
- Select/Show cannot report success back; the status line says what was sent.
- Very large Select/Show requests (roughly 3,000+ elements) are refused; narrow the selection.
