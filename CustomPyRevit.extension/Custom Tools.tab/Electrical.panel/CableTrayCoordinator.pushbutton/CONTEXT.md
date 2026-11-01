# Cable Tray Coordinator - context for humans and AI

Status: **implemented, needs Revit validation**. Read the repo rules first
(`.github/copilot-instructions.md`, `docs/pyrevit-patterns.md`, `docs/pyrevit-versions.md`).
IronPython 2.7, Revit 2023-2025, modeless window + ExternalEvent.

## 1. Goal

Coordinate cable trays in the ceiling void: get a tray past an obstacle (beam, duct, pipe,
column...) with a few clicks, using predefined methods, with a preview before the model changes.

## 2. Features

- **Detour mode**: pick one straight tray + the obstacle(s) (host or linked model), or two cut
  points on the tray. The tool cuts the tray, moves the middle over/under/left/right and re-joins it
  at the original level with fittings, in one transaction (one Undo).
- **Bridge mode**: the user has cut a gap; pick the tray on each side (+ optional obstacle). The tool
  draws the connecting run (straight, or around the obstacle) and joins both ends.
- **Auto**: computes the offset needed for each direction from the obstacle geometry inside the
  tray corridor (tray size + clearance), picks the smallest, rounds it up (default 25 mm).
- **Presets**: built-in (Auto 45/90, rise over, drop under, offset left/right at 45/90, manual 300 mm
  drop, straight join) + user presets saved to `%APPDATA%/pyRevit/CableTrayCoordinator.settings.json`.
- **Parameters**: direction, bend angle (90/60/45/30), offset, clearance, round-up step, minimum
  straight between bends, "keep route if a fitting fails" (default off = all or nothing).
- **Preview**: live WPF canvas (side view for over/under, plan view for left/right) with the existing
  tray, removed part, obstacle + clearance, new route, cut points and offset label; summary of cuts,
  segments, fittings and added length; warnings (e.g. manual offset still clashes).
- "Use current Revit selection" (1 tray = detour, 2 trays = bridge, other elements = obstacles);
  the selection is also read when the tool opens.

## 3. How it avoids disturbing the rest of the run

- Only the picked segment is edited. Its start end is untouched; whatever was connected at its far
  end is disconnected *before* the tray is shortened (so Revit does not drag fittings/neighbours),
  then re-connected to the new far piece.
- New segments copy the source's type, level, size, comments, service type, workset and all
  project/shared parameter values.
- Sloped/vertical segments get `CurveNormal` set to the bent "up" vector; without this
  `NewElbowFitting` fails with "failed to insert elbow" (see jeremytammik/CableTraySample).
- Fitting failure rolls the whole change back (unless "keep route" is ticked). After commit the
  created elements are checked, because pyRevit's `Transaction` swallows commit failures.
- Apply re-reads the trays from the model and re-plans, so the preview is never applied to stale
  geometry.

## 4. Code layout

- `lib/cable_tray_logic.py` - pure Python (no Revit): local tray frame, obstacle clipping,
  required offsets, route points, segment normals, presets/params, preview primitives, action queue.
- `script.py` - window, ExternalEvent queue, picks (window hidden while picking), Revit reads and
  `build_route` (the only model-changing code).
- `ui.xaml` - layout. Tests: `tests/test_cable_tray_coordinator.py` (logic, XAML wiring, modeless
  plumbing, and `build_route` against fake Revit objects).

## 5. Limits / future work

- Straight, horizontal trays only (no sloped/vertical source trays, no curved trays).
- Bridge mode needs both trays in line, at the same level and the same size (no transitions).
- The detour must fit inside one segment; if it would run past a fitting, use Bridge mode.
- The new route is not clash-checked against *other* elements; add them as obstacles.
- Possible next steps: offset relative to the obstacle with a max ceiling height, combined
  over+sideways routes, clash check of the new route, DirectContext3D preview in the Revit view.

## 6. Revit acceptance checks (disposable project)

1. Reload pyRevit; **Custom Tools > Electrical > Cable Tray Coordinator** opens a modeless window;
   Revit stays usable.
2. Detour: pick a tray, add a beam (host or linked) that clashes; Auto 45 shows a preview; Apply.
   Check: cut positions, 4 elbows, original start connection intact, the far-end fitting/neighbour
   still connected and not moved, parameters copied, a single Ctrl+Z undoes everything.
3. Repeat with 90 bends (vertical risers) over and under, and with left/right.
4. Two cut points + "Manual drop 300 mm" preset.
5. Bridge: split a tray, delete a piece to make a gap, pick both ends, Straight and Over.
6. Cable tray type without vertical bends assigned: Apply must report the failure and change nothing.
7. Esc during each pick: status says cancelled, window comes back.
