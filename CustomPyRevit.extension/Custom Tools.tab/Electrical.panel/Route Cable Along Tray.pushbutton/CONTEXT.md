# Route Cable Along Tray - context for humans and AI

Status: **scaffold only**. `script.py` just shows an alert. Read the repo rules first
(`.github/copilot-instructions.md`, `docs/pyrevit-patterns.md`, `docs/pyrevit-versions.md`,
`docs/revit-api-references.md`). IronPython 2.7, Revit 2023-2025. Sibling scaffold:
`Place Ceiling Devices.pushbutton` (see its CONTEXT.md for shared lessons: verify after commit,
one transaction per run, linked models, units).

## 1. Goal

A pushbutton that generates a cable/circuit path using **cable trays as the primary route**.
Typical case: the user creates an electrical circuit from a sub-board to a main board (the
sub-board is the load of a circuit on the main board). The tool then computes a path that
follows the tray network, writes it to the circuit, and gives the user a flexible layer to
shape the route and see cable lengths. Deterministic geometry, no AI at runtime.

## 2. Requirements (from the user)

- Primary route follows cable trays; off-tray legs only where needed (board to tray, tray to board).
- User circuits sub-board -> main board first; the tool works on that circuit (selection).
- When the user edits the path, the tool helps determine the path and cable length:
  - show alternative routes (shortest, fewest fittings, avoid tray X);
  - add via points / force or exclude specific trays;
  - vertical drops/rises from the board to the tray;
  - allowances: slack %, termination length at each end, bend/fitting allowance;
  - length breakdown (tray run, drops, allowances, total) with a dry-run preview;
  - report in the model's units and ideally both metric and imperial (the user is not
    familiar with metric).

## 3. API facts (verified with tools/revit_api_lookup.py, Revit 2024 RevitAPI.xml)

On `Autodesk.Revit.DB.Electrical.ElectricalSystem`:
- `GetCircuitPath()` -> list of XYZ nodes.
- `SetCircuitPath(IList<XYZ>)`; `IsCircuitPathValid(IList<XYZ>)` to pre-check.
- `CircuitPathMode` (type `ElectricalCircuitPathMode`; Custom can only be set explicitly if
  `HasCustomCircuitPath`), `PathOffset` / `HasPathOffset` (offset of horizontal segments).
- `Length` (read-only value computed by Revit).
Still to verify before coding: enum member names, whether `Length` reflects a custom path
including vertical segments, behaviour of `SetCircuitPath` when the first/last node is not at
the equipment, availability in 2023 and 2025, and cable tray members (`CableTray`,
`CableTrayConduitBase`, connectors, `LocationCurve`, `ConnectorManager`).
Revit does NOT route circuits along cable trays itself; the tool must compute the route.

## 4. Proposed approach

1. Collect trays and tray fittings (host and linked, apply link transform); build a graph:
   nodes = connector end points (merge within tolerance), edges = tray segments / fittings
   weighted by length.
2. Find the board connection points (panel location/origin or connectors) and add drop edges to
   the nearest tray node(s); candidates, not just one.
3. Dijkstra/A* for shortest path; k-shortest or penalty re-runs for alternatives; support
   via-points and excluded trays.
4. Convert to a node list (add offset for tray side/height), validate with
   `IsCircuitPathValid`, preview, then `SetCircuitPath` in one transaction.
5. Verify after commit with a fresh `GetCircuitPath()` / `Length` read.

## 5. Known risks / open questions

- Gaps or unconnected trays: need a gap-bridging tolerance and a warning, not silent jumps.
- Trays in linked models; different levels; sloped/vertical trays and fittings.
- `Length` may not include allowances/drops: compute our own length and show both.
- Existing user-customised paths: never overwrite without confirmation; offer undo (single
  transaction).
- Multiple circuits at once (later); cable size/fill checks are out of scope initially.

## 6. Design advice

- Modeless window from `Templates.panel/Modeless Window.pushbutton`; ExternalEvent for model
  work; never call the Revit API from click handlers.
- Keep graph/path/length logic in a pure-Python module with no Revit imports and unit-test it
  in `tests/` (`python -m unittest discover -s tests -v`).
- Build order: (1) pure graph + Dijkstra + length maths with tests, (2) tray collection into
  the graph, (3) read-only preview/report of the route and lengths, (4) write path with
  validation + post-commit verify, (5) alternatives / via points / allowances UI.

## 7. How to pick up later

- [ ] Verify the open API items in section 3 with the lookup tool.
- [ ] Write the pure-Python graph module + tests.
- [ ] Tray collection (incl. links) and board connection points.
- [ ] Read-only report first, then write support.
- [ ] Update README; give Revit test steps (reload pyRevit, select circuit, run, check).
