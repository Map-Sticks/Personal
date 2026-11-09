"""Unit tests for the pure Draw Switchlines layout logic (no Revit required)."""
import importlib.util
from pathlib import Path
import unittest

LIB = (Path(__file__).resolve().parents[1] / "CustomPyRevit.extension" / "ETC PyTools.tab"
       / "Electrical.panel" / "Draw Switchlines.pushbutton" / "lib" / "switchline_logic.py")
_spec = importlib.util.spec_from_file_location("switchline_logic", LIB)
L = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(L)


def fit(key, u, v):
    return L.Node(key, "fitting", u, v)


def wall(key, u, v, facing):
    return L.Node(key, "switch", u, v, facing=facing, wall=True)


def sensor(key, u, v):
    return L.Node(key, "switch", u, v)


def axis_aligned(seg):
    (a, b) = seg
    return abs(a[0] - b[0]) < L.TOL or abs(a[1] - b[1]) < L.TOL


def near(p, q, tol=1e-3):
    return abs(p[0] - q[0]) < tol and abs(p[1] - q[1]) < tol


class HelperTests(unittest.TestCase):
    def test_paper_to_model_matches_bedroom_gap(self):
        self.assertAlmostEqual(L.paper_to_model(3.0, 50), 150 / 304.8)

    def test_ceiling_device_keywords(self):
        self.assertTrue(L.is_ceiling_device("Occupancy Sensor 360 Recessed : x"))
        self.assertTrue(L.is_ceiling_device("PIR Detector"))
        self.assertFalse(L.is_ceiling_device("Switch - Vertical - 1Way - Single Gang : ESS"))
        self.assertFalse(L.is_ceiling_device(None))

    def test_alternate_split_orders_along_row(self):
        fittings = [fit(i, u, 0) for i, u in enumerate([30, 0, 20, 10, 40])]
        a, b = L.alternate_split(fittings)
        self.assertEqual([f.u for f in a], [0, 20, 40])
        self.assertEqual([f.u for f in b], [10, 30])

    def test_trim_path_shortens_both_ends(self):
        path = L.trim_path([(0, 0), (10, 0), (10, 10)], 1.0, 2.0)
        self.assertEqual(path, [(1.0, 0.0), (10, 0), (10.0, 8.0)])

    def test_merge_collinear_joins_overlaps_only(self):
        merged = L.merge_collinear([((0, 0), (5, 0)), ((4, 0), (8, 0)), ((10, 0), (12, 0))])
        self.assertEqual(sorted(merged), [((0, 0), (8, 0)), ((10, 0), (12, 0))])

    def test_junction_found_at_tee_not_at_corner(self):
        segs = [((0, 0), (10, 0)), ((5, 0), (5, 5)), ((10, 0), (10, 5))]
        joints = L.junctions(segs)
        self.assertEqual(len(joints), 1)
        self.assertTrue(near(joints[0], (5, 0)))

    def test_hop_splits_new_line_over_existing(self):
        lines, arcs = L.apply_hops([((0, 0), (10, 0))], [((5, -3), (5, 3))], 0.5)
        self.assertEqual(lines, [((0, 0), (4.5, 0)), ((5.5, 0), (10, 0))])
        self.assertEqual(arcs, [((4.5, 0), (5.5, 0), (5, 0.5))])

    def test_new_vertical_crossing_new_horizontal_hops_horizontal(self):
        lines, arcs = L.apply_hops([((5, -3), (5, 3)), ((0, 0), (10, 0))], [], 0.5)
        self.assertEqual(len(arcs), 1)
        self.assertIn(((5, -3), (5, 3)), lines)

    def test_chain_avoids_passing_through_other_fitting(self):
        # L from (0,0) to (10,10) horizontal-first would pass through (5,0).
        f = [fit(1, 0, 0), fit(2, 10, 10)]
        segs = L.chain_network(f, 0.5, True, obstacles=[(0, 0), (10, 10), (5, 0)])
        self.assertTrue(any(abs(a[0]) < L.TOL and abs(b[0]) < L.TOL for a, b in segs))


class LayoutTests(unittest.TestCase):
    def test_every_segment_is_orthogonal_and_stops_short_of_fittings(self):
        f = [fit(1, 0, 0), fit(2, 0, 10), fit(3, 8, 10)]
        lay = L.build_layout([wall(9, -5, 2, (1, 0))], f, scale=50)
        gap = L.paper_to_model(3.0, 50)
        for seg in lay["lines"]:
            self.assertTrue(axis_aligned(seg))
            for p in seg:
                for node in f:
                    self.assertGreater(L.manhattan(p, node.pt), gap - 1e-6)

    def test_stacked_wall_switches_get_separate_lanes(self):
        f = [fit(1, 0, 10), fit(2, 0, 20)]
        lay = L.build_layout([wall(8, 1, 0, (0, 1)), wall(9, 1.3, 0, (0, 1))], f, scale=50)
        horizontals = set(round(a[1], 4) for a, b in lay["lines"] if abs(a[1] - b[1]) < L.TOL)
        self.assertGreaterEqual(len(horizontals), 1)
        self.assertEqual(len(lay["outputs"]), 1)
        self.assertEqual(sorted(lay["outputs"][0]["switches"]), [8, 9])

    def test_corridor_dual_output_sensor_feeds_both_trunks(self):
        f = [fit(i, i * 10.0, 0) for i in range(8)]
        lay = L.build_layout([sensor(100, 15, 0)], f, mode=L.MODE_ALTERNATE, style=L.STYLE_TRUNK,
                             scale=100)
        names = [o["name"] for o in lay["outputs"]]
        self.assertEqual(names, ["A", "B"])
        self.assertEqual(lay["outputs"][0]["fittings"], [0, 2, 4, 6])
        self.assertEqual(lay["outputs"][1]["fittings"], [1, 3, 5, 7])
        for out in lay["outputs"]:
            self.assertEqual(out["switches"], [100])
        offset = L.paper_to_model(6.0, 100)
        trunk_vs = set(round(a[1], 4) for a, b in lay["lines"]
                       if abs(a[1] - b[1]) < L.TOL and abs(a[0] - b[0]) > 20)
        self.assertEqual(trunk_vs, set([round(offset, 4), round(-offset, 4)]))

    def test_two_wall_switches_take_one_output_each(self):
        f = [fit(i, i * 10.0, 0) for i in range(6)]
        walls = [wall(201, -6, -2, (1, 0)), wall(200, -6, 2, (1, 0))]
        lay = L.build_layout(walls, f, mode=L.MODE_ALTERNATE, style=L.STYLE_TRUNK, scale=100)
        self.assertEqual(sorted(o["switches"][0] for o in lay["outputs"]), [200, 201])
        self.assertTrue(all(len(o["switches"]) == 1 for o in lay["outputs"]))

    def test_single_fitting_route_ends_at_gap(self):
        lay = L.build_layout([wall(9, 0, 0, (1, 0))], [fit(1, 10, 0)], scale=50)
        gap = L.paper_to_model(3.0, 50)
        ends = [p for seg in lay["lines"] for p in seg]
        self.assertTrue(any(near(p, (10 - gap, 0)) for p in ends))

    def test_requires_switch_and_fitting(self):
        with self.assertRaises(ValueError):
            L.build_layout([], [fit(1, 0, 0)])
        with self.assertRaises(ValueError):
            L.build_layout([sensor(1, 0, 0)], [])


def total_length(segs):
    return sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in segs)


class SessionTests(unittest.TestCase):
    P = L.resolve_params(50)

    def test_parallel_overlap_and_conflict_cost(self):
        h1 = ((0.0, 0.0), (2.0, 0.0))
        h2 = ((1.0, 0.01), (3.0, 0.01))
        v = ((1.5, -1.0), (1.5, 1.0))
        self.assertAlmostEqual(L.parallel_overlap(h1, h2, 0.05), 1.0)
        self.assertEqual(L.parallel_overlap(h1, v, 0.05), 0.0)
        self.assertEqual(L.conflict_cost([v], [h1], 0.05, 1.0), 1.0)
        self.assertEqual(L.conflict_cost([h1], [h2], 0.05, 1.0), L.OVERLAP_PENALTY)

    def test_close_crossings_merge_into_one_hop(self):
        new = [((0.0, 0.0), (4.0, 0.0))]
        existing = [((2.0, -1.0), (2.0, 1.0)), ((2.05, -1.0), (2.05, 1.0))]
        lines, arcs, line_src, arc_src = L.apply_hops_indexed(new, existing, 0.1)
        self.assertEqual(len(arcs), 1)
        self.assertEqual(line_src, [0, 0])
        self.assertEqual(arc_src, [0])
        start, end, _ = arcs[0]
        self.assertLess(start[0], 2.0)
        self.assertGreater(end[0], 2.05)

    def test_tight_crossing_gets_a_smaller_hop_not_none(self):
        # Both lines end close to the crossing: the hop shrinks to fit instead of being skipped.
        h = ((0.0, 0.0), (0.15, 0.0))
        v = ((0.1, -0.12), (0.1, 1.0))
        lines, arcs, _, _ = L.apply_hops_indexed([h, v], [], 0.3)
        self.assertEqual(len(arcs), 1)
        start, end, _ = arcs[0]
        self.assertTrue(all(axis_aligned(s) for s in lines))
        for p in (start, end):
            self.assertGreaterEqual(p[1], -0.12 - 1e-9)
            self.assertLessEqual(p[1], 1.0)

    def test_wall_banks_cluster_side_by_side_switches(self):
        a = wall(1, 0.0, 0.0, (1.0, 0.0))
        b = wall(2, 0.0, 0.3, (1.0, 0.0))
        c = wall(3, 0.0, 5.0, (1.0, 0.0))
        d = wall(4, 0.0, 0.6, (-1.0, 0.0))
        banks = L.wall_banks([a, b, c, d], self.P)
        self.assertEqual(sorted(sorted(s.key for s in bank) for bank in banks), [[1, 2], [3], [4]])

    def test_occupied_lanes_detects_existing_line_in_front_of_bank(self):
        bank = [wall(1, 0.0, 0.0, (1.0, 0.0)), wall(2, 0.0, 0.3, (1.0, 0.0))]
        u = L.lane_reach(self.P, 0)
        existing = [((u, -1.0), (u, 2.0)), ((5.0, -1.0), (5.0, 2.0))]
        self.assertEqual(L.occupied_lanes(bank, self.P, existing), set([0]))

    def test_bank_of_switch_groups_never_runs_along_the_bank_face(self):
        # Bedroom bed-head bank: five switches on one wall, each its own group/fitting.
        ys = [0.0, 0.29, 0.58, 0.87, 1.16]
        targets = [(3.0, -2.0), (5.0, 8.0), (1.0, 3.0), (4.0, 1.0), (2.0, 6.0)]
        groups = [L.make_group([wall(10 + i, 0.0, y, (1.0, 0.0))], [fit(20 + i, tu, tv)])
                  for i, (y, (tu, tv)) in enumerate(zip(ys, targets))]
        layout = L.build_session(groups, scale=50)
        self.assertEqual(sorted(set(layout["line_groups"])), [0, 1, 2, 3, 4])
        self.assertTrue(all(axis_aligned(s) for s in layout["lines"]))
        first_lane = L.lane_reach(self.P, 0) - self.P['lane'] * 0.5
        for (a, b) in layout["lines"]:
            if abs(a[0] - b[0]) < L.TOL:
                self.assertGreater(a[0], first_lane)
        segs = list(zip(layout["lines"], layout["line_groups"]))
        for i, (s1, g1) in enumerate(segs):
            for s2, g2 in segs[i + 1:]:
                if g1 != g2:
                    self.assertEqual(L.parallel_overlap(s1, s2, self.P['lane'] * 0.5), 0.0)

    def test_two_way_second_switch_tees_into_first_route(self):
        fittings = [fit(1, 0.0, 6.0), fit(2, 3.0, 6.0)]
        one = L.build_session([L.make_group([wall(10, 0.0, 0.0, (0.0, 1.0))], fittings)], scale=50)
        two = L.build_session([L.make_group([wall(10, 0.0, 0.0, (0.0, 1.0)),
                                             wall(11, 0.3, 0.0, (0.0, 1.0))], fittings)], scale=50)
        extra = total_length(two["lines"]) - total_length(one["lines"])
        self.assertLess(extra, 1.0)
        self.assertGreater(len(two["joints"]), len(one["joints"]))

    def test_session_tags_every_item_with_its_group(self):
        g1 = L.make_group([wall(10, 0.0, 0.0, (1.0, 0.0))], [fit(1, 2.0, 2.0)])
        g2 = L.make_group([sensor(11, 6.0, 0.0)], [fit(2, 5.0, 3.0), fit(3, 7.0, 3.0),
                                                   fit(4, 9.0, 3.0), fit(5, 11.0, 3.0)],
                          mode=L.MODE_ALTERNATE)
        layout = L.build_session([g1, g2], scale=50)
        self.assertEqual(len(layout["line_groups"]), len(layout["lines"]))
        self.assertEqual(len(layout["arc_groups"]), len(layout["arcs"]))
        self.assertEqual(len(layout["joint_groups"]), len(layout["joints"]))
        self.assertEqual(sorted(set(layout["line_groups"])), [0, 1])
        names = [(o["group"], o["name"]) for o in layout["outputs"]]
        self.assertEqual(names, [(0, "A"), (1, "A"), (1, "B")])
        self.assertEqual(layout["outputs"][1]["switches"], [11])
        self.assertEqual(layout["outputs"][2]["switches"], [11])
        self.assertEqual(g2["style"], L.STYLE_TRUNK)
        self.assertIn("Group 2 output B", L.describe(layout))

    def test_sensor_in_two_groups_acts_as_dual_output(self):
        s = sensor(11, 4.0, 0.0)
        g1 = L.make_group([s], [fit(1, 0.0, 2.0), fit(3, 4.0, 2.0)])
        g2 = L.make_group([s], [fit(2, 2.0, 2.0), fit(4, 6.0, 2.0)])
        layout = L.build_session([g1, g2], scale=50)
        self.assertEqual([o["switches"] for o in layout["outputs"]], [[11], [11]])

    def test_empty_session_rejected(self):
        with self.assertRaises(ValueError):
            L.build_session([], scale=50)


# The user's "1 BED ROOM, NESTED ENSUITE" test view (x shifted by +830 ft): kind, u, v, facing.
BEDROOM = {
    351: ('fit', 7.287, 11.615, None), 352: ('fit', 2.122, 4.925, None),
    353: ('fit', 2.122, 12.799, None), 354: ('fit', -3.783, 8.862, None),
    355: ('fit', -6.681, 7.638, (1.0, 0.0)), 356: ('wall', -6.681, 5.065, (1.0, 0.0)),
    357: ('wall', -6.681, 4.774, (1.0, 0.0)), 358: ('wall', -6.681, 5.357, (1.0, 0.0)),
    359: ('wall', -6.681, 5.648, (1.0, 0.0)), 360: ('wall', 5.191, 14.501, (-1.0, 0.0)),
    361: ('fit', 10.706, 15.101, None), 362: ('wall', -1.844, 2.510, (0.0, 1.0)),
    363: ('fit', -3.783, 12.799, None), 364: ('fit', -6.912, 10.178, (1.0, 0.0)),
    365: ('fit', -3.783, 4.925, None), 366: ('wall', -6.681, 5.939, (1.0, 0.0)),
    367: ('fit', 7.287, 15.101, None), 368: ('fit', 10.706, 11.615, None),
    369: ('wall', -1.549, 2.510, (0.0, 1.0)), 370: ('sensor', 7.287, 13.517, None),
}
BEDROOM_GROUPS = [([356], [354]), ([366, 369], [365, 352, 353, 363]), ([357, 362], [355]),
                  ([360, 370], [367, 361, 368])]


def bedroom_node(key):
    kind, u, v, facing = BEDROOM[key]
    if kind == 'fit':
        return L.Node(key, "fitting", u, v, facing=facing)
    return L.Node(key, "switch", u, v, facing=facing, wall=kind == 'wall')


def proper_crossing(s1, s2):
    """Point where two perpendicular segments cross inside both (not at an end), else None."""
    h, v = (s1, s2) if abs(s1[0][1] - s1[1][1]) < L.TOL else (s2, s1)
    if abs(h[0][1] - h[1][1]) > L.TOL or abs(v[0][0] - v[1][0]) > L.TOL:
        return None
    x, y = v[0][0], h[0][1]
    lo_x, hi_x = sorted((h[0][0], h[1][0]))
    lo_y, hi_y = sorted((v[0][1], v[1][1]))
    if lo_x + 1e-3 < x < hi_x - 1e-3 and lo_y + 1e-3 < y < hi_y - 1e-3:
        return (x, y)
    return None


class BedroomRoutingTests(unittest.TestCase):
    """The user's bedroom groups: the faults reported on v2 must not come back."""

    @classmethod
    def setUpClass(cls):
        cls.P = L.resolve_params(50)
        cls.nodes = dict((k, bedroom_node(k)) for k in BEDROOM)
        groups = [L.make_group([cls.nodes[k] for k in sw], [cls.nodes[k] for k in fx])
                  for sw, fx in BEDROOM_GROUPS]
        used = set(k for sw, fx in BEDROOM_GROUPS for k in sw + fx)
        others = [cls.nodes[k] for k in BEDROOM if k not in used]
        cls.layout = L.build_session(groups, scale=50, extra_obstacles=others)
        cls.segs = list(zip(cls.layout["lines"], cls.layout["line_groups"]))

    def test_lines_never_pass_through_a_fitting(self):
        r = self.P['fitting_gap'] * 0.9 - 1e-3
        for seg, _ in self.segs:
            for key, (kind, u, v, _) in BEDROOM.items():
                if kind == 'fit':
                    self.assertGreaterEqual(L._point_seg_distance((u, v), seg), r, (key, seg))

    def test_no_line_runs_along_or_behind_the_bed_head_bank(self):
        wall_u = -6.681
        lo, hi = 4.774 - self.P['lane'] * 0.5, 5.939 + self.P['lane'] * 0.5
        face = wall_u + self.P['switch_gap'] + self.P['lane'] * 0.5
        for (a, b), _ in self.segs:
            if max(a[1], b[1]) < lo or min(a[1], b[1]) > hi:
                continue
            self.assertGreaterEqual(min(a[0], b[0]), wall_u + self.P['switch_gap'] - 1e-3)
            if abs(a[0] - b[0]) < L.TOL:
                self.assertGreater(a[0], face, ((a, b)))

    def test_wall_fitting_is_only_reached_from_the_front(self):
        kind, u, v, facing = BEDROOM[355]
        for (a, b), g in self.segs:
            for p in (a, b):
                if abs(p[0] - u) + abs(p[1] - v) < self.P['fitting_gap'] * 1.5:
                    self.assertGreater(p[0], u + 1e-3)

    def test_lines_only_cross_where_a_hop_is_drawn(self):
        for i, (s1, _) in enumerate(self.segs):
            for s2, _ in self.segs[i + 1:]:
                self.assertIsNone(proper_crossing(s1, s2), (s1, s2))

    def test_every_line_end_is_connected(self):
        ends = []
        for a, b, _ in self.layout["arcs"]:
            ends.extend([a, b])
        reach = self.P['fitting_gap'] + 1e-3
        for seg, _ in self.segs:
            for p in seg:
                near_node = any(abs(p[0] - n.u) + abs(p[1] - n.v) <= reach
                                for n in self.nodes.values())
                touches = any(other is not seg and L._point_seg_distance(p, other) < 1e-3
                              for other, _ in self.segs)
                at_hop = any(near(p, e) for e in ends)
                self.assertTrue(near_node or touches or at_hop, p)


if __name__ == "__main__":
    unittest.main()
