"""Unit tests for the pure Snap Fire Pairs tile logic (no Revit required)."""
import importlib.util
from pathlib import Path
import unittest

LIB = (Path(__file__).resolve().parents[1] / "CustomPyRevit.extension" / "ETC PyTools.tab"
       / "Electrical.panel" / "Snap Fire Pairs.pushbutton" / "lib" / "tile_snap_logic.py")
_spec = importlib.util.spec_from_file_location("tile_snap_logic", LIB)
L = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(L)


def det(key, x, y):
    return L.Device(key, L.DETECTOR, x, y)


def spk(key, x, y):
    return L.Device(key, L.SPEAKER, x, y)


def targets(moves):
    return {m.device.id: (round(m.x, 1), round(m.y, 1)) for m in moves}


# NMH Ground open-area ceiling: 1200 E-W x 600 N-S, tile centres X = 543 (mod 1200),
# Y = 151 (mod 600), calibrated from the user's hand-placed pair 7044705 / 7044703.
NMH = L.Grid(-57.0, -149.0, 1200.0, 600.0)


class GridFromCornersTests(unittest.TestCase):
    def test_imprecise_clicks_round_to_module_and_keep_direction(self):
        grid = L.grid_from_corners((1000.0, 2000.0), (-195.0, 2604.0))
        self.assertEqual((-195.0, 2000.0, 1200.0, 600.0), (grid.ox, grid.oy, grid.sx, grid.sy))
        rotated = L.grid_from_corners((0.0, 0.0), (598.0, 1203.0))
        self.assertEqual((600.0, 1200.0), (rotated.sx, rotated.sy))

    def test_rejects_points_on_one_line_or_spanning_many_tiles(self):
        with self.assertRaises(ValueError):
            L.grid_from_corners((0.0, 0.0), (1200.0, 40.0))
        with self.assertRaises(ValueError):
            L.grid_from_corners((0.0, 0.0), (3600.0, 1800.0))


class PlanMovesTests(unittest.TestCase):
    def test_user_reference_pair_is_already_in_place(self):
        moves, warnings = L.plan_moves(NMH, [det(1, -257757.1, -10048.8), spk(2, -257157.1, -10048.8)])
        self.assertEqual([], warnings)
        self.assertTrue(all(m.distance < 1.0 for m in moves))

    def test_pair_straddling_a_grid_line_moves_into_one_tile_keeping_sides(self):
        # Midpoint (-257000, -10340) sits 9 mm from the E-W joint at Y = -10349.
        # It must land in the user's reference tile, speaker kept on the west side.
        moves, _ = L.plan_moves(NMH, [det(1, -256900.0, -10350.0), spk(2, -257100.0, -10330.0)])
        t = targets(moves)
        self.assertEqual((-257157.0, -10049.0), t[1])
        self.assertEqual((-257757.0, -10049.0), t[2])
        for key in t:
            self.assertGreaterEqual(NMH.distance_to_line(*t[key]), 299.0)

    def test_rotated_tile_runs_pair_north_south(self):
        grid = L.Grid(0.0, 0.0, 600.0, 1200.0)
        t = targets(L.plan_moves(grid, [det(1, 150.0, 500.0), spk(2, 450.0, 520.0)])[0])
        self.assertEqual({1: (300.0, 300.0), 2: (300.0, 900.0)}, t)

    def test_square_tiles_use_two_adjacent_tiles(self):
        grid = L.Grid(0.0, 0.0, 600.0, 600.0)
        t = targets(L.plan_moves(grid, [det(1, 500.0, 250.0), spk(2, 750.0, 280.0)])[0])
        self.assertEqual({1: (300.0, 300.0), 2: (900.0, 300.0)}, t)

    def test_unpaired_device_goes_to_tile_centre_and_is_flagged(self):
        moves, _ = L.plan_moves(NMH, [det(1, 0.0, 0.0), spk(2, 5000.0, 0.0)])
        t = targets(moves)
        self.assertEqual((543.0, 151.0), t[1])
        self.assertTrue(all(any("no partner" in n for n in m.notes) for m in moves))

    def test_two_pairs_snapping_to_the_same_tile_are_reported(self):
        devices = [det(1, 500.0, 100.0), spk(2, 600.0, 100.0), det(3, 300.0, 200.0), spk(4, 800.0, 200.0)]
        _, warnings = L.plan_moves(NMH, devices)
        self.assertTrue(warnings)


class ClassifyTests(unittest.TestCase):
    def test_nmh_family_and_type_names(self):
        family = "AUR_FS_FAD_HEAT AND SMOKE DETECTORS_R3"
        self.assertEqual(L.DETECTOR, L.classify("Smoke Detector - Surface Mounted 2", family))
        self.assertEqual(L.SPEAKER, L.classify("136mm Diameter", "40_FI_AD_Sounder Speaker"))
        self.assertIsNone(L.classify("Heat Detector - Surface Mounted 2", family))
        self.assertIsNone(L.classify("Smoke Detector Conceal - Surface Mounted", family))
        self.assertIsNone(L.classify(
            "Smoke Detector Conceal - Surface Mounted with remote indicator", family))


if __name__ == "__main__":
    unittest.main()
