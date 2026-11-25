"""Unit tests for the Ultimate Revit Browser & Scheduler (all 4 phases)."""

import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

LIB_DIR = str(
    Path(__file__).resolve().parents[1]
    / "CustomPyRevit.extension"
    / "ETC PyTools.tab"
    / "Electrical.panel"
    / "RevitBrowserScheduler.pushbutton"
    / "lib"
)
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

import browser_state as state  # noqa: E402
import diff_engine  # noqa: E402
import excel_io  # noqa: E402
import preset_store  # noqa: E402
import query_engine  # noqa: E402


def _make_record(eid, category="Lighting Fixtures", family="LED Panel", type_name="600x600",
                 level="Level 01", mark="L-01", comments="", area="12.5 m2", type_mark="LP1",
                 type_id=900, space="101 Office", space_id=501, room="101 Office", room_id=601,
                 is_linked=False, link_name="Host", pinned=False, group_id=-1, group_name="",
                 checkout_status="NotOwned", checkout_owner="", area_readonly=True):
    return {
        "row_key": state.make_row_key(eid, is_linked=is_linked, link_instance_id=77 if is_linked else None),
        "id": eid,
        "unique_id": "uid-{}".format(eid),
        "is_linked": is_linked,
        "link_name": link_name,
        "link_instance_id": 77 if is_linked else None,
        "is_type_element": False,
        "type_id": type_id,
        "category": category,
        "category_id": -2001120,
        "family": family,
        "type": type_name,
        "level": level,
        "phase_created": "New Construction",
        "phase_demolished": "None",
        "workset": "Electrical",
        "design_option": "Main Model",
        "group_id": group_id,
        "group_name": group_name,
        "pinned": pinned,
        "host": "Ceiling",
        "room": room,
        "room_id": room_id,
        "space": space,
        "space_id": space_id,
        "visible": True,
        "checkout_status": checkout_status,
        "checkout_owner": checkout_owner,
        "values": {
            "I:Mark": mark,
            "I:Comments": comments,
            "I:Area": area,
            "T:Type Mark": type_mark,
        },
        "param_meta": {
            "I:Mark": {"name": "Mark", "is_type": False, "is_read_only": False, "user_modifiable": True, "storage_type": "String"},
            "I:Comments": {"name": "Comments", "is_type": False, "is_read_only": False, "user_modifiable": True, "storage_type": "String"},
            "I:Area": {"name": "Area", "is_type": False, "is_read_only": area_readonly, "user_modifiable": not area_readonly, "storage_type": "Double"},
            "T:Type Mark": {"name": "Type Mark", "is_type": True, "is_read_only": False, "user_modifiable": True, "storage_type": "String"},
        },
        "counts": {"Lighting Fixtures": 3},
    }


class ModifiabilityAndDeleteSafetyTests(unittest.TestCase):
    def test_cell_modifiability_guardrails(self):
        normal = _make_record(1)
        linked = _make_record(2, is_linked=True, link_name="Arch.rvt")
        owned = _make_record(3, checkout_status="OwnedByOtherUser", checkout_owner="Alice")
        grouped = _make_record(4, group_id=888, group_name="Typical Office")
        pinned = _make_record(5, pinned=True)

        self.assertEqual((True, "editable"), state.evaluate_cell_modifiability(normal, "I:Mark")[:2])
        self.assertEqual((True, "editable_type"), state.evaluate_cell_modifiability(normal, "T:Type Mark")[:2])
        self.assertEqual((False, "pseudo"), state.evaluate_cell_modifiability(normal, "Category")[:2])
        self.assertEqual((False, "count"), state.evaluate_cell_modifiability(normal, "Count: Lighting Fixtures")[:2])
        self.assertEqual((False, "read_only"), state.evaluate_cell_modifiability(normal, "I:Area")[:2])
        self.assertEqual((False, "linked"), state.evaluate_cell_modifiability(linked, "I:Mark")[:2])
        self.assertEqual((False, "owned_by_other"), state.evaluate_cell_modifiability(owned, "I:Mark")[:2])
        self.assertEqual((False, "in_group"), state.evaluate_cell_modifiability(grouped, "I:Mark")[:2])
        self.assertEqual((False, "pinned"), state.evaluate_cell_modifiability(pinned, "I:Mark")[:2])
        self.assertEqual((True, "editable"), state.evaluate_cell_modifiability(pinned, "I:Mark", allow_pinned=True)[:2])

    def test_delete_safety_classification(self):
        self.assertEqual((True, "ok"), state.evaluate_delete_safety(_make_record(1))[:2])
        self.assertEqual((False, "block"), state.evaluate_delete_safety(_make_record(2, is_linked=True))[:2])
        self.assertEqual((False, "block"), state.evaluate_delete_safety(_make_record(3, pinned=True))[:2])
        self.assertEqual((False, "block"), state.evaluate_delete_safety(_make_record(4, group_id=99))[:2])
        self.assertEqual((False, "block"), state.evaluate_delete_safety(
            _make_record(5, checkout_status="OwnedByOtherUser"))[:2])
        self.assertEqual((True, "warn"), state.evaluate_delete_safety(_make_record(6, category="Levels"))[:2])


class QueryEngineAndTakeoffTests(unittest.TestCase):
    def setUp(self):
        self.cols = ["Category", "Family", "Type", "Level", "I:Mark", "I:Area", "Space"]
        self.records = [
            _make_record(1, level="Level 2", mark="L-01", area="10.0 m2", space="101 Office", space_id=501),
            _make_record(2, level="Level 10", mark="L-01", area="25.5 m2", space="102 Lobby", space_id=502),
            _make_record(3, level="Level 1", mark="", area="30.0 m2", category="Fire Alarm Devices",
                         family="Smoke", type_name="Optical", space="101 Office", space_id=501),
        ]

    def test_structured_query_operators_and_qa_tokens(self):
        self.assertEqual([2, 3], [r["id"] for r in query_engine.filter_records_by_query(
            self.records, "Area>20", self.cols)])
        self.assertEqual([3], [r["id"] for r in query_engine.filter_records_by_query(
            self.records, "Mark:EMPTY", self.cols)])
        self.assertEqual([1, 2], [r["id"] for r in query_engine.filter_records_by_query(
            self.records, "Mark:DUPLICATE", self.cols)])
        self.assertEqual([1, 2, 3], [r["id"] for r in query_engine.filter_records_by_query(
            self.records, "Mark:EMPTY OR Mark:DUPLICATE", self.cols)])
        self.assertEqual([1, 2], [r["id"] for r in query_engine.filter_records_by_query(
            self.records, "-Smoke", self.cols)])

    def test_natural_sorting(self):
        ordered = query_engine.sort_records(self.records, "Level", ascending=True)
        self.assertEqual(["Level 1", "Level 2", "Level 10"], [r["level"] for r in ordered])

    def test_selected_spaces_combination_scope(self):
        combo = query_engine.filter_records_by_scope(
            self.records, "Selected Spaces / Rooms", selected_space_ids=[501])
        self.assertEqual([1, 3], [r["id"] for r in combo])

    def test_grouped_takeoff_and_totals(self):
        groups = query_engine.build_grouped_takeoff(
            self.records, self.cols, ["Category", "Family", "Type"])
        self.assertEqual(2, len(groups))
        led_grp = [g for g in groups if g["values"]["Category"] == "Lighting Fixtures"][0]
        self.assertEqual(2, led_grp["count"])
        self.assertEqual("35.50", led_grp["values"]["I:Area"])
        totals = query_engine.compute_grand_totals(self.records, self.cols)
        self.assertEqual(3, totals["count"])
        self.assertEqual(65.5, totals["sums"]["I:Area"])


class StagedEditsAndDeltaTrackerTests(unittest.TestCase):
    def test_staged_bulk_operations_and_conflict_flagging(self):
        r1 = _make_record(10, mark="A-01")
        r2 = _make_record(11, mark="A-02", pinned=True)
        r3 = _make_record(12, mark="A-03")
        staged = state.StagedEdits()

        cnt, skipped = staged.stage_auto_number([r1, r2, r3], "I:Mark", prefix="DB-", start=1, padding=2)
        self.assertEqual(2, cnt)
        self.assertEqual(1, len(skipped))
        self.assertEqual("DB-01", staged.get_value(r1["row_key"], "I:Mark"))
        self.assertEqual("DB-02", staged.get_value(r3["row_key"], "I:Mark"))

        # Simulate external Revit edit to r1 while staged
        r1_external = _make_record(10, mark="CHANGED-IN-REVIT")
        self.assertEqual(1, staged.mark_external_update(r1_external))
        self.assertTrue(staged.edits[(r1["row_key"], "I:Mark")]["conflict"])

    def test_delta_tracker_ok_filtering_and_storm_guard(self):
        r1 = _make_record(100, type_id=500)
        r2 = _make_record(200, type_id=500)
        tracker = state.DeltaTracker(storm_threshold=3)
        tracker.rebuild_index([r1, r2], active_category_ids=[-2001120], watch_types=True)

        # Own transaction is ignored
        self.assertFalse(tracker.record_document_changed([], [100], [], tx_names=[state.TX_PREFIX + " Edit"]))
        # Unrelated element is ignored
        self.assertFalse(tracker.record_document_changed([], [999], [888], tx_names=["Move"]))
        # Tracked element + type modification hit
        self.assertTrue(tracker.record_document_changed([], [100, 500], [200], tx_names=["Edit"]))
        added, mod, deleted, tmod, storm = tracker.pop_delta()
        self.assertEqual({100}, mod)
        self.assertEqual({200}, deleted)
        self.assertEqual({500}, tmod)
        self.assertFalse(storm)

        # Storm guard trips on huge burst
        self.assertTrue(tracker.record_document_changed(list(range(10)), [], [], tx_names=["Paste"]))
        self.assertTrue(tracker.storm_triggered)


class PresetStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_builtin_starter_presets_and_user_roundtrip(self):
        store = preset_store.PresetStore(user_dir=self.tmp)
        builtins = store.list_presets()
        names = [p["name"] for p in builtins]
        self.assertIn("Electrical & Lighting Takeoff (Totals)", names)
        self.assertIn("Selected Spaces - Combined Takeoff", names)
        self.assertIn("Spaces & Rooms Schedule", names)

        custom = preset_store.make_preset(
            "My Custom Takeoff", categories=["Doors", "Windows"],
            grouped_mode=True, group_by=["Category", "Type"],
            selected_space_ids=[501, 502],
        )
        store.save_preset(custom)
        reloaded = [p for p in store.list_presets() if p["name"] == "My Custom Takeoff"]
        self.assertEqual(1, len(reloaded))
        self.assertEqual([501, 502], reloaded[0]["selected_space_ids"])
        self.assertTrue(store.delete_preset("My Custom Takeoff"))


class ExcelRoundTripAndThreeWayDiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build_minimal_xlsx(self, path, headers, rows, meta_header=None, baselines=None):
        """Create a valid .xlsx using stdlib zipfile (no xlsxwriter needed for reader test)."""
        import json
        from xml.sax.saxutils import escape

        def col_letter(idx):
            s = ""
            n = idx + 1
            while n > 0:
                n, rem = divmod(n - 1, 26)
                s = chr(65 + rem) + s
            return s

        def make_sheet_xml(all_rows):
            parts = ['<?xml version="1.0" encoding="UTF-8"?>',
                     '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
            for r_i, row in enumerate(all_rows, start=1):
                parts.append('<row r="{}">'.format(r_i))
                for c_i, val in enumerate(row):
                    ref = "{}{}".format(col_letter(c_i), r_i)
                    parts.append('<c r="{}" t="inlineStr"><is><t>{}</t></is></c>'.format(
                        ref, escape(str(val))))
                parts.append('</row>')
            parts.append('</sheetData></worksheet>')
            return "".join(parts)

        sched_xml = make_sheet_xml([headers] + rows)
        meta_rows = []
        if meta_header is not None:
            meta_rows.append(["META_HEADER", json.dumps(meta_header)])
        for k, v in (baselines or {}).items():
            meta_rows.append([k, json.dumps(v)])
        meta_xml = make_sheet_xml(meta_rows)

        wb_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets>'
            '<sheet name="Schedule" sheetId="1" r:id="rId1"/>'
            '<sheet name="_Meta" sheetId="2" state="hidden" r:id="rId2"/>'
            '</sheets></workbook>'
        )
        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/>'
            '</Relationships>'
        )
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("xl/workbook.xml", wb_xml)
            zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)
            zf.writestr("xl/worksheets/sheet1.xml", sched_xml)
            zf.writestr("xl/worksheets/sheet2.xml", meta_xml)

    def test_pure_python_xlsx_reader_and_three_way_diff_guardrails(self):
        r1 = _make_record(1, mark="L-01", type_mark="LP1", type_id=900)
        r2 = _make_record(2, mark="L-02", type_mark="LP1", type_id=900)
        r3 = _make_record(3, mark="L-03", is_linked=True, link_name="Arch.rvt")
        r4 = _make_record(4, mark="REVIT-NEWER", type_id=901)  # Changed in Revit since export!

        cols = ["UniqueId", "Element Id", "Category", "I:Mark", "I:Area", "T:Type Mark"]
        headers = [state.column_display_label(c) for c in cols]
        meta_header = {"columns": cols, "headers": headers}
        baselines = {
            "uid-1": {"values": {"I:Mark": "L-01", "I:Area": "12.5 m2", "T:Type Mark": "LP1"}},
            "uid-2": {"values": {"I:Mark": "L-02", "I:Area": "12.5 m2", "T:Type Mark": "LP1"}},
            "uid-4": {"values": {"I:Mark": "L-04-OLD"}},
        }
        # Excel edits:
        # - r1: valid Mark edit ("L-01-NEW"), blocked read-only Area edit, Type Mark -> "LP-A"
        # - r2: conflicting Type Mark -> "LP-B" (same type_id 900 as r1!)
        # - r3: linked element Mark edit -> blocked!
        # - r4: stale edit ("L-04-EXCEL") when Revit is already "REVIT-NEWER" -> 3-way conflict!
        excel_rows = [
            ["uid-1", "1", "Lighting Fixtures", "L-01-NEW", "99.0 m2", "LP-A"],
            ["uid-2", "2", "Lighting Fixtures", "L-02", "12.5 m2", "LP-B"],
            ["uid-3", "3", "Lighting Fixtures", "HACKED", "12.5 m2", "LP1"],
            ["uid-4", "4", "Lighting Fixtures", "L-04-EXCEL", "12.5 m2", "LP1"],
        ]
        xlsx_path = os.path.join(self.tmp, "test_schedule.xlsx")
        self._build_minimal_xlsx(xlsx_path, headers, excel_rows, meta_header, baselines)

        wb = excel_io.read_schedule_xlsx(xlsx_path)
        plan = diff_engine.build_import_plan(wb, [r1, r2, r3, r4])
        s = plan["summary"]

        self.assertEqual(1, s["ready"])            # r1 I:Mark
        self.assertEqual(2, s["blocked"])          # r1 I:Area (read-only) + r3 I:Mark (linked)
        self.assertEqual(1, s["conflicts"])        # r4 I:Mark (3-way conflict)
        self.assertEqual(2, s["type_conflicts"])   # r1 & r2 conflicting T:Type Mark


class XamlAndScriptWiringTests(unittest.TestCase):
    def test_all_xaml_event_handlers_exist_on_window_class(self):
        import ast
        import re

        bundle_dir = Path(LIB_DIR).parent
        xaml_text = (bundle_dir / "UI.xaml").read_text(encoding="utf-8")
        script_text = (bundle_dir / "script.py").read_text(encoding="utf-8")

        self.assertNotIn('IsCancel="True"', xaml_text)
        self.assertIn("handle_esc=False", script_text)

        tree = ast.parse(script_text)
        window_methods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "UltimateBrowserWindow":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        window_methods.add(item.name)

        event_attrs = (
            "Click", "Checked", "Unchecked", "SelectionChanged", "TextChanged",
            "MouseDoubleClick", "Sorting", "BeginningEdit", "CellEditEnding",
            "Closed",
        )
        pattern = re.compile(r'\b(?:' + "|".join(event_attrs) + r')="([^"]+)"')
        handlers = sorted(set(pattern.findall(xaml_text)))
        self.assertTrue(handlers, "Expected XAML event handlers to be discovered")
        missing = [h for h in handlers if h not in window_methods]
        self.assertEqual([], missing, "Missing handlers on UltimateBrowserWindow: {}".format(missing))

    def test_critical_wpf_and_ironpython_invariants(self):
        import re

        bundle_dir = Path(LIB_DIR).parent
        script_text = (bundle_dir / "script.py").read_text(encoding="utf-8")

        # Every Dispatcher.BeginInvoke must wrap its callable in System.Action(...) for IronPython 2.7.
        calls = re.findall(r"Dispatcher\.BeginInvoke\(([^\n]+)", script_text)
        self.assertTrue(calls, "Expected Dispatcher.BeginInvoke calls")
        for call in calls:
            self.assertTrue(call.lstrip().startswith("Action("),
                            "Dispatcher.BeginInvoke must wrap callback in Action(...): {}".format(call))

        # Editing must never rely on DisplayIndex (breaks when user reorders columns).
        self.assertNotIn("DisplayIndex", script_text)
        # CellEditEnding must ignore cancelled edits (Esc).
        self.assertIn("DataGridEditAction.Commit", script_text)

    def test_csv_fallback_export_writes_utf8_bom_csv(self):
        tmp = tempfile.mkdtemp()
        try:
            out_path = os.path.join(tmp, "fallback.xlsx")
            res = excel_io.export_schedule_csv(out_path, [_make_record(1, mark=u"L-01")], ["Category", "I:Mark"])
            self.assertEqual("csv", res["format"])
            self.assertTrue(res["path"].endswith(".csv"))
            raw = Path(res["path"]).read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            text = raw.decode("utf-8-sig")
            self.assertIn("UniqueId,Element Id,Category,Mark", text)
            self.assertIn("uid-1,1,Lighting Fixtures,L-01", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
