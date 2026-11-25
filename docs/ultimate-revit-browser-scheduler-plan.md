# Ultimate Revit Browser & Scheduler — Implementation Plan

> **Status:** Original specification & architectural blueprint (immutable baseline).
> Any implementation progress updates are recorded in a separate progress markdown file.

## 1. Objective

Build a new modeless pyRevit button — **Revit Browser & Scheduler** (`RevitBrowserScheduler.pushbutton` under `CustomPyRevit.extension/ETC PyTools.tab/Electrical.panel/`, ribbon label `Browser & Scheduler`) — alongside the existing `ParameterBrowser.pushbutton` (leaving the classic tool untouched).

Target runtime:
- **Engine:** IronPython 2.7 (`__persistentengine__ = True`, compatible with pyRevit 4.8.x–7.x and CPython 3 for offline unit tests).
- **Revit Versions:** Revit 2023–2025 (32-bit `ElementId.IntegerValue` on 2023, 64-bit `ElementId.Value` on 2024+).
- **No external pip packages or Microsoft Excel installation required.**

---

## 2. Core Functional Requirements

1. **Universal Revit Search & Browser**
   - Search and browse any combination of Revit categories and system element classes: Model categories, Annotation categories, Analytical categories, Views, Sheets, Schedules, Levels, Grids, Rooms, Spaces, Areas, Groups, MEP Systems, Materials, Element Types (FamilySymbols / ElementTypes), and Linked Model elements (**strictly read-only**).
   - Scopes: `Whole Project`, `Active View`, `Level(s)`, `Current Revit Selection`, `Workset`, and `Selected Spaces / Rooms` (including a user-chosen combination of specific Spaces/Rooms).
   - Multi-token full-text search + structured column query operators (`Category:Walls`, `Level="L02"`, `Mark!=""`, `Area>20`, `Count>=4`, `Comments~regex`, `Mark:EMPTY`, `Mark:DUPLICATE`, `-exclude`).
   - Two-way Revit navigation: `Select`, `Show / Zoom`, `Isolate in View`, `Reset Isolate`, `Open View/Sheet`, optional `Sync with Revit Selection` (`UIApplication.SelectionChanged`), and a collapsible Related/Spatial Contents panel.

2. **Multi-Category Schedule Builder**
   - Combine arbitrary categories/classes into a single live schedule.
   - Stratified parameter discovery across every selected category and element type (never limited to the first 20 elements).
   - Clear disambiguation between **`[I]` Instance** and **`[T]` Type** parameters, keyed internally by `(scope, key_type, id_or_name)` to prevent duplicate-name collisions.
   - Computed / pseudo columns: `Element Id`, `UniqueId`, `Category`, `Family`, `Type`, `Level`, `Phase Created`, `Phase Demolished`, `Workset`, `Design Option`, `Group`, `Pinned`, `Host`, `Room`, `Space`, `Link Source`, `Edit Status`, and `Count: <Category>`.
   - Natural sorting (e.g., `L1, L2, L10`), **Grouping & Summary/Takeoff mode** (itemized vs. grouped by column with `Count`, `Sum`, `Min`, `Max`, `Avg` totals).

3. **Excel Round-Trip (Protected `.xlsx` Export + Smart 3-Way Import)**
   - **Export** via pyRevit's built-in `xlsxwriter` (with `.csv` fallback):
     - `Schedule` sheet: frozen headers, auto-filter, column widths, unlocked white cells for editable instance parameters, unlocked blue-tinted cells for editable type parameters, **grey locked cells** for non-modifiable fields/elements, optional Excel sheet protection, and a `Lock Reason` / status indicator.
     - Hidden `_Meta` sheet: schema version, document identity, column metadata (`param_key`, `is_type`, `storage_type`, `is_pseudo`), and the **original exported baseline value** per `(UniqueId, param_key)`.
     - `_Legend` sheet explaining color coding and safe editing rules.
   - **Import** via pure-Python `zipfile` + `xml.etree.ElementTree` `.xlsx` parser (zero dependency on `openpyxl`, `xlrd`, or Excel COM):
     - Matches elements by `UniqueId` first, `ElementId` fallback.
     - **3-Way Diff & Conflict Detection**: compares `Exported Baseline` vs `Excel New` vs `Current Revit Value`.
     - **Non-Modifiable Element & Cell Guardrails**:
       1. Pseudo / computed columns → blocked.
       2. Read-only / formula / non-`UserModifiable` parameters → blocked.
       3. Linked model elements → blocked (always read-only).
       4. Workshared elements owned by another user (`WorksharingUtils.GetCheckoutStatus`) → blocked.
       5. Group members (`GroupId != InvalidElementId`) → blocked/flagged to prevent group corruption.
       6. Pinned elements (`element.Pinned`) → flagged & skipped by default (opt-in override).
       7. Type parameters (`[T]`) → blast-radius count shown + conflicting edits to the same `TypeId` across rows blocked until resolved.
       8. Missing/deleted elements and invalid unit/type conversions → flagged before commit.
     - Interactive preview + Yes/No confirmation + single undoable `revit.Transaction` with cancellable `forms.ProgressBar`.

4. **Direct In-Browser Editing & Safe Deletion**
   - Staged inline cell editing in WPF `DataGrid` (non-modifiable cells locked with reason).
   - Pending edits highlighted and counted; nothing mutates Revit until explicit `Apply Changes` (Yes/No confirmation + single undoable transaction) or `Revert Staged`.
   - Bulk-edit actions on selected rows: `Set Value...`, `Find & Replace...`, `Auto-Number...`, and `Paste from Clipboard`.
   - Safe `Delete Selected...` with pre-delete impact analysis: blocks linked/owned elements, warns on pinned/group/critical system elements, runs a **rolled-back cascade preview transaction** to report exact dependent/hosted elements Revit will also delete, and requires Yes/No confirmation.

5. **Optimised Active Refresh (Strictly for Listed Elements)**
   - Subscribes to `ControlledApplication.DocumentChanged`, `UIApplication.ViewActivated`, and `UIApplication.DocumentClosing` (unsubscribed cleanly in `on_closed` and `DocumentClosing`).
   - **O(k) incremental tracking**: `DocumentChanged` performs only fast `set` intersections against `tracked_element_ids`, `tracked_type_ids`, and `tracked_category_ids`, ignoring our own transactions.
   - 300 ms debounced `DispatcherTimer` coalesces bursts into one `ExternalEvent.Raise()`.
   - `ExternalEvent.Execute` fetches **only** dirty IDs via `doc.GetElement(eid)` and patches only affected rows in memory and in the `DataTable`.
   - **Storm Guard** (>500 dirty elements switches that burst to a `"Stale — Click to Refresh"` badge) and **Mode toggle** (`Live`, `Notify Only`, `Manual`).
   - Preserves staged edits by flagging conflicts rather than silently overwriting them.

6. **Presets System (User Presets + Built-in Starter Presets)**
   - JSON presets stored per-user in `%APPDATA%\pyRevit\RevitBrowserScheduler\presets\*.json` (architecture cleanly extensible for a shared team folder later, without exposing team-folder complexity yet).
   - Captures: categories/classes, element kinds (`instances`, `types`, `links`), scope, level/workset/selected-space filters, ordered columns + widths, query/filter string, sort column/direction, group-by & summary/takeoff mode, spatial panel settings, and auto-refresh mode.
   - **Built-in Starter Presets**:
     1. *Electrical & Lighting Takeoff (Totals)* — multi-category electrical/lighting fixtures & devices, grouped by Category + Family : Type with `Count` totals.
     2. *Multi-Category Quantity Takeoff* — Doors, Windows, Walls, Floors, Lighting & Electrical grouped with `Count` and `Sum` totals.
     3. *Spaces & Rooms Schedule* — Spaces + Rooms with Number, Name, Level, Area, Department, and electrical `Count:` rollups.
     4. *Selected Spaces — Combined Takeoff* — scopes to a user-selected combination of specific Spaces/Rooms (or current Revit selection) and lists/totals all elements inside those combined spaces.
     5. *Views & Sheets Index* — all Views & Sheets with Sheet Number, Name, Scale, Phase, Template.
     6. *Model QA — Missing & Duplicate Marks* — model elements pre-filtered to surface missing or duplicate `Mark` values.

---

## 3. Architecture & File Layout

```text
CustomPyRevit.extension/ETC PyTools.tab/Electrical.panel/RevitBrowserScheduler.pushbutton/
├── bundle.yaml                 # title, tooltip, author, engine: persistent: true
├── icon.png                    # unique 96x96 8-bit RGBA icon
├── UI.xaml                     # modeless WPF layout (handle_esc=False, no IsCancel="True")
├── script.py                   # thin WPF window + ExternalEvent + Revit event subscriptions
└── lib/
    ├── browser_state.py        # record model, staged edits, O(k) delta merge, conflict detection
    ├── query_engine.py         # search/filter parser, natural sorting, grouping & takeoff totals
    ├── preset_store.py         # preset schema, starter presets, user JSON save/load/manage
    ├── excel_io.py             # xlsxwriter protected exporter + pure-Python zipfile/XML .xlsx reader
    ├── diff_engine.py          # 3-way diff, non-modifiable classifier, type-conflict detector
    └── revit_adapter.py        # Revit API collectors, [I]/[T] param read/write, UnitFormatUtils, WorksharingUtils, cascade delete preview
```

---

## 4. Implementation Phases

### Phase 1 — Core Engine, Universal Browser & Multi-Category Scheduler
- Create `RevitBrowserScheduler.pushbutton` bundle (`bundle.yaml`, unique 96x96 RGBA `icon.png`, panel `bundle.yaml` layout entry).
- Implement `lib/browser_state.py`, `lib/query_engine.py`, and `lib/revit_adapter.py` (universal category/class enumeration, multi-category + Types + Linked read-only collection, stratified `[I]`/`[T]` parameter discovery, natural sort, grouping/totals, and Selected Spaces combination scoping).

### Phase 2 — Presets System & Optimised Active Refresh
- Implement `lib/preset_store.py` with full preset serialization, management helpers, and all 6 built-in starter presets (Takeoffs/Totals, Specific Spaces, Selected Spaces Combination, Views & Sheets, Model QA).
- Implement O(k) `DocumentChanged` / `ViewActivated` / `DocumentClosing` delta tracking, 300 ms debounce timer, storm circuit breaker, and `Live / Notify / Manual` modes.

### Phase 3 — Direct In-Browser Editing & Safe Deletion
- Implement staged inline editing, bulk edit helpers (`Set Value`, `Find & Replace`, `Auto-Number`, clipboard paste), Type parameter blast-radius calculation, and pre-delete cascade impact preview + safe deletion.

### Phase 4 — Excel Round-Trip (Protected `.xlsx` Export + Smart 3-Way Import)
- Implement `lib/excel_io.py` (`xlsxwriter` export with locked non-modifiable cells, color coding, comments, `_Meta` baseline sheet + pure-Python `zipfile`/`ElementTree` `.xlsx` reader) and `lib/diff_engine.py` (3-way diff, non-modifiable element/cell guardrails, Type conflict detection).
- Wire all four phases into `UI.xaml` and `script.py`.

### Verification & Documentation
- Add comprehensive mock-based unit tests in `tests/test_revit_browser_scheduler.py`.
- Update `tests/test_extension_layout.py` and `README.md`.
- Run `python -m py_compile` on all new scripts and `python -m unittest discover -s tests -v`.
- Provide exact Revit testing steps and clearly note what only live Revit can confirm.

---

## 5. Key Limitations & Engineering Mitigations

| Limitation | Mitigation |
|---|---|
| IronPython 2.7 (no f-strings, type hints, `pathlib`, `openpyxl`) | Pure Python 2.7/3 compatible syntax; `xlsxwriter` for export + stdlib `zipfile`/`xml.etree.ElementTree` for `.xlsx` import. |
| Revit 2023 (32-bit `IntegerValue`) vs 2024+ (64-bit `Value`) | Central `element_id_value()` and `as_eid()` helpers in `browser_state.py` / `revit_adapter.py`. |
| `ElementType.Name` unreadable in IronPython | Read via `revit.query.get_name` / `SYMBOL_NAME_PARAM`. |
| Modeless WPF API context & `Esc` key | `__persistentengine__ = True`, `ExternalEvent` created in `__init__`, `ActionQueue`, `handle_esc=False`, zero `IsCancel="True"` in XAML. |
| `DocumentChanged` performance & leaks | O(k) `set` intersection only inside event, ignore self-transactions, 300 ms debounce, 500-element storm guard, unsubscribe in `on_closed` + `DocumentClosing`. |
| Duplicate parameter names & Instance vs Type | Structured parameter key `(scope, key_type, id_or_name)`; explicit `[I]` / `[T]` tagging and Type blast-radius warnings. |
| Revit internal units vs display strings | `SetValueString()` / `UnitFormatUtils.TryParse()` for `Double`, Yes/No & enum mapping for `Integer`, name-to-`ElementId` lookup. |
| Non-modifiable elements & cascade deletion | Unified Modifiability Inspector (read-only, pseudo, linked, worksharing owner, group, pinned) + rolled-back cascade delete preview transaction. |
