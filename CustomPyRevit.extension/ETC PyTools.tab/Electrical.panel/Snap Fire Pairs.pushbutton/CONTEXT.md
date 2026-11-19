# Snap Fire Pairs - context for humans and AI

Status: **v0.1, the first working version.** It snaps existing devices in **one room or
corridor** into the ceiling tiles. It does not yet re-space, check walls or show coverage
(see section 6). Read the repo rules first (`.github/copilot-instructions.md`,
`docs/pyrevit-patterns.md`). IronPython 2.7, Revit 2023-2025.

## 1. Why this exists

The work was trialled in chat through pyRevit MCP on NMH (New Mandurah Hospital) Ground TRL,
Nov 2026. Each pass needed many screenshots and scripts, so chat was slow and expensive for
repeat work. The geometry is simple. **The hard part is knowing where the ceiling grid is.**
The Revit API exposes the tile size and direction (fill pattern). It does not reliably expose
the origin the architect aligned the grid to by hand: on NMH it was 204 mm (X) and 334 mm (Y)
off. Sprinklers and electrical light fittings were not on the grid either.

**Design decision:** the user tells the tool where the grid is with two clicks (opposite
corners of one tile). The rest is deterministic, previewed and confirmed. The tool decides
nothing that cannot be seen, and it flags what it is unsure of.

## 2. House rules (designer practice, AS 1670.1 / 1670.4 basis)

- Move existing devices in X/Y only. Never add or delete; never change height or offset.
- Only surface smoke detectors and EWIS speakers. Ignore concealed detectors, detectors with
  remote indicators, heat, aspirating (ASD), beam and flame detectors.
- **Standard pair:** detector + speaker share ONE 1200 x 600 tile, each device at the centre
  of its 600 x 600 half, so they are 600 apart along the long side. User reference pair:
  NMH detector 7044705 / speaker 7044703. Never on, or straddling, a grid line or T-bar.
- Small rooms can have their own grid and **rotated** tiles (600 E-W x 1200 N-S), so the pair
  then runs north-south. Read the tile direction in every room.
- Spacing: about 10 m between detectors, >= 300 mm and <= 5 m from walls. Do not force exact
  corridor centring; keep corridor runs roughly in line.
- A room about 11 m long needs 2 detectors, placed towards the ends.
- Physical walls are the boundaries, not room separators (open bays count as one space).
- Inpatient bedroom: detector, no speaker. Ensuite: speaker only, no detector (sprinklered).
- Hold-open smoke/fire doors: detector within 1.5 m. Flag "near door - confirm"; do not hunt
  for doors.
- Flush-set (plasterboard) ceilings have no grid: centre on the room or keep the layout.

## 3. What v0.1 does (`script.py`, `lib/tile_snap_logic.py`)

1. The active view must be a plan or RCP. The user selects the detectors and speakers in one
   ceiling area. Nested sub-components are skipped. `classify()` filters by name: exclusions
   test the **type name only**, because NMH's family "HEAT AND SMOKE DETECTORS" holds wanted
   and unwanted types.
2. The user clicks two opposite corners of one tile (snaps: intersections, endpoints).
   `grid_from_corners()` rounds the size to 300 mm and keeps the clicked corner as the origin.
3. `pair_devices()`: greedy nearest detector-to-speaker pairs within 1500 mm.
4. `plan_moves()`:
   - Pair on a long tile (one side >= twice the other): tile centre +/- a quarter of the long
     side, along the long side.
   - Pair on a square tile: the two tiles either side of the nearest joint.
   - Side order is kept (whichever device is west or south stays there).
   - Single device: tile centre, flagged "no partner".
   - Flags: big move (> 1500 mm), clashing targets (< 300 mm apart), already in place.
5. Preview table in the pyRevit output window, then Yes/No. One transaction:
   `ElementTransformUtils.MoveElement` with dz = 0. Pinned elements are skipped and reported.

## 4. How to test in Revit

1. Reload pyRevit. The **Snap Fire Pairs** button is in ETC PyTools > Electrical.
2. Open the RCP. Select the pairs in one room. Click the button, then click two opposite
   tile corners (zoom in first).
3. Expected: the preview lists each device and its move. After Yes, every pair sits inside
   one tile, the same as reference pair 7044705 / 7044703. One Undo reverts everything.
4. Only Revit can confirm:
   - that `PickPoint` works in the RCP (it needs a work plane; the error message tells the
     user how to set one);
   - that snapping reaches linked ceiling grid lines;
   - that the speaker symbol orientation looks right after a pair is turned.

## 5. Lessons learned (do not repeat)

- Do not take the grid origin from `GetFillGrids()` + link transform, sprinklers or electrical
  lights. Only the size and direction are reliable.
- pyRevit MCP: `run_query` / `run_modify` install a `sys.settrace` hook, which is about 100x
  slower. Call `sys.settrace(None)` in scripts.
- MCP `capture_view mode='screen'` can lag one step behind `show_elements`; wait about 4 s.
- Red dashed symbols overlapping a pair are usually concealed detectors in the ceiling void
  (higher Z). They are a plan-symbol overlap only.
- Read type names with `revit.query.get_name` (direct `.Name` can fail in IronPython).

## 6. Roadmap - adding "smarts" without guesswork

Each step stays deterministic and testable, and each one tells the user when it is unsure.

- [ ] **Wall check:** collect walls from the host and links (apply the link transform). Flag
      targets < 300 mm or > 5 m from a wall. If the pair's tile fails, try the neighbouring
      tiles and pick the closest valid one.
- [ ] **Re-space a corridor or room:** use the room's main axis, `n = ceil(length / 10 m)`,
      even pitch, then snap each target into a tile. Keep the device count; report a
      shortfall or surplus.
- [ ] **Ceiling awareness:** find the linked ceiling under each device. Compare the clicked
      tile with the ceiling's fill-pattern size and direction, and warn if they differ.
      Remember the clicked origin per ceiling id (JSON next to the model) so each ceiling is
      clicked only once. Skip flush ceilings.
- [ ] **Coverage heat map:** rasterise walls on a 100 mm grid, flood-fill spaces, then show the
      distance to the nearest detector (as in the chat prototype, which used CPython + numpy).
      In IronPython, either draw filled regions or detail lines, or export a PNG through
      `System.Drawing`.
- [ ] **Room-type rules** (bedroom / ensuite / toilet) from room names, as a configurable
      keyword list, reported only.
- [ ] Keep using chat + MCP for review and the exceptions; the button handles the repeat work.

Chat prototype scripts (CPython optimiser, raster analysis) are in the user's Copilot skill
`revit-model-query/examples/trl-ground-pilot/`. The full house rules are in
`revit-model-query/references/designer-practice.md`.
