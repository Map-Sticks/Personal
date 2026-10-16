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

`Place Ceiling Devices` (Electrical panel) is a **scaffold only**: it shows a "not implemented"
alert. Requirements and lessons learned are in its `CONTEXT.md`.

`Route Cable Along Tray` (Electrical panel) is also a **scaffold only**; it will route a circuit
path along cable trays and report cable length. See its `CONTEXT.md`.

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
level, searches across the visible columns, and can select or show the highlighted rows in Revit. All
Revit reads and selection changes run through ExternalEvent; the window itself only uses cached data.
Settings are stored in `%APPDATA%/pyRevit/ParameterBrowser.settings.json`.

```text
CustomPyRevit.extension/
└── Custom Tools.tab/
    ├── Electrical.panel/
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
3. Reload pyRevit from its ribbon tab. The **Custom Tools** tab and
   **Getting Started** panel should appear.
4. After editing a script or its bundle metadata, reload pyRevit to see the
   changes. Test model-changing tools in a disposable project first.

## Add a button

1. Create a folder ending in `.pushbutton` inside an existing `.panel` folder.
   The folder name becomes the command name unless `bundle.yaml` provides a
   title.
2. Add `script.py` as the command entry point. Add `bundle.yaml` when you want
   to set metadata such as the button title or tooltip.
3. Add optional assets such as `icon.png` in the button folder. Shared Python
   modules can live in a `lib/` folder at the button, panel, tab, or extension
   level.
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

These tests cover settings, column resolution, scope and search filtering, value formatting, XAML
handler wiring, and that selection and parameter reads only happen inside the ExternalEvent handler
(with document and closed-window guards). Revit must still confirm the WPF window and API behaviour.
