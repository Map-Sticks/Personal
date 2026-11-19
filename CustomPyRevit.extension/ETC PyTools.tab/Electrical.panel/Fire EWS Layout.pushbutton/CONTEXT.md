# Fire & EWS Layout: where to resume

## What works, and what does not

- Revit 2025.3+ can read the actual, instance-specific grid from a linked ceiling with `Ceiling.GetCeilingGridLines(False)`. The tool already transforms that geometry into the host model, sections modeled walls near the ceiling underside, and plots both in the HTML preview.
- New placement currently accepts one empty ceiling, room, or MEP space. Ceiling mode ignores room separators, but it still uses one ceiling footprint for both the area to assess and the area where devices may be placed.
- `lib/fire_ews_revit.py` (`ceiling_zone`) collects wall candidates in the host and selected link within a 500 mm padding around the ceiling bounding box. That is only a collector filter. `lib/fire_ews_new.py` (`plan`, `_exposed_wall_samples`) still clips the assessment to the ceiling footprint, so it can exclude a real wall beyond the tile edge. Simply increasing the padding will not fix this.
- A single-ceiling placement dry run verified the detector and speaker and rolled back. It did **not** validate the mixed-finish case below. Do not treat it as acceptance of the current workflow.

## Next session: inspect the model before changing the planner

1. **Find a real mixed-finish example in Revit.** Start in the Ground RCP and the architectural Fitout G-L2 link. Identify an adjoining tiled field and plasterboard margin serving the same physical area. Record the ceiling and wall IDs, link transforms, underside elevations, gaps between footprints, and distances from ceiling edges to wall faces. Overlay those on the RCP and check what is a real partition versus a room separator. Do this read-only before choosing wall-search limits or gap tolerances.
2. **Separate the assessment area from the placement surfaces.** Let the user pick touching or adjoining ceilings together. Use the tiled field and its plasterboard margin to seed one area; the proposed device points for this mixed-finish workflow must stay on the selected tiled field. Keep plasterboard-only placement as a separate case. Do not use the room schedule or the union's bounding rectangle as the boundary.
3. **Search outward for the walls that actually bound the area.** Use a bounded search beyond the selected ceiling edges, then check wall face geometry and connectivity before using a wall as a barrier. Ignore room separator lines. Avoid jumping across a real partition or continuing into an adjacent area through an unresolved opening. When wall evidence is missing or ambiguous, show the open edge and withhold automatic placement rather than pretending the ceiling edge is a wall.
4. **Preview before enabling placement.** Distinguish the wall-bounded assessment area, tiled placement zone, plasterboard margin, and uncertain edges in the report. Assess reach over the entire accepted area, including the margin. Keep the empty-area rule and explicit Place confirmation; do not move existing devices. Verify hosting with a dry run before committing anything.

## Acceptance cases

- **Mixed finish:** selecting the tiled field and plasterboard margin together finds the physical wall beyond the tile edge, includes the margin in the reach check, and proposes targets only on full tiles.
- **Not all lines are barriers:** a room separator across the selected area does not split it; a modeled partition at ceiling height blocks a direct reach path.
- **Search failures are visible:** a missing, ambiguous, or out-of-range wall is explained in the preview, not silently replaced by a ceiling edge.
- **Regression:** a single tiled or flush ceiling still previews appropriately; no devices are created until the user confirms an eligible preview.
