"""Cable Tray Coordinator logic, XAML wiring and route-building tests; no Revit/WPF needed."""

import importlib.util
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
import xml.etree.ElementTree as ET


BUNDLE = (Path(__file__).resolve().parents[1] / "CustomPyRevit.extension"
          / "ETC PyTools.tab" / "Electrical.panel" / "CableTrayCoordinator.pushbutton")

spec = importlib.util.spec_from_file_location("cable_tray_logic", BUNDLE / "lib" / "cable_tray_logic.py")
logic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logic)

ft = logic.mm_to_ft


def mm(value):
    return round(logic.ft_to_mm(value), 3)


def box(x0, x1, y0, y1, z0, z1):
    """World edge segments of an axis-aligned box given in mm."""
    corners = [(ft(x), ft(y), ft(z)) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    return [(corners[i], corners[j]) for i in range(8) for j in range(i + 1, 8) if bin(i ^ j).count("1") == 1]


def box_faces(x0, x1, y0, y1, z0, z1):
    """World face quads of an axis-aligned box given in mm (like the script's bbox fallback)."""
    c = [(ft(x), ft(y), ft(z)) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    faces = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
    return [tuple(c[i] for i in f) for f in faces]


def tray(x0=0, x1=10000, y=0, z=3000, width=300, height=100, z1=None, ends_connected=(False, False), id_value=1):
    start, end = (ft(x0), ft(y), ft(z)), (ft(x1), ft(y), ft(z if z1 is None else z1))
    return {"id_value": id_value, "start": start, "end": end, "width": ft(width), "height": ft(height),
            "ends": [{"point": start, "connected": ends_connected[0]}, {"point": end, "connected": ends_connected[1]}]}


def params(**kw):
    base = {"direction": "Auto", "angle": 45, "clearance_mm": 50, "auto_offset": True, "round_mm": 25,
            "min_straight_mm": 100, "offset_mm": 300}
    base.update(kw)
    return logic.params_in_feet(base)


# A 200 mm beam crossing the tray at x=4900..5100, 3000 wide, bottom at 2950 (clashes with the 3000 tray).
BEAM = box(4900, 5100, -1500, 1500, 2950, 3200)


class GeometryTests(unittest.TestCase):
    def test_frame_round_trip_and_left_axis(self):
        frame, run = logic.horizontal_frame((1.0, 2.0, 3.0), (1.0, 12.0, 3.0))
        self.assertAlmostEqual(10.0, run)
        self.assertEqual((-1.0, 0.0, 0.0), frame.v)  # left of +Y travel is -X
        p = (4.0, 5.0, 6.0)
        back = frame.to_world(frame.to_local(p))
        for a, b in zip(p, back):
            self.assertAlmostEqual(a, b)

    def test_sloped_or_vertical_trays_are_rejected(self):
        with self.assertRaisesRegex(logic.PlanError, "sloped"):
            logic.horizontal_frame((0, 0, 0), (ft(5000), 0, ft(50)))
        with self.assertRaisesRegex(logic.PlanError, "vertical"):
            logic.horizontal_frame((0, 0, 0), (0, 0, ft(500)))

    def test_clip_to_slab(self):
        clipped = logic.clip_to_slab((0, -2, 0), (0, 2, 0), 1, -1, 1)
        self.assertEqual(((0, -1, 0), (0, 1, 0)), clipped)
        self.assertIsNone(logic.clip_to_slab((0, 2, 0), (0, 3, 0), 1, -1, 1))
        self.assertEqual(((0, 0.5, 0), (1, 0.5, 0)), logic.clip_to_slab((0, 0.5, 0), (1, 0.5, 0), 1, -1, 1))

    def test_clip_polygon_to_slab(self):
        square = ((0, -2, -2), (0, 2, -2), (0, 2, 2), (0, -2, 2))
        clipped = logic.clip_primitive(square, 1, -1, 1)
        self.assertEqual([-1, 1], sorted(set(round(p[1], 6) for p in clipped)))
        self.assertIsNone(logic.clip_primitive(square, 1, 3, 4))

    def test_tray_through_big_wall_clashes_even_with_no_edge_in_corridor(self):
        # 200 mm wall, 10 m long and 6 m high: none of its edges come near the tray at z=3000.
        frame, _ = logic.horizontal_frame(tray()["start"], tray()["end"])
        half_w, half_h, c = ft(150), ft(50), ft(50)
        edges = logic._local_obstacles(frame, [box(4900, 5100, -5000, 5000, 0, 6000)])
        self.assertIsNone(logic.obstacle_extents(edges, half_w, half_h, c)["clash"])
        faces = logic._local_obstacles(frame, [box_faces(4900, 5100, -5000, 5000, 0, 6000)])
        ext = logic.obstacle_extents(faces, half_w, half_h, c)
        self.assertEqual([4900, 5100], [round(logic.ft_to_mm(x)) for x in ext["clash"]["s"]])

    def test_required_offsets_for_crossing_beam(self):
        frame, _ = logic.horizontal_frame((0, 0, ft(3000)), (ft(10000), 0, ft(3000)))
        local = [(frame.to_local(a), frame.to_local(b)) for a, b in BEAM]
        ext = logic.obstacle_extents(local, ft(150), ft(50), ft(50))
        self.assertTrue(ext["clash"])
        offsets = logic.required_offsets(ext, ft(150), ft(50), ft(50))
        self.assertAlmostEqual(300, mm(offsets["Over"]))    # beam top 200 above + 50 + half height 50
        self.assertAlmostEqual(150, mm(offsets["Under"]))   # tray top 50 above beam bottom -50, +50 clearance
        self.assertAlmostEqual(1700, mm(offsets["Left"]))
        self.assertEqual("Under", logic.choose_direction(offsets))

    def test_round_up(self):
        self.assertAlmostEqual(150, logic.round_up(126, 25))
        self.assertAlmostEqual(150, logic.round_up(150, 25))
        self.assertAlmostEqual(126, logic.round_up(126, 0))

    def test_measured_takeup_replaces_min_straight_estimate_in_planning(self):
        self.assertEqual((ft(600), True), logic.fitting_allowance({"takeup": {90: ft(300)}}, 90))
        self.assertEqual((ft(250), False), logic.fitting_allowance({"takeup": {45: ft(1)},
                                                                    "fitting_allowance": ft(250)}, 90))
        p = logic.params_in_feet(dict(logic.DEFAULT_PARAMS, min_straight_mm=100))
        p["takeup"] = {90: ft(430)}
        planning = logic._planning_params(p, 90)
        self.assertAlmostEqual(ft(960), planning["min_straight"])
        self.assertAlmostEqual(ft(100), planning["user_min_straight"])
        self.assertTrue(planning["takeup_measured"])

    def test_clearance_boundary_contact_is_independent_of_segment_length(self):
        bounds = [((0, 1), (0, 1), (0, 1))]
        for run in (0.1, 10, 100):
            self.assertTrue(logic.route_is_clear([(-run, 0.5, 0.5), (0, 0.5, 0.5)], bounds))
            self.assertFalse(logic.route_is_clear([(-run, 0.5, 0.5), (0.5, 0.5, 0.5)], bounds))
        self.assertTrue(logic.route_is_clear([(-10, 0, 0.5), (10, 0, 0.5)], bounds))

    def test_segment_normals_follow_the_bend(self):
        self.assertIsNone(logic.segment_normal((1.0, 0.0, 0.0)))
        self.assertIsNone(logic.segment_normal((0.0, 1.0, 0.0)))
        self.assertEqual((-1.0, 0.0, 0.0), logic.segment_normal((0.0, 0.0, 1.0)))  # riser faces back
        self.assertEqual((1.0, 0.0, 0.0), logic.segment_normal((0.0, 0.0, -1.0)))
        n = logic.segment_normal(logic.normalize((1.0, 0.0, 1.0)))
        self.assertAlmostEqual(0.0, logic.dot(n, logic.normalize((1.0, 0.0, 1.0))))
        self.assertGreater(n[2], 0)


class DetourPlanTests(unittest.TestCase):
    def test_auto_detour_drops_under_beam_with_45_degree_bends(self):
        plan = logic.plan_detour(tray(), [BEAM], [], params())
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual("Under", plan["direction"])
        self.assertAlmostEqual(150, mm(plan["offset"]))
        self.assertEqual((4700, 5300), tuple(round(mm(c)) for c in plan["cut"]))
        zs = [round(mm(s["start"][2])) for s in plan["segments"]] + [round(mm(plan["segments"][-1]["end"][2]))]
        self.assertEqual([3000, 2850, 2850, 3000], zs)
        self.assertEqual(3, len(plan["segments"]))
        self.assertIsNotNone(plan["segments"][0]["normal"])
        self.assertIsNone(plan["segments"][1]["normal"])
        self.assertIn("Fittings: 4", "\n".join(plan["summary"]))

    def test_over_90_degree_has_vertical_risers(self):
        plan = logic.plan_detour(tray(), [BEAM], [], params(direction="Over", angle=90))
        self.assertTrue(plan["ok"], plan["error"])
        first = plan["segments"][0]
        self.assertAlmostEqual(first["start"][0], first["end"][0])
        self.assertAlmostEqual(300, mm(first["end"][2] - first["start"][2]))
        self.assertEqual((-1.0, 0.0, 0.0), tuple(round(c, 6) + 0.0 for c in first["normal"]))

    def test_manual_offset_too_small_warns(self):
        plan = logic.plan_detour(tray(), [BEAM], [], params(direction="Over", auto_offset=False, offset_mm=200))
        self.assertTrue(plan["ok"])
        self.assertIn("still clash", plan["warnings"][0])

    def test_left_right_detours_stay_level(self):
        post = box(4950, 5050, -100, 100, 0, 6000)  # a column through the tray
        plan = logic.plan_detour(tray(), [post], [], params(direction="Left"))
        self.assertTrue(plan["ok"], plan["error"])
        self.assertTrue(all(abs(mm(s["start"][2]) - 3000) < 1e-6 for s in plan["segments"]))
        self.assertAlmostEqual(300, mm(plan["offset"]))  # 100 + 50 + 150, rounded to 25
        self.assertIsNone(plan["segments"][0]["normal"])
        auto = logic.plan_detour(tray(), [post], [], params())
        self.assertIn(auto["direction"], ("Left", "Right"))

    def test_cut_points_with_manual_drop(self):
        cuts = [(ft(6000), ft(10), ft(3050)), (ft(3000), 0, ft(3000))]
        plan = logic.plan_detour(tray(), [], cuts, params(direction="Under", auto_offset=False, offset_mm=300))
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual((3000, 6000), tuple(round(mm(c)) for c in plan["cut"]))
        self.assertEqual([3000, 2700, 2700], [round(mm(s["start"][2])) for s in plan["segments"]])
        self.assertEqual(3300, round(mm(plan["segments"][1]["start"][0])))

    def test_auto_with_cut_points_and_no_obstacle_needs_direction(self):
        cuts = [(ft(3000), 0, ft(3000)), (ft(6000), 0, ft(3000))]
        plan = logic.plan_detour(tray(), [], cuts, params(auto_offset=False))
        self.assertFalse(plan["ok"])
        self.assertIn("choose a direction", plan["error"])

    def test_detour_past_connected_tray_end_is_refused(self):
        plan = logic.plan_detour(tray(x1=5200, ends_connected=(True, True)), [BEAM], [], params())
        self.assertFalse(plan["ok"])
        self.assertIn("Bridge", plan["error"])
        self.assertEqual("end", plan["run_side"])

    def test_clash_at_free_end_moves_that_end(self):
        plan = logic.plan_detour(tray(x1=5200, ends_connected=(True, False)), [BEAM], [], params())
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual((False, True), plan["open_ends"])
        last = plan["local_points"][-1]
        self.assertAlmostEqual(5200, mm(last[0]), 3)
        self.assertGreater(abs(last[1]) + abs(last[2]), 0)
        self.assertEqual(0, plan["local_points"][0][1])
        self.assertIn(u"New tray segments: 2   Fittings: 2", plan["summary"])

    def test_wall_beside_free_end_shifts_it_sideways(self):
        # The user's case: a wall overlaps the side of the tray from x=12268 to past its free end.
        wall = box(12268, 15583, 571, 876, -5334, 0)
        t = tray(x0=8696, x1=14445, y=892, z=-1590, width=305, height=100, ends_connected=(True, False))
        plan = logic.plan_detour(t, [wall], [], params(angle=45))
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual((False, True), plan["open_ends"])
        self.assertIn(plan["direction"], ("Left", "Right"))
        # 876 - (892 - 152.5) = 136.5 overlap + 50 clearance, rounded to 25.
        self.assertAlmostEqual(200, mm(plan["offset"]), 3)
        self.assertAlmostEqual(892 + 200, mm(plan["segments"][-1]["end"][1]), 3)

    def test_wall_beside_free_start_shifts_it_sideways(self):
        # Same wall, but the tray is drawn from its free end (as in the model), with measured take-up:
        # the bend would start before the tray start, which must not leave an on-axis point behind.
        wall = box(12268, 15278, 571, 876, -5334, 0)
        t = tray(x0=14445, x1=8696, y=892, z=-1590, width=305, height=100, ends_connected=(False, True))
        plan = logic.plan_detour(t, [wall], [], params(angle=45, takeup={45: ft(265.6), 90: ft(533.4)}))
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual((True, False), plan["open_ends"])
        self.assertAlmostEqual(200, mm(plan["offset"]), 3)
        points = plan["local_points"]
        self.assertNotEqual(0, points[0][1])
        self.assertEqual(3, len(points))  # shifted straight and the bend back to the cut
        self.assertTrue(all(abs(p[2]) < 1e-9 for p in points))
        # Away from the wall (which is on the low-y side).
        self.assertAlmostEqual(892 + 200, mm(plan["segments"][0]["start"][1]), 3)

    def test_clash_covering_whole_free_tray_shifts_it(self):
        wide = box(-500, 6000, -1500, 1500, 2950, 3200)
        plan = logic.plan_detour(tray(x1=5200), [wide], [], params(direction="Under"))
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual((True, True), plan["open_ends"])
        self.assertEqual(1, len(plan["segments"]))
        self.assertIn(u"New tray segments: 0   Fittings: 0", plan["summary"])

    def test_obstacle_clear_of_tray(self):
        far_beam = box(4900, 5100, -1500, 1500, 3500, 3700)
        plan = logic.plan_detour(tray(), [far_beam], [], params())
        self.assertFalse(plan["ok"])
        self.assertIn("No clash", plan["error"])
        manual = logic.plan_detour(tray(), [far_beam], [], params(direction="Under", auto_offset=False))
        self.assertTrue(manual["ok"], manual["error"])
        self.assertIn("does not clash", manual["warnings"][0])

    def test_nothing_picked(self):
        plan = logic.plan_detour(tray(), [], [], params())
        self.assertFalse(plan["ok"])
        self.assertIn("obstacle", plan["error"])

    def test_short_segments_are_repaired(self):
        plan = logic.plan_detour(tray(), [BEAM], [], params(direction="Under", angle=90, auto_offset=False,
                                                            offset_mm=60, min_straight_mm=100))
        self.assertTrue(plan["ok"], plan["error"])
        self.assertGreaterEqual(mm(plan["offset"]), 200)
        self.assertTrue(all(mm(s["length"]) >= 200 for s in plan["segments"]))
        self.assertTrue(any("avoid short" in w for w in plan["warnings"]))
        self.assertEqual(100, mm(plan["minimum_straight"]))
        self.assertEqual(100, mm(plan["fitting_allowance"]))

    def test_individual_obstacles_have_separate_detours(self):
        obstacles = [box(2900, 3100, -1500, 1500, 2950, 3200),
                     box(6900, 7100, -1500, 1500, 2950, 3200)]
        plan = logic.plan_detour(tray(), obstacles, [], params(individual_obstacles=True))
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual(7, len(plan["segments"]))
        self.assertTrue(any("2 individual" in w for w in plan["warnings"]))
        self.assertTrue(all(mm(s["length"]) >= 100 - 1e-5 for s in plan["segments"]))
        world = [s["start"] for s in plan["segments"]] + [plan["segments"][-1]["end"]]
        self.assertTrue(logic.route_is_clear(world, logic.obstacle_bounds(obstacles, (0.0, ft(150), ft(50)), ft(50))))

    def test_individual_obstacles_work_on_rotated_grid(self):
        angle = math.radians(30)

        def rot(p):
            return (p[0] * math.cos(angle) - p[1] * math.sin(angle),
                    p[0] * math.sin(angle) + p[1] * math.cos(angle), p[2])

        t = tray(0, 40000)
        t["start"], t["end"] = rot(t["start"]), rot(t["end"])
        t["ends"] = [{"point": t[k], "connected": False} for k in ("start", "end")]
        beams = [[tuple(rot(p) for p in face) for face in box_faces(4000 + i * 7500, 4400 + i * 7500,
                                                                    -3000, 3000, 2800, 3300)]
                 for i in range(5)]
        plan = logic.plan_detour(t, beams, [], params(individual_obstacles=True))
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual(5, len(plan["individual_routes"]))
        self.assertEqual(300, mm(plan["offset"]))

    def test_individual_nearby_obstacles_merge(self):
        obstacles = [BEAM, box(5150, 5350, -1500, 1500, 2950, 3200)]
        plan = logic.plan_detour(tray(), obstacles, [], params(individual_obstacles=True))
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual(3, len(plan["segments"]))
        self.assertTrue(any("merged" in w for w in plan["warnings"]))

    def test_auto_tries_another_direction_when_smallest_does_not_fit(self):
        obstacles = [box(1000, 1200, -50, 50, 2950, 3200),
                     box(0, 800, -50, 50, 3200, 3300)]
        plan = logic.plan_detour(tray(ends_connected=(True, True)), obstacles, [], params())
        self.assertTrue(plan["ok"], plan["error"])
        self.assertIn(plan["direction"], (logic.LEFT, logic.RIGHT))
        # A free start can simply drop under the clash instead.
        self.assertEqual(logic.UNDER, logic.plan_detour(tray(), obstacles, [], params())["direction"])


class BridgePlanTests(unittest.TestCase):
    def trays(self, gap_from=4000, gap_to=6000, **kw):
        return tray(0, gap_from, id_value=1), tray(gap_to, 10000, id_value=2, **kw)

    def test_straight_join_without_obstacle(self):
        a, b = self.trays()
        plan = logic.plan_bridge(a, b, [], params())
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual("Straight", plan["direction"])
        self.assertEqual(1, len(plan["segments"]))
        self.assertAlmostEqual(2000, mm(plan["segments"][0]["length"]))

    def test_bridge_over_beam_snaps_slivers(self):
        a, b = self.trays()
        plan = logic.plan_bridge(a, b, [BEAM], params(direction="Over"))
        self.assertTrue(plan["ok"], plan["error"])
        self.assertFalse(plan.get("spatial_bridge"))
        self.assertEqual(45, plan["angle"])
        pts = plan["local_points"]
        self.assertAlmostEqual(0, pts[0][0])
        self.assertAlmostEqual(2000, mm(pts[-1][0]))
        for s in plan["segments"]:
            self.assertGreaterEqual(mm(s["length"]), 100 - 1e-6)

    def test_bridge_misaligned_or_different_sizes(self):
        a, _ = self.trays()
        offset_b = tray(6000, 10000, y=500, id_value=2)
        self.assertTrue(logic.plan_bridge(a, offset_b, [], params())["ok"])
        nudged = tray(6000, 10000, y=3, id_value=2)
        self.assertTrue(logic.plan_bridge(a, nudged, [], params())["ok"])
        higher = tray(6000, 10000, z=3004, id_value=2)
        self.assertTrue(logic.plan_bridge(a, higher, [], params())["ok"])
        bigger = tray(6000, 10000, width=450, id_value=2)
        self.assertIn("different sizes", logic.plan_bridge(a, bigger, [], params())["error"])

    def test_bridge_connected_end_and_short_gap(self):
        a = tray(0, 4000, ends_connected=(False, True), id_value=1)
        b = tray(6000, 10000, id_value=2)
        self.assertIn("already connected", logic.plan_bridge(a, b, [], params())["error"])
        a, b = self.trays(4900, 5100)
        plan = logic.plan_bridge(a, b, [BEAM], params(direction="Over"))
        self.assertIn("too short", plan["error"])

    def test_same_tray_twice(self):
        a, _ = self.trays()
        self.assertIn("two different", logic.plan_bridge(a, dict(a), [], params())["error"])

    def assert_spatial_route(self, a, b, obstacles, minimum=300, **kw):
        plan = logic.plan_bridge(a, b, obstacles, params(min_straight_mm=minimum, **kw))
        self.assertTrue(plan["ok"], plan["error"])
        self.assertTrue(plan["spatial_bridge"])
        points = [s["start"] for s in plan["segments"]] + [plan["segments"][-1]["end"]]
        ends = logic.nearest_free_ends(a, b)
        # Free ends may be pulled back along their own tray to make room for the fittings.
        for end, point, trim in zip(ends, (points[0], points[-1]), plan["trims"]):
            self.assertAlmostEqual(trim, logic.distance(end["point"], point))
            self.assertEqual(end["point"], plan["original_ends"][ends.index(end)])
        trimmed = [logic._trimmed(t, e, trim) for t, e, trim in zip((a, b), ends, plan["trims"])]
        a, b = trimmed[0][0], trimmed[1][0]
        ends = (trimmed[0][1], trimmed[1][1])
        dirs = [logic.normalize(logic.sub(s["end"], s["start"])) for s in plan["segments"]]
        # Unmeasured estimate: each elbow takes Min. straight / 2 off both straights; unions nothing.
        elbow = [0.0] + [0.0 if logic.dot(d0, d1) > 1 - 1e-9 else minimum / 2.0
                         for d0, d1 in zip(dirs[:-1], dirs[1:])] + [0.0]
        a_dir = logic.normalize(logic.sub(ends[0]["point"],
                                          a["end"] if a["start"] == ends[0]["point"] else a["start"]))
        b_dir = logic.normalize(logic.sub(b["end"] if b["start"] == ends[1]["point"] else b["start"],
                                          ends[1]["point"]))
        elbow[0] = 0.0 if logic.dot(a_dir, dirs[0]) > 1 - 1e-9 else minimum / 2.0
        elbow[-1] = 0.0 if logic.dot(dirs[-1], b_dir) > 1 - 1e-9 else minimum / 2.0
        for i, s in enumerate(plan["segments"]):
            self.assertGreaterEqual(mm(s["length"]), minimum + elbow[i] + elbow[i + 1] - 1e-5)
        flat = logic.obstacle_bounds(obstacles, (ft(150), ft(150), ft(50)), ft(50))
        for s, d in zip(plan["segments"], dirs):
            sloped = abs(d[2]) > 1e-9 and abs(d[2]) < 1 - 1e-9
            if sloped:
                radii = [ft(150), ft(150), ft(50) / math.sqrt(1 - d[2] ** 2)]
                radii[0 if abs(d[0]) > abs(d[1]) else 1] = 0.0
                boxes = logic.obstacle_bounds(obstacles, tuple(radii), ft(50))
            else:
                boxes = flat
            self.assertTrue(logic.route_is_clear([s["start"], s["end"]], boxes), s)
        for t_dir, d in ((a_dir, dirs[0]), (b_dir, dirs[-1])):
            plan_d = logic.normalize((d[0], d[1], 0.0))
            self.assertAlmostEqual(1, logic.dot(t_dir, plan_d))
        slope_sines = [math.sin(math.radians(angle)) for angle in logic.SLOPE_ANGLES]
        for d in dirs:
            self.assertTrue(abs(d[2]) < 1e-9 or abs(abs(d[2]) - 1) < 1e-9 or
                            any(abs(abs(d[2]) - s) < 1e-9 for s in slope_sines), d)
        joint_cosines = [0.0] + [math.cos(math.radians(angle)) for angle in logic.SLOPE_ANGLES]
        for before, after in zip(dirs, dirs[1:]):
            self.assertTrue(any(abs(logic.dot(before, after) - c) < 1e-9 for c in joint_cosines),
                            (before, after))
        for segment in plan["segments"]:
            if segment["normal"]:
                self.assertAlmostEqual(1, logic.length(segment["normal"]))
                self.assertAlmostEqual(0, logic.dot(segment["normal"],
                                                    logic.sub(segment["end"], segment["start"])))
        for view in ("plan", "elevation"):
            plan["preview_view"] = view
            prim = logic.preview_primitives(plan, 600, 300)
            self.assertEqual(view, prim["view"])
            for x, y in prim["route"]:
                self.assertTrue(0 <= x <= 600 and 0 <= y <= 300)
        return plan

    def test_different_elevation_bridge_avoids_obstacle(self):
        a = tray(0, 4000)
        b = tray(7000, 10000, z=4000, id_value=2)
        obstacle = box_faces(4900, 5500, -1000, 1000, 2500, 4200)
        self.assert_spatial_route(a, b, [obstacle])

    def test_perpendicular_bridge_with_level_change(self):
        a = tray(0, 4000)
        b = tray(0, 4000, id_value=2, z=4000)
        b["start"], b["end"] = (ft(7000), ft(3000), ft(4000)), (ft(7000), ft(7000), ft(4000))
        b["ends"] = [{"point": b[k], "connected": False} for k in ("start", "end")]
        obstacles = [box_faces(4800, 5200, -1000, 2000, 2800, 4200)]
        self.assert_spatial_route(a, b, obstacles)

    def test_level_change_smaller_than_two_90_elbows_uses_slopes(self):
        # Revit sample: 90-degree vertical elbows take 533 mm each, so an 810 mm drop cannot be two risers.
        a = tray(0, 4000, z=3000)
        b = tray(5600, 9000, z=2190, id_value=2)
        plan = logic.plan_bridge(a, b, [], self.sample_params())
        self.assertTrue(plan["ok"], plan["error"])
        self.assertTrue(plan["sloped"])
        self.assertNotIn(90, plan["bend_angles"])
        self.assertTrue(plan["takeup_measured"])
        self.assertIn(u"\u00b0 slopes", plan["summary"][0])
        dirs = [logic.normalize(logic.sub(s["end"], s["start"])) for s in plan["segments"]]

        def angle(d0, d1):
            return int(round(math.degrees(math.acos(max(-1.0, min(1.0, logic.dot(d0, d1)))))))
        joints = [0.0] + [self.SAMPLE_TAKEUP[angle(d0, d1)] for d0, d1 in zip(dirs[:-1], dirs[1:])] + [0.0]
        flat = (1.0, 0.0, 0.0)
        joints[0] = self.SAMPLE_TAKEUP.get(angle(flat, dirs[0]), 0.0)
        joints[-1] = self.SAMPLE_TAKEUP.get(angle(dirs[-1], flat), 0.0)
        for i, s in enumerate(plan["segments"]):
            self.assertGreaterEqual(mm(s["length"]), 100 + joints[i] + joints[i + 1] - 1e-5)

    def test_spatial_bridge_warns_when_fittings_force_a_long_loop(self):
        # Ends too short to pull back and a 100 mm drop: even a 30-degree slope cannot fit its elbows.
        a = tray(3000, 4000, z=3000)
        b = tray(4500, 5500, z=2900, id_value=2)
        p = self.sample_params()
        plan = logic.plan_bridge(a, b, [], p)
        self.assertTrue(plan["ok"], plan["error"])
        self.assertTrue(any("fittings need more room" in w for w in plan["warnings"]))
        short = logic.plan_bridge(tray(0, 4000, z=3000), tray(5600, 9000, z=2190, id_value=2), [], p)
        self.assertFalse(any("fittings need more room" in w for w in short["warnings"]))

    SAMPLE_TAKEUP = {30: 198.7, 45: 265.6, 60: 340.2, 90: 533.4}

    def sample_params(self, **kw):
        p = params(min_straight_mm=100, **kw)
        p["takeup"] = dict((a, ft(v)) for a, v in self.SAMPLE_TAKEUP.items())
        return p

    def test_small_drop_under_close_beam_uses_shallow_slope_and_trims_end(self):
        # Revit sample (user report): 381 mm drop, beam 615 mm from the upper end. 45-degree elbows leave
        # no straight on a 45-degree slope, so the old search made a 9 m loop.
        a = tray(14445, -5252, y=892, z=-1590, width=305, height=102)
        # Real Revit ends are never exactly in line (here 1e-13 ft apart); the route must still arrive.
        b = tray(-7322, -16034, y=892 + 3e-11, z=-1971, width=305, height=102, id_value=2)
        beam = box_faces(-6477, -5867, -1765, 5855, -1816, -1054)
        for direction in ("Auto", "Under"):
            plan = logic.plan_bridge(a, b, [beam], self.sample_params(direction=direction))
            self.assertTrue(plan["ok"], plan["error"])
            route = sum(mm(s["length"]) for s in plan["segments"])
            self.assertLess(route, 3000, plan["summary"])
            self.assertFalse(any("fittings need more room" in w for w in plan["warnings"]))
            self.assertTrue(all(s["start"][2] <= ft(-1590) + 1e-9 and s["end"][2] <= ft(-1590) + 1e-9
                                for s in plan["segments"]))
            self.assertIn(30, plan["bend_angles"])
            self.assertGreater(plan["trims"][0], 0)
            self.assertTrue(any("pulled back" in line for line in plan["summary"]))
            self.assertAlmostEqual(plan["segments"][0]["start"][0], ft(-5252) + plan["trims"][0])
            self.assertEqual(plan["segments"][-1]["end"], b["start"])
            dirs = [logic.normalize(logic.sub(s["end"], s["start"])) for s in plan["segments"]]
            joint = [0.0]
            for d0, d1 in zip(dirs[:-1], dirs[1:]):
                angle = int(round(math.degrees(math.acos(max(-1.0, min(1.0, logic.dot(d0, d1)))))))
                joint.append(self.SAMPLE_TAKEUP[angle])
            joint.append(0.0)
            first = int(round(math.degrees(math.asin(abs(dirs[0][2])))))
            joint[0] = self.SAMPLE_TAKEUP[first] if first else 0.0
            for i, s in enumerate(plan["segments"]):
                self.assertGreaterEqual(mm(s["length"]), 100 + joint[i] + joint[i + 1] - 1e-5)
                sloped = 1e-9 < abs(dirs[i][2]) < 1 - 1e-9
                if sloped:
                    cos = math.sqrt(1 - dirs[i][2] ** 2)
                    radii = (0.0, ft(152.5), ft(51) / cos)
                else:
                    radii = (ft(152.5), ft(152.5), ft(51))
                self.assertTrue(logic.route_is_clear([s["start"], s["end"]],
                                                     logic.obstacle_bounds([beam], radii, ft(50))), s)

    def test_spatial_bridge_does_not_trim_when_not_needed(self):
        a = tray(0, 4000, z=3000)
        b = tray(5600, 9000, z=2190, id_value=2)
        plan = logic.plan_bridge(a, b, [], self.sample_params())
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual((0.0, 0.0), tuple(plan.get("trims") or (0.0, 0.0)))
        self.assertFalse(any("pulled back" in line for line in plan["summary"]))

    WALL_CORNER = box_faces(12268, 15430, 571, 876, -5334, 0)

    def test_sideways_step_past_wall_corner(self):
        # Revit sample (user report): parallel trays 587 mm apart across a 1067 mm gap, wall corner in
        # between. Two 90-degree elbows need ~1170 mm, so only an angled sideways step fits.
        a = tray(8973, 11519, y=695, z=-2413, width=305, height=102)
        b = tray(12586, 18295, y=1283, z=-2413, width=305, height=102, id_value=2)
        plan = logic.plan_bridge(a, b, [self.WALL_CORNER], self.sample_params())
        self.assertTrue(plan["ok"], plan["error"])
        self.assertTrue(plan["jog_angles"], plan["summary"])
        self.assertTrue(any("sideways steps" in line for line in plan["summary"]))
        self.assertTrue(all(abs(s["start"][2] - ft(-2413)) < 1e-9 for s in plan["segments"]))
        self.assertLess(sum(mm(s["length"]) for s in plan["segments"]), 2000, plan["summary"])
        self.assertEqual(plan["segments"][-1]["end"], b["start"])
        self.assertTrue(logic.route_is_clear(plan["local_points"], logic.obstacle_bounds(
            [[tuple(plan["frame"].to_local(p) for p in f) for f in self.WALL_CORNER]],
            (ft(152.5), ft(152.5), ft(51)), ft(50))))

    def test_level_change_and_sideways_step_combined(self):
        # Different elevation and a sideways offset: slope down, then step across, in one route.
        a = tray(0, 4000, y=0, z=3000)
        b = tray(6500, 10000, y=600, z=2600, id_value=2)
        plan = logic.plan_bridge(a, b, [], self.sample_params())
        self.assertTrue(plan["ok"], plan["error"])
        self.assertTrue(plan["jog_angles"] and plan["slope_angles"], plan["summary"])
        self.assertLess(sum(mm(s["length"]) for s in plan["segments"]), 3500, plan["summary"])

    def sized_params(self, **kw):
        p = self.sample_params(**kw)
        p["takeup"].update(bend_radius=ft(304.8), bend_leg=ft(76.2))
        return p

    def test_adjusted_takeup_follows_elbow_geometry(self):
        # Revit channel bend, 305 mm radius and 76 mm legs: 90-degree take-up 533 -> 379 at R150 (legs kept).
        option = {"radius": ft(150), "leg": None, "standard_radius": ft(304.8), "standard_leg": ft(76.2)}
        reduced = logic.adjusted_takeup(dict((a, ft(v)) for a, v in self.SAMPLE_TAKEUP.items()), option)
        self.assertAlmostEqual(378.6, mm(reduced[90]), places=1)
        self.assertAlmostEqual(265.6 - 154.8 * math.tan(math.radians(22.5)), mm(reduced[45]), places=1)
        option["leg"] = ft(25)
        self.assertAlmostEqual(327.4, mm(logic.adjusted_takeup({90: ft(533.4)}, option)[90]), places=1)

    def test_fitting_options_shorten_legs_first_and_respect_min_radius(self):
        p = logic.params_in_feet(logic.normalize_params({"min_bend_radius_mm": 200}))
        takeup = {"bend_radius": ft(304.8), "bend_leg": ft(76.2)}
        options = logic.fitting_options(takeup, p)
        # 300 mm is within MIN_FITTING_CHANGE_MM of the standard 305 mm, so it is not offered.
        self.assertEqual([(None, 25.0), (200.0, 25.0)],
                         [(o["radius"] and round(mm(o["radius"]), 1), round(mm(o["leg"]), 1)) for o in options])
        p["shorten_legs"] = False
        p["min_bend_radius"] = ft(150)
        self.assertEqual([150.0], [round(mm(o["radius"]), 1) for o in logic.fitting_options(takeup, p)])
        takeup["bend_radius"] = ft(750)
        self.assertEqual([600.0, 450.0, 300.0, 150.0],
                         [round(mm(o["radius"]), 1) for o in logic.fitting_options(takeup, p)])
        self.assertTrue(all(o["leg"] is None for o in logic.fitting_options(takeup, p)))
        p["min_bend_radius"] = 0.0
        self.assertEqual([], logic.fitting_options(takeup, p))
        self.assertEqual([], logic.fitting_options({90: ft(533.4)}, dict(p, shorten_legs=True)))

    def test_smaller_elbows_used_only_when_standard_ones_do_not_fit(self):
        a = tray(0, 4000, z=3000)
        b = tray(4700, 9000, z=2700, id_value=2)
        p = self.sized_params()
        base = logic.plan_bridge(a, b, [], p)
        self.assertGreater(max(base["trims"]), 0)
        plan = logic.plan_with_fittings(lambda q: logic.plan_bridge(a, b, [], q), p)
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual((0.0, 0.0), tuple(plan.get("trims") or (0.0, 0.0)))
        option = plan["fitting_option"]
        self.assertAlmostEqual(150, mm(option["radius"]))
        self.assertAlmostEqual(25, mm(option["leg"]))
        self.assertTrue(any("Smaller elbows" in line for line in plan["summary"]))
        self.assertTrue(any("bending radius" in w for w in plan["warnings"]))
        # Roomy bridge: standard elbows fit, nothing changes.
        roomy = logic.plan_with_fittings(lambda q: logic.plan_bridge(a, tray(5000, 9000, z=2200, id_value=2), [], q), p)
        self.assertNotIn("fitting_option", roomy)

    def test_min_radius_limits_smaller_elbows(self):
        a = tray(0, 4000, z=3000)
        b = tray(4700, 9000, z=2700, id_value=2)
        p = self.sized_params(min_bend_radius_mm=300)
        plan = logic.plan_with_fittings(lambda q: logic.plan_bridge(a, b, [], q), p)
        option = plan.get("fitting_option") or {}
        self.assertTrue(option.get("radius") is None or mm(option["radius"]) >= 300 - 1e-6)
        p = self.sized_params(min_bend_radius_mm=0, shorten_fitting_legs=False)
        self.assertNotIn("fitting_option", logic.plan_with_fittings(lambda q: logic.plan_bridge(a, b, [], q), p))

    # Revit channel bend geometry for a 305 x 102 tray: take-up = Rc x tan(angle/2) + 76.2 mm leg, with
    # Rc = 304.8 + 305/2 for horizontal bends and 304.8 + 102/2 for vertical ones.
    def crossing_params(self, clearance, **kw):
        options = dict(clearance_mm=clearance, direction=logic.OVER, min_bend_radius_mm=150)
        options.update(kw)
        p = params(**options)
        p["takeup"] = {"bend_radius": ft(304.8), "bend_leg": ft(76.2), "union": ft(76.2)}
        for a in logic.ANGLES:
            p["takeup"][a] = ft(457.3 * math.tan(math.radians(a) / 2) + 76.2)
            p["takeup"][("v", a)] = ft(355.8 * math.tan(math.radians(a) / 2) + 76.2)
        return p

    def plan_crossing(self, clearance, **kw):
        # Lifting a tray over another tray that crosses it at the same level.
        crossing = box(4848, 5152, -2000, 2000, 2949, 3051)
        source = tray(0, 10000, width=305, height=102)
        plan = logic.plan_with_fittings(lambda q: logic.plan_detour(source, [crossing], [], q),
                                        self.crossing_params(clearance, **kw))
        self.assertTrue(plan["ok"], plan["error"])
        return plan, mm(logic.achieved_clearance(plan))

    def test_smaller_clearance_never_gives_a_bigger_gap(self):
        # User report: Clearance 50 left a much bigger gap over a crossing tray than Clearance 200.
        gaps = [self.plan_crossing(c, shallower_bends=False)[1] for c in (25, 50, 100, 150, 200)]
        self.assertEqual(sorted(gaps), gaps)

    def test_shallower_bends_keep_the_clearance_set(self):
        plan, gap = self.plan_crossing(50)
        # Only the 25 mm Offset step rounding is added to the 50 mm clearance.
        self.assertLessEqual(gap, 50 + 25)
        self.assertLess(plan["angle"], 45)
        self.assertFalse(any("Offset increased" in w for w in plan["warnings"]))
        self.assertTrue(any("instead of 45" in line for line in plan["summary"]), plan["summary"])
        self.assertTrue(any(line.startswith("Gap to obstacle: ") for line in plan["summary"]))
        # A roomy clearance keeps the chosen angle.
        self.assertEqual(45, self.plan_crossing(250)[0]["angle"])

    def test_offset_increase_says_how_to_get_closer(self):
        plan, gap = self.plan_crossing(50, shallower_bends=False, min_bend_radius_mm=0,
                                       shorten_fitting_legs=False)
        warning = [w for w in plan["warnings"] if "Offset increased" in w][0]
        self.assertIn(u"gap to the obstacle becomes {}".format(logic.fmt_mm(ft(gap))), warning)
        self.assertIn("shallower Angle", warning)

    def test_over_detour_uses_vertical_bend_takeup(self):
        p = self.crossing_params(300, shallower_bends=False)
        source = tray(0, 10000, width=305, height=102)
        plan = logic.plan_detour(source, [box(4848, 5152, -2000, 2000, 2949, 3051)], [], p)
        self.assertTrue(plan["ok"], plan["error"])
        self.assertAlmostEqual(mm(2 * p["takeup"][("v", 45)]), mm(plan["fitting_allowance"]), places=2)
        side = logic.plan_detour(source, [box(4848, 5152, -500, 50, 0, 6000)], [],
                                 dict(p, direction=logic.LEFT))
        self.assertTrue(side["ok"], side["error"])
        self.assertAlmostEqual(mm(2 * p["takeup"][45]), mm(side["fitting_allowance"]), places=2)

    def test_individual_detours_reserve_the_fitting_allowance_once(self):
        p = self.crossing_params(50, individual_obstacles=True, direction=logic.LEFT)
        source = tray(0, 10000, width=305, height=102)
        walls = [box(2900, 3100, -500, 50, 0, 6000), box(6900, 7100, -500, 50, 0, 6000)]
        plan = logic.plan_detour(source, walls, [], p)
        self.assertTrue(plan["ok"], plan["error"])
        self.assertAlmostEqual(mm(2 * p["takeup"][45]), mm(plan["fitting_allowance"]), places=2)

    def test_unmeasured_shallow_angles_are_not_used(self):
        p = self.sample_params()
        self.assertEqual((30, 45, 60), logic.usable_angles(p, (15, 22.5, 30, 45, 60)))
        self.assertEqual((15, 22.5), logic.usable_angles(params(), (15, 22.5)))

    def test_union_takeup_kept_off_straight_into_target(self):
        # Revit sample: the union where the route meets the target end-on takes 76 mm, which left a
        # 58 mm straight when it was ignored.
        a, b = tray(0, 4000), tray(4700, 9000, y=500, id_value=2)
        p = self.sample_params()
        p["takeup"]["union"] = ft(76.2)
        plan = logic.plan_bridge(a, b, [], p)
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual([45], plan["jog_angles"])
        self.assertGreaterEqual(mm(plan["segments"][-1]["length"]), 100 + 265.6 + 76.2 - 1e-6)

    def test_spatial_bridge_pulls_end_out_of_obstacle_at_endpoint(self):
        a = tray(0, 4000)
        b = tray(7000, 10000, z=4000, id_value=2)
        obstacle = box_faces(3900, 4100, -100, 100, 2900, 3100)
        plan = self.assert_spatial_route(a, b, [obstacle])
        self.assertGreater(plan["trims"][0], 0)
        self.assertLess(plan["segments"][0]["start"][0], ft(3900 - 50))

    def test_spatial_bridge_refuses_obstacle_on_untrimmable_end(self):
        a = tray(3500, 4000)
        b = tray(7000, 10000, z=4000, id_value=2)
        plan = logic.plan_bridge(a, b, [box_faces(3900, 4100, -100, 100, 2900, 3100)], params())
        self.assertFalse(plan["ok"])
        self.assertIn("No clear bridge", plan["error"])

    def test_straight_bridge_cannot_cross_selected_obstacle(self):
        a, b = self.trays()
        plan = logic.plan_bridge(a, b, [BEAM], params(direction=logic.STRAIGHT))
        self.assertFalse(plan["ok"])
        self.assertIn("clashes", plan["error"])

    def test_spatial_bridge_under_strategy_does_not_rise_above_ends(self):
        a, b = self.trays(z=4000)
        plan = self.assert_spatial_route(a, b, [BEAM], direction=logic.UNDER)
        self.assertLessEqual(max(s["end"][2] for s in plan["segments"]), ft(4000))
        self.assertLessEqual(min(s["end"][2] for s in plan["segments"]), ft(2750))

    def test_spatial_bridge_over_strategy_clears_top_of_obstacle(self):
        a, b = self.trays(z=4000)
        obstacle = box_faces(4900, 5100, -1000, 1000, 2800, 4200)
        plan = self.assert_spatial_route(a, b, [obstacle], direction=logic.OVER)
        self.assertGreaterEqual(max(s["end"][2] for s in plan["segments"]), ft(4400) - 1e-5)

    def test_oblique_source_axis_is_rejected(self):
        a, b = self.trays()
        b["end"] = logic.add(b["end"], (0, ft(1000), 0))
        b["ends"][1]["point"] = b["end"]
        plan = logic.plan_bridge(a, b, [], params())
        self.assertFalse(plan["ok"])
        self.assertIn("parallel or perpendicular", plan["error"])


def ytray(y0, y1, x=6000, z=3000, ends_connected=(False, False), id_value=2, width=300):
    """A tray running along +y (perpendicular to tray())."""
    start, end = (ft(x), ft(y0), ft(z)), (ft(x), ft(y1), ft(z))
    return {"id_value": id_value, "start": start, "end": end, "width": ft(width), "height": ft(100),
            "label": "Main", "ends": [{"point": start, "connected": ends_connected[0]},
                                      {"point": end, "connected": ends_connected[1]}]}


class TeeBridgeTests(unittest.TestCase):
    def assert_tee(self, plan, point_mm):
        self.assertTrue(plan["ok"], plan["error"])
        self.assertTrue(plan.get("tee"))
        last = plan["segments"][-1]
        for got, want in zip(last["end"], point_mm):
            self.assertAlmostEqual(want, mm(got), 3)
        # The branch arrives horizontally, square to the main tray.
        d = logic.normalize(logic.sub(last["end"], last["start"]))
        self.assertAlmostEqual(0.0, d[2], 6)
        self.assertAlmostEqual(0.0, logic.dot(d, (0, 1, 0)), 6)

    def test_free_end_pointing_at_side_tees_automatically(self):
        a = tray(0, 3000, ends_connected=(True, False))
        b = ytray(-5000, 5000, ends_connected=(True, True))
        plan = logic.plan_bridge(a, b, [], params())
        self.assert_tee(plan, (6000, 0, 3000))
        self.assertTrue(plan["tee"]["auto"])
        self.assertFalse(plan["tee"]["swapped"])
        self.assertEqual(1, len(plan["segments"]))
        self.assertTrue(any(u"Tee into the side" in line for line in plan["summary"]))

    def test_main_first_is_detected_and_swapped(self):
        a = tray(0, 3000, ends_connected=(True, False))
        b = ytray(-5000, 5000, ends_connected=(True, True))
        plan = logic.plan_bridge(b, a, [], params())
        self.assert_tee(plan, (6000, 0, 3000))
        self.assertTrue(plan["tee"]["swapped"])

    def test_click_near_end_of_second_tray_connects_to_that_end(self):
        a = tray(0, 3000, ends_connected=(True, False))
        b = ytray(-5000, 5000)
        b["pick_point"] = (ft(6000), ft(4800), ft(3000))
        plan = logic.plan_bridge(a, b, [], params())
        self.assertTrue(plan["ok"], plan["error"])
        self.assertFalse(plan.get("tee"))
        self.assertAlmostEqual(5000, mm(plan["segments"][-1]["end"][1]), 3)

    def test_click_along_second_tray_tees_where_clicked_or_nearer(self):
        a = tray(0, 3000, ends_connected=(True, False))
        b = ytray(-5000, 5000)
        b["pick_point"] = (ft(6000), ft(2000), ft(3000))
        plan = logic.plan_bridge(a, b, [], params())
        self.assertTrue(plan["ok"], plan["error"])
        self.assertFalse(plan["tee"]["auto"])
        # The tee slides from the click towards the shortest branch.
        self.assertLess(mm(plan["tee"]["s"]), 7000)
        self.assertAlmostEqual(6000, mm(plan["segments"][-1]["end"][0]), 3)

    def test_tee_to_lower_main_avoids_obstacle(self):
        a = tray(0, 3000, z=3000, ends_connected=(True, False))
        b = ytray(-5000, 5000, z=2000, ends_connected=(True, True))
        beam = box_faces(4000, 4300, -1500, 1500, 1800, 3300)
        plan = logic.plan_bridge(a, b, [beam], params())
        self.assertTrue(plan["ok"], plan["error"])
        self.assertAlmostEqual(2000, mm(plan["segments"][-1]["end"][2]), 3)
        self.assertAlmostEqual(6000, mm(plan["segments"][-1]["end"][0]), 3)
        boxes = logic.obstacle_bounds([beam], (ft(150), ft(150), ft(50)), ft(50))
        for s in plan["segments"]:
            if abs(s["end"][2] - s["start"][2]) < 1e-9:
                self.assertTrue(logic.route_is_clear([s["start"], s["end"]], boxes), s)

    def test_in_line_trays_never_tee(self):
        a = tray(0, 3000)
        b = tray(5000, 9000, id_value=2)
        b["pick_point"] = (ft(7000), 0, ft(3000))
        self.assertIsNone(logic.tee_request(a, b))

    def test_tee_keeps_room_for_fitting_from_main_ends(self):
        a = tray(0, 3000, ends_connected=(True, False))
        b = ytray(-5000, 5000)
        b["pick_point"] = (ft(6000), ft(4000), ft(3000))
        plan = logic.plan_bridge(a, b, [box_faces(5200, 5800, -6000, 4300, 2900, 3100)], params())
        self.assertTrue(plan["ok"], plan["error"])
        along, _ = logic.tee_takeups(params(), a, b)
        self.assertLessEqual(mm(plan["tee"]["s"]), 10000 - mm(along) - 100 + 1e-6)


class RunRerouteTests(unittest.TestCase):
    def run_tray(self, x0=0, x1=5200):
        t = tray(x0, x1, ends_connected=(True, True))
        straight = tray(5200, 9000, ends_connected=(True, True), id_value=7)
        straight.update(kind="tray", near=straight["start"], label="Next")
        t["runs"] = {"end": {"items": [{"kind": "fitting", "id_value": 6, "label": "Union"}, straight],
                             "stop": None}}
        return t

    def test_clash_at_connected_end_reroutes_the_run(self):
        plan = logic.plan_detour(self.run_tray(), [BEAM], [], params())
        self.assertTrue(plan["ok"], plan["error"])
        reroute = plan["run_reroute"]
        self.assertEqual("end", reroute["side"])
        self.assertEqual([6], [i["id_value"] for i in reroute["removed"]])
        self.assertEqual(7, reroute["cut_tray"]["id_value"])
        self.assertLess(mm(plan["segments"][0]["start"][0]), 4900 - 150 - 50)
        self.assertGreater(mm(plan["segments"][-1]["end"][0]), 5100 + 150 + 50)
        self.assertTrue(any(u"Connected run re-routed" in line for line in plan["summary"]))

    def test_clash_that_also_blocks_the_next_straight_cuts_further(self):
        wide = box(4900, 7000, -1500, 1500, 2950, 3200)
        plan = logic.plan_detour(self.run_tray(), [wide], [], params())
        self.assertTrue(plan["ok"], plan["error"])
        self.assertGreater(mm(plan["segments"][-1]["end"][0]), 7000 + 150 + 50)
        self.assertGreater(mm(plan["run_reroute"]["cut_at"]), 7000 - 5200)

    def test_no_run_data_still_refuses(self):
        t = self.run_tray()
        del t["runs"]
        plan = logic.plan_detour(t, [BEAM], [], params())
        self.assertFalse(plan["ok"])
        self.assertIn("connected end", plan["error"])

    def test_run_without_clear_straight_explains(self):
        t = self.run_tray()
        t["runs"]["end"]["items"] = t["runs"]["end"]["items"][:1]
        t["runs"]["end"]["stop"] = "a tee"
        plan = logic.plan_detour(t, [BEAM], [], params())
        self.assertFalse(plan["ok"])
        self.assertIn("a tee", plan["error"])


class ParamsAndPresetsTests(unittest.TestCase):
    def test_normalize_params_falls_back_on_bad_values(self):
        p = logic.normalize_params({"direction": "Sideways", "angle": 33, "offset_mm": "abc", "clearance_mm": 75,
                                    "auto_offset": 0})
        self.assertEqual("Auto", p["direction"])
        self.assertEqual(45, p["angle"])
        self.assertEqual(300.0, p["offset_mm"])
        self.assertEqual(75.0, p["clearance_mm"])
        self.assertFalse(p["auto_offset"])
        self.assertEqual(logic.DEFAULT_PARAMS, logic.normalize_params(None))

    def test_fine_bend_angles_are_accepted(self):
        self.assertEqual(22.5, logic.normalize_params({"angle": "22.5"})["angle"])
        self.assertEqual(15, logic.normalize_params({"angle": 15.0})["angle"])
        self.assertTrue(logic.normalize_params({})["shallower_bends"])
        self.assertFalse(logic.params_in_feet({"shallower_bends": False})["shallower_bends"])

    def test_parse_mm_messages(self):
        self.assertEqual(12.5, logic.parse_mm("12,5", "offset_mm"))
        with self.assertRaisesRegex(logic.PlanError, "Offset must be a number"):
            logic.parse_mm("x", "offset_mm")
        with self.assertRaisesRegex(logic.PlanError, "between"):
            logic.parse_mm("-1", "clearance_mm")
        for value in ("nan", "inf", "-inf"):
            with self.assertRaises(logic.PlanError):
                logic.parse_mm(value, "min_straight_mm")

    def test_user_preset_keeps_minimum_and_obstacle_mode(self):
        preset, _ = logic.make_user_preset("300 mm fittings",
                                         {"min_straight_mm": 300, "individual_obstacles": True}, [])
        restored = logic.normalize_user_presets([preset])[0]
        self.assertEqual(300, restored["min_straight_mm"])
        self.assertTrue(restored["individual_obstacles"])

    def test_bend_radius_settings_are_normalized_and_kept_in_presets(self):
        p = logic.normalize_params({"min_bend_radius_mm": "abc", "shorten_fitting_legs": 0})
        self.assertEqual(150.0, p["min_bend_radius_mm"])
        self.assertFalse(p["shorten_fitting_legs"])
        with self.assertRaisesRegex(logic.PlanError, "Min. bend radius"):
            logic.parse_mm("-5", "min_bend_radius_mm")
        preset, _ = logic.make_user_preset("tight", {"min_bend_radius_mm": 300, "shorten_fitting_legs": False}, [])
        restored = logic.normalize_user_presets([preset])[0]
        self.assertEqual(300, restored["min_bend_radius_mm"])
        self.assertFalse(restored["shorten_fitting_legs"])

    def test_zero_minimum_has_nonzero_geometry_floor(self):
        p = params(min_straight_mm=0)
        self.assertEqual(10, mm(p["min_straight"]))
        self.assertEqual(10, mm(p["fitting_allowance"]))

    def test_presets(self):
        names = [p["name"] for p in logic.BUILTIN_PRESETS]
        self.assertEqual(len(names), len(set(names)))
        for p in logic.BUILTIN_PRESETS:
            self.assertIn(p["direction"], logic.DIRECTIONS)
            self.assertIn(p["angle"], logic.ANGLES)
        user = logic.normalize_user_presets([{"name": names[0]}, {"name": "Mine", "direction": "Over"},
                                             {"name": "Mine"}, "junk"])
        self.assertEqual(["Mine"], [p["name"] for p in user])
        self.assertEqual(len(names) + 1, len(logic.all_presets(user)))
        with self.assertRaisesRegex(logic.PlanError, "built-in"):
            logic.make_user_preset(names[0], {}, [])
        preset, replaces = logic.make_user_preset(" Mine ", {"direction": "Left"}, ["Mine"])
        self.assertTrue(replaces)
        self.assertEqual("Left", preset["direction"])


class PreviewTests(unittest.TestCase):
    def test_elevation_preview_fits_canvas(self):
        plan = logic.plan_detour(tray(), [BEAM], [], params(direction="Over"))
        prim = logic.preview_primitives(plan, 600, 300)
        self.assertEqual("elevation", prim["view"])
        for x, y in prim["route"]:
            self.assertTrue(0 <= x <= 600 and 0 <= y <= 300)
        ys = [y for _, y in prim["route"]]
        self.assertLess(min(ys), ys[0])  # over goes up = smaller screen y
        self.assertEqual(2, len(prim["cuts"]))
        self.assertTrue(prim["obstacles"])
        self.assertTrue(any("+300 mm" in t for _, _, t in prim["labels"]))

    def test_plan_preview_for_sideways_detour(self):
        post = box(4950, 5050, -100, 100, 0, 6000)
        plan = logic.plan_detour(tray(), [post], [], params(direction="Right"))
        prim = logic.preview_primitives(plan, 600, 300)
        self.assertEqual("plan", prim["view"])
        ys = [y for _, y in prim["route"]]
        self.assertGreater(max(ys), ys[0])  # right = down on screen

    def test_failed_plan_still_draws_context(self):
        plan = logic.plan_detour(tray(), [], [], params())
        prim = logic.preview_primitives(plan, 600, 300)
        self.assertFalse(prim["empty"])
        self.assertEqual([], prim["route"])


class RegenerateOptionsTests(unittest.TestCase):
    def plan_fn(self, source, obstacles, cut_points=()):
        calls = []

        def run(variant):
            calls.append(variant)
            p = logic.params_in_feet(variant)
            return logic.plan_with_fittings(lambda q: logic.plan_detour(source, obstacles, list(cut_points), q), p)
        return run, calls

    def user_params(self, **kw):
        base = dict(logic.DEFAULT_PARAMS, clearance_mm=75, min_straight_mm=120)
        base.update(kw)
        return base

    def test_variants_cover_every_direction_and_angle_but_keep_the_user_limits(self):
        variants = logic.option_variants(logic.MODE_DETOUR, self.user_params(), 3)
        self.assertEqual(4 * len(logic.ANGLES) * 2, len(variants))
        self.assertEqual({75.0}, set(v["clearance_mm"] for v in variants))
        self.assertEqual({120.0}, set(v["min_straight_mm"] for v in variants))
        self.assertEqual({True}, set(v["auto_offset"] for v in variants))
        self.assertEqual(set(logic.DETOUR_DIRECTIONS), set(v["direction"] for v in variants))
        with_cuts = logic.option_variants(logic.MODE_DETOUR, self.user_params(), 3, cut_points=True)
        self.assertEqual({False}, set(v["individual_obstacles"] for v in with_cuts))
        bridge = logic.option_variants(logic.MODE_BRIDGE, self.user_params(), 0)
        self.assertEqual({logic.AUTO}, set(v["direction"] for v in bridge))
        manual = logic.option_variants(logic.MODE_DETOUR, self.user_params(), 0, cut_points=True)
        self.assertEqual({False}, set(v["auto_offset"] for v in manual))

    def test_options_are_distinct_ranked_and_offer_different_approaches(self):
        run, calls = self.plan_fn(tray(), [BEAM])
        variants = logic.option_variants(logic.MODE_DETOUR, self.user_params(), 1)
        candidates, stats = logic.evaluate_options(run, variants)
        self.assertEqual(len(variants), stats["planned"])
        self.assertEqual(len(candidates), len(set(logic.option_signature(c["plan"]) for c in candidates)))
        scores = [c["score"] for c in candidates]
        self.assertEqual(sorted(scores), scores)
        options, rejected = logic.choose_options(candidates)
        self.assertEqual([], rejected)
        self.assertEqual(logic.OPTION_COUNT, len(options))
        self.assertEqual(list(range(1, len(options) + 1)), [o["number"] for o in options])
        self.assertEqual(sorted(o["score"] for o in options), [o["score"] for o in options])
        # Over, under, left and right all make the list before a second variant of any of them.
        self.assertEqual(set(logic.DETOUR_DIRECTIONS), set(o["plan"]["direction"] for o in options))
        self.assertTrue(logic.option_list_text(options[0]).startswith(u"1. [Best] "))
        for option in options:
            plan = logic.plan_with_fittings(
                lambda q: logic.plan_detour(tray(), [BEAM], [], q), logic.params_in_feet(option["params"]))
            # Choosing an option puts its settings in the window, which must plan the same route again.
            self.assertEqual(logic.option_signature(option["plan"]), logic.option_signature(plan))

    def test_ranking_prefers_a_tight_short_route(self):
        run, _ = self.plan_fn(tray(), [BEAM])
        candidates, _ = logic.evaluate_options(run, logic.option_variants(logic.MODE_DETOUR, self.user_params(), 1))
        best = candidates[0]["plan"]
        # The beam is only 250 mm deep but 3 m wide: going under it beats going round it.
        self.assertEqual(logic.UNDER, best["direction"])
        self.assertIn(u"Drop under", candidates[0]["label"])
        self.assertIn(u"gap", candidates[0]["label"])
        shift = [c for c in candidates if c["plan"]["direction"] == logic.LEFT and all(c["plan"]["open_ends"])]
        self.assertEqual(1, len(shift))
        self.assertTrue(shift[0]["label"].startswith(u"Move the whole tray left 1"), shift[0]["label"])

    def test_duplicate_routes_keep_the_setting_that_shows_the_real_angle(self):
        crossing = box(4848, 5152, -2000, 2000, 2949, 3051)
        source = tray(0, 10000, width=305, height=102)
        takeup = BridgePlanTests.crossing_params(None, 50)["takeup"]

        def run(variant):
            p = logic.params_in_feet(variant)
            p["takeup"] = takeup
            return logic.plan_with_fittings(lambda q: logic.plan_detour(source, [crossing], [], q), p)

        variants = logic.option_variants(logic.MODE_DETOUR, self.user_params(clearance_mm=50), 1)
        candidates, _ = logic.evaluate_options(run, variants)
        for candidate in candidates:
            self.assertEqual(candidate["params"]["angle"], candidate["plan"]["angle"])

    def test_test_build_failures_are_replaced_by_the_next_option(self):
        run, _ = self.plan_fn(tray(), [BEAM])
        candidates, _ = logic.evaluate_options(run, logic.option_variants(logic.MODE_DETOUR, self.user_params(), 1))
        failing = candidates[0]
        options, rejected = logic.choose_options(
            candidates, lambda c: u"clashes with Wall" if c is failing else None, count=3)
        self.assertEqual([failing], rejected)
        self.assertEqual(u"clashes with Wall", failing["rejected"])
        self.assertEqual(3, len(options))
        self.assertNotIn(failing, options)
        none, tried = logic.choose_options(candidates, lambda c: u"no", count=3, extra_trials=2)
        self.assertEqual([], none)
        self.assertEqual(5, len(tried))

    def test_routes_that_ignore_the_angle_are_planned_once_per_direction(self):
        a = tray(0, 4000)
        b = tray(4700, 8700, z=2700, id_value=2)
        calls = []

        def run(variant):
            calls.append(variant)
            return logic.plan_with_fittings(lambda q: logic.plan_bridge(a, b, [], q), logic.params_in_feet(variant))

        variants = logic.option_variants(logic.MODE_BRIDGE, self.user_params(), 0)
        candidates, stats = logic.evaluate_options(run, variants)
        self.assertEqual(1, len(calls))
        self.assertTrue(candidates)
        self.assertTrue(candidates[0]["plan"].get("spatial_bridge"))

    def test_search_stops_at_the_time_budget(self):
        run, calls = self.plan_fn(tray(), [BEAM])
        ticks = iter(range(100))
        _, stats = logic.evaluate_options(run, logic.option_variants(logic.MODE_DETOUR, self.user_params(), 1),
                                          clock=lambda: next(ticks), budget_s=3)
        self.assertTrue(stats["stopped"])
        self.assertEqual(3, len(calls))

    def test_relaxed_options_name_what_they_change_and_never_touch_clearance(self):
        user = self.user_params(shorten_fitting_legs=False, min_bend_radius_mm=0, min_straight_mm=300)
        variants = logic.option_variants(logic.MODE_DETOUR, user, 1)
        relaxed, text = logic.relaxed_variants(variants, user)
        self.assertEqual(len(variants), len(relaxed))
        self.assertEqual({75.0}, set(v["clearance_mm"] for v in relaxed))
        self.assertEqual({150.0}, set(v["min_bend_radius_mm"] for v in relaxed))
        self.assertEqual({150.0}, set(v["min_straight_mm"] for v in relaxed))
        self.assertIn(u"bend radius down to 150 mm", text)
        self.assertIn(u"Min. straight 150 mm", text)
        loose = self.user_params(shorten_fitting_legs=True, min_bend_radius_mm=150, min_straight_mm=50)
        self.assertEqual(([], u""), logic.relaxed_variants(variants, loose))

    def test_relaxed_option_says_which_of_your_limits_it_overrides(self):
        user = self.user_params(shorten_fitting_legs=False, min_bend_radius_mm=0, min_straight_mm=300)
        relaxed, text = logic.relaxed_variants(logic.option_variants(logic.MODE_DETOUR, user, 1), user)
        option = {"number": 2, "relaxed": text, "params": relaxed[0]}
        own = logic.own_limits(user)
        notice = logic.relaxed_notice(option, own)
        self.assertIn(u"Option 2 is RELAXED", notice)
        self.assertIn(u"Min. straight 150 mm (yours: 300 mm)", notice)
        self.assertIn(u"Min. bend radius 150 mm (yours: 0 (never reduce))", notice)
        self.assertIn(u"25 mm elbow legs on (yours: off)", notice)
        self.assertIn(u"Clearance is unchanged", notice)
        self.assertTrue(logic.relaxed_in_use(relaxed[0], option))
        self.assertFalse(logic.relaxed_in_use(dict(relaxed[0], min_straight_mm=200), option))
        self.assertFalse(logic.relaxed_in_use(relaxed[0], dict(option, relaxed=u"")))
        kept = logic.keep_own_limits(relaxed[0], own)
        self.assertEqual((300.0, 0.0, False), (kept["min_straight_mm"], kept["min_bend_radius_mm"],
                                               kept["shorten_fitting_legs"]))
        self.assertEqual(relaxed[0]["direction"], kept["direction"])

    def test_failed_variants_report_the_first_reason(self):
        run, _ = self.plan_fn(tray(), [])
        candidates, stats = logic.evaluate_options(run, logic.option_variants(logic.MODE_DETOUR, self.user_params(), 0))
        self.assertEqual([], candidates)
        self.assertEqual(stats["planned"], stats["failed"])
        self.assertTrue(stats["error"])


class XamlWiringTests(unittest.TestCase):
    def test_every_xaml_handler_and_name_exists_in_script(self):
        root = ET.parse(str(BUNDLE / "ui.xaml")).getroot()
        source = (BUNDLE / "script.py").read_text(encoding="utf-8")
        handlers, names = set(), set()
        for el in root.iter():
            for key, value in el.attrib.items():
                if key in ("Click", "SelectionChanged", "TextChanged", "Checked", "Unchecked", "SizeChanged", "KeyUp"):
                    handlers.add(value)
                if key.endswith("}Name"):
                    names.add(value)
        self.assertTrue(handlers)
        for handler in handlers:
            self.assertRegex(source, r"def {}\(self, sender, args\)".format(handler))
        used = set(re.findall(r"self\.((?:btn|cmb|txt|lbl|chk|lst|rb|cnv|panel)[A-Z]\w*)\b", source))
        self.assertTrue(used)
        self.assertEqual(set(), used - names)

    def test_modeless_rules(self):
        source = (BUNDLE / "script.py").read_text(encoding="utf-8")
        self.assertIn("__persistentengine__ = True", source)
        self.assertIn("UI.ExternalEvent.Create(self.handler)", source)
        self.assertNotRegex(source, r"f\"|f'")


# --- fake Revit objects for build_route ----------------------------------------------------------
class XYZ(object):
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = float(x), float(y), float(z)

    def DistanceTo(self, o):
        return math.sqrt((self.X - o.X) ** 2 + (self.Y - o.Y) ** 2 + (self.Z - o.Z) ** 2)

    def DotProduct(self, o):
        return self.X * o.X + self.Y * o.Y + self.Z * o.Z


class Line(object):
    def __init__(self, a, b):
        self.a, self.b = a, b

    @staticmethod
    def CreateBound(a, b):
        return Line(a, b)

    def GetEndPoint(self, i):
        return self.a if i == 0 else self.b

    @property
    def Direction(self):
        d = self.a.DistanceTo(self.b)
        return XYZ((self.b.X - self.a.X) / d, (self.b.Y - self.a.Y) / d, (self.b.Z - self.a.Z) / d)


class Connector(object):
    def __init__(self, owner, origin):
        self.Owner, self.Origin, self.ConnectorType, self.refs = owner, origin, "End", []

    @property
    def IsConnected(self):
        return bool(self.refs)

    @property
    def AllRefs(self):
        return list(self.refs)

    def ConnectTo(self, other):
        self.refs.append(other)
        other.refs.append(self)

    def DisconnectFrom(self, other):
        self.refs.remove(other)
        other.refs.remove(self)


class Location(object):
    def __init__(self, owner, curve):
        self.owner, self._curve = owner, curve

    @property
    def Curve(self):
        return self._curve

    @Curve.setter
    def Curve(self, curve):
        self._curve = curve
        self.owner.log.append(("curve", self.owner.Id.Value))
        self.owner.connectors[0].Origin, self.owner.connectors[1].Origin = curve.a, curve.b


class Element(object):
    next_id = [100]

    def __init__(self, doc, log):
        Element.next_id[0] += 1
        self.Id = SimpleNamespace(Value=Element.next_id[0])
        self.IsValidObject = True
        self.Document = doc
        self.log = log
        doc.elements[self.Id.Value] = self


class FakeTray(Element):
    created = []

    def __init__(self, doc, a, b, log):
        Element.__init__(self, doc, log)
        self.connectors = [Connector(self, a), Connector(self, b)]
        self.ConnectorManager = SimpleNamespace(Connectors=self.connectors)
        self.Location = Location(self, Line(a, b))
        self.ReferenceLevel = SimpleNamespace(Id="L1")
        self.Parameters = []
        self.CurveNormal = None

    def GetTypeId(self):
        return "T1"

    def get_Parameter(self, _):
        return None

    @classmethod
    def Create(cls, doc, type_id, a, b, level_id):
        tray_ = cls(doc, a, b, doc.log)
        cls.created.append(tray_)
        doc.log.append(("create", tray_.Id.Value))
        return tray_


class Fitting(Element):
    def __init__(self, doc, kind, *conns):
        Element.__init__(self, doc, doc.log)
        self.kind = kind
        self.connectors = [Connector(self, c.Origin) for c in conns]
        self.MEPModel = SimpleNamespace(ConnectorManager=SimpleNamespace(Connectors=self.connectors))
        for c, own in zip(conns, self.connectors):
            c.ConnectTo(own)


class FakeDoc(object):
    def __init__(self, fail_elbows=False):
        self.elements, self.log, self.fail_elbows, self.IsWorkshared = {}, [], fail_elbows, False
        self.Create = SimpleNamespace(NewElbowFitting=self.elbow, NewUnionFitting=self.union,
                                      NewTeeFitting=self.tee)

    def tee(self, c1, c2, c3):
        self.log.append(("tee",))
        return Fitting(self, "tee", c1, c2, c3)

    def elbow(self, c1, c2):
        if self.fail_elbows:
            raise RuntimeError("failed to insert elbow")
        self.log.append(("elbow",))
        return Fitting(self, "elbow", c1, c2)

    def union(self, c1, c2):
        self.log.append(("union",))
        return Fitting(self, "union", c1, c2)

    def GetElement(self, eid):
        return self.elements.get(eid.Value)

    def Regenerate(self):
        self.log.append(("regen",))

    def Delete(self, ids):
        for eid in ids:
            self.elements[eid.Value].IsValidObject = False
            del self.elements[eid.Value]
        return list(ids)


class NetList(list):
    @classmethod
    def __class_getitem__(cls, _):
        return cls

    def Add(self, value):
        self.append(value)

    @property
    def Count(self):
        return len(self)


class EventHook(object):
    def __init__(self):
        self.listeners = []

    def __iadd__(self, callback):
        self.listeners.append(callback)
        return self

    def __isub__(self, callback):
        self.listeners.remove(callback)
        return self


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        modules = {}

        def module(name, **members):
            result = ModuleType(name)
            result.__dict__.update(members)
            modules[name] = result
            return result

        module("clr", AddReference=Mock())
        module("System")
        module("System.Collections")
        module("System.Collections.Generic", List=NetList)
        module("System.Windows", Point=Mock(), Visibility=SimpleNamespace(Visible="Visible", Collapsed="Collapsed"))
        module("System.Windows.Controls", Canvas=Mock(), TextBlock=Mock())
        module("System.Windows.Media", Color=Mock(), DoubleCollection=Mock(), PenLineCap=Mock(),
               PenLineJoin=Mock(), PointCollection=Mock(), SolidColorBrush=Mock())
        module("System.Windows.Shapes", Line=Mock(), Polyline=Mock(), Rectangle=Mock())
        self.forms = SimpleNamespace(WPFWindow=object, alert=Mock(), ask_for_string=Mock())
        self.logger = Mock()
        bip = SimpleNamespace(RBS_CABLETRAY_WIDTH_PARAM="W", RBS_CABLETRAY_HEIGHT_PARAM="H",
                              ALL_MODEL_INSTANCE_COMMENTS="C", INVALID="INVALID")
        db = SimpleNamespace(XYZ=XYZ, Line=Line, ConnectorType=SimpleNamespace(End="End", Logical="Logical"),
                             BuiltInParameter=bip, InternalDefinition=type("InternalDefinition", (), {}),
                             StorageType=SimpleNamespace(Double=1, Integer=2, String=3, ElementId=4),
                             ElementId=object)
        ui = SimpleNamespace(IExternalEventHandler=object, ExternalEvent=Mock(),
                             ExternalEventRequest=SimpleNamespace(Accepted="Accepted", Pending="Pending"))
        self.revit = SimpleNamespace(doc=None, uidoc=None, Transaction=Mock())
        module("pyrevit", DB=db, UI=ui, HOST_APP=SimpleNamespace(version="2025"), forms=self.forms,
               revit=self.revit, script=SimpleNamespace(get_logger=lambda: self.logger, get_bundle_file=lambda n: n))
        module("Autodesk")
        module("Autodesk.Revit")
        module("Autodesk.Revit.DB")
        module("Autodesk.Revit.DB.Electrical", CableTray=FakeTray)
        module("Autodesk.Revit.UI")
        module("Autodesk.Revit.UI.Selection", ISelectionFilter=object, ObjectType=SimpleNamespace())
        module("Autodesk.Revit.Exceptions", OperationCanceledException=type("Cancelled", (Exception,), {}))
        modules["cable_tray_logic"] = logic
        script_spec = importlib.util.spec_from_file_location("cable_tray_script", BUNDLE / "script.py")
        self.mod = importlib.util.module_from_spec(script_spec)
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {"APPDATA": self.temp.name}):
            script_spec.loader.exec_module(self.mod)
        FakeTray.created = []

        self.doc = SimpleNamespace(IsValidObject=True, Title="Proj")
        self.doc.Equals = lambda other: other is self.doc
        w = self.mod.CableTrayCoordinator.__new__(self.mod.CableTrayCoordinator)
        w.closed = False
        w.initialising = False
        w.queue = logic.ActionQueue()
        w.ext_event = Mock()
        w.ext_event.Raise.return_value = "Accepted"
        w.doc, w.doc_title = self.doc, "Proj"
        w.status = []
        w.set_status = w.status.append
        w.takeup = {}
        self.w = w

    def test_no_project_alerts_and_opens_nothing(self):
        self.forms.alert.assert_called_once()
        self.assertIsNone(self.mod.WINDOW)

    def test_handler_delegates_to_owning_window(self):
        owner = Mock()
        self.mod.CoordinatorEventHandler(owner).Execute("app")
        owner.run_queued.assert_called_once_with("app")

    def test_actions_are_queued_not_run_on_click(self):
        action = Mock()
        self.w.run_in_revit("Pick tray", action)
        action.assert_not_called()
        self.w.run_queued("app")
        action.assert_called_once_with("app")

    def test_rejected_raise_clears_queue_and_closed_window_drops_work(self):
        self.w.ext_event.Raise.return_value = "Denied"
        self.w.run_in_revit("Apply", Mock())
        self.assertEqual(0, len(self.w.queue))
        self.assertIn("could not queue", self.w.status[-1])
        self.w.ext_event.Raise.return_value = "Accepted"
        action = Mock()
        self.w.run_in_revit("Apply", action)
        self.w.closed = True
        self.w.run_queued("app")
        action.assert_not_called()

    def test_other_document_is_refused(self):
        other = SimpleNamespace(ActiveUIDocument=SimpleNamespace(Document=SimpleNamespace()))
        with self.assertRaisesRegex(self.mod.UserError, "Switch back"):
            self.w.active_uidoc(other)

    def test_user_errors_go_to_status(self):
        self.w.on_action_error("Apply", self.mod.UserError("nope"))
        self.assertEqual("nope", self.w.status[-1])

    def make_tray(self, doc, x0, x1):
        return FakeTray(doc, XYZ(ft(x0), 0, ft(3000)), XYZ(ft(x1), 0, ft(3000)), doc.log)

    def test_detour_cuts_reconnects_far_end_and_adds_elbows(self):
        doc = FakeDoc()
        source = self.make_tray(doc, 0, 10000)
        neighbour = self.make_tray(doc, 10000, 15000)
        source.connectors[1].ConnectTo(neighbour.connectors[0])
        start_before = source.connectors[0].Origin
        plan = logic.plan_detour(tray(), [BEAM], [], params())
        created, fittings, warnings = self.mod.build_route(doc, plan, [source], False)

        self.assertEqual([], warnings)
        self.assertEqual(4, len(created))          # 3 detour pieces + far piece
        self.assertEqual(4, len(fittings))
        self.assertTrue(all(f.kind == "elbow" for f in fittings))
        self.assertIs(start_before, source.connectors[0].Origin)  # start end untouched
        self.assertAlmostEqual(4700, mm(source.Location.Curve.GetEndPoint(1).X))
        far = created[-1]
        self.assertIn(neighbour.connectors[0], far.connectors[1].refs)  # far neighbour re-attached
        self.assertEqual([], [r for r in source.connectors[1].refs if r.Owner is neighbour])
        # The far end was detached before the tray was shortened.
        self.assertLess(doc.log.index(("curve", source.Id.Value)), doc.log.index(("create", created[0].Id.Value)))
        self.assertIsNotNone(created[0].CurveNormal)
        self.assertIsNone(created[1].CurveNormal)

    def test_elbow_failure_raises_unless_kept(self):
        plan = logic.plan_detour(tray(), [BEAM], [], params())
        doc = FakeDoc(fail_elbows=True)
        with self.assertRaisesRegex(self.mod.UserError, "Nothing was changed"):
            self.mod.build_route(doc, plan, [self.make_tray(doc, 0, 10000)], False)
        doc = FakeDoc(fail_elbows=True)
        created, fittings, warnings = self.mod.build_route(doc, plan, [self.make_tray(doc, 0, 10000)], True)
        self.assertEqual(4, len(created))
        self.assertEqual(4, len(warnings))

    def test_build_sets_reduced_size_on_elbows_only(self):
        class Param(object):
            def __init__(self, value, read_only=False):
                self.value, self.IsReadOnly, self.StorageType = value, read_only, 1

            def AsDouble(self):
                return self.value

            def Set(self, value):
                self.value = value

        sized = []

        def elbow(c1, c2):
            fitting = Fitting(doc, "elbow", c1, c2)
            fitting.params = {"Bend Radius": Param(ft(304.8)), "Tray Length": Param(ft(76.2))}
            fitting.LookupParameter = fitting.params.get
            sized.append(fitting)
            return fitting

        doc = FakeDoc()
        doc.Create.NewElbowFitting = elbow
        plan = logic.plan_detour(tray(), [BEAM], [], params())
        plan["fitting_option"] = {"radius": ft(150), "leg": ft(25), "standard_radius": ft(304.8),
                                  "standard_leg": ft(76.2)}
        _, fittings, warnings = self.mod.build_route(doc, plan, [self.make_tray(doc, 0, 10000)], False)
        self.assertEqual([], warnings)
        self.assertEqual(4, len(sized))
        for fitting in sized:
            self.assertAlmostEqual(150, mm(fitting.params["Bend Radius"].value))
            self.assertAlmostEqual(25, mm(fitting.params["Tray Length"].value))
        locked = Fitting(doc, "elbow")
        locked.LookupParameter = {"Bend Radius": Param(1.0, read_only=True)}.get
        self.assertEqual((None, None), self.mod.fitting_size_params(locked))
        self.assertEqual((None, None), self.mod.fitting_size_params(Fitting(doc, "union")))

    def test_resized_elbow_pulls_the_left_behind_straight_onto_its_connector(self):
        # Revit sample: shortening legs extended the level tray but left the slope 51 mm short.
        doc = FakeDoc()
        level, slope = self.make_tray(doc, 0, 2000), self.make_tray(doc, 2000, 2500)
        elbow = doc.Create.NewElbowFitting(level.connectors[1], slope.connectors[0])
        elbow.connectors[1].Origin = XYZ(ft(1949), 0, ft(3000))
        self.mod.close_fitting_gaps(elbow)
        self.assertAlmostEqual(1949, mm(slope.Location.Curve.GetEndPoint(0).X))
        self.assertAlmostEqual(2500, mm(slope.Location.Curve.GetEndPoint(1).X))
        self.assertAlmostEqual(2000, mm(level.Location.Curve.GetEndPoint(1).X))

    def test_straight_bridge_uses_unions(self):
        doc = FakeDoc()
        a, b = self.make_tray(doc, 0, 4000), self.make_tray(doc, 6000, 10000)
        plan = logic.plan_bridge(tray(0, 4000, id_value=1), tray(6000, 10000, id_value=2), [], params())
        created, fittings, _ = self.mod.build_route(doc, plan, [a, b], False)
        self.assertEqual(1, len(created))
        self.assertEqual(["union", "union"], [f.kind for f in fittings])
        self.assertTrue(a.connectors[1].IsConnected and b.connectors[0].IsConnected)

    def test_join_refuses_near_straight_kink(self):
        doc = FakeDoc()
        a = self.make_tray(doc, 0, 4000)
        b = FakeTray(doc, XYZ(ft(4000), 0, ft(3000)), XYZ(ft(8000), ft(70), ft(3000)), doc.log)  # ~1 deg
        with self.assertRaisesRegex(self.mod.UserError, "less than"):
            self.mod.join(doc, a, b, XYZ(ft(4000), 0, ft(3000)))
        self.assertEqual([], [e for e in doc.log if e[0] in ("union", "elbow")])

    def test_free_end_detour_moves_that_end_without_far_piece(self):
        doc = FakeDoc()
        source = self.make_tray(doc, 0, 5200)
        plan = logic.plan_detour(tray(x1=5200, ends_connected=(True, False)), [BEAM], [], params())
        created, fittings, _ = self.mod.build_route(doc, plan, [source], False)
        self.assertEqual(len(plan["segments"]), len(created))
        self.assertEqual(len(plan["segments"]), len(fittings))
        self.assertEqual(0, mm(source.Location.Curve.a.X))
        self.assertAlmostEqual(mm(plan["segments"][0]["start"][0]), mm(source.Location.Curve.b.X), 6)
        self.assertFalse(created[-1].connectors[1].IsConnected)  # the shifted end stays free

    def test_whole_free_tray_shift_moves_the_tray_itself(self):
        doc = FakeDoc()
        source = self.make_tray(doc, 0, 5200)
        wide = box(-500, 6000, -1500, 1500, 2950, 3200)
        plan = logic.plan_detour(tray(x1=5200), [wide], [], params(direction="Under"))
        created, fittings, _ = self.mod.build_route(doc, plan, [source], False)
        self.assertEqual(([], []), (created, fittings))
        self.assertAlmostEqual(mm(plan["segments"][0]["start"][2]), mm(source.Location.Curve.a.Z), 6)
        self.assertLess(mm(source.Location.Curve.a.Z), 3000)

    def make_tee_case(self):
        doc = FakeDoc()
        branch = self.make_tray(doc, 0, 3000)
        main = FakeTray(doc, XYZ(ft(6000), ft(-5000), ft(3000)), XYZ(ft(6000), ft(5000), ft(3000)), doc.log)
        beyond = FakeTray(doc, XYZ(ft(6000), ft(5000), ft(3000)), XYZ(ft(6000), ft(8000), ft(3000)), doc.log)
        main.connectors[1].ConnectTo(beyond.connectors[0])
        plan = logic.plan_bridge(tray(0, 3000, ends_connected=(True, False)),
                                 ytray(-5000, 5000, ends_connected=(True, True)), [], params())
        return doc, branch, main, beyond, plan

    def test_tee_bridge_splits_main_and_places_tee(self):
        doc, branch, main, beyond, plan = self.make_tee_case()
        created, fittings, warnings = self.mod.build_route(doc, plan, [branch, main], False)
        self.assertEqual([], warnings)
        second = created[-1]
        self.assertEqual(0, mm(main.Location.Curve.b.Y))
        self.assertEqual((0, 5000), (mm(second.Location.Curve.a.Y), mm(second.Location.Curve.b.Y)))
        self.assertIn(beyond.connectors[0], second.connectors[1].refs)  # far neighbour moved to the new half
        tee = fittings[-1]
        self.assertEqual("tee", tee.kind)
        owners = [c.refs[0].Owner for c in tee.connectors]
        self.assertEqual([main, second, created[-2]], owners)

    def test_tee_bridge_with_main_picked_first_uses_swapped_order(self):
        doc, branch, main, _, _ = self.make_tee_case()
        plan = logic.plan_bridge(ytray(-5000, 5000, ends_connected=(True, True)),
                                 tray(0, 3000, ends_connected=(True, False)), [], params())
        created, fittings, _ = self.mod.build_route(doc, plan, [main, branch], False)
        self.assertEqual("tee", fittings[-1].kind)
        self.assertEqual(0, mm(main.Location.Curve.b.Y))

    def test_tee_revert_restores_main_and_its_far_connection(self):
        doc, branch, main, beyond, plan = self.make_tee_case()
        sources = [branch, main]
        infos = [{"start": self.mod.xyz_tuple(t.Location.Curve.a), "end": self.mod.xyz_tuple(t.Location.Curve.b)}
                 for t in sources]
        connections = [[self.mod.end_connections(t, self.mod.to_xyz(i[k])) for k in ("start", "end")]
                       for t, i in zip(sources, infos)]
        created, fittings, _ = self.mod.build_route(doc, plan, sources, False)
        record = {"created": created + fittings, "sources": sources, "infos": infos, "connections": connections}
        with patch.object(self.mod, "tray_and_fitting_ids", return_value=set()):
            self.mod.revert_route(doc, record)
        self.assertEqual((-5000, 5000), (mm(main.Location.Curve.a.Y), mm(main.Location.Curve.b.Y)))
        self.assertIn(beyond.connectors[0], main.connectors[1].refs)
        self.assertEqual(3000, mm(branch.Location.Curve.b.X))
        self.assertTrue(all(not e.IsValidObject for e in created + fittings))

    def make_run(self, doc):
        source = self.make_tray(doc, 0, 5200)
        cut = self.make_tray(doc, 5200, 9000)
        union = Fitting(doc, "union", source.connectors[1], cut.connectors[0])
        return source, cut, union

    def test_read_run_walks_fittings_and_stops_at_free_end_or_tee(self):
        doc = FakeDoc()
        source, cut, union = self.make_run(doc)
        with patch.object(self.mod, "read_tray", side_effect=lambda t: {
                "id": t.Id, "start": self.mod.xyz_tuple(t.Location.Curve.a),
                "end": self.mod.xyz_tuple(t.Location.Curve.b)}):
            run = self.mod.read_run(source, (ft(5200), 0, ft(3000)))
            self.assertEqual(["fitting", "tray"], [i["kind"] for i in run["items"]])
            self.assertIs(cut.Id, run["items"][1]["id"])
            self.assertEqual(5200, mm(run["items"][1]["near"][0]))
            self.assertEqual(u"a free end", run["stop"])
            other = self.make_tray(doc, 9000, 12000)
            Fitting(doc, "tee", cut.connectors[1], other.connectors[0], other.connectors[0])
            self.assertIn(u"a tee or cross", self.mod.read_run(source, (ft(5200), 0, ft(3000)))["stop"])

    def test_run_reroute_removes_run_and_joins_cut_straight(self):
        doc = FakeDoc()
        source, cut, union = self.make_run(doc)
        t = tray(0, 5200, ends_connected=(True, True))
        straight = tray(5200, 9000, ends_connected=(True, True), id_value=7)
        straight.update(kind="tray", near=straight["start"], label="Next", id=cut.Id)
        t["runs"] = {"end": {"items": [{"kind": "fitting", "id": union.Id, "id_value": 6, "label": "Union"},
                                       straight], "stop": None}}
        plan = logic.plan_detour(t, [BEAM], [], params())
        self.assertTrue(plan["run_reroute"])
        created, fittings, warnings = self.mod.build_route(doc, plan, [source, cut], False)
        self.assertEqual([], warnings)
        self.assertFalse(union.IsValidObject)
        self.assertLess(mm(source.Location.Curve.b.X), 4700)
        self.assertGreater(mm(cut.Location.Curve.a.X), 5300)
        self.assertEqual(9000, mm(cut.Location.Curve.b.X))
        self.assertEqual(len(plan["segments"]) + 1, len(fittings))
        self.assertTrue(source.connectors[1].IsConnected and cut.connectors[0].IsConnected)

    def test_bridge_status_explains_tee_or_end_join(self):
        a = tray(0, 3000, ends_connected=(True, False))
        b = ytray(-5000, 5000)
        self.assertIn(u"Tee into the side of Main", self.mod.bridge_status([a, b]))
        b["pick_point"] = (ft(6000), ft(4900), ft(3000))
        self.assertIn(u"end-to-end", self.mod.bridge_status([a, b]))

    def prepare_retry(self):
        doc = FakeDoc()
        doc.IsValidObject, doc.Title = True, "Proj"
        doc.Equals = lambda other: other is doc
        self.w.doc = doc
        source = self.make_tray(doc, 0, 10000)
        neighbour = self.make_tray(doc, 10000, 15000)
        source.connectors[1].ConnectTo(neighbour.connectors[0])

        def snapshot(t):
            info = tray(id_value=t.Id.Value)
            info.update(id=t.Id, label="Tray", start=self.mod.xyz_tuple(t.Location.Curve.GetEndPoint(0)),
                        end=self.mod.xyz_tuple(t.Location.Curve.GetEndPoint(1)))
            info["ends"] = [{"point": self.mod.xyz_tuple(c.Origin), "connected": c.IsConnected}
                            for c in t.connectors]
            return info

        patcher = patch.object(self.mod, "read_tray", side_effect=snapshot)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.revit.Transaction.return_value = MagicMock()
        self.w.tray, self.w.bridge, self.w.mode = snapshot(source), [], logic.MODE_DETOUR
        self.w.obstacles = [{"segments": BEAM, "key": (None, 3), "label": "Beam [3]"}]
        self.w.cut_points = []
        self.w.last_created, self.w.revert_record = [], None
        self.w.change_application, self.w.own_change = None, False
        self.w.btnRevert = SimpleNamespace(IsEnabled=False)
        self.w.refresh_obstacles, self.w.persist_settings = Mock(), Mock()
        self.w.obstacle_source = Mock(return_value=("beam", None, None))
        self.clashes = patch.object(self.mod, "route_clashes", return_value=([], 0))
        self.clashes.start()
        self.addCleanup(self.clashes.stop)
        ids = patch.object(self.mod, "tray_and_fitting_ids", side_effect=lambda d, _: set(
            k for k, e in d.elements.items() if isinstance(e, (FakeTray, Fitting))))
        ids.start()
        self.addCleanup(ids.stop)
        self.measure = patch.object(self.mod, "measure_takeup", return_value={})
        self.measure.start()
        self.addCleanup(self.measure.stop)
        self.w.update_preview, self.w.update_mode_ui = Mock(), Mock()
        app = SimpleNamespace(ActiveUIDocument=SimpleNamespace(Document=doc, Selection=Mock()),
                              Application=SimpleNamespace(DocumentChanged=EventHook()))
        return doc, source, neighbour, app

    def test_apply_revert_and_retry_retains_selection_and_restores_connections(self):
        doc, source, neighbour, app = self.prepare_retry()
        self.w.do_apply(app, dict(logic.DEFAULT_PARAMS))
        created = list(self.w.last_created)
        self.assertTrue(self.w.btnRevert.IsEnabled)
        self.assertEqual(1, len(app.Application.DocumentChanged.listeners))
        self.assertLess(mm(source.Location.Curve.GetEndPoint(1).X), 10000)
        obstacles = list(self.w.obstacles)
        self.w.do_revert(app)
        self.assertEqual(10000, mm(source.Location.Curve.GetEndPoint(1).X))
        self.assertIn(neighbour.connectors[0], source.connectors[1].AllRefs)
        self.assertTrue(all(not e.IsValidObject for e in created))
        self.assertTrue(source.IsValidObject and neighbour.IsValidObject)
        self.assertEqual(obstacles, self.w.obstacles)
        self.assertFalse(self.w.btnRevert.IsEnabled)
        self.w.do_apply(app, dict(logic.DEFAULT_PARAMS, direction=logic.OVER))
        self.assertTrue(self.w.btnRevert.IsEnabled)
        self.assertEqual(1, len(app.Application.DocumentChanged.listeners))

    def test_revert_restores_picked_cut_points_even_after_selection_changes(self):
        _, _, _, app = self.prepare_retry()
        cuts = [(ft(4500), 0, ft(3000)), (ft(5500), 0, ft(3000))]
        self.w.cut_points = list(cuts)
        self.w.do_apply(app, dict(logic.DEFAULT_PARAMS, direction=logic.UNDER, auto_offset=False))
        self.w.obstacles = []
        self.w.mode = logic.MODE_BRIDGE
        self.w.do_revert(app)
        self.assertEqual(cuts, self.w.cut_points)
        self.assertEqual(logic.MODE_DETOUR, self.w.mode)
        self.assertEqual(1, len(self.w.obstacles))

    def test_external_model_change_invalidates_revert_but_own_changes_do_not(self):
        doc, _, _, app = self.prepare_retry()
        self.w.do_apply(app, dict(logic.DEFAULT_PARAMS))

        def event(operation, *names):
            return SimpleNamespace(GetDocument=lambda: doc, Operation=operation,
                                   GetTransactionNames=lambda: list(names))

        self.w.own_change = True
        self.w.on_document_changed(None, event("TransactionCommitted", "Move"))
        self.assertIsNotNone(self.w.revert_record)
        self.w.own_change = False
        # Revit may raise the event after the flag is reset: the tool's own transaction is still ignored.
        self.w.on_document_changed(None, event("TransactionCommitted", "Cable Tray Coordinator: Over"))
        self.w.on_document_changed(None, event("TransactionRolledBack", "Move"))
        self.assertIsNotNone(self.w.revert_record)
        self.w.on_document_changed(None, event("TransactionUndone", "Cable Tray Coordinator: Over"))
        self.assertIsNone(self.w.revert_record)
        self.assertFalse(self.w.btnRevert.IsEnabled)
        with self.assertRaisesRegex(self.mod.UserError, "No unchanged"):
            self.w.do_revert(app)

    def test_run_reroute_revert_posts_revit_undo_and_restores_picks(self):
        doc, source, _, app = self.prepare_retry()
        self.w.do_apply(app, dict(logic.DEFAULT_PARAMS))
        self.w.revert_record["undo"] = True
        self.mod.UI.RevitCommandId = SimpleNamespace(LookupPostableCommandId=lambda c: "undo:" + c)
        self.mod.UI.PostableCommand = SimpleNamespace(Undo="Undo")
        app.CanPostCommand = Mock(return_value=True)
        app.PostCommand = Mock()
        self.w.do_revert(app)
        app.PostCommand.assert_called_once_with("undo:Undo")
        self.assertIsNotNone(self.w.revert_record)  # nothing restored until Revit has undone it
        self.w.on_document_changed(None, SimpleNamespace(GetDocument=lambda: doc, Operation="TransactionUndone",
                                                         GetTransactionNames=lambda: []))
        self.assertIsNone(self.w.revert_record)
        self.assertIsNone(self.w.pending_undo)
        self.assertEqual(logic.MODE_DETOUR, self.w.mode)
        self.assertIn("reverted", self.w.status[-1])

    def test_revert_tolerates_internal_cascade_but_not_user_elements(self):
        doc, source, neighbour, app = self.prepare_retry()
        self.w.do_apply(app, dict(logic.DEFAULT_PARAMS))
        delete = doc.Delete
        doc.Delete = lambda ids: delete(ids) + [SimpleNamespace(Value=99999)]
        self.w.do_revert(app)
        self.assertEqual(10000, mm(source.Location.Curve.GetEndPoint(1).X))

    def test_real_obstacle_clash_rolls_back_apply(self):
        _, _, _, app = self.prepare_retry()
        self.clashes.stop()
        with patch.object(self.mod, "route_clashes", return_value=(["Beam [3]"], 0)):
            with self.assertRaisesRegex(self.mod.UserError, "clashes with: Beam"):
                self.w.do_apply(app, dict(logic.DEFAULT_PARAMS))
        self.clashes.start()
        self.assertIsNone(self.w.revert_record)

    def test_route_clashes_uses_solid_intersection_volume(self):
        solids = {"tray": ["t"], "beam": ["b"], "far": ["f"]}
        bounds = {"t": [(0, 1)] * 3, "b": [(0.5, 2)] * 3, "f": [(5, 6)] * 3}
        boolean = Mock(return_value=SimpleNamespace(Volume=0.01))
        db = self.mod.DB
        db.BooleanOperationsUtils = SimpleNamespace(ExecuteBooleanOperation=boolean)
        db.BooleanOperationsType = SimpleNamespace(Intersect="Intersect")
        with patch.object(self.mod, "element_solids", side_effect=lambda e, tf=None: solids[e]), \
                patch.object(self.mod, "solid_bounds", side_effect=lambda s: bounds[s]):
            self.assertEqual((["Beam"], 0), self.mod.route_clashes(["tray"], [("Beam", "beam", None),
                                                                               ("Far", "far", None)]))
            boolean.assert_called_once_with("t", "b", "Intersect")
            boolean.return_value = SimpleNamespace(Volume=1e-9)
            self.assertEqual(([], 0), self.mod.route_clashes(["tray"], [("Beam", "beam", None)]))
            boolean.side_effect = RuntimeError("boolean failed")
            self.assertEqual(([], 1), self.mod.route_clashes(["tray"], [("Beam", "beam", None)]))

    def test_revert_refuses_cascade_deletion_outside_route(self):
        doc, _, neighbour, app = self.prepare_retry()
        self.w.do_apply(app, dict(logic.DEFAULT_PARAMS))
        delete = doc.Delete
        doc.Delete = lambda ids: delete(ids) + [neighbour.Id]
        with self.assertRaisesRegex(self.mod.UserError, "outside the last route"):
            self.w.do_revert(app)

    def test_revert_bridge_restores_both_sources(self):
        doc, a, _, app = self.prepare_retry()
        # Replace the detour inputs with an unconnected gap.
        self.mod.detach_end(doc, a, a.Location.Curve.GetEndPoint(1))
        a.Location.Curve = Line(XYZ(0, 0, ft(3000)), XYZ(ft(4000), 0, ft(3000)))
        b = self.make_tray(doc, 6000, 10000)
        infos = [self.mod.read_tray(t) for t in (a, b)]
        self.w.mode, self.w.tray, self.w.bridge = logic.MODE_BRIDGE, None, infos
        self.w.obstacles = []
        self.w.do_apply(app, dict(logic.DEFAULT_PARAMS))
        # Simulate fitting take-up of both original ends during the same tool transaction.
        a.Location.Curve = Line(a.Location.Curve.a, XYZ(ft(3900), 0, ft(3000)))
        b.Location.Curve = Line(XYZ(ft(6100), 0, ft(3000)), b.Location.Curve.b)
        self.w.do_revert(app)
        self.assertEqual(4000, mm(a.Location.Curve.b.X))
        self.assertEqual(6000, mm(b.Location.Curve.a.X))
        self.assertFalse(a.connectors[1].IsConnected or b.connectors[0].IsConnected)

    def test_fitting_takeup_shorter_than_minimum_is_learned_and_retry_fits(self):
        _, _, _, app = self.prepare_retry()
        self.mod.read_tray.side_effect = (lambda f: lambda t: dict(f(t), takeup_key="k"))(
            self.mod.read_tray.side_effect)
        build = self.mod.build_route
        measured = {}

        def shortening(*args):
            created, fittings, warnings = build(*args)
            target = min(created, key=self.mod.curve_length)
            curve = target.Location.Curve
            length = curve.a.DistanceTo(curve.b)
            measured.setdefault("takeup", length - ft(50))
            keep = (length - measured["takeup"]) / length
            target.Location.Curve = Line(curve.a, XYZ(*[a + (b - a) * keep for a, b in
                                                        zip((curve.a.X, curve.a.Y, curve.a.Z),
                                                            (curve.b.X, curve.b.Y, curve.b.Z))]))
            measured["count"] = self.mod.fittings_on(target, set(f.Id.Value for f in fittings))
            return created, fittings, warnings

        params = dict(logic.DEFAULT_PARAMS)
        self.w.txtMinStraight = SimpleNamespace(Text="unchanged")
        with patch.object(self.mod, "build_route", side_effect=shortening):
            with self.assertRaisesRegex(self.mod.UserError, "preview now allows") as ctx:
                self.w.do_apply(app, params)
            self.assertIsNone(self.w.revert_record)
            self.assertEqual(2, measured["count"])
            self.assertAlmostEqual(measured["takeup"] / 2, self.w.takeup["k"][45])
            self.w.update_preview.reset_mock()
            self.w.on_action_error("Apply", ctx.exception)
            self.w.update_preview.assert_called_once()
            self.assertEqual("unchanged", self.w.txtMinStraight.Text)
            self.w.persist_settings.assert_not_called()
        # Revit rolls the failed Apply back; the next plan reserves the learned take-up per straight.
        plan = self.w.compute_plan(params, [dict(self.w.tray, takeup_key="k")])
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["takeup_measured"])
        self.assertAlmostEqual(measured["takeup"], plan["fitting_allowance"])

    def test_known_takeup_still_too_short_does_not_loop(self):
        self.w.takeup = {"k": {90: ft(400)}}
        error = self.w.short_straight_error(dict(logic.DEFAULT_PARAMS),
                                            [(SimpleNamespace(Id=SimpleNamespace(Value=7)), ft(80), ft(300), True)],
                                            [90], [dict(tray(), takeup_key="k")])
        self.assertFalse(error.refresh_preview)
        self.assertIn("another bend angle", str(error))
        self.assertEqual(ft(400), self.w.takeup["k"][90])

    def test_short_existing_bridge_tray_is_named_not_auto_fixed(self):
        error = self.w.short_straight_error(dict(logic.DEFAULT_PARAMS),
                                            [(SimpleNamespace(Id=SimpleNamespace(Value=7)), ft(80), ft(220), False)],
                                            [90], [tray()])
        self.assertFalse(error.refresh_preview)
        self.assertEqual({}, self.w.takeup)
        self.assertIn("Existing tray 7 would be left 80 mm", str(error))
        self.assertIn("lower Min. straight to 80 mm", str(error))

    def test_regenerate_test_builds_are_always_rolled_back_and_fill_the_option_list(self):
        w = self.w
        w.mode, w.tray, w.bridge, w.cut_points = logic.MODE_DETOUR, tray(), [], []
        w.obstacles = [{"segments": BEAM, "key": (None, 3), "label": "Beam [3]"}]
        w.fresh_trays, w.refresh_obstacles, w.ensure_takeup = Mock(return_value=["T"]), Mock(), Mock()
        w.active_uidoc = Mock(return_value=SimpleNamespace(Document=self.doc))
        w.update_mode_ui, w.update_preview = Mock(), Mock()
        w.compute_plan = lambda p, infos: logic.plan_with_fittings(
            lambda q: logic.plan_detour(tray(), [BEAM], [], q), logic.params_in_feet(p))
        w.options, w.options_key = [], None
        w.lstOptions = SimpleNamespace(Items=Mock(), SelectedIndex=-1, IsEnabled=True)
        w.lblOptions, w.btnRegenerate = SimpleNamespace(Text=u""), SimpleNamespace(IsEnabled=True)
        w.set_params = Mock()
        transactions = []
        self.mod.DB.Transaction = lambda doc, name: transactions.append(Mock()) or transactions[-1]
        builds = []

        def build(doc, plan, params, trays, infos, learn=True):
            self.assertFalse(learn)
            self.assertTrue(w.own_change)
            builds.append(plan)
            if len(builds) == 1:
                raise self.mod.UserError(u"The built route clashes with: Wall")
        w.checked_build = build
        with patch.object(self.mod, "read_detour_tray", return_value=tray()):
            w.do_regenerate(None, dict(logic.DEFAULT_PARAMS, clearance_mm=75))
        self.assertEqual(len(builds), len(transactions))
        for t in transactions:
            t.RollBack.assert_called_once_with()
            t.Commit.assert_not_called()
        self.assertFalse(w.own_change)
        self.assertEqual(logic.OPTION_COUNT, len(w.options))
        self.assertEqual(logic.OPTION_COUNT, w.lstOptions.Items.Add.call_count)
        self.assertIn(u"1 route(s) dropped after a test build (The built route clashes with: Wall)",
                      w.lblOptions.Text)
        self.assertEqual(0, w.lstOptions.SelectedIndex)
        w.select_option(2)
        w.set_params.assert_called_once_with(logic.normalize_params(w.options[2]["params"]))
        self.assertEqual(w.options_key, w.selection_key())
        w.obstacles = []
        self.assertNotEqual(w.options_key, w.selection_key())

    def test_relaxed_option_asks_before_apply_and_is_never_remembered(self):
        w = self.w
        user = dict(logic.DEFAULT_PARAMS, shorten_fitting_legs=False, min_bend_radius_mm=0, min_straight_mm=300)
        relaxed, text = logic.relaxed_variants([dict(user)], user)
        w.own_limits = logic.own_limits(user)
        w.options = [{"number": 1, "relaxed": text, "params": relaxed[0], "label": u"x"}]
        w.lstOptions = SimpleNamespace(SelectedIndex=0)
        w.read_params = Mock(return_value=dict(relaxed[0]))
        w.plan, w.run_in_revit, w.set_status = {"ok": True}, Mock(), Mock()
        self.forms.alert.return_value = False
        w.apply_clicked(None, None)
        w.run_in_revit.assert_not_called()
        self.assertIn(u"RELAXED", self.forms.alert.call_args[0][0])
        self.forms.alert.return_value = True
        w.apply_clicked(None, None)
        w.run_in_revit.assert_called_once()
        w.user_presets = []
        with patch.object(self.mod, "save_settings") as save:
            w.persist_settings()
        saved = save.call_args[0][0]["params"]
        self.assertEqual((300.0, 0.0, False), (saved["min_straight_mm"], saved["min_bend_radius_mm"],
                                               saved["shorten_fitting_legs"]))
        w.read_params.return_value = dict(relaxed[0], min_straight_mm=120)
        self.forms.alert.reset_mock()
        w.apply_clicked(None, None)
        self.forms.alert.assert_not_called()
        with patch.object(self.mod, "save_settings") as save:
            w.persist_settings()
        self.assertEqual(120, save.call_args[0][0]["params"]["min_straight_mm"])

    def test_takeup_is_measured_once_per_tray_type_and_size(self):
        self.w.own_change = False
        source = SimpleNamespace()
        with patch.object(self.mod, "takeup_key", return_value=(5, 300, 100)), \
                patch.object(self.mod, "measure_takeup", return_value={90: ft(430)}) as measure:
            self.w.ensure_takeup(self.doc, [source, source])
            measure.assert_called_once_with(self.doc, source)
        self.assertEqual({90: ft(430)}, self.w.takeup_for([{"takeup_key": (5, 300, 100)}, {}]))
        self.assertFalse(self.w.own_change)
        with patch.object(self.mod, "takeup_key", return_value=(6, 300, 100)), \
                patch.object(self.mod, "measure_takeup", side_effect=RuntimeError("no fitting")):
            self.w.ensure_takeup(self.doc, [source])
        self.assertEqual({}, self.w.takeup[(6, 300, 100)])
        self.assertFalse(self.w.own_change)

    def test_bridge_pulls_back_trimmed_source_end(self):
        doc, a, _, app = self.prepare_retry()
        self.mod.detach_end(doc, a, a.Location.Curve.GetEndPoint(1))
        a.Location.Curve = Line(XYZ(0, 0, ft(3000)), XYZ(ft(4000), 0, ft(3000)))
        b = FakeTray(doc, XYZ(ft(7000), 0, ft(4000)), XYZ(ft(10000), 0, ft(4000)), doc.log)
        infos = [self.mod.read_tray(t) for t in (a, b)]
        infos[1].update(start=(ft(7000), 0.0, ft(4000)), end=(ft(10000), 0.0, ft(4000)))
        infos[1]["ends"] = [{"point": infos[1][k], "connected": False} for k in ("start", "end")]
        obstacle = box_faces(3900, 4100, -100, 100, 2900, 3100)
        plan = logic.plan_bridge(infos[0], infos[1], [obstacle], params())
        self.assertTrue(plan["ok"], plan["error"])
        trim = plan["trims"][0]
        self.assertGreater(trim, 0)
        created, _, _ = self.mod.build_route(doc, plan, [a, b], False)
        self.assertAlmostEqual(ft(4000) - trim, a.Location.Curve.b.X)
        self.assertEqual(0, a.Location.Curve.a.X)
        self.assertAlmostEqual(a.Location.Curve.b.X, created[0].Location.Curve.a.X)
        self.assertAlmostEqual(ft(7000) + plan["trims"][1], b.Location.Curve.a.X)

    def test_existing_short_source_tray_is_not_blamed(self):
        doc, source, neighbour, app = self.prepare_retry()
        a = self.make_tray(doc, 3950, 4000)
        b = self.make_tray(doc, 6000, 10000)
        self.mod.detach_end(doc, source, source.Location.Curve.GetEndPoint(1))
        infos = [self.mod.read_tray(t) for t in (a, b)]
        self.w.mode, self.w.tray, self.w.bridge, self.w.obstacles = logic.MODE_BRIDGE, None, infos, []
        self.w.do_apply(app, dict(logic.DEFAULT_PARAMS))
        self.assertIsNotNone(self.w.revert_record)

    def test_user_preset_applies_minimum_while_builtin_preserves_it(self):
        self.w.lstPresets = SimpleNamespace(SelectedItem="Mine")
        self.w.lblPresetInfo = SimpleNamespace(Text="")
        self.w.read_params = lambda: dict(logic.DEFAULT_PARAMS, min_straight_mm=350)
        self.w.update_preview = Mock()
        self.w.set_params = Mock()
        preset, _ = logic.make_user_preset("Mine", dict(logic.DEFAULT_PARAMS, min_straight_mm=500), [])
        self.w.user_presets = [preset]
        self.w.preset_changed(None, None)
        self.assertEqual(500, self.w.set_params.call_args[0][0]["min_straight_mm"])
        self.w.lstPresets.SelectedItem = logic.BUILTIN_PRESETS[0]["name"]
        self.w.preset_changed(None, None)
        self.assertEqual(350, self.w.set_params.call_args[0][0]["min_straight_mm"])

    def test_close_unsubscribes_document_change_guard(self):
        _, _, _, app = self.prepare_retry()
        self.w.do_apply(app, dict(logic.DEFAULT_PARAMS))
        self.w.on_closed(None, None)
        self.assertEqual([], app.Application.DocumentChanged.listeners)
        self.assertTrue(self.w.closed)

    def test_rolled_back_commit_does_not_offer_revert(self):
        _, _, _, app = self.prepare_retry()
        build = self.mod.build_route

        def rolled_back(*args):
            created, fittings, warnings = build(*args)
            created[0].IsValidObject = False
            return created, fittings, warnings

        with patch.object(self.mod, "build_route", side_effect=rolled_back):
            with self.assertRaisesRegex(self.mod.UserError, "rolled the change back"):
                self.w.do_apply(app, dict(logic.DEFAULT_PARAMS))
        self.assertIsNone(self.w.revert_record)
        self.assertFalse(self.w.btnRevert.IsEnabled)

    def test_current_selection_keeps_all_obstacles_not_only_first_twenty(self):
        _, source, _, _ = self.prepare_retry()
        obstacles = [SimpleNamespace(Category=object(), Id=SimpleNamespace(Value=i)) for i in range(25)]
        uidoc = SimpleNamespace(Document=SimpleNamespace(GetElement=lambda element: element),
                               Selection=SimpleNamespace(GetElementIds=lambda: [source] + obstacles))
        with patch.object(self.mod, "read_obstacle", side_effect=lambda e: {"key": (None, e.Id.Value)}):
            count = self.w.load_selection(uidoc)
        self.assertEqual((1, 25), count)
        self.assertEqual(25, len(self.w.obstacles))

    def test_apply_refreshes_host_and_link_obstacles(self):
        host_id, linked_id, link_id = object(), object(), object()
        host, linked = object(), object()
        link_doc = SimpleNamespace(Title="Structure", GetElement=lambda eid: linked if eid is linked_id else None)
        link = SimpleNamespace(Id=SimpleNamespace(Value=77), GetLinkDocument=lambda: link_doc,
                               GetTotalTransform=lambda: "transform")
        doc = SimpleNamespace(GetElement=lambda eid: link if eid is link_id else host if eid is host_id else None)
        self.w.obstacles = [{"element_id": host_id, "link_id": None},
                            {"element_id": linked_id, "link_id": link_id}]
        with patch.object(self.mod, "read_obstacle", side_effect=[{"segments": ["new-host"]},
                                                                  {"segments": ["new-linked"]}]) as read:
            self.mod.CableTrayCoordinator.refresh_obstacles(self.w, doc)
        self.assertEqual(["new-host"], self.w.obstacles[0]["segments"])
        self.assertEqual(["new-linked"], self.w.obstacles[1]["segments"])
        self.assertEqual((linked, "transform", "Structure", 77), read.call_args_list[1][0])

    def test_missing_link_obstacle_is_an_explicit_error(self):
        self.w.obstacles = [{"element_id": object(), "link_id": object()}]
        with self.assertRaisesRegex(self.mod.UserError, "missing or unloaded"):
            self.mod.CableTrayCoordinator.refresh_obstacles(
                self.w, SimpleNamespace(GetElement=lambda _: None))


if __name__ == "__main__":
    unittest.main()
