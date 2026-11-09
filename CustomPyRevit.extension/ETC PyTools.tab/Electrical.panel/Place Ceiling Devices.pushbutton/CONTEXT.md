# Place Ceiling Devices - context for humans and AI

Status: **scaffold only**. `script.py` just shows an alert. Read the repo rules first
(`.github/copilot-instructions.md`, `docs/pyrevit-patterns.md`, `docs/pyrevit-versions.md`,
`docs/revit-api-references.md`). Code must be IronPython 2.7 compatible, Revit 2023-2025.

## 1. Goal

A pyRevit pushbutton that auto-places ceiling-hosted devices (smoke detectors first, then
light fittings) in selected MEP Spaces using **deterministic geometry, no AI at runtime**.
The user selects spaces (or a level) and the tool lays out devices using configurable rules.
Later: scale to a whole floor.

## 2. Origin / proof of concept

Built interactively via pyrevit-mcp `execute_revit_code` against the Revit 2024 sample
`Snowdon Towers Sample Electrical.rvt` (linked Architectural / Structural / HVAC / Plumbing /
Site models). Test family: **Pendant-Dome**, type `18" D x 15" H`, Type Mark **F2**
(FamilySymbol id 1457348 in that file). Ids are project-specific: look the symbol up by
family name + Type Mark (`ALL_MODEL_TYPE_MARK`) instead. It stood in for a smoke detector.

## 3. User rules (smoke-detector style)

- Max spacing between devices: 7 m (7x7 m grid).
- Max distance from any wall: 3.5 m. Min distance from a wall: 0.3 m.
- A single central device if the space is small enough.
- Use the MINIMUM number of devices that complies.
- Devices on a clean grid/line with consistent equal spacing; middle of room when possible.
- Units: the PoC project was switched to metric (meters / square meters) via `doc.SetUnits`.
  Revit internal units are feet; 1 ft = 0.3048 m (`M = 0.3048`).
- The user is not familiar with the metric system: reports should state distances in the
  model's units and ideally both metric and imperial.

## 4. Revit API lessons (critical)

a. MEP Spaces/Rooms often live in a **linked** Architectural model. In the PoC, Spaces existed
   in the host (`OST_MEPSpaces`, 80) and Rooms only in the link (host Rooms = 0). Handle both,
   applying the link transform.
b. Face/work-plane-based hosted fixtures placed by level end up on the floor and upside down
   (known Revit quirk). **Working solution:** create a horizontal ReferencePlane at ceiling
   height `doc.Create.NewReferencePlane(XYZ(1,0,z), XYZ(0,0,z), XYZ(0,1,0), view)`, name it,
   regenerate, then `doc.Create.NewFamilyInstance(refPlane.GetReference(), XYZ(x,y,z),
   XYZ(1,0,0), symbol)`, then set `instance.IsWorkPlaneFlipped = True` so pendants hang down.
   Reuse planes by name per height. Creating on a SketchPlane reference failed
   ("Failed to create family instance on face").
c. Setting "Offset from Host" / moving elements did not change height for level-based
   placement; use the reference-plane approach.
d. The `place_family` MCP tool cannot match type names containing the inch quote `"`.
e. Ceiling heights: derive from linked ceiling elements' bounding box Min.Z within the space;
   fallback = level elevation + 8 ft. **Do NOT fall back to the space top** (gave a 31 ft bug).
   Many spaces have no ceilings.
f. Verify AFTER commit with a separate fresh query (earlier runs reported success in-transaction
   but changes were absent); also confirm visibility in the active view with
   `FilteredElementCollector(doc, view.Id)`. One transaction per run so one Undo reverts it.
g. Plan/ceiling view range affects visibility; the user's view was `L2 - Lighting`
   (CeilingPlan).

## 5. Approved layout algorithm (mezzanine dining room 200)

1. Take the space boundary polygon (`GetBoundarySegments`, tessellate curves).
2. Compute the main-body bounding box in the room's dominant wall direction (rotate if not
   axis-aligned).
3. IGNORE small alcoves/protrusions: trim rows/columns whose cross-section < ~70% of max
   (50% was too low and let an alcove drag detectors to 0.35 m from a wall).
4. `n = ceil(extent / 7 m)` per axis (fewest equal strips); one device at the centre of each
   strip.
5. If a centre is outside the polygon or < 0.3 m from a wall, nudge to the best valid point
   in that strip (maximise wall distance, penalise distance from centre) and flag it.

Result for room 200 (10.0 x 6.8 m core): 2 devices 5.0 m apart on the centreline, ~2.4 m
from walls.

Corridors / elongated rooms: a single straight centreline at equal pitch `n = ceil(length/7)`;
search the cross-axis for the line maximising minimum wall distance with all points inside.
The name-based "Corridor" check in the PoC is a hack - detect by **aspect ratio** instead.

## 6. Failures / what went wrong (do not repeat)

i. Spaces from other levels were considered because the filter used bounding-box crossing the
   cut plane; double-height L1 retail spaces (106, 108, 109, 13) rise through L2 and produced
   overlapping clumped devices. **Rule:** filter by `Space.Level == target level` only
   (optional explicit opt-in checkbox for rising-through spaces). When the user has a view
   open on a level, ASK/CONFIRM the level first and show the space count.
ii. Early greedy / "most interior point" nudging gave uneven corridor spacing (8.5 m gap > 7 m)
    and fixtures 0.09 m from walls.
iii. Over-placing: the first approach used coverage grids and put 4 devices where 2 suffice.
iv. Checks were too shallow (only counts/visibility). Required validation before committing:
    per-space min wall distance >= 0.3 m; max wall distance <= 3.5 m (regular rooms);
    nearest-neighbour spacing <= 7 m; cross-space clash check (flag devices < ~1.5 m apart from
    different spaces). **Abort/report instead of committing if violated.**
v. Space 109 had a 4.97 m coverage point vs a 4.95 m target - do not claim margins that are
   not there.
vi. The pendant family has a ~1.8 m (6 ft) stem so fixtures hang below the host; real detectors
    are flush - support per-family mounting handling.

## 7. Design advice for the real tool

**Scope:** use the current selection of spaces first; else the active view's level with explicit
confirmation of level + space count; never place in unselected spaces; default = `Space.Level`
only.

**Filters (editable):** min area (e.g. skip < 2 m2); name-keyword exclude list (Stair, Elevator,
Shaft, Chute, Riser, Plant) and optional include list; skip unplaced / zero-area / no-boundary
spaces and report them; optional skip spaces that already contain devices.

**Rules:** JSON presets per device type (smoke, heat, light): `max_spacing`,
`max_wall_distance`, `min_wall_clearance`, ceiling height mode (linked ceiling / fixed offset /
level+height), family + type picker; units follow the model. Do not claim code compliance
(NFPA 72 / AS 1670 / BS 5839 etc. vary) - the user supplies the rules.

**Safety:** mark tool-created elements (shared parameter or Comments value) so re-runs replace
only their own devices and never delete user elements. Reuse reference planes; clean up ones
created.

**UX:** dry-run preview/report table (space, count, pitch, wall distances, nudged, skipped with
reasons), one transaction, post-commit verification, logging.

**Geometry:** handle linked-model spaces, columns/holes, curved walls, L-shapes, sloped
ceilings. Keep geometry in a separate **pure-Python module with no Revit imports** so it can be
unit-tested (IronPython/CPython; `python -m unittest discover -s tests -v`) outside Revit.

**Build order:** (1) selection/level/filter + JSON presets, (2) dialog + dry-run report,
(3) shape detection incl. corridors, (4) clash checks + tagging.

## 8. PoC results (Snowdon L2, metric)

| Space | Result |
|---|---|
| 200 Mezzanine Dining | 2 devices |
| 201 Corridor | 4 in a line, 6.63 m pitch |
| 203 Studio | 2 at 5.0 m |
| 204 Two Story Studio | 1 |
| 205 Live/Work | 4 (2x2, 4.8 x 5.0 m) |
| 202 Live/Work | 4 (2x2, 4.4 x 4.6 m) |
| 120 / 122 / 128 tiny service rooms | 1 each |
| 113 Chute | skipped |
| **Total** | **20** |

## 9. How to pick up later

- [ ] Add pure-Python geometry module + unit tests in `tests/` (start with section 5).
- [ ] Selection / level / filter logic with confirmation (section 7).
- [ ] JSON rule presets and family/type picker (look up by family name + Type Mark).
- [ ] Dialog (start from `Templates.panel/Modeless Window.pushbutton`; ExternalEvent for model
      work) and dry-run report.
- [ ] Placement via reference plane + `IsWorkPlaneFlipped`, single transaction, post-commit verify.
- [ ] Validation + clash checks + tagging of tool-created elements.
- [ ] Update the README button list; test in Revit (reload pyRevit, run, check results).
- [ ] Report distances in model units and, ideally, both metric and imperial.
