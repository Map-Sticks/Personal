PARAMETER BROWSER 2.0 (modeless WPF window, read-only)

Files:
- script.py                        window + ExternalEvent handler (all Revit access)
- UI.xaml                          window layout (handler names must match script.py methods)
- lib/parameter_browser_logic.py   Revit-free logic, unit tested in tests/test_parameter_browser.py

How it works:
- __persistentengine__ = True (and engine: persistent in bundle.yaml) keeps the handlers alive.
- The command reads Spaces and Rooms once (valid API context) into plain Python data, then opens the
  window with show(modal=False). The window never holds Revit Elements.
- Source, scope, level, search and column order work on the cached data only (System.Data.DataTable
  + DataView.RowFilter, so no IronPython property binding is needed).
- Refresh Model, loading values for newly chosen columns, Select in Revit and Show / Zoom are queued
  and run in IExternalEventHandler.Execute via ExternalEvent.Raise().
- Select/Show only run when the project the list was read from is active; Refresh Model loads the
  currently active project and active view.
- "Current view" scope uses the view that was active at the last refresh.
- Clicking the ribbon button again closes the old window and opens a fresh one.
- Settings: %APPDATA%\pyRevit\ParameterBrowser.settings.json
