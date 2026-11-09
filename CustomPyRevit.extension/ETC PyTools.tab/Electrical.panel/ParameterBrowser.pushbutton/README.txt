PARAMETER BROWSER 2.1 (modeless WPF window, read-only)

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
- Elements in selection (side panel): Refresh Model also reads every family instance of the content
  categories (Lighting/Electrical Fixtures, Electrical Equipment, Lighting/Fire Alarm/Communication/
  Data/Security/Nurse Call Devices) and indexes them with FamilyInstance.get_Space(phase) /
  get_Room(phase), using the phase(s) the Spaces/Rooms are placed in (latest first). The phase-less
  .Space/.Room properties are NOT used: they are ambiguous in IronPython and use the project's last
  phase, which left every element "not in any space". The panel shows the phase used and any errors. Clicking rows lists their
  elements as Category > Family : Type > element. Select/Show act on the highlighted tree node, or on
  everything listed if none is highlighted. "Not in any Space/Room" lists unassigned elements (often
  ceiling devices above the space's upper limit). Categories... hides categories in the panel only.
- "Count: <category>" columns (via Columns...) show those counts per row and sort numerically.
- Not included: linear elements (conduit, tray, duct, pipe), elements in linked models, and Rooms
  that live in a linked model (FamilyInstance.Room only sees Rooms in the host model).
- Settings: %APPDATA%\pyRevit\ParameterBrowser.settings.json
