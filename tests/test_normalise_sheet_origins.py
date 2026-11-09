"""Tests for Normalise Sheet Origins: pure logic plus the script with a mocked Revit API."""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

BUTTON_DIR = (
    Path(__file__).resolve().parents[1]
    / "CustomPyRevit.extension" / "ETC PyTools.tab" / "Views and Sheets.panel"
    / "Normalise Sheet Origins.pushbutton"
)
spec = importlib.util.spec_from_file_location(
    "sheet_origins_logic", str(BUTTON_DIR / "lib" / "sheet_origins_logic.py")
)
logic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logic)


def root_info(**overrides):
    info = {
        "is_view": False, "has_category": True, "in_group": False, "is_nested": False,
        "is_revision_schedule": False, "is_viewport_or_schedule": False, "has_bbox": True,
    }
    info.update(overrides)
    return info


class LogicTests(unittest.TestCase):
    def test_natural_sort(self):
        self.assertEqual(sorted(["A10", "a2", "A1"], key=logic.natural_sort_key), ["A1", "a2", "A10"])

    def test_corner_shift_moves_target_onto_reference(self):
        self.assertEqual(logic.corner_shift((0.0, 0.0), (0.5, 0.25)), (-0.5, -0.25))

    def test_needs_shift_respects_tolerance(self):
        self.assertFalse(logic.needs_shift((0.001, -0.001), 0.01))
        self.assertTrue(logic.needs_shift((0.0, 0.02), 0.01))

    def test_movement_roots(self):
        self.assertTrue(logic.is_movement_root(root_info()))
        self.assertTrue(logic.is_movement_root(root_info(has_bbox=False, is_viewport_or_schedule=True)))
        for skipped in (
            root_info(is_view=True),
            root_info(has_category=False),
            root_info(in_group=True),
            root_info(is_nested=True),
            root_info(is_revision_schedule=True, is_viewport_or_schedule=True),
            root_info(has_bbox=False),
        ):
            self.assertFalse(logic.is_movement_root(skipped), skipped)

    def test_audit_status_and_csv_row(self):
        self.assertEqual(logic.audit_status((0.0, 0.00001), 0.0001), "Aligned")
        self.assertEqual(logic.audit_status((0.0, 0.01), 0.0001), "Misaligned")
        self.assertEqual(logic.csv_row(['A, "B"', None, 1.5]), '"A, ""B""","","1.5"')

    def test_drop_followers_keeps_hosts_and_breaks_no_cycles(self):
        deps = {1: {1, 2, 99}, 2: set(), 3: {4}, 4: {3}}
        self.assertEqual(logic.drop_followers([1, 2, 3, 4], deps), [1, 3, 4])


class FakeXYZ(object):
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = x, y, z


class FakeElement(object):
    def __init__(self, key, sheet, box=(0.0, 0.0), category=True, pinned=False, dependents=()):
        self.Id = SimpleNamespace(Value=key)
        self.OwnerViewId = sheet.Id if sheet else None
        self.GroupId = SimpleNamespace(Value=-1)
        self.Category = SimpleNamespace(Name="Cat") if category else None
        self.Pinned = pinned
        self.SuperComponent = None
        self.box = box
        self.dependents = [SimpleNamespace(Value=d) for d in dependents]

    def get_BoundingBox(self, view):
        if self.box is None:
            return None
        return SimpleNamespace(Min=SimpleNamespace(X=self.box[0], Y=self.box[1], Z=0.0))

    def GetDependentElements(self, element_filter):
        return self.dependents


class ScriptHarness(unittest.TestCase):
    def setUp(self):
        db = SimpleNamespace(
            UnitUtils=SimpleNamespace(
                ConvertToInternalUnits=lambda v, u: v / 304.8,
                ConvertFromInternalUnits=lambda v, u: v * 304.8,
            ),
            UnitTypeId=SimpleNamespace(Millimeters="mm"),
            XYZ=FakeXYZ,
            TransactionStatus=SimpleNamespace(Committed="Committed"),
            ElementId=object,
        )
        for name in ("FamilyInstance", "ScheduleSheetInstance", "Viewport", "Sketch", "SketchPlane"):
            setattr(db, name, type(name, (FakeElement,), {}))
        db.View = type("View", (FakeElement,), {})
        self.db = db
        pyrevit = ModuleType("pyrevit")
        pyrevit.DB = db
        pyrevit.forms = SimpleNamespace(alert=Mock())
        pyrevit.revit = SimpleNamespace()
        pyrevit.script = SimpleNamespace(get_output=Mock())
        generic = ModuleType("System.Collections.Generic")

        class TypedList(list):
            def Add(self, item):
                self.append(item)

        generic.List = _AnyKey(TypedList)
        system = ModuleType("System")
        system.__path__ = []
        collections = ModuleType("System.Collections")
        collections.__path__ = []
        patcher = patch.dict(sys.modules, {
            "pyrevit": pyrevit, "sheet_origins_logic": logic, "System": system,
            "System.Collections": collections, "System.Collections.Generic": generic,
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        script_spec = importlib.util.spec_from_file_location(
            "normalise_sheet_origins_script", str(BUTTON_DIR / "script.py")
        )
        self.module = importlib.util.module_from_spec(script_spec)
        script_spec.loader.exec_module(self.module)
        self.sheet = SimpleNamespace(Id=SimpleNamespace(Value=100), SheetNumber="A1")

    def set_sheet_contents(self, elements):
        collector = Mock()
        collector.WherePasses.return_value = collector
        collector.WhereElementIsNotElementType.return_value = collector
        collector.ToElements.return_value = elements
        self.db.FilteredElementCollector = Mock(return_value=collector)
        self.db.ElementOwnerViewFilter = Mock(return_value="owner")

    def make_doc(self, titleblock, move_to=None, commit="Committed"):
        doc = Mock()
        transaction = Mock()
        transaction.Commit.return_value = commit
        transaction.HasStarted.return_value = True
        transaction.HasEnded.return_value = False
        self.db.Transaction = Mock(return_value=transaction)

        def move(doc_, ids, vector):
            if move_to is None:
                titleblock.box = (titleblock.box[0] + vector.X, titleblock.box[1] + vector.Y)
            else:
                titleblock.box = move_to
        self.db.ElementTransformUtils = SimpleNamespace(MoveElements=Mock(side_effect=move))
        return doc, transaction


class ScriptTests(ScriptHarness):
    def test_movement_roots_skip_followers_views_and_internal_elements(self):
        sheet = self.sheet
        titleblock = self.db.FamilyInstance(1, sheet, dependents=(1, 2, 3))
        nested = self.db.FamilyInstance(2, sheet)
        nested.SuperComponent = titleblock
        revision = self.db.ScheduleSheetInstance(3, sheet, box=None)
        revision.IsTitleblockRevisionSchedule = True
        schedule_view = self.db.View(4, sheet, box=None)
        sketch_plane = FakeElement(5, sheet, box=None, category=False)
        viewport = self.db.Viewport(6, sheet, box=None)
        legend_text = FakeElement(7, sheet)
        hidden_internal = FakeElement(8, sheet, box=None)
        self.set_sheet_contents([
            titleblock, nested, revision, schedule_view, sketch_plane, viewport,
            legend_text, hidden_internal,
        ])

        roots = self.module.movement_roots(object(), sheet)

        self.assertEqual([self.module.id_value(item.Id) for item in roots], [1, 6, 7])

    def test_move_sheet_moves_all_roots_and_restores_pins(self):
        titleblock = self.db.FamilyInstance(1, self.sheet, box=(0.1, 0.2), pinned=True)
        viewport = self.db.Viewport(6, self.sheet, pinned=True)
        self.set_sheet_contents([titleblock, viewport])
        doc, transaction = self.make_doc(titleblock)

        result = self.module.move_sheet(doc, self.sheet, titleblock, (0.0, 0.0), 0.0001)

        self.assertEqual(result["status"], "Moved")
        self.assertEqual(result["moved"], 2)
        vector = self.db.ElementTransformUtils.MoveElements.call_args[0][2]
        self.assertAlmostEqual(vector.X, -0.1)
        self.assertAlmostEqual(vector.Y, -0.2)
        self.assertEqual(vector.Z, 0.0)
        self.assertTrue(titleblock.Pinned and viewport.Pinned)
        transaction.Commit.assert_called_once()
        transaction.RollBack.assert_not_called()

    def test_move_sheet_rolls_back_when_title_block_does_not_arrive(self):
        titleblock = self.db.FamilyInstance(1, self.sheet, box=(0.1, 0.2))
        self.set_sheet_contents([titleblock])
        doc, transaction = self.make_doc(titleblock, move_to=(0.1, 0.2))

        result = self.module.move_sheet(doc, self.sheet, titleblock, (0.0, 0.0), 0.0001)

        self.assertTrue(result["status"].startswith("FAILED"))
        self.assertEqual(result["moved"], 0)
        transaction.RollBack.assert_called_once()
        transaction.Commit.assert_not_called()

    def test_move_sheet_reports_errors_and_rolls_back(self):
        titleblock = self.db.FamilyInstance(1, self.sheet, box=(0.1, 0.2))
        self.set_sheet_contents([titleblock])
        doc, transaction = self.make_doc(titleblock)
        self.db.ElementTransformUtils.MoveElements.side_effect = RuntimeError("owned by Bob")

        result = self.module.move_sheet(doc, self.sheet, titleblock, (0.0, 0.0), 0.0001)

        self.assertIn("owned by Bob", result["status"])
        transaction.RollBack.assert_called_once()

    def test_aligned_sheet_is_not_touched(self):
        titleblock = self.db.FamilyInstance(1, self.sheet, box=(0.0, 0.00001))
        doc, transaction = self.make_doc(titleblock)

        result = self.module.move_sheet(doc, self.sheet, titleblock, (0.0, 0.0), 0.0001)

        self.assertEqual(result["status"], "Already aligned")
        self.db.Transaction.assert_not_called()


class AuditTests(ScriptHarness):
    def make_sheet(self, key, number):
        return SimpleNamespace(Id=SimpleNamespace(Value=key), SheetNumber=number, Name="Plan")

    def test_audit_classifies_each_sheet_and_reports_offset_from_reference(self):
        reference = self.make_sheet(1, "R")
        shifted = self.make_sheet(2, "A2")
        aligned = self.make_sheet(3, "A3")
        empty = self.make_sheet(4, "A4")
        double = self.make_sheet(5, "A5")
        blocks = {
            1: [self.db.FamilyInstance(10, reference, box=(0.0, 0.0))],
            2: [self.db.FamilyInstance(20, shifted, box=(0.05, -0.2))],
            3: [self.db.FamilyInstance(30, aligned, box=(0.00001, 0.0))],
            5: [self.db.FamilyInstance(50, double), self.db.FamilyInstance(51, double)],
        }

        rows, complete = self.module.audit_sheets(
            [reference, shifted, aligned, empty, double], blocks, reference, (0.0, 0.0), 0.0001
        )

        self.assertTrue(complete)

        self.assertEqual([row["status"] for row in rows], [
            "Reference", "Misaligned", "Aligned", "No title block", "Multiple title blocks",
        ])
        self.assertEqual(rows[1]["offset"], (0.05, -0.2))
        self.assertIs(rows[1]["titleblock"], blocks[2][0])
        self.assertEqual(self.module.offset_cells(rows[1]), ["15.240", "-60.960"])
        self.assertEqual(self.module.offset_cells(rows[3]), ["-", "-"])

    def test_audit_stops_when_progress_is_cancelled(self):
        sheets = [self.make_sheet(key, "A{}".format(key)) for key in range(1, 6)]
        blocks = dict((key, [self.db.FamilyInstance(key * 10, sheets[key - 1])]) for key in range(1, 6))
        progress = SimpleNamespace(cancelled=False)

        def update(value, max_value):
            if value == 2:
                progress.cancelled = True
        progress.update_progress = update

        rows, complete = self.module.audit_sheets(
            sheets, blocks, sheets[0], (0.0, 0.0), 0.0001, progress
        )

        self.assertFalse(complete)
        self.assertEqual(len(rows), 2)

    def test_align_stops_between_sheets_on_cancel_and_marks_the_rest(self):
        group = Mock()
        self.db.TransactionGroup = Mock(return_value=group)
        rows = [{"sheet": self.make_sheet(k, "A{}".format(k)), "titleblock": None,
                 "action": "Not moved"} for k in range(1, 4)]
        progress = SimpleNamespace(cancelled=False)

        def update(value, max_value):
            progress.cancelled = True
        progress.update_progress = update

        with patch.object(self.module, "move_sheet",
                          return_value={"status": "Moved", "moved": 3}) as move:
            complete = self.module.align_sheets(object(), rows, (0.0, 0.0), 0.0001, progress)

        self.assertFalse(complete)
        move.assert_called_once()
        self.assertEqual([row["action"] for row in rows], [
            "Moved (3 elements)", "Not moved (cancelled)", "Not moved (cancelled)",
        ])
        group.Assimilate.assert_called_once()

    def test_record_csv_lists_every_sheet_with_audit_and_action(self):
        import os
        import tempfile
        reference = self.make_sheet(1, "R")
        shifted = self.make_sheet(2, "A2")
        rows = [
            {"sheet": reference, "offset": None, "status": "Reference", "action": ""},
            {"sheet": shifted, "offset": (0.05, 0.0), "status": "Misaligned",
             "action": "Moved (4 elements)"},
        ]
        path = os.path.join(tempfile.mkdtemp(), "record.csv")
        self.module.forms.save_file = Mock(return_value=path)
        doc = SimpleNamespace(Title="Project")

        self.assertEqual(self.module.save_record(doc, rows, reference, (0.0, 0.0)), path)

        with open(path, encoding="utf-8-sig") as handle:
            text = handle.read()
        self.assertIn('"Reference sheet","R - Plan"', text)
        self.assertIn('"A2","Plan","15.240","0.000","Misaligned","Moved (4 elements)"', text)


class _AnyKey(object):
    def __init__(self, value):
        self.value = value

    def __getitem__(self, key):
        return self.value


if __name__ == "__main__":
    unittest.main()
