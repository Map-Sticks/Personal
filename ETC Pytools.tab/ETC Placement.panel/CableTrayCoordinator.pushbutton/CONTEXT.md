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
  draws the connecting run (straight, or around the obstacle) and joins both ends. Parallel or
  perpendicular horizontal source trays may be at different elevations. Non-aligned bridges use
  a bounded, rectilinear visibility search with 90-degree bends, outward/inward end tangents and
  conservative obstacle envelopes. Level changes may use 30/45/60-degree slopes and sideways shifts
  up to 1500 mm (or more if two 90-degree elbows cannot fit) may use 30/45/60-degree level steps,
  e.g. a 45-degree offset round a wall corner; slopes, steps and pulled-back ends combine in one route.
  Over/Under constrain vertical search; Left/Right constrain
  the side of the route. Auto searches all directions. Manual offset extends the spatial search
  margin rather than setting a fixed rise/drop. Straight join still requires aligned, level ends.
- **Auto**: computes the offset needed for each direction from the obstacle geometry inside the
  tray corridor (tray size + clearance), picks the smallest, rounds it up (default 25 mm).
- **Presets**: built-in (Auto 45/90, rise over, drop under, offset left/right at 45/90, manual 300 mm
  drop, straight join) + user presets saved to `%APPDATA%/pyRevit/CableTrayCoordinator.settings.json`.
- **Parameters**: direction, bend angle (90/60/45/30/22.5/15), offset, clearance, offset step (calculated
  offsets are rounded *up* to a multiple of it for tidy setting-out; not a tolerance; 0 = off),
  minimum straight between bends, "keep route if a fitting fails" (default off = all or nothing).
  All parameters are remembered in settings (saved on Apply and on close) and in user presets;
  built-in presets set direction/angle/offset/clearance but do not reset minimum straight. Short
  bend legs are lengthened by increasing the offset; short middle spans
  are widened. Auto tries another direction if the smallest offset cannot fit. Impossible routes
  are rejected with suggestions before Apply. Min. straight is the *finished* straight length
  left after the fittings are placed. On first Apply for a tray type/size the tool measures the
  real fitting take-up (side/up/down elbows of each angle, test-placed next to the tray in a
  transaction that is always rolled back, cached per session by type id + size) and plans every
  straight as Min. straight + the take-up of the fittings at its ends (detours: 2 x take-up at the
  plan's angle; spatial bridges: per joint, including the measured union take-up where the route
  meets a tray end-on). Unmeasured angles fall back to
  Min. straight / 2 per fitting. A 10 mm floor and Revit's ShortCurveTolerance also apply. If a
  tray still ends up short, the transaction rolls back, the take-up actually seen is learned,
  the preview is refreshed and the message says how much each fitting takes; Min. straight is
  not changed. An existing bridge source tray too short for its fitting is named instead.
- **Smaller elbows when needed**: while measuring take-up the tool checks whether the tray's
  elbows have writable instance parameters "Bend Radius" and "Tray Length" (Revit's channel/ladder
  bends do) and that resizing them changes the take-up as predicted (radius change x tan(angle/2)
  + leg change). If the plan with the standard elbows fails, loops ("fittings need more room"),
  pulls a tray end back, needs a bigger offset or merges detours, it is re-planned with smaller
  elbows: first 25 mm legs (if "Allow shorter elbow legs"), then bend radii 600/450/300/150 mm and
  the user's value, never below **Min. bend radius** (0 = never reduce). The first option without
  those problems wins (otherwise the one with the fewest). The summary says "Smaller elbows so the
  route fits: ..." and a warning asks to check the cables' minimum bending radius. On Apply each
  elbow is resized right after it is placed (Revit inserts standard elbows; two of them may not
  fit on a straight planned for smaller ones). Tees and unions are not resized. Both settings are
  remembered and saved in user presets. Plans are ranked by (problems, route length), so a
  smaller clearance never gives a bigger gap. After each resize the tool regenerates and moves any
  connected straight end left short of the elbow connector onto it (Revit extends only one
  straight when a leg/radius changes, leaving a connected-but-open gap).
- **Clearance control**: over/under detours use the measured *vertical* elbow take-up (smaller
  than the horizontal one), and 15/22.5-degree bends are available. If the offset still has to
  grow beyond clearance and "Allow shallower bends to keep the clearance" is on (default), the
  shallower measured angles are tried (standard then smaller elbows); the summary says "Bend angle
  X instead of Y ...". Detour summaries show "Gap to obstacle: G (Clearance set C)"; the gap is
  clearance + Offset step rounding (set Offset step to 0/5 for a near-exact gap). If the offset
  is still increased the warning gives the resulting gap and how to get closer.
- **Individual obstacles (detour)**: optional separate detours for each selected obstacle, with
  unaffected straight sections between them. Nearby detours merge if the connecting straight
  would be too short. Automatic cut points only; picked cut points retain their single-detour
  meaning. Each detour is checked (in the tray's own frame, so rotated grids work) against the
  other selected obstacles and rejected if one blocks it; it then uses the same offset as a
  grouped detour over that obstacle.
- **Sideways shift / run re-route (detour)**: when the clash reaches a *free* tray end, that end
  is simply moved (no return bend), e.g. sideways past a wall, or the whole tray if both ends are
  free. When it reaches a *connected* end, the tool walks the connected run (up to 7 straights,
  stopping at free ends, tees/crosses, curved trays or loops), cuts the first clear straight
  beyond the obstacle, removes the elements in between and re-joins with an obstacle-checked
  spatial bridge. The preview names the removed elements and the cut tray.
- **Tee into the side (bridge)**: when picking the two trays, where you click on the second tray
  decides the join: near one of its ends = end-to-end join; along its side = tee into it at that
  point. Using the current selection (no clicks), a free end pointing at the side of the other
  tray is detected automatically and the tee position slides along the main tray to the best
  route. The main tray is split, the tee placed with `NewTeeFitting`; tee take-up is measured
  like elbows. The status line says which join was detected.
- **Revert last route**: restores original tray geometry and connections, removes only the tool's
  last created elements, and restores the original tray/obstacle/cut-point selection for another
  attempt. No transaction remains open between actions. Any other model transaction (including
  native Undo of the route) disables Revert for safety; the tool's own transactions are recognised
  by name. Closing/reloading loses the in-memory revert record; use Revit Undo for older routes.
  Applying a different route replaces the last-route record. A connected-run re-route deletes
  existing elements, so its Revert posts Revit's own Undo instead and then restores the selections.
- **Regenerate options** (Route options box): re-reads the picked trays/obstacles from the model
  (a refresh), plans every direction x bend angle (x grouped/individual obstacles) with the user's
  own Clearance, Min. straight, Min. bend radius and elbow options, drops failed/clashing and
  duplicate routes, ranks the rest by a cost in mm of extra tray (extra length, 400 per fitting,
  gap beyond clearance, tray moved off its line, pulled-back ends, smaller elbows, odd angles,
  re-routed runs), then test-builds the best different approaches in a transaction that is always
  rolled back (same short-straight and solid clash checks as Apply; take-up is not learned) and
  lists the top 5 ("[Best]" first). Picking one loads its settings, so the preview and Apply build
  exactly that route. After Apply the list is disabled; Revert re-enables it to try another. Only
  if nothing fits your own limits are "RELAXED" options offered (25 mm elbow legs, bend radius
  down to 150 mm, half the Min. straight, never less clearance). The list header and label say
  what was relaxed; selecting one loads those values into Parameters and the status names each
  overridden limit with your own value; Apply asks Yes/No first; settings are saved with your own
  limits, not the relaxed ones (unless you edit those boxes afterwards). The search
  stops after 25 s. Changing the picks clears the list.
- **Preview**: live WPF canvas (side view for over/under, plan view for left/right) with the existing
  tray, removed part, obstacle + clearance, new route, cut points and offset label; summary of cuts,
  segments, fittings and added length; warnings (e.g. manual offset still clashes).
  Spatial bridges and mixed-direction individual routes have a plan/elevation toggle. The
  per-group summary records each individual detour's direction, offset and cuts.
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
  geometry. Selected host/linked obstacles are also refreshed before applying. All current
  selection obstacles are retained (not silently capped at 20); oversized geometry falls back
  to a complete bounding-box envelope with a logged warning.
- Before commit, the created trays/fittings are intersected (Revit solids,
  `BooleanOperationsUtils`) with every selected host/linked obstacle. A real clash rolls back the
  whole route and names the obstacle (unless the user already accepted a "will still clash" manual
  offset). Clearance itself is enforced by the planner, not by this check. Solid pairs Revit cannot
  intersect are reported as a warning to check visually. In-line bridges keep their 45/60/30-degree
  routes; they only fall back to the 90-degree spatial search if the planned route would clash.
- Revert disconnects trial elements before deleting them and refuses if the deletion would remove
  the source trays, their original neighbours or any other tray/fitting outside the route (internal
  elements Revit drops with the route are tolerated). It restores both source curves (including bridge
  fitting take-up) and reconnects their original neighbours. A DocumentChanged subscription guards
  it and is removed when the window closes.

## 4. Code layout

- `lib/cable_tray_logic.py` - pure Python (no Revit): local tray frame, obstacle clipping,
  required offsets, route points, segment normals, presets/params, preview primitives, action queue,
  and Regenerate's variants/scoring/selection (`option_variants`, `evaluate_options`, `choose_options`).
- `script.py` - window, ExternalEvent queue, picks (window hidden while picking), Revit reads and
  `build_route` (the only model-changing code); `checked_build` is shared by Apply and the
  rolled-back Regenerate test builds (`trial_build`).
- `ui.xaml` - layout. Tests: `tests/test_cable_tray_coordinator.py` (logic, XAML wiring, modeless
  plumbing, and `build_route` against fake Revit objects).

## 5. Limits / future work

- Straight, horizontal trays only (no sloped/vertical source trays, no curved trays).
- Source trays must be horizontal; bridges support parallel/perpendicular axes and equal sizes
  only (no oblique source axes or size transitions). Spatial bridges use 90-degree turns,
  30/45/60-degree vertical slopes and 30/45/60-degree sideways steps (a slope or step follows the
  start or a horizontal run and continues horizontally in the same direction) regardless of the
  selected bend angle; the fitting
  families must support them. When the fittings do not fit between the picked ends (e.g. a small
  drop next to a beam), the planner pulls the free tray end(s) back along the tray in steps of
  100-1500 mm (never more than half a tray, always leaving one minimum straight) and keeps the
  route with the least new + removed tray; the summary shows "Tray ends pulled back..." and
  Revert restores the original ends. Only free ends are moved. When large measured fittings
  still leave only a long loop, the preview
  warns ("fittings need more room"); move a tray end or obstacle selection if it is unacceptable.
- Spatial search has coordinate/state/edge limits and can reject complex cases with a clear
  message. It does not guarantee a globally shortest route or solve every feasible obstacle layout.
- The detour must fit inside one segment unless it reaches a tray end (free end shift, or
  connected-run re-route limited to 7 straights and stopped by tees/crosses/curved trays).
- Tees require a tee family in the tray's routing preferences; a branch narrower than the main
  depends on the family supporting it. Tee joins are horizontal-plane only (both trays horizontal).
- The new route is only clash-checked against the *selected* obstacles; add others as obstacles.
- Take-up is measured with the tray's own routing preferences; unusual families (e.g. vertical
  bends not assigned) simply leave those angles unmeasured. Spatial-bridge obstacle envelopes are
  axis-aligned in the first tray's frame, so diagonal obstacles are treated conservatively.
- Elbow resizing is detected on the horizontal 90-degree probe and assumed to work the same for
  vertical bends and other angles; families with other parameter names (or type-level radius)
  are simply not resized. The tool does not know the cables' real minimum bending radius - set
  Min. bend radius to suit them.
- Possible next steps: offset relative to the obstacle with a max ceiling height, combined
  over+sideways routes, DirectContext3D preview in the Revit view.

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
8. Bridge two parallel trays at different levels, then two perpendicular trays, with a selected
   host or linked beam in between. Auto should produce an obstacle-checked 90-degree path.
   Switch between plan/elevation previews. Apply and inspect all connector orientations and
   physical clearances. Over/Under and Left/Right should constrain the chosen route as described above.
9. Set Min. straight to the family's required allowance; save a user preset, switch built-in
   presets, close/reopen, and reload the user preset. The saved minimum should persist. Try a
   small offset: preview should increase it or explain why no route fits. If fittings shorten
   pieces too much, Apply must roll back rather than leave short pieces to repair manually.
10. Apply, then Revert last route. Original geometry, parameters, neighbouring connections and
    selected obstacles/cut points should remain; change a preset and Apply again without repicking.
    Repeat in both modes, including connected far ends and fitting take-up. Make an unrelated
    model edit: Revert must disable. Use native Undo separately; confirm no previous work is removed.
11. Pick one long tray and several header beams (also on a rotated grid). Tick Avoid obstacles
    individually: spaced beams should get separate detours, nearby beams should merge with a warning.
    Try mixed Auto directions and both previews. Apply must be one Undo item and preserve both ends.
12. Set clearance to 0 next to a beam: if a real elbow intersects the beam solid, Apply must roll
    back and name the beam. With the default clearance the same route should apply.
13. Bridge two in-line trays with a small level difference (about one tray depth) and a beam just
    past the upper tray's end (Auto or Under). Expect a short 30-degree drop under the beam, the
    summary line "Tray ends pulled back to make room for the fittings: first tray 100 mm", no loop.
    Apply: everything connected; Revert restores the original tray lengths.
14. Detour a tray whose free end runs alongside a wall (wall selected as obstacle). Auto should
    offset sideways with only one bend pair; the free end moves, no return bend. Apply, Revert.
15. Detour a tray whose *connected* end is crossed by another tray/service (selected). Expect
    "Connected run re-routed: N element(s) removed"; Apply, then Revert: Revit Undo runs and the
    window keeps the tray/obstacle selection.
16. Bridge a tray end to the side of a perpendicular tray: Pick trays and click along the side of
    the second tray (status "Tee into the side of ..."), Apply: main tray split, tee placed, all
    connected. Repeat clicking near its end (end-to-end join), and with Use current selection
    (auto-detect). Revert must restore the main tray as one piece.
17. Bridge two parallel trays about 600 mm apart sideways across a ~1 m gap with a wall corner
    between them (wall selected). Auto: "45 deg sideways steps", no loop, Apply clash-free. Lower
    the second tray ~400 mm: expect a slope plus a sideways step in the same route.
18. Bridge two in-line trays 700 mm apart with a 300 mm drop (or 500 mm sideways). Min. bend
    radius 150: summary "Smaller elbows so the route fits: bend radius 150 mm ...", no pulled-back
    ends; after Apply the elbows' Bend Radius is 150 mm and Tray Length 25 mm, all ends connected.
    Min. bend radius 0 and legs unticked: standard 305 mm elbows (the route may pull an end back).
19. Detour Over a crossing tray, Clearance 50, Offset step 25: summary "Gap to obstacle: ~73 mm
    (Clearance set 50 mm)", possibly "Bend angle 15/22.5 instead of 45"; Clearance 25 gives a
    smaller gap, never a bigger one. Untick "Allow shallower bends": gap ~123 mm. All elbow
    connectors touch their straights (no open gaps).
20. Detour over a crossing tray (or bridge two trays at different levels with a beam between):
    click Regenerate options. Within a few seconds up to 5 options appear, "[Best]" first and
    selected; the model is unchanged (no extra elements, nothing in Undo). Click each option: the
    preview and settings change. Apply option 1, then Revert last route and Apply option 2. Change
    the obstacle pick: the list clears. Bridge two in-line 500 mm trays 400 mm apart with a
    400 mm drop, Min. straight 500, Min. bend radius 0, elbow legs unticked: nothing fits, so the
    option is marked RELAXED; selecting it shows "Option 1 is RELAXED - it goes below your limits:
    ... Min. straight 250 mm (yours: 500 mm) ..." and Apply asks Yes/No (No builds nothing). After
    closing and reopening, Min. straight is still 500 mm.
