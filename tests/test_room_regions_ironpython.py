"""Cross-runtime checks for Room Regions' pure logic and .NET DataView filter."""

import math
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "CustomPyRevit.extension", "ETC PyTools.tab",
                   "Spaces.panel", "RoomRegions.pushbutton", "lib")
sys.path.insert(0, LIB)
try:
    import room_regions_logic as logic
finally:
    sys.path.remove(LIB)

try:
    import clr
except ImportError:
    DataTable = None
else:
    clr.AddReference("System.Data")
    from System import Boolean
    from System.Data import DataTable


class CrossRuntimeTests(unittest.TestCase):
    def test_element_id_and_fifo_queue(self):
        class ModernId(object):
            Value = 8589934592

        class LegacyId(object):
            IntegerValue = 42

        self.assertEqual(8589934592, int(logic.element_id_value(ModernId())))
        self.assertEqual(42, logic.element_id_value(LegacyId()))
        queue = logic.ActionQueue()
        calls = []
        queue.add("first", lambda app: calls.append(app))
        queue.add("second", lambda app: calls.append(app + "-2"))
        queue.drain("api", lambda label, error: self.fail(label))
        self.assertEqual(["api", "api-2"], calls)

    def test_circle_area_and_unicode_search_expression(self):
        circle = [
            {"kind": "arc", "start": (1.0, 0.0), "mid": (0.0, 1.0), "end": (-1.0, 0.0)},
            {"kind": "arc", "start": (-1.0, 0.0), "mid": (0.0, -1.0), "end": (1.0, 0.0)},
        ]
        cleaned, warnings = logic.clean_loop(circle, 0.001, 0.0011)
        self.assertEqual([], warnings)
        self.assertAlmostEqual(math.pi, logic.signed_area(cleaned))
        query = logic.row_filter(["link"], ["id'1"], False, True, u"\u00c9tage [1]")
        self.assertIn(u"\u00c9tage", query)
        self.assertIn("id''1", query)
        self.assertIn("[[]1[]]", query)

    def test_highlighted_rows_and_shift_tick_targets(self):
        keys = ["b", "d", "a", "c"]
        self.assertEqual([0, 2], logic.tick_indexes(keys, ["b", "a", "hidden"], None, "a", False))
        self.assertEqual([1], logic.tick_indexes(keys, ["b", "a"], None, "d", False))
        self.assertEqual([1, 2, 3], logic.tick_indexes(keys, ["b"], "d", "c", True))

    @unittest.skipUnless(DataTable is not None, "IronPython/.NET DataTable is unavailable")
    def test_real_dataview_filters_and_edits_unicode_rows(self):
        table = DataTable("RoomRegions")
        for name in ("__checked", "__level_match", "__phase_match"):
            table.Columns.Add(name, clr.GetClrType(Boolean))
        for name in ("__key", "__source", "__link", "__search", "number"):
            table.Columns.Add(name)

        values = [
            ("1", "link", "id'1", u"\u00c9tage A[1] 50%*", "2", True, True),
            ("2", "link", "id'2", u"\u00c9tage A[1] 50%*", "1", True, True),
            ("3", "space", "", u"\u00c9tage A[1] 50%*", "3", False, True),
        ]
        for key, source, link, search, number, level, phase in values:
            row = table.NewRow()
            row["__checked"] = False
            row["__level_match"] = level
            row["__phase_match"] = phase
            row["__key"], row["__source"], row["__link"] = key, source, link
            row["__search"], row["number"] = search, number
            table.Rows.Add(row)

        view = table.DefaultView
        view.RowFilter = logic.row_filter(
            ["link"], ["id'1"], False, True, u"\u00c9tage A[1] 50%*")
        self.assertEqual(1, view.Count)
        self.assertEqual("1", view[0]["__key"])
        view[0]["__checked"] = True
        self.assertTrue(bool(table.Rows[0]["__checked"]))

        view.RowFilter = logic.row_filter(["link", "space"], ["id'1", "id'2"], True, True, "")
        view.Sort = "number ASC"
        self.assertEqual(["2", "1", "3"], [row["__key"] for row in view])


if __name__ == "__main__":
    unittest.main()
