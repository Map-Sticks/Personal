# CircuitHub - context and roadmap

Status: first upgrade of the imported v11.3 baseline: panel rename/bulk rename and action safety.
Mock tests pass; Windows/Revit validation is still required. Virtual panels, panel creation, and
level editing are not implemented in this increment.
Read the repo rules first (`.github/copilot-instructions.md`, `docs/*.md`).

## What it does today

Modeless WPF window (`script.py` + `ui.xaml`) listing Panel > Circuit > Fixture rows with level
filter and search. Actions: move circuits to another panel (bulk, button or Alt-drag), reassign /
add / remove fixtures to circuits (from Revit selection or highlighted rows), create a new circuit
from the Revit selection (optionally assigned to a highlighted panel), rename a circuit's Load
Name, create a panel schedule, undo, and optional auto-refresh on document change (off by default).
Also includes configurable category filtering, inline add/remove actions, natural circuit-number
sorting, optional empty panels, unassigned circuits grouped by fixture level, and panel picking
in the view. Model-changing actions use ExternalEvent. Add/reassign actions have heuristic phase
pre-checks; known Revit validation failures have friendly messages.

The **Panels** tab edits the built-in **Panel Name** parameter, not the family/type name:

- Highlight one real panel row and use **Rename one highlighted panel...**.
- Highlight multiple real panel rows for literal, case-sensitive find/replace plus prefix/suffix.
  Replacement happens first; a blank Find skips replacement. Leading/trailing whitespace in the
  final name is trimmed.
- Expand the confirmation dialog's details to review every old name, element id, and proposed name.
  Cancel and unchanged names create no transaction.
- Empty/control-character names, duplicate destinations (case-insensitive), missing panels and
  non-writable parameters are rejected. Names already occupied by another panel are blocked even
  if that panel is also being renamed; name swaps require an unused intermediate name.
- A failed parameter write rolls back the entire batch. One successful batch is one Undo item.
- Empty equipment remains accessible through **Show empty panels**. Unassigned groups are not
  real panels and cannot be renamed.

## Author's wishes for the next version (priority)

1. **Virtual / "invisible" panels.** Move away from having to place a physical panel family
   in the model to host circuits. Create a panel by simply giving it a **name** (no modelled
   geometry). Feasibility is NOT yet verified: Revit circuits need a `BaseEquipment` that is an
   Electrical Equipment element, so a virtual panel probably means an unplaced/hidden
   proxy equipment instance (e.g. on a "virtual" workset / hidden category / in a non-printing
   location) created and managed by the tool. Verify with `tools/revit_api_lookup.py` and test in
   Revit before promising anything.
2. **Create panels** from within the tool (name, type, level, supply/distribution system).
3. **Rename panels** (Panel Name parameter, `RBS_ELEC_PANEL_NAME`) and bulk rename:
   implemented in this increment; needs Revit acceptance testing.
4. **Change a panel's level** (and other panel properties) from the tool.
5. Do all of the above in the tool instead of clunky Revit schedules.

## Safety and performance changes in this increment

- Circuit membership is indexed once per cache build; fixture membership lookups no longer scan
  every circuit. Unreadable system types are logged and skipped, not included as power circuits.
  Unreadable membership aborts the cache build and action instead of using an incomplete index.
- Document changes invalidate the cache even with Auto refresh off. The next API action rebuilds
  dirty data before preflight; automatic refresh is coalesced with an action's own refresh. If
  document-change subscription fails, actions rebuild rather than trust stale data.
- Model-access callbacks (including selection filtering, tree rebuilds, Undo and view selection)
  run inside ExternalEvent, guarded against document switching/closing. Row highlighting and
  details use plain cached text so selection does not occupy the action queue.
  Actions are not silently overwritten: a second queued action is rejected with a warning.
- ElementId reads use Value with a 2023 IntegerValue fallback. Constructors explicitly use Int64
  for 2024+ and Int32 for 2023; names and picker options identify panels by Panel Name and id.
- Transaction commits are checked before logging success. `CheckedTransaction` subclasses the
  pyRevit wrapper to retain its setup and forced-modal failure handling, but checks the native
  Commit result before disposal. This is necessary because supported pyRevit versions can swallow
  commit exceptions, and newer wrappers dispose before their status property can be queried.
- Target Only and Locate Selection expand the level-based unassigned group; missing element
  level lookup returns `<No Level>` without calling an undefined helper.
- Inline and highlighted-row removal affect only the displayed fixture/circuit links; the explicit
  **Quick: Remove selected from all circuits** action retains its all-memberships behavior.
- Picking a panel hides/restores the modeless window; Escape is handled as cancellation.

## Remaining review notes

- With Auto refresh off, the visible tree/details can remain stale after external edits or Undo
  until Refresh, although actions no longer use stale membership data.
- Existing best-effort property/level/phase helpers and some UI handlers still swallow exceptions.
  These have not been comprehensively rewritten.
- Settings now use `%APPDATA%/CircuitHub/circuit_hub_settings.json`; save failures are reported.
- `panel_level_name` guesses level via several parameters; confirm it matches the intended level for
  your families.
- `phase_class` guesses phase from explicit parameters or poles, not voltage. Pole count is not
  sufficient to establish phase compatibility. Unknown phase passes validation; circuit creation
  no longer has v8's explicit mixed-phase pre-check.
- Rename planning, event dispatch, cache indexing and transaction failure paths are mock-tested.
  Existing phase inference and the full electrical-circuit API behavior still need dedicated tests.

## API verification

Local `tools/revit_api_lookup.py` checks could not find Revit XML/assemblies in Codespaces.
Online references checked for this increment:

- [BuiltInParameter: RBS_ELEC_PANEL_NAME = Panel Name](https://www.revitapidocs.com/2025/fb011c91-be7e-f737-28c7-3f1e1917a0e0.htm).
- [ElementId.Value, since 2024](https://www.revitapidocs.com/2024/6f216e39-b66d-5df5-c60c-b9aaccb1e28a.htm)
  and [Int64 constructor](https://www.revitapidocs.com/2024/0b12e329-90c1-4610-3bae-83df3236266c.htm).
- [Document.IsValidObject](https://www.revitapidocs.com/2025/41477e88-aa81-4648-aa8b-8f2d482d4705.htm),
  [ExternalEvent.Raise](https://www.revitapidocs.com/2025/13bf4411-c400-dcd2-458c-7f09357d9ecb.htm),
  and [ExternalEventRequest outcomes](https://www.revitapidocs.com/2025/f3a47284-4a83-d8f7-8d97-8235de7be1ba.htm).
- pyRevit transaction wrapper implementations at
  [4.8.16](https://github.com/pyrevitlabs/pyRevit/blob/v4.8.16.24121%2B2117/pyrevitlib/pyrevit/revit/db/transaction.py)
  and [6.5.5](https://github.com/pyrevitlabs/pyRevit/blob/v6.5.5.26237%2B2044/pyrevitlib/pyrevit/revit/db/transaction.py).

These verify API names and wrapper behavior, not the writability of particular project families,
worksharing ownership, real transaction outcomes, or virtual-panel feasibility.

## How to develop

IronPython 2.7, Revit 2023-2025, follow `docs/pyrevit-patterns.md`. Verify any new API with
`python tools/revit_api_lookup.py <name> [--year N]`. Keep one transaction per user action.
Always give Revit test steps (reload pyRevit, open CircuitHub, which button, expected result).

Run the mock checks with `python -m unittest discover -s tests -v`.

## Revit acceptance checks (disposable project)

1. Close any existing CircuitHub window, reload pyRevit, then click **Custom Tools > Electrical >
   Circuit Hub**. Test with IronPython 2.7 on the supported Revit 2023-2025 installations.
2. Enable **Show empty panels**, highlight one real panel, open **Panels**, and rename it.
   Expand the preview details. Confirm the Panel Name parameter, tree, target details and panel
   picker reflect the new name, while family/type, circuits, level and distribution system do not
   change. Clear Search if the new name no longer matches the old search text.
3. Ctrl-select two or more panels, enter prefix `TEST-`, preview, first Cancel, then repeat and
   confirm. Cancellation must change nothing; confirmation must update all selected panels.
   **Undo last** must revert the entire batch in one step. With Auto refresh off, click Refresh.
4. Try a blank name, an existing panel name (also with different casing), and a batch whose
   replacement produces duplicates. Try equipment with a missing/read-only Panel Name parameter.
   Expect a clear warning and no changes. Test worksharing ownership/commit failure separately.
5. Leave the tool open, switch to a second project (including one with matching numeric element
   ids), and request a rename or selection action. It must be blocked without changing either
   project. Switch back to continue; after closing the original project, reopen CircuitHub.
6. With Auto refresh off, change circuit memberships or Undo outside CircuitHub, then attempt
   Add/Reassign/Remove. Preflight must use current memberships. Enable Auto refresh and verify a
   CircuitHub edit does not cause duplicate full refreshes.
7. On an unassigned circuit, test **Target only** and **Locate selection**. For a fixture with
   multiple power-circuit memberships, inline `-` and **Rows: Remove fixtures** must remove only
   the displayed/highlighted link. The explicit remove-from-all action must still remove all.
8. Regression-test circuit moves, Alt-drag, load rename, circuit creation, category filtering,
   schedules and panel picking (including Escape). Highlighting a row must not block the next
   action. Model/API context, WPF wiring, actual electrical validation and Undo require Revit;
   Linux syntax checks and mocks cannot establish those results.
