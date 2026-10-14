"""Check button behavior without requiring Revit or pyRevit."""

import runpy
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "CustomPyRevit.extension"
    / "Custom Tools.tab"
    / "Getting Started.panel"
    / "List Sheets.pushbutton"
    / "script.py"
)


class ListSheetsTests(unittest.TestCase):
    def setUp(self):
        self.output = Mock()
        self.doc = SimpleNamespace(IsFamilyDocument=False)
        self.get_sheets = Mock(return_value=[])
        self.pyrevit = ModuleType("pyrevit")
        self.pyrevit.revit = SimpleNamespace(
            doc=self.doc, query=SimpleNamespace(get_sheets=self.get_sheets)
        )
        self.pyrevit.script = SimpleNamespace(get_output=lambda: self.output)

    def run_button(self):
        with patch.dict(sys.modules, {"pyrevit": self.pyrevit}):
            runpy.run_path(str(SCRIPT_PATH), run_name="__main__")

    def test_lists_all_sheets_sorted_by_number(self):
        self.get_sheets.return_value = [
            SimpleNamespace(SheetNumber="S101", Name="Structure", IsPlaceholder=False),
            SimpleNamespace(SheetNumber="a102", Name="Proposed Plan", IsPlaceholder=True),
            SimpleNamespace(SheetNumber="A101", Name="Floor Plan", IsPlaceholder=False),
        ]

        self.run_button()

        self.get_sheets.assert_called_once_with(
            include_placeholders=True, include_noappear=True, doc=self.doc
        )
        self.output.print_table.assert_called_once_with(
            table_data=[
                ["A101", "Floor Plan", "No"],
                ["a102", "Proposed Plan", "Yes"],
                ["S101", "Structure", "No"],
            ],
            columns=["Sheet Number", "Sheet Name", "Placeholder"],
        )
        self.output.print_md.assert_any_call("Total sheets: 3")

    def test_no_sheets(self):
        self.run_button()

        self.output.print_md.assert_called_once_with("This project contains no sheets.")
        self.output.print_table.assert_not_called()

    def test_no_document(self):
        self.pyrevit.revit.doc = None

        self.run_button()

        self.output.print_md.assert_called_once_with(
            "Open a Revit project to list its sheets."
        )
        self.get_sheets.assert_not_called()
        self.output.print_table.assert_not_called()

    def test_family_document(self):
        self.doc.IsFamilyDocument = True

        self.run_button()

        self.output.print_md.assert_called_once_with(
            "This is a family document. Open a Revit project to list its sheets."
        )
        self.get_sheets.assert_not_called()
        self.output.print_table.assert_not_called()


if __name__ == "__main__":
    unittest.main()
