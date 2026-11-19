import math
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


LIB = (Path(__file__).resolve().parents[1] / "CustomPyRevit.extension" / "ETC PyTools.tab"
       / "Electrical.panel" / "Fire EWS Layout.pushbutton" / "lib")
sys.path.insert(0, str(LIB))
try:
    import fire_ews_geometry as geom
    import fire_ews_report as report
    import fire_ews_review as review
    import fire_ews_new as new_layout
finally:
    sys.path.remove(str(LIB))


def fixture(finish="tile", holes=()):
    loops = [[(-100, -100), (2500, -100), (2500, 1300), (-100, 1300)]] + list(holes)
    boundary = geom.edges(loops)
    box = geom.bounds(loops)
    bars = [((x, 0), (x, 1200)) for x in (0, 1200, 2400)]
    bars += [((0, y), (2400, y)) for y in (0, 600, 1200)]
    ceiling = dict(id="12:1", name="Tile <approved>", finish=finish, box=box, loops=loops,
                   z=2700, boundary=boundary, bars=bars,
                   grid=geom.TileGrid(bars, (600, 1200)) if finish == "tile" else None)
    room = dict(id=1, number="R01", name="Salle \u00c9", box=box, loops=loops, area=3.64,
                zmin=0, zmax=3000, boundary=boundary, walls=boundary, open_edges=[],
                wall_heights_verified=True)
    return room, ceiling


def device(eid, point, kind="detector"):
    return dict(id=eid, point=point, z=2700, kind=kind)


def criteria(**kwargs):
    return review.parse_rules(dict(dict(reference="Demo", wall="200", travel="800",
                                        separation="300"), **kwargs))


class FireEwsLayoutTests(unittest.TestCase):
    def test_new_layout_previews_detector_or_pair_and_refuses_incomplete_coverage(self):
        room, ceiling = fixture()
        rules = criteria(detector_radius="3000", detector_edge="3000")
        _, singles, count = new_layout.plan(room, [ceiling], [], rules, False)
        self.assertEqual(len(singles), 1)
        self.assertGreater(count, 0)
        _, pair, _ = new_layout.plan(room, [ceiling], [], rules, True)
        self.assertEqual([p["kind"] for p in pair], ["detector", "speaker"])
        self.assertEqual(geom.distance(pair[0]["to"], pair[1]["to"]), 600)
        self.assertEqual([p["bar"] for p in pair], [300, 300])
        with self.assertRaisesRegex(ValueError, "No complete proposal"):
            new_layout.plan(room, [ceiling], [], criteria(detector_radius="20", detector_edge="20"), True)

    def test_new_layout_flush_needs_no_travel_limit_and_rejects_open_room(self):
        room, ceiling = fixture(finish="flush")
        rules = review.parse_rules(dict(reference="Demo", wall="200", separation="300",
                                        detector_radius="3000", detector_edge="3000"), new_placement=True)
        _, proposals, _ = new_layout.plan(room, [ceiling], [], rules, False)
        self.assertEqual(proposals[0]["to"], (1200, 600))
        room["open_edges"] = room["boundary"]
        with self.assertRaisesRegex(ValueError, "closed room"):
            new_layout.plan(room, [ceiling], [], rules, False)

    def test_ceiling_placement_ignores_room_separators_but_respects_physical_partition(self):
        zone, ceiling = fixture()
        zone.update(ceiling_zone=True, open_edges=[((1250, -100), (1250, 1300))])
        rules = criteria(detector_radius="4000", detector_edge="4000")
        _, proposals, _ = new_layout.plan(zone, [ceiling], [], rules, True)
        self.assertEqual(len(proposals), 2)

        loops = [[(-100, -100), (2900, -100), (2900, 1300), (-100, 1300)]]
        boundary = geom.edges(loops)
        bars = [((x, 0), (x, 1200)) for x in (0, 1200, 1600, 2800)]
        bars += [((0, y), (2800, y)) for y in (0, 600, 1200)]
        box = geom.bounds(loops)
        ceiling.update(boundary=boundary, box=box, bars=bars, grid=geom.TileGrid(bars, (600, 1200)))
        zone.update(boundary=boundary, box=box, walls=boundary)
        partition = geom.edges([[(1320, -150), (1480, -150), (1480, 1400), (1320, 1400)]])
        zone.update(walls=zone["walls"]+partition, barriers=partition, wall_solids=[partition])
        _, proposals, _ = new_layout.plan(zone, [ceiling], [], rules, False)
        self.assertEqual(len(proposals), 2)
        self.assertLess(min(p["to"][0] for p in proposals), 1320)
        self.assertGreater(max(p["to"][0] for p in proposals), 1480)

    def test_rotated_shifted_grid_requires_finite_support(self):
        room, ceiling = fixture()
        a = math.radians(33)
        p = lambda q: (20000+q[0]*math.cos(a)-q[1]*math.sin(a),
                       -5000+q[0]*math.sin(a)+q[1]*math.cos(a))
        lines = [(p(x), p(y)) for x, y in ceiling["bars"]]
        boundary = [(p(x), p(y)) for x, y in ceiling["boundary"]]
        box = geom.bounds([[p(q) for q in room["loops"][0]]])
        tiles = geom.TileGrid(lines, (600, 1200)).tiles(box, boundary)
        self.assertEqual(len(tiles), 4)
        self.assertLess(min(geom.distance(t[0], p((600, 300))) for t in tiles), .01)
        self.assertEqual(len(geom.TileGrid(lines[1:], (600, 1200)).tiles(box, boundary)), 2)

    def test_hole_blocks_panel_and_direct_spacing_ray(self):
        hole = [(500, 220), (700, 220), (700, 370), (500, 370)]
        room, ceiling = fixture(holes=(hole,))
        self.assertEqual(len(ceiling["grid"].tiles(room["box"], ceiling["boundary"])), 3)
        self.assertFalse(geom.contained_ray((300, 300), (900, 300), room["boundary"]))

    def test_detector_only_can_move_and_speaker_only_can_propose_without_detector(self):
        room, ceiling = fixture()
        d = device(1, (450, 320))
        result = review.review(room, [ceiling], [d], ["detector"], criteria())
        self.assertEqual(result["proposals"][0]["to"], (600, 300))
        self.assertAlmostEqual(result["metrics"][0]["wall"], 420)
        self.assertAlmostEqual(result["metrics"][0]["grid_clear"], 280)
        speaker = review.review(room, [ceiling], [], ["speaker"], criteria(), add_candidate=True)
        self.assertIsNone(speaker["proposals"][0]["id"])
        self.assertIsNone(speaker["metrics"][0]["sample_count"])
        self.assertIn("Tile &lt;approved&gt;", report.room_html(room, result))

    def test_combined_preserves_accepted_pair_and_skips_pinned_device(self):
        room, ceiling = fixture()
        devices = [device(1, (310, 295)), device(2, (910, 295), "speaker"),
                   dict(device(3, (1500, 830)), fixed="Pinned")]
        result = review.review(room, [ceiling], devices, ["detector", "speaker"], criteria())
        self.assertEqual(result["proposals"], [])
        self.assertIn("Existing pair already aligned on measured tile halves", result["metrics"][0]["issues"])

    def test_audit_and_ambiguous_ceiling_do_not_propose(self):
        room, ceiling = fixture()
        snap = device(1, (450, 320))
        audit = review.review(room, [ceiling], [snap], ["detector"], review.parse_rules({}))
        self.assertEqual(audit["proposals"], [])
        twin = dict(ceiling, id="13:1", z=2851)
        self.assertEqual(review.review(room, [ceiling, twin], [snap], ["detector"], criteria())["proposals"], [])

    def test_no_candidates_for_open_room_or_unmapped_system(self):
        room, ceiling = fixture()
        room["open_edges"] = room["boundary"][:1]
        rules = criteria()
        self.assertEqual(review.review(room, [ceiling], [], ["speaker"], rules, add_candidate=True)["proposals"], [])
        room["open_edges"] = []
        rules["unmapped"] = {"speaker"}
        self.assertEqual(review.review(room, [ceiling], [], ["speaker"], rules, add_candidate=True)["metrics"], [])

    def test_ceiling_area_counts_across_separators_but_not_across_physical_walls(self):
        zone, ceiling = fixture()
        zone.update(ceiling_zone=True, open_edges=zone["boundary"], walls=[], barriers=[])
        devices = [device(1, (600, 300)), device(2, (1800, 300))]
        ledger = review.review(zone, [ceiling], devices, ["detector"], criteria(),
                               add_candidate=True)["metrics"]
        self.assertEqual(ledger[0]["pitch"], 1200)
        self.assertIsNone(ledger[0]["wall"])
        wall = geom.edges([[(1100, -150), (1200, -150), (1200, 1400), (1100, 1400)]])
        zone.update(walls=wall, barriers=wall, wall_solids=[wall])
        ledger = review.review(zone, [ceiling], devices, ["detector"], criteria())["metrics"]
        self.assertIsNone(ledger[0]["peer"])
        self.assertEqual(ledger[0]["wall"], 500)
        self.assertEqual(ledger[1]["wall"], 600)

    def test_wall_section_uses_slice_height_and_not_the_triangle_bounds(self):
        triangle = [(0, 0, 1000), (200, 0, 3000), (0, 200, 3000)]
        self.assertEqual(geom.section_triangle(triangle, 2000), ((100.0, 0.0), (0.0, 100.0)))
        self.assertIsNone(geom.section_triangle(triangle, 3010))

    def test_context_preserves_ceiling_sections_and_checks_room_wall_spans(self):
        spec = importlib.util.spec_from_file_location("fire_ews_revit_test", LIB / "fire_ews_revit.py")
        adapter = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"pyrevit": SimpleNamespace(DB=object()),
                                      "pyrevit.revit": SimpleNamespace(query=object())}):
            spec.loader.exec_module(adapter)
        zone, ceiling = fixture()
        zone["ceiling_zone"] = True
        room = dict(zone, ceiling_zone=False, wall_spans=[(0, 2500)])
        adapter.room_context([zone, room], [ceiling], [device(1, (600, 300))])
        self.assertTrue(zone["wall_heights_verified"])
        self.assertFalse(room["wall_heights_verified"])
        self.assertEqual(zone["ambiguous_devices"], {1})
        self.assertEqual(room["ambiguous_devices"], {1})

    def test_ceiling_zone_allows_verified_full_tile_on_ceiling_edge(self):
        zone, ceiling = fixture()
        loops = [[(0, 0), (2400, 0), (2400, 1200), (0, 1200)]]
        boundary, box = geom.edges(loops), geom.bounds(loops)
        ceiling.update(loops=loops, boundary=boundary, box=box)
        zone.update(loops=loops, boundary=boundary, box=box, ceiling_zone=True,
                    ceiling_patch=ceiling["id"], open_edges=boundary)
        existing = device(1, (450, 320))
        result = review.review(zone, [ceiling], [existing], ["detector"], criteria())
        self.assertEqual(result["proposals"][0]["to"], (600, 300))
        self.assertEqual(result["proposals"][0]["wall"], 400)
        self.assertEqual(result["proposals"][0]["bar"], 300)
        self.assertIn("Target to wall mm", report.room_html(zone, result))

        zone.update(barriers=[((500, 0), (500, 1200))])
        blocked = review.review(zone, [ceiling], [existing], ["detector"], criteria())
        self.assertEqual(blocked["proposals"], [])
        self.assertTrue(blocked["metrics"][0]["decision"].startswith("Blocked:"))

    def test_no_proposals_explains_audit_mode_and_move_limit(self):
        room, ceiling = fixture()
        devices = [device(1, (450, 320))]
        audit = review.review(room, [ceiling], devices, ["detector"], review.parse_rules({}))
        self.assertIn("Measurement only", audit["notes"][0])
        self.assertIn("Audit only", audit["metrics"][0]["decision"])
        limited = review.review(room, [ceiling], devices, ["detector"], criteria(travel="100"))
        self.assertEqual(limited["metrics"][0]["decision"], "Blocked: move limit")
        self.assertIn("Move assessment", report.room_html(room, limited))

    def test_pair_preview_uses_half_centres_and_tries_next_tile_if_blocked(self):
        room, ceiling = fixture()
        devices = [device(1, (310, 340)), device(2, (910, 340), "speaker"),
                   device(3, (900, 285), "obstacle")]
        result = review.review(room, [ceiling], devices, ["detector", "speaker"], criteria(),
                               pair_preview=True)
        targets = {p["id"]: p["to"] for p in result["proposals"]}
        self.assertEqual(targets, {1: (300, 900), 2: (900, 900)})
        self.assertTrue(all(p["bar"] == 300 for p in result["proposals"]))
        blocked = review.review(room, [ceiling], devices, ["detector", "speaker"],
                                criteria(separation="601"), pair_preview=True)
        self.assertEqual(blocked["proposals"], [])
        self.assertIn("pair spacing below entered object clearance", blocked["metrics"][0]["decision"])

    def test_small_tiled_single_pair_uses_central_tile_unless_it_fails_checks(self):
        room, ceiling = fixture()
        loops = [[(-100, -100), (3700, -100), (3700, 1900), (-100, 1900)]]
        boundary, box = geom.edges(loops), geom.bounds(loops)
        bars = [((x, 0), (x, 1800)) for x in (0, 1200, 2400, 3600)]
        bars += [((0, y), (3600, y)) for y in (0, 600, 1200, 1800)]
        ceiling.update(loops=loops, boundary=boundary, box=box, bars=bars,
                       grid=geom.TileGrid(bars, (600, 1200)))
        room.update(loops=loops, boundary=boundary, box=box, walls=boundary, area=7.6)
        devices = [device(1, (1510, 340)), device(2, (2110, 340), "speaker")]

        def targets(elements, **settings):
            result = review.review(room, [ceiling], elements, ["detector", "speaker"],
                                   criteria(**settings), pair_preview=True)
            return {p["id"]: p["to"] for p in result["proposals"]}

        central = {1: (1500, 900), 2: (2100, 900)}
        nearest = {1: (1500, 300), 2: (2100, 300)}
        self.assertEqual(targets(devices), central)
        self.assertEqual(targets(devices, travel="400"), nearest)
        self.assertEqual(targets(devices + [device(3, (2100, 900), "obstacle")]), nearest)
        self.assertEqual(targets(devices + [device(4, (3450, 1550))]), nearest)

    def test_flush_pair_aligns_along_area_centreline_and_reports_ambiguous_pairs(self):
        room, ceiling = fixture(finish="flush")
        devices = [device(1, (400, 440)), device(2, (1020, 340), "speaker")]
        result = review.review(room, [ceiling], devices, ["detector", "speaker"], criteria(),
                               pair_preview=True)
        targets = {p["id"]: p["to"] for p in result["proposals"]}
        self.assertEqual(targets, {1: (410, 600), 2: (1010, 600)})
        self.assertTrue(all(p["bar"] is None for p in result["proposals"]))
        devices.append(device(3, (1050, 800), "speaker"))
        ambiguous = review.review(room, [ceiling], devices, ["detector", "speaker"], criteria(),
                                  pair_preview=True)
        self.assertEqual(ambiguous["proposals"], [])
        self.assertIn("more than one possible partner", ambiguous["metrics"][0]["decision"])


if __name__ == "__main__":
    unittest.main()
