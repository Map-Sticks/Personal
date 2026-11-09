"""Tests for the modified native Rename Selected Sheets button (name or number)."""

import runpy
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "CustomPyRevit.extension"
    / "NATIVE PYREVIT BUTTON MODS"
    / "Rename Selected Sheets.pushbutton"
    / "script.py"
)

BIP = SimpleNamespace(SHEET_NAME="SHEET_NAME", SHEET_NUMBER="SHEET_NUMBER")


class FakeParam(object):
    def __init__(self, sheet, key):
        self.sheet, self.key = sheet, key

    def AsString(self):
        return self.sheet.values[self.key]

    def Set(self, value):
        others = [s for s in self.sheet.model if s is not self.sheet]
        if self.key == "SHEET_NUMBER" and any(s.values[self.key] == value for s in others):
            raise Exception("Sheet number already in use")
        self.sheet.values[self.key] = value


class FakeSheet(object):
    def __init__(self, model, sid, number, name):
        self.model = model
        self.Id = SimpleNamespace(IntegerValue=sid)
        self.values = {"SHEET_NUMBER": number, "SHEET_NAME": name}
        self.Parameter = {k: FakeParam(self, k) for k in self.values}
        model.append(self)

    @property
    def SheetNumber(self):
        return self.values["SHEET_NUMBER"]


class RenameSelectedSheetsTests(unittest.TestCase):
    def setUp(self):
        self.model = []
        self.a = FakeSheet(self.model, 1, "A101", "Plan")
        self.b = FakeSheet(self.model, 2, "A102", "Section")
        self.c = FakeSheet(self.model, 3, "B101", "Detail")

        collector = Mock()
        collector.OfClass.return_value = collector
        collector.WhereElementIsNotElementType.return_value = collector
        collector.ToElements.side_effect = lambda: list(self.model)

        self.transactions = []

        @contextmanager
        def transaction(name):
            self.transactions.append(name)
            yield

        self.forms = SimpleNamespace(
            select_sheets=Mock(), alert=Mock(return_value=True), ask_for_string=Mock(),
            CommandSwitchWindow=SimpleNamespace(show=Mock()),
        )
        db = SimpleNamespace(
            BuiltInParameter=BIP, ViewSheet=object,
            FilteredElementCollector=Mock(return_value=collector),
        )
        self.pyrevit = ModuleType("pyrevit")
        self.pyrevit.revit = SimpleNamespace(doc=object(), Transaction=transaction)
        self.pyrevit.DB = db
        self.pyrevit.forms = self.forms

    def run_button(self, sheets, field, mode, strings=()):
        self.forms.select_sheets.return_value = sheets
        self.forms.CommandSwitchWindow.show.side_effect = [
            field, (mode, {"Show Report": False}),
        ]
        self.forms.ask_for_string.side_effect = list(strings)
        with patch.dict(sys.modules, {"pyrevit": self.pyrevit}), patch("builtins.print"):
            return runpy.run_path(str(SCRIPT_PATH), run_name="__main__")

    def test_renames_sheet_numbers_with_prefix(self):
        self.run_button([self.a, self.b], "Sheet Number", "Add Prefix", ["X-"])
        self.assertEqual((self.a.SheetNumber, self.b.SheetNumber), ("X-A101", "X-A102"))
        self.assertEqual(self.a.values["SHEET_NAME"], "Plan")
        self.assertEqual(self.transactions, ["Rename Selected Sheets (Sheet Number)"])

    def test_sheet_numbers_can_be_swapped(self):
        self.run_button([self.a, self.b], "Sheet Number", "Find & Replace", ["A10", "A10"])
        self.assertEqual(self.transactions, [])  # nothing changed

        self.a.values["SHEET_NUMBER"], self.b.values["SHEET_NUMBER"] = "1", "2"
        ns = self.run_button([], "Sheet Number", "to UPPERCASE")
        pairs = [
            {"sheet": self.a, "sheet_id": 1, "label": "Plan", "old_name": "1", "new_name": "2"},
            {"sheet": self.b, "sheet_id": 2, "label": "Section", "old_name": "2", "new_name": "1"},
        ]
        with patch.dict(sys.modules, {"pyrevit": self.pyrevit}):
            renamed, failed = ns["_execute_renames"](pairs, "Sheet Number")
        self.assertEqual(failed, [])
        self.assertEqual((self.a.SheetNumber, self.b.SheetNumber), ("2", "1"))

    def test_skips_number_that_exists_on_unselected_sheet(self):
        self.run_button([self.a], "Sheet Number", "Find & Replace", ["A", "B"])
        self.assertEqual(self.a.SheetNumber, "A101")
        self.assertEqual(self.transactions, [])
        self.assertIn("No valid unique", self.forms.alert.call_args[0][0])

    def test_name_mode_still_renames_names_only(self):
        self.run_button([self.a, self.c], "Sheet Name", "to UPPERCASE")
        self.assertEqual(self.a.values["SHEET_NAME"], "PLAN")
        self.assertEqual(self.c.values["SHEET_NAME"], "DETAIL")
        self.assertEqual(self.a.SheetNumber, "A101")

    def test_cancel_at_field_choice_does_nothing(self):
        self.forms.select_sheets.return_value = [self.a]
        self.forms.CommandSwitchWindow.show.side_effect = [None]
        with patch.dict(sys.modules, {"pyrevit": self.pyrevit}):
            runpy.run_path(str(SCRIPT_PATH), run_name="__main__")
        self.assertEqual(self.transactions, [])
        self.assertEqual(self.forms.CommandSwitchWindow.show.call_count, 1)

    def test_resolve_conflicts_rejects_empty_and_duplicate_targets(self):
        with patch.dict(sys.modules, {"pyrevit": self.pyrevit}):
            ns = runpy.run_path(str(SCRIPT_PATH), run_name="not_main")
        pairs = [
            {"sheet_id": 1, "label": "", "old_name": "A", "new_name": ""},
            {"sheet_id": 2, "label": "", "old_name": "B", "new_name": "Z"},
            {"sheet_id": 3, "label": "", "old_name": "C", "new_name": "Z"},
        ]
        valid, conflicts = ns["_resolve_conflicts"](pairs, {1: "A", 2: "B", 3: "C"})
        self.assertEqual(valid, [])
        self.assertEqual(len(conflicts), 3)


if __name__ == "__main__":
    unittest.main()
