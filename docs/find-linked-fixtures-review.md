# Find Linked Fixtures: change review and detailed assessment

## Scope and compatibility

Reviewed the complete [button script](../CustomPyRevit.extension/ETC%20PyTools.tab/Electrical.panel/FindLinkedFixtures.pushbutton/script.py),
[XAML](../CustomPyRevit.extension/ETC%20PyTools.tab/Electrical.panel/FindLinkedFixtures.pushbutton/ui.xaml),
saved-settings format, and the repository's modeless-window/API guidance before changing the button.
Target: IronPython 2.7, pyRevit 4.8.x through 6.x, Revit 2023-2025.
No new runtime dependencies. The icon and existing project-specific settings file were not changed.

The requested expansion applies to both linked scanning and host family selection:

- **Linked Scan Categories...** is a searchable, multi-select list populated from the selected
  linked document's top-level `Settings.Categories`, not a fixed `BuiltInCategory` list.
  Nurse Call Devices appears when exposed by that document. If a nurse call family is actually
  classified as Generic Models or another category, select its actual category.
- **Host Family Categories...** lists categories of loaded host `FamilySymbol` objects. It does
  not load missing families. Reopen the tool after loading or changing families.
- Category selection does **not** make arbitrary geometry placeable. Scanning still requires
  `LocationPoint`; walls, curve-based devices, pipes and other non-point elements are rejected.
  Verbose mode lists those rejections.
- Wall and Ceiling modes accept `OneLevelBased` families. Ceiling (Face) accepts `WorkPlaneBased`
  families on horizontal host reference planes. Hosted, adaptive, view-based and curve-based
  target families are rejected explicitly rather than attempted with an unsuitable overload.
- Existing checkbox settings migrate by category ID, preserving previous selections and the
  original three-category default. Nurse Call Devices is available but not automatically enabled.
  Host targets still default to Electrical Fixtures. Explicit empty selections remain empty.
- Saved mapping/rule target IDs are still used. Target labels now include category and ID to
  avoid ambiguous display names. Changing target categories clears the family-name filter so an
  old electrical-only filter cannot conceal new nurse call families.

### Priority follow-up: Space geometry, elevation and search

The follow-up fixes spatial/level correctness first. Automatic updates to previously placed host
devices were explicitly **deferred by the user**; existing host-device matching and duplicate
prevention are not implemented in this pass.

- Hole-aware, winding-independent even/odd loop containment replaces the filled-loop `any()`.
  Boundary curves use Revit's `Curve.Tessellate()` instead of eight evenly spaced samples.
  Missing/failed boundary geometry stops the scan; partial loops are not silently accepted.
- `Space.IsPointInSpace` checks actual 3D volume, including vertical limits and sloping tops.
  Horizontal tolerance may reach **outer boundaries only**, never inner voids. A tolerance
  candidate must be verified inside the real Space at the original device Z; tolerance cannot
  reach another storey. Boundary-inset probing is conservative and may reject unusual corners
  or curves; real-model acceptance checks remain necessary.
- **Match linked level elevation** compares the source level at the device XY, transformed into
  the host, with the selected Space level (1 mm tolerance). It does not compare names.
  **Space volume only** skips this optional level-assignment check, but still checks full
  Space volume. Devices without a readable linked level are rejected in matching mode with
  instructions to use volume-only mode.
- Saved **Same Level Name** settings intentionally migrate to **Match linked level elevation**;
  **Ignore Level** migrates to **Space volume only**. These changes can reduce previously
  accepted results that were on the wrong storey or in a void.
- **Linked h. mm** = `(link.GetTotalTransform().OfPoint(device.Location.Point).Z -
  spaceLevel.ProjectElevation) * 304.8`. It is signed, in millimetres, and uses the host Space's
  level even when shared elevation display settings differ. The tooltip names the reference
  level. It describes the family insertion point (shown as **Ins**), not the top/bottom of its geometry.
  Rows without a point show `n/a`.
- **Linked h. mm** also shows **3D bottom / top**: the Z extent of the instance's visible geometry
  (`get_Geometry` at Fine detail, solids triangulated, nested `GeometryInstance`s expanded; falls
  back to `get_BoundingBox(None)`), transformed by the link and measured from the same Space
  level. This catches families placed at elevation 0 that offset their 3D internally (e.g. a
  700 mm "Height" parameter): **Ins** reads 0, **3D** reads ~700. The tooltip gives the 3D centre.
  Unreadable geometry shows `3D n/a` and logs a warning; the row is still accepted.
- Linked height is **informational only**, as requested. Default/rule/manual placement heights
  are unchanged. Wall/Ceiling placement is now based on the Space's level `ProjectElevation`,
  including when the active view is 3D or belongs to another level. The resulting instance
  elevation is verified within 1 mm; failed/read-only height writes cannot silently succeed
  at the wrong height and are rolled back by the existing per-row subtransaction.
- Space display reads the actual `ROOM_NAME` parameter, with a logged element-name fallback.
  Results show `number - name [level] (ID)`, preventing identical labels from overwriting one
  another. Search is case-insensitive across number, name and level and supports multiple
  whitespace-separated terms. It uses cached strings only, not API reads in UI callbacks.

### RVT settings, transfer and recovery

- The reported JSON had `"maps": {}` and `"rules": []`: it contains no mappings or rules to
  restore. This proves their absence in that copy, not when/how they were lost. The repository's
  original bundle JSON, containing **22 mappings**, was left unchanged as a recovery source.
- Previously the primary save destination was `find_linked_fixtures_settings.json` next to the
  executing button script; deploying another folder therefore changed which file was read.
  The earlier "changed on disk while this window was open" warning meant a content-fingerprint
  mismatch, **not** that an application held the file open. The user's most recent runtime
  error was unavailable for reproduction; the exact historic loss cause remains unproven.
- **Save to RVT** now explicitly writes project-specific settings to a dedicated `DataStorage`
  element using Extensible Storage. A fixed schema GUID
  `62c080d4-e64b-48fc-b6aa-61c4598f9b37` identifies a public-read/public-write string `Payload`
  containing a version-1 JSON envelope. It is not secret storage. No settings are written merely
  by scanning, filtering, placing or closing. The old bundle JSON is not the primary destination.
- Model reads and writes run through the guarded ExternalEvent. Save compares the model payload
  with the window's loaded snapshot inside a transaction, writes, regenerates, verifies exact
  read-back before commit, checks commit status, and verifies payload and normalized settings
  after commit. Native `DB.Transaction` is used here to inspect explicit start/commit statuses.
  Errors do not update the window's saved snapshot or fall back to another destination.
  A precommit error rolls back; a postcommit verification error is reported without claiming
  the committed change could be rolled back.
- **Save/sync the Revit project after Save to RVT**. The settings transaction changes the open
  document, not the RVT on disk or central. Model save, sync, Undo, worksharing ownership and
  close/reopen persistence still require real Revit acceptance tests.
- Model settings take precedence even with zero mappings/rules. Only when no model record exists
  is a bundle JSON loaded as a clearly labelled, read-only migration **draft**. The previous
  Custom Tools location remains a recovery source; if both locations contain different JSON,
  choose which draft to load. Neither file is overwritten. Save to RVT is required to adopt it.
  Missing model data and missing legacy JSON start an unsaved default draft.
  Corrupt/future-version payloads, duplicate storage records and unreadable JSON fail explicitly;
  they do not silently become empty defaults.
- Import reads UTF-8 with optional BOM, validates the outer structure, and shows mapping/rule
  counts for confirmation, including zero counts. It replaces window settings, not merges them,
  and does not change model storage until Save to RVT. **Export Settings...** writes only to a
  user-selected JSON path and verifies read-back; it does not save model settings. An existing
  export destination is backed up once to `.json.bak` before replacement. Keep independent backups.
- Initial/failed/partially completed startup cannot save incomplete UI settings. Numeric settings
  and rule heights are validated, rather than silently falling back during collection.
  Closing with valid unsaved changes offers Save to RVT and close / Discard / Cancel; the deferred
  save keeps the window open on failure. Invalid edits can be corrected or explicitly discarded.
  Reset requires confirmation and changes the window only until saved.
- If another window, a synced update or Undo changes stored model settings, Save refuses to
  overwrite the stale snapshot. **Export Settings...** preserves current edits; **Reload RVT**
  explicitly reloads the model after confirmation to discard edits, and permits saving again.
  Workshared ownership/read-only errors are surfaced without silent local-file fallback.
- The footer shows the source, window mapping/rule counts and how many target IDs are present
  in this host project. A zero target-ID count does not mean the mappings were deleted.
- Rules retain target IDs that are filtered out or absent from the current host project instead
  of silently saving null. This does not resolve IDs to a different project. Existing mappings use
  numeric target IDs from the original host model; use the same project or remap missing types.
- The reported `483488L is not JSON serializable` save error is addressed at the persistence
  boundary: mapping values and rule target IDs are written as exact decimal strings, so boxed
  CLR Int64/IronPython long values never reach the JSON encoder. Loading accepts both legacy
  numeric IDs and decimal strings and restores integers for comparisons. Null targets remain
  null; 64-bit precision is preserved without float conversion. Invalid IDs fail explicitly before
  touching storage. This follows the repository's existing string-ID JSON pattern.
- Nothing can reconstruct mappings from an empty `maps` object. Recover a prior JSON or `.bak`,
  inspect the import counts, then verify target IDs and mapping selections in Revit.

## Safety and regression fixes included

| Area | Finding before change | Implemented protection |
|---|---|---|
| Modeless document context | Captured project could differ from the active project | ExternalEvent handler rejects closed/inactive source documents |
| Event lifetime | One global request slot could silently replace a pending action | Window-owned handler/event, created in `__init__`; pending requests are not overwritten and retry instructions are shown |
| Modeless reads | Family filter, mapping and rule callbacks read Revit objects outside API context | Category, family, display and ID metadata is cached in API context; those UI callbacks use cached values |
| Stale results/catalogs | Link or Space changes could leave old results available | Clear stale rows; invalidate/refresh linked categories, including before a scan if a refresh was missed |
| Target filtering | Already-rendered combos did not follow filter changes | Refresh rule/result target combos without rebuilding height/mode controls |
| Partial placement | A failed row could leave created elements inside the batch transaction | One subtransaction per row; failed rows roll back independently |
| Commit reporting | Success was recorded before checking final commit | Only mark rows placed after a committed batch; retain select/delete-last IDs |
| Repeat click | Previously placed rows could be placed again in the same results | Uncheck successful rows and reject already-placed rows |
| Ceiling (Face) | Created a new SketchPlane and used a host-element overload rather than the selected plane's reference | Use `ReferencePlane.GetReference()` with the four-argument reference placement overload; reject nonhorizontal planes |
| IDs | Repeated deprecated `IntegerValue` access | Prefer 64-bit `Value` with a legacy fallback |
| Validation/settings | NaN/infinity accepted; read errors could become defaults; incomplete startup could save empty rules; bundle files shared project-specific IDs | Explicit RVT storage with transactional verification, version checks, model-conflict detection, startup guard and JSON import/export |
| Settings UI | Saved level-filter mode was not restored | Restore the saved ComboBox option |
| Family filter | Unchecking every family meant show all | Reject that ambiguous choice with an explicit message, retaining the current filter |
| Spatial containment | Voids were filled and vertical scope was ignored | Hole-aware tessellated loops plus actual Space-volume checks; outer-only horizontal tolerance |
| Level/elevation | Names and active-view level could select the wrong elevation | Compare transformed level elevations; place relative to the selected Space level and verify resulting height |
| Linked height | Stored as zero, not visible | Show transformed insertion-point height above Space level separately from placement height |
| Space search | Element display name could omit the actual Space name; duplicate labels collided | Read actual Name parameter, include number/name/level/ID and search cached fields |

These are intentional safety changes. In particular, "Wall" still means positioning a
non-hosted family near a boundary, not creating a wall-hosted family. A category selection
cannot override that restriction.

## Detailed assessment: remaining issues and improvements

The following are outstanding limitations, not evidence that the changes were fully validated
in Revit. The original highest-priority hole/vertical/placement-level findings are addressed
above; source-identity updates and duplicate prevention remain deferred by user choice.

| Priority | Location in script | Evidence and consequence | Recommended next change |
|---|---|---|---|
| High | `_scan` duplicate set | "Ignore duplicates" only removes same-family/type, rounded-coordinate duplicates inside the current linked scan. It never searches already-placed host instances. Rescanning and placing again can duplicate host devices, despite the new same-results repeat guard. | Store source link-instance/element identity on placements and compare existing host instances, with an explicit update/skip policy. |
| Medium | `_find_wall`, `_calc_inward`, `_rotate_to_face_inward` | Boundary projection is not true wall hosting. The code projects onto curves without a segment-domain check, looks up linked boundary IDs in the selected link without confirming the boundary's link instance, and uses an endpoint chord/Space center for direction. Concave rooms, arcs, multiple links and asymmetric family orientations need dedicated checks. | Separate boundary-placement from true hosting in the UI; validate owning link and bounded projection; use local tangent/inside tests and verify family facing conventions. |
| Medium | `tag_inst` | Tagging failures are swallowed. "Tag placed" writes Comments, or falls back to Mark; it does not create a Revit annotation tag and can overwrite an existing value. Wall/Ceiling elevation writes are now verified separately. | Report metadata failures. Rename to "Write source note" and use dedicated source-identity storage instead of overwriting Mark. |
| Medium | `_auto_match_ref_plane` | Substring matching can confuse level `1` with `10`, or height `300` with `1300`, and takes the first match. | Match structured tokens and geometric level/height; require confirmation for ambiguous matches. Prefer explicit reference-plane selection today. |
| Medium | `save_model_settings`, `save_s` | RVT settings are shared project data: worksharing may require borrowing/relinquishing the dedicated DataStorage. JSON exports have a first-file backup but are not atomic or cross-process locked. Imported target IDs still refer to the original project. | Validate multi-user save/sync/ownership and reload recovery in Revit. Use unique export names and independent backups; remap target IDs when transferring across projects. |
| Medium | `_undo` | "Undo Last" deletes recorded created IDs in a new transaction; it is not Revit Undo. Dependent elements may be deleted as well, and transaction commit status is not checked there. | Rename to "Delete Last Placement", preview dependent deletions and check commit status. Use native Revit Undo when appropriate. |
| Low | `_load_sym`, `_render`, `_rpanel` | Family/reference-plane catalogs are snapshots, and every result creates WPF controls with a full target list. Broad category scans plus Verbose can be expensive in large projects. | Add a guarded Refresh Catalogs action, spatial prefiltering, progress/cancellation and a virtualized result grid; benchmark with real models before selecting thresholds. |
| Low | `_amap`, family filtering | Saved source mappings use family/type text without source category/document identity; family filters group solely by family name. Same-name content may be ambiguous across categories/projects. | Use stable, project-aware identity for mappings and category-qualified family filter items, with explicit migration. |
| Low | `space_polys`, `inside_space` | Revit tessellation has display-oriented accuracy; tolerance probes use small segment-normal insets and can conservatively miss acute corners or unusual curved boundaries. | Benchmark curved/concave Spaces and corner cases against Revit volume checks before considering exact geometric projection. Do not remove the volume check. |

Suggested next implementation: source-identity duplicate prevention and safe in-place updates
when the user authorizes that scope, then verified hosting behavior. Real-model storage acceptance
and performance follow separately. Use a small test Space and review accepted rows and resulting
elevations before bulk placement; a new scan does not protect against existing host duplicates.

## Verification completed outside Revit

Current result: **110 button-specific tests and all 335 repository tests passed**.
The settings recovery JSON in the bundle remains unchanged.
The priority follow-up includes regression coverage for Space geometry, levels, linked height and search.
Python compilation, Pylance syntax checks and `git diff --check` also passed.

The [mock tests](../tests/test_find_linked_fixtures.py) execute the actual script's functions and
classes through an AST loader that omits Revit/WPF imports and button launch.
They do not emulate Revit's hosting rules, WPF binding or failure processor.

- Category defaults and old-checkbox migration; explicit empty selections; Nurse Call Devices
  selection; canceled/prechecked pickers; unchecked-but-highlighted options; dynamic link
  catalogs and stale/unloaded-link handling.
- Linked scans use the chosen `ElementId` with `OfCategoryId` and apply the link transform;
  non-point, name, Space, level and scan-duplicate rejection remain covered. Host collection
  is no longer restricted to Electrical Fixtures.
- Rule/result dropdown refresh, preserved height/mode edits, first-match rules, keyword
  matching, settings round trip, 64-bit/legacy IDs and finite numeric validation.
- Active-document/closed-window guards, pending-event preservation and denied-event reporting.
- Failed/exceptional row rollback, successful subtransaction commit, rejected batch commit,
  and selection/delete-last IDs after a successful batch.
- Wall/Ceiling placement signatures, boundary projection/rotation and height-setting calls;
  Ceiling (Face) reference overload and nonhorizontal-plane rejection.
- Reproduced the previous inner-hole and missing-Space-name failures before patching; tests now
  cover inner voids, nested islands, disjoint/reversed loops, outer-only tolerance, wrong-storey/
  sloped-top rejection, tessellation and boundary failures.
- Signed linked height with translated/rotated coordinates and differing shared-elevation
  display; display-only height column, separate editable placement height and absent-point
  `n/a`; level matching with mismatched names or elevations and missing linked levels.
- Actual Space names, number/name/level multi-term searches and duplicate-label disambiguation;
  placement in 3D/different-level active views, invalid Space levels, final height verification
  and failed/read-only/missing parameter behavior.
- Settings read errors and premature-save failures reproduced before patching; BOM/legacy JSON
  migration, import/cancel, missing targets, startup-save guard, export backup preservation,
  serialization failures and no automatic legacy-file writes are regression-tested.
- API-bound storage tests execute production schema/entity/collector and transaction logic against
  mocks: exact large-ID payload round trip, model precedence (including empty settings), migration
  without writes, document isolation, stale-snapshot conflict/reload, duplicate/corrupt/future records,
  ownership/read-only failures, start/commit failures, rollback and pre/postcommit verification.
- Window lifecycle tests cover verified save snapshots, failed-save edit preservation, explicit
  JSON export, numeric validation and close Save/Discard/Cancel, including deferred close on success.
- XAML named controls and wiring; Python compilation and Pylance syntax checks.

Commands:

```text
python -m unittest discover -s tests -p test_find_linked_fixtures.py -v
python -m unittest discover -s tests -v
python -m py_compile "CustomPyRevit.extension/ETC PyTools.tab/Electrical.panel/FindLinkedFixtures.pushbutton/script.py"
git diff --check
```

Local `tools/revit_api_lookup.py` could not find Revit XML files in this Linux environment.
API choices were checked against published
[OfCategoryId](https://www.revitapidocs.com/2023/63304108-73f8-844e-82fc-5b8fad9839b0.htm),
[Settings](https://www.revitapidocs.com/2023/9aa29bb7-d720-8c97-0ccb-e3e6046c545c.htm),
[FamilyPlacementType](https://www.revitapidocs.com/2023/2abb8627-1da3-4069-05c9-19e4be5e02ad.htm),
[reference-based placement](https://www.revitapidocs.com/2023/be4b822c-829a-7e7b-8c03-a3a324bfb75b.htm)
and [ReferencePlane.GetReference](https://www.revitapidocs.com/2023/825a593b-4acc-c2ad-9df7-b281a3a2575b.htm)
documentation, and the pyRevit 4.8.16 `TemplateListItem`/`SelectFromList` source.
The priority pass also verified
[Level.ProjectElevation](https://www.revitapidocs.com/2023/895ef506-bfea-cc4e-31f8-aad2af6672e4.htm),
[Curve.Tessellate](https://www.revitapidocs.com/2023/f95f3199-3251-c708-c5a3-a0e9ef95ecfa.htm),
and [Space.IsPointInSpace](https://revapidocs.com/2023/33c97031-a9ad-00d0-4d4a-42522201d2db.htm).
The settings pass verified Autodesk's
[Extensible Storage guide](https://help.autodesk.com/cloudhelp/2023/ITA/Revit-API/files/Revit_API_Developers_Guide/Advanced_Topics/Storing_Data_in_the_Revit_model/Revit_API_Revit_API_Developers_Guide_Advanced_Topics_Storing_Data_in_the_Revit_model_Extensible_Storage_html.html)
and [DataStorage.Create](https://www.revitapidocs.com/2023/8051ae69-476d-209d-c83c-148bafd6e43c.htm),
plus the generic
[Entity.Set field/value overload](https://www.revitapidocs.com/2023/fef836d9-61bf-4acb-cdeb-7f03d7f23a51.htm).
Neither IronPython execution nor Revit 2023/2024/2025 runtime compatibility is certified here.

## Revit acceptance checklist

Use a disposable copy of a project, not the live model. Back up the existing settings file.
Close any previous button window, reload pyRevit, then open
**ETC PyTools > Electrical > Find Linked Fixtures**.

1. **Existing workflow:** select the original loaded link and a simple bounded MEP Space in
   a plan on the same level. Confirm previous scan selections, mapping targets, rule order,
   family filter and migrated level mode. Scan and compare electrical results to the old button,
   allowing for intentionally rejected void/wrong-storey devices. Search for a Space by part
   of its name, number and combined name/level; results must show its actual name and unique ID.
2. **Nurse call scan:** open **Linked Scan Categories...**, search `nurse`, check Nurse Call
   Devices (or the family's actual category), and confirm. Scan a Space containing known
   examples. Expect point-based devices to appear with the correct family/type; a name or
   level filter can still exclude them. Test another category as well.
3. **Host choices:** load representative nurse call and electrical families before opening the
   tool. Choose them in **Host Family Categories...**. Expect their target types in existing
   result and rule dropdowns. Verify height/mode edits survive filter changes. Cancel a picker,
   select multiple categories, and save/reopen to verify persistence.
4. **Placement modes:** place one compatible `OneLevelBased` family each in Wall and Ceiling
   mode; check XY, Space-relative elevation, facing, and boundary fallback. Repeat from a 3D
   view or a plan on another level (disable Active View Spaces Only if necessary); the Space
   level must still be used. Place a `WorkPlaneBased` family
   using Ceiling (Face) and an explicitly selected horizontal reference plane. Confirm it
   references that plane and no extra SketchPlane is created. Try a wall-hosted, curve-based
   or view-specific family: expect a clear unsupported-placement message and no model change.
5. **Failure isolation/commit:** mix one valid and one deliberately invalid row. Confirm no
   orphan geometry remains from the failed row. Exercise a genuine Revit commit failure if
   available; success must not be reported when the batch rolls back. Check native Undo and
   the button's Select Last/Undo Last behavior on the disposable model.
6. **Repeat/stale state:** click Place Checked again and confirm existing successful rows are
   not recreated. Change Space, link or scan categories: old scan rows must clear. Change
   links rapidly, including an unloaded link; stale category lists must not be used.
7. **Document/event safety:** keep the window open, switch active project and try Scan/Place/
   Select Last/Undo Last. Expect a project-context error and no cross-document action.
   Close the source project, retry, and close the window with an action pending. Rapid
   clicks must not silently replace a pending action; follow the retry message.
8. **Geometry/elevation acceptance:** inspect a Space with a hole and devices near its inner
   and outer boundaries. A point inside the void must be rejected even with a large scan
   tolerance; a point just outside an outer wall may pass tolerance, but another-storey device
   must not. Test concave/curved boundaries and sloped tops. Confirm expected acceptance
   conservatively; tessellated tolerance probes can miss unusual corners.
9. **Linked height:** manually compare a known device's transformed insertion Z minus the
   host Space level's project elevation with **Linked h. mm**. Test a translated/rotated/mirrored
   link and shared-elevation level display. The displayed height must not change row placement
   height. Give host/linked levels different names but matching transformed elevations; they
   should pass matching mode. Same names at different elevations must fail. A device without a
   source level can pass **Space volume only**, but only if it is in the Space volume/tolerance.
   Place a fixture at elevation 0 whose family offsets its 3D by 700 mm: **Ins** must read 0 and
   **3D** bottom ~700 (compare with a section/elevation of the link).
10. **Deferred risks:** test duplicate host devices and ambiguous reference-plane names
    separately. Existing host duplicates are not prevented and automatic updates are not
    implemented. These probes measure outstanding issues, not guaranteed behavior.

Repeat core checks on Revit 2023, 2024 and 2025 and the deployed pyRevit/IronPython versions.
Only Revit can confirm actual API overload binding, family compatibility, failure processing,
WPF behavior and final model geometry. Do not equate passing mock tests with production approval.

### Settings-specific Revit checks

1. Keep a safe copy of the old JSON. Close all tool windows, reload pyRevit, open the button in
   the original host project, then use **Import Settings...** and choose the older JSON. Confirm
   nonzero mapping/rule counts before accepting. A zero-count file is not a recovered mapping file.
2. Verify footer source and target-ID count, scan original linked family types, and confirm mapped
   targets. Click **Save to RVT**: expect verified mapping/rule counts and a save/sync reminder,
   with no JSON permission/open-file warning. **Save the RVT or Sync with Central**, close/reopen
   the model and button, scan again: targets, rule order, categories, heights and filters must return.
   Verify the original bundle/import JSON has not changed.
3. Filter a rule target out, Save to RVT, save/sync and reopen, then restore the filter. Its ID must
   survive. Import into another project: missing targets must be reported, not silently deleted.
   Two projects must retain independent settings. Empty saved RVT settings must not reload the
   nonempty bundle JSON. Test decimal-string and old numeric IDs, including `483488` if available.
4. Export to a new JSON path and inspect mapping/rule counts and quoted decimal target IDs.
   Change settings without Save to RVT: scan/filter/placement must not persist the edits.
   Close with edits: Cancel keeps the window open; Discard preserves model settings;
   Save and close only closes after verified saving. A failed save must retain edits.
5. In two tool windows, load the same RVT settings, then change/save in one. Saving from the
   stale window must warn, preserve edits and not overwrite the newer record. Export the stale
   edits, use Reload RVT, confirm discard, and save again. Repeat after native Undo and a synced
   update; active-document guards must also reject Save when another project is active.
6. In a disposable workshared model, test another user's ownership of the settings DataStorage.
   Save must fail visibly without changing the saved snapshot or writing a JSON fallback. Export
   edits, resolve ownership/relinquish/sync, Reload RVT and retry. Confirm successful shared settings
   are visible to the other user after sync/reload. Local read-back cannot prove central persistence.
7. Malformed imports must not replace window settings. Startup/schema/duplicate-record failures
   must not silently become defaults or overwrite existing data. Open and close immediately before
   initialization; no settings write should occur. Exercise WPF save-and-close timing in Revit.
   A damaged model record requires investigation; do not delete stored data merely to bypass errors.
8. For the tab move, use a disposable project without an RVT settings record and keep backups of
   both bundle JSON files. If the previous and current files differ, confirm the prompt can load
   either draft and Cancel leaves both untouched. Save the chosen draft to RVT, save/sync, reopen
   and confirm the model settings now take precedence. If the files match, no choice is needed.
