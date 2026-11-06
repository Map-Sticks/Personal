# Custom pyRevit Buttons

This repository is a starter workspace for writing custom pyRevit buttons. The
button scripts are in `CustomPyRevit.extension`; edit them in VS Code or
Codespaces, then load and test them in Revit on Windows.

> A Codespace is useful for editing and reviewing Python, but it cannot run
> Revit or the pyRevit/Revit API. Run and verify model-changing commands in a
> Revit installation.

## Included buttons

`Document Info` is a read-only pushbutton that reports the active document name
and the number of selected elements in the pyRevit output window.

`List Sheets` is a read-only pushbutton that lists every sheet in the active
project, sorted alphabetically by sheet number (case-insensitive). Its output
table contains the sheet number, sheet name, and placeholder status, with a
total sheet count. Placeholder sheets and sheets not shown in the sheet list
are included; linked projects are not queried. It reports a clear message if
there is no open project, the active document is a family, or there are no sheets.

`Normalise Sheet Origins` (Getting Started panel) audits and aligns sheets to a reference sheet. Pick
the reference; every sheet is audited and misaligned ones are listed with their offset. Align all,
choose some, or audit only, and optionally save a CSV record. Each moved sheet's title block and all its content (viewports,
schedules, text, lines, etc.) move by one shift so the title block's bottom-left corner matches the
reference. Elements inside the title block family follow it automatically. Each sheet is checked
after the move and rolled back if the corner did not land; the output window lists each sheet's offset
and the action taken. See [docs/normalise-sheet-origins.md](docs/normalise-sheet-origins.md).

`Place Ceiling Devices` (Electrical panel) is a **scaffold only**: it shows a "not implemented"
alert. Requirements and lessons learned are in its `CONTEXT.md`.

`Route Cable Along Tray` (Electrical panel) is also a **scaffold only**; it will route a circuit
path along cable trays and report cable length. See its `CONTEXT.md`.

`Find Linked Fixtures` (Electrical panel) scans point-based elements inside a selected host MEP
Space. **Linked Scan Categories...** offers all top-level categories exposed by the selected
linked document, including Nurse Call Devices; **Host Family Categories...** offers categories
with loaded host family types. Both selectors are searchable and support multiple selections.
Existing scan choices, mappings and rules are retained, and placement targets default to
Electrical Fixtures. Wall/Ceiling modes require non-hosted, one-level-based families; Ceiling
(Face) requires work-plane-based families and a horizontal host reference plane. This is not a
general-purpose system-element copier or a true wall-hosting tool. Space search matches number,
actual Space name and level; results include all three plus an ID. Scanning checks Space volume
and inner voids; tolerance is horizontal and outside outer boundaries only. **Linked h. mm**
shows two transformed heights above the selected Space's level: **Ins** (family insertion point)
and **3D** (bottom / top of the visible geometry, which includes any offset built into the family,
e.g. a device placed at 0 whose 3D is pushed up 700 mm by a family parameter). Both are
independent of editable placement heights. Placement uses that Space level, not the active view's level. Legacy level-name
filter settings migrate to transformed-level-elevation matching; **Space volume only** also
supports devices without a usable linked level. Automatic updates and host-duplicate prevention
remain deferred. **Save to RVT** writes settings into project Extensible Storage via an
ExternalEvent transaction and verifies the stored payload; **save/sync the Revit project afterward**
to persist them to disk/central. Scan, placement and close do not silently save settings.
Closing with edits offers Save/Discard/Cancel. **Reload RVT** recovers from another window/user
changing the stored settings; export your edits before reloading if needed.
**Import Settings...** reads a previous JSON and confirms its mapping/rule counts;
**Export Settings...** creates a separately chosen JSON backup. Import is a window draft until saved
to RVT. Existing RVT settings always take precedence, even when empty. Without RVT settings, the
old bundle JSON is offered as a read-only migration draft, never overwritten automatically.
The footer identifies the source, counts mappings/rules and reports how many target IDs exist in
this host project. IDs are still project-specific; imported missing targets require remapping.
IDs travel as exact decimal strings for IronPython JSON compatibility; legacy numeric IDs also load.
Review the
[detailed assessment and Revit acceptance checks](docs/find-linked-fixtures-review.md)
before production use.

`CircuitHub` (Electrical panel) manages circuits and panels in a modeless window. Its **Panels** tab
supports single-panel rename and bulk literal find/replace with prefixes/suffixes, a confirmation
preview, duplicate-name checks, and one undoable transaction. It also includes category filtering,
row-specific add/remove actions, unassigned circuits grouped by fixture level, and panel picking.
Panel rows show the panel family and type, can be filtered by level, family and type, and can be
selected in the model (for the Properties palette) or shown in Revit directly from the list.
Model-access actions are routed through ExternalEvent with document guards and indexed circuit
memberships. Settings are stored in `%APPDATA%/CircuitHub/circuit_hub_settings.json`.
Virtual panels, panel creation, and level editing remain future work. See
[CircuitHub context and Revit acceptance checks](CustomPyRevit.extension/Custom%20Tools.tab/Electrical.panel/CircuitHub.pushbutton/CONTEXT.md);
actual Revit validation is still required.

`Parameter Browser` (Electrical panel) is a read-only, modeless window that lists Spaces or Rooms
with chosen parameter columns. It filters by whole project, current view (as of the last refresh) or
level, searches across the visible columns, and can select or show the highlighted rows in Revit.
A side panel lists the electrical/low-voltage family instances inside the selected Spaces or Rooms
(grouped by category and family type, with a "not in any space" view), and optional `Count:` columns
give per-category quantities per row, replacing separate per-category schedules. All
Revit reads and selection changes run through ExternalEvent; the window itself only uses cached data.
Settings are stored in `%APPDATA%/pyRevit/ParameterBrowser.settings.json`.

`Cable Tray Coordinator` (Electrical panel) is a modeless window that re-routes a straight
cable tray past an obstacle in the ceiling void. Pick a method (Auto, rise over, drop under,
offset left/right, 90/60/45/30 degree bends, manual offset such as "drop 300 mm", or saved user
presets), pick the tray and the obstacle (host or linked), and review the live preview before Apply.
**Detour** mode cuts the tray, moves the middle and re-joins it with fittings; **Bridge** mode fills
a gap between two cut tray ends, straight or around the obstacle. Auto mode works out the smallest
clear offset from the obstacle geometry. The rest of the run is not moved, parameters are copied,
and everything happens in one undoable transaction. Bridges also support parallel/perpendicular
horizontal trays at different elevations via obstacle-checked routes with 90-degree turns,
30/45/60-degree slopes and 30/45/60-degree sideways steps (e.g. a 45-degree offset round a wall
corner), pulling free tray ends back when the fittings need more room. Optional individual
obstacle avoidance makes separate detours along a long segment, merging nearby obstacles when
needed to prevent short connecting pieces. Minimum segment lengths are remembered in settings
and user presets; the real fitting take-up is measured per tray type/size and added to them, and
short bend legs are repaired in the preview or rejected if they cannot fit. When the standard
elbows do not fit, the route is re-planned with shorter elbow legs and smaller bend radii (never
below the remembered **Min. bend radius**) and those sizes are set on the placed elbows.
Bend angles include 22.5 and 15 degrees; with **Allow shallower bends to keep the clearance**
the tool uses a shallower bend when the chosen angle would push the route further from the
obstacle than the clearance, and the summary reports the actual gap to the obstacle.
A clash at a free tray end moves that end sideways/over/under (e.g. past a wall); a clash at a
connected end re-routes the connected run (up to 7 straights). In Bridge mode, clicking along the
side of the second tray (or a free end pointing at its side) tees into it instead of joining its end.
**Regenerate options** re-reads the picks, tries every direction and bend angle with your clearance
and minimum settings, test-builds the best routes (always undone) and lists the top 5 to pick from.
Only if nothing fits your settings does it offer **RELAXED** options (shorter elbow legs, bend
radius down to 150 mm, half the Min. straight; never less clearance). These name the limits they
break, ask for confirmation before Apply and are never saved as your settings.
**Revert last route** restores the original geometry/connections and retains the selections for
another attempt; subsequent model edits disable it for safety. Before commit the new route is
solid-intersected with the selected obstacles and rolled back on a real clash. See
[Cable Tray Coordinator context and Revit acceptance checks](CustomPyRevit.extension/Custom%20Tools.tab/Electrical.panel/CableTrayCoordinator.pushbutton/CONTEXT.md);
actual Revit validation is still required.

`Parameter Browser WebView` (Electrical panel, **pyRevit 7+**) is the same read-only browser rendered as
HTML in pyRevit 7's WebView2 output window, following the pattern of the working WebView example: the
command reads the model once and injects the data plus `app.js`/`app.css`; no custom window,
persistent engine or ExternalEvent is involved. Search, sorting, source/scope/level, the column chooser
(with order, drag-to-reorder and resizable columns), `Count:` columns, the "Elements in selection" tree,
Copy and CSV export all run in the browser. **Select / Show** use pyRevit's `revit://outputhelpers`
select links, and **Refresh model** re-runs the button. Settings live in the WebView2 profile's
localStorage (seeded from the classic button's settings file).

## ETC PyTools toolbar

The imported work-computer buttons are organized under
`CustomPyRevit.extension/ETC PyTools.tab`:

- **Electrical:** Manual Wall Placement; Place Fixtures.
- **Family Tools:** Family Offset Setup; Switch Colour Setup; Middle Family Parameter Setup.
- **Model Tools:** Align Section Box; Color Scheme Editor; Flip Workplane; Focus Select.
- **Revisions:** Custom Revision Setup; Apply Revisions; Remove Revisions.
- **Spaces:** Align Elevation Rows; Space Plans Stage 3.
- **Views and Sheets:** Legend Builder; Number Viewports.
- **Asset Management:** NWB Asset ID (asset IDs plus Space-based room IDs/names).

The uploaded Cable Tray Coordinator, Find Linked Fixtures, Circuiting/Circuit Hub, Normalise Sheet
Origins, and Parameter Browser scripts match buttons already in **Custom Tools**, so those copies were
not duplicated. The older Space Plans v1 and single-purpose NWB Asset ID variants were excluded in
favor of the explicitly later Stage 3.6 and expanded NWB versions. The redundant selection-only Legend
Builder was also omitted. The three archived Place Fixtures ZIP backups were removed; the current
bundle is kept.

```text
CustomPyRevit.extension/
└── Custom Tools.tab/
    ├── Electrical.panel/
    │   ├── CableTrayCoordinator.pushbutton/
    │   │   ├── bundle.yaml
    │   │   ├── CONTEXT.md
    │   │   ├── lib/
    │   │   │   └── cable_tray_logic.py
    │   │   ├── script.py
    │   │   └── ui.xaml
    │   ├── CircuitHub.pushbutton/
    │   │   ├── bundle.yaml
    │   │   ├── CONTEXT.md
    │   │   ├── lib/
    │   │   │   └── circuit_hub_logic.py
    │   │   ├── script.py
    │   │   └── ui.xaml
    │   ├── ParameterBrowser.pushbutton/
    │   │   ├── bundle.yaml
    │   │   ├── lib/
    │   │   │   └── parameter_browser_logic.py
    │   │   ├── README.txt
    │   │   ├── script.py
    │   │   └── UI.xaml
    │   ├── ParameterBrowser WebView.pushbutton/
    │   │   ├── app.css
    │   │   ├── app.js
    │   │   ├── bundle.yaml
    │   │   ├── lib/
    │   │   │   ├── parameter_browser_logic.py
    │   │   │   └── parameter_browser_webview_data.py
    │   │   ├── README.txt
    │   │   └── script.py
    │   ├── Place Ceiling Devices.pushbutton/
    │   │   ├── CONTEXT.md
    │   │   ├── bundle.yaml
    │   │   └── script.py
    │   └── Route Cable Along Tray.pushbutton/
    │       ├── CONTEXT.md
    │       ├── bundle.yaml
    │       └── script.py
    └── Getting Started.panel/
        ├── Document Info.pushbutton/
        │   ├── bundle.yaml
        │   └── script.py
        └── List Sheets.pushbutton/
            ├── bundle.yaml
            └── script.py
```

```text
CustomPyRevit.extension/
└── ETC PyTools.tab/
    ├── Asset Management.panel/
    │   └── NWB Asset ID.pushbutton/
    ├── Electrical.panel/
    │   ├── Manual Wall Placement.pushbutton/
    │   ├── Place Fixtures.pushbutton/
    │   └── FixtureTools.lib/
    ├── Family Tools.panel/
    │   ├── Family Offset Setup.pushbutton/
    │   ├── Middle Family Parameters.pushbutton/
    │   └── Switch Colour Setup.pushbutton/
    ├── Model Tools.panel/
    │   ├── Align Section Box.pushbutton/
    │   ├── Color Scheme Editor.pushbutton/
    │   ├── Flip Workplane.pushbutton/
    │   └── Focus Select.pushbutton/
    ├── Revisions.panel/
    │   ├── Apply Revisions.pushbutton/
    │   ├── Custom Revision Setup.pushbutton/
    │   └── Remove Revisions.pushbutton/
    ├── Spaces.panel/
    │   ├── Align Elevation Rows.pushbutton/
    │   └── Space Plans Stage 3.pushbutton/
    └── Views and Sheets.panel/
        ├── Legend Builder.pushbutton/
        └── Number Viewports.pushbutton/
```

## AI-assisted development kit

- `.github/copilot-instructions.md` - rules every AI assistant should follow in this repo.
- `docs/pyrevit-patterns.md` - transactions, modeless WPF + ExternalEvent, XAML, debugging.
- `docs/pyrevit-versions.md` - pyRevit release to Python engine matrix and IronPython 2.7 rules.
- `docs/revit-api-references.md` - local and online API sources.
- `tools/revit_api_lookup.py` - search the installed Revit's `RevitAPI.xml` (e.g.
  `python tools/revit_api_lookup.py ExternalEvent --ui --year 2025`).
- `docs/pyrevit-mcp.md` - pyRevit MCP connector setup (`.vscode/mcp.json` registers the server).
- `Templates.panel/Modeless Window.pushbutton` - working modeless window template (sets Comments on the
  selection). Copy it for every new window-based tool.

## Load the extension in Revit

1. Install pyRevit in a supported Revit environment.
2. In Revit, open pyRevit Settings and add this repository's root directory to
   **Custom Extension Directories**. That directory contains
   `CustomPyRevit.extension`.
3. Reload pyRevit from its ribbon tab. The **Custom Tools** and **ETC PyTools**
   tabs should appear. Existing starter buttons remain under **Custom Tools**;
   the imported buttons are grouped in the panels listed above.
4. After editing a script or its bundle metadata, reload pyRevit to see the
   changes. Test model-changing tools in a disposable project first.

## Add a button

1. Create a folder ending in `.pushbutton` inside an existing `.panel` folder.
   The folder name becomes the command name unless `bundle.yaml` provides a
   title.
2. Add `script.py` as the command entry point and `bundle.yaml` with the
   button title and tooltip.
3. Add a unique, purpose-made `icon.png` for every button (96x96 recommended);
   do not reuse a generic icon from another bundle. Shared Python modules can
   live in a `lib/` folder at the button, panel, tab, or extension level.
4. Reload pyRevit and test the button in Revit.

The same folder-suffix convention applies when adding tabs (`.tab`) and panels
(`.panel`). See the [pyRevit extension documentation](https://docs.pyrevitlabs.io/extensions/)
for bundle types, metadata, script engines, and the Revit API environment.

## Check List Sheets

The automated checks use a mock pyRevit API and can run in Codespaces without
installing Revit or third-party Python packages:

```sh
python -m unittest discover -s tests -p test_list_sheets.py -v
```

These checks cover sorting, placeholder inclusion, the output table and total,
and empty or invalid document states. They do not replace testing inside Revit.

Before merging this button's pull request:

1. Get this branch's files onto your Windows machine and load the extension
   using the steps above.
2. Open a project with sheets and click **Custom Tools > Getting Started >
   List Sheets**.
3. Compare the output against a sheet schedule that includes all sheets,
   including placeholders. Check the numbers, names, placeholder status, and total.
4. Try a project without sheets and a family document; both should show an
   explanatory message instead of a table.
5. Confirm that clicking the button does not change the model.

## Check CircuitHub

```sh
python -m unittest discover -s tests -p test_circuit_hub.py -v
```

These tests use mock Revit/WPF objects to check rename validation, cancellation, rollback,
document guards, deferred callbacks, membership indexing, row-specific removal, and XAML control
wiring. They do not run Revit or IronPython. Run all repository checks with
`python -m unittest discover -s tests -v`, then follow the
[CircuitHub acceptance checks](CustomPyRevit.extension/Custom%20Tools.tab/Electrical.panel/CircuitHub.pushbutton/CONTEXT.md#revit-acceptance-checks-disposable-project)
in a disposable Revit project.

## Check Parameter Browser

```sh
python -m unittest discover -s tests -p test_parameter_browser.py -v
```

These tests cover settings, column resolution, scope and search filtering, value formatting,
element-in-space indexing, grouping and count columns, XAML
handler wiring, and that selection and parameter reads only happen inside the ExternalEvent handler
(with document and closed-window guards). Revit must still confirm the WPF window and API behaviour.

## Check Cable Tray Coordinator

```sh
python -m unittest discover -s tests -p test_cable_tray_coordinator.py -v
```

These tests cover obstacle clipping and required offsets, auto direction choice, route and segment
normal geometry for each bend angle, detour/bridge validation, presets and parameter parsing,
preview primitives, XAML handler wiring, and `build_route` against fake Revit objects (far-end
reconnection, fitting-failure rollback). Revit must still confirm fitting creation and picking; follow the
[acceptance checks](CustomPyRevit.extension/Custom%20Tools.tab/Electrical.panel/CableTrayCoordinator.pushbutton/CONTEXT.md#6-revit-acceptance-checks-disposable-project).

## Check Parameter Browser WebView

```sh
python -m unittest discover -s tests -p test_parameter_browser_webview.py -v
```

These cover the WebView payload (exact string element ids, JSON safety), the script's model read and
injection with mock Revit objects, and, when `node` is installed, `app.js` helpers checked against the
Python logic (fields, scope, search, sorting, contents grouping, `revit://` links, CSV, settings).
Revit must still confirm the output window, the select/show links and Refresh model.
