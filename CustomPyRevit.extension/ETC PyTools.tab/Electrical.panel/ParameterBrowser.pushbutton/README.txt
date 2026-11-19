PARAMETER BROWSER 2.2 (modeless WPF window, read-only)

Files:
- script.py                        window + ExternalEvent handler (all Revit access)
- UI.xaml                          window layout (handler names must match script.py methods)
- lib/parameter_browser_logic.py   Revit-free logic, unit tested in tests/test_parameter_browser.py

How it works:
- __persistentengine__ = True (and engine: persistent in bundle.yaml) keeps the handlers alive.
- The window opens with only the project's level names loaded. All Space/Room and content-element
  reads run on demand through IExternalEventHandler.Execute via ExternalEvent.Raise().
- Source, scope, level, search and column order work on cached plain-Python data
  (System.Data.DataTable + DataView.RowFilter).
- Refresh Selected reads the active source (Spaces or Rooms) and selected scope. Level scans use
  ElementLevelFilter; Current view scans use the active view's collector. Related content elements
  are prefiltered by one multicategory and bounding-box filter around the refreshed spaces/rooms,
  plus elements previously assigned to those spaces so moved elements can be rechecked. A 1-foot
  bounding-box margin catches nearby hosted content. Cached rows outside a refreshed level remain.
- Refresh All re-reads both Spaces and Rooms, all content categories, and whole-project unassigned
  results. Use it to restore a complete snapshot after broad changes or when switching projects.
- Scan Unassigned separately scans content elements in the whole project; Refresh Selected does not
  update this whole-project result. The UI indicates when unassigned data is stale/unscanned.
- Parameter names are gathered from the first 20 spaces/rooms in a scan to avoid walking every
  parameter on every element. Selected values load on demand for the cached records.
- Select/Show only run when the project read is active; Refresh All loads the currently active project.
- Clicking the ribbon button again closes the old window and opens a fresh one.
- Contents: family instances of Lighting/Electrical Fixtures, Electrical Equipment, Lighting/Fire
  Alarm/Communication/Data/Security/Nurse Call Devices. Association uses FamilyInstance.get_Space(phase) /
  get_Room(phase) with explicit phases. The phase-less .Space/.Room properties are not used.
- "Not in any Space/Room" is a whole-project result and can include ceiling devices above spatial
  boundaries. Categories... only changes which categories are displayed.
- "Count: <category>" columns show the cached content counts for each spatial element.
- Not included: linear elements (conduit, tray, duct, pipe), elements in linked models, and Rooms
  that live in a linked model.
- Settings: %APPDATA%\pyRevit\ParameterBrowser.settings.json
