"""Parameter Browser logic and modeless wiring tests; no Revit/WPF installation needed."""

import importlib.util
import os
from pathlib import Path
import re
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET


BUNDLE = (Path(__file__).resolve().parents[1] / "CustomPyRevit.extension"
          / "Custom Tools.tab" / "Electrical.panel" / "ParameterBrowser.pushbutton")

spec = importlib.util.spec_from_file_location(
    "parameter_browser_logic", BUNDLE / "lib" / "parameter_browser_logic.py")
logic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logic)


class Id:
    def __init__(self, value):
        self.Value = value


class Param:
    def __init__(self, name, value_string=None, storage="StorageType.String", value=None, has_value=True):
        self.Definition = SimpleNamespace(Name=name)
        self.value_string = value_string
        self.StorageType = storage
        self.value = value
        self.HasValue = has_value

    def AsValueString(self):
        return self.value_string

    def AsString(self):
        return self.value

    def AsInteger(self):
        return self.value

    def AsDouble(self):
        return self.value

    def AsElementId(self):
        return self.value


class Element:
    def __init__(self, eid, level_id, params):
        self.Id = Id(eid)
        self.LevelId = Id(level_id)
        self.Category = SimpleNamespace(Name="Spaces")
        self.Parameters = list(params)
        self.lookups = []

    def LookupParameter(self, name):
        self.lookups.append(name)
        for p in self.Parameters:
            if p.Definition.Name == name:
                return p
        return None


class IdList(list):
    def Add(self, item):
        self.append(item)

    @property
    def Count(self):
        return len(self)


class LogicTests(unittest.TestCase):
    def test_settings_are_normalised(self):
        self.assertEqual(("Spaces", "Whole project", logic.DEFAULT_FIELDS), logic.normalize_settings(None))
        self.assertEqual(("Rooms", "Level", ["Area"]), logic.normalize_settings(
            {"source": "Rooms", "scope": "Level", "fields": ["Area", " "]}))
        self.assertEqual(("Spaces", "Whole project", logic.DEFAULT_FIELDS), logic.normalize_settings(
            {"source": "Doors", "scope": "x", "fields": "Name"}))

    def test_resolve_fields_keeps_order_and_falls_back(self):
        self.assertEqual(["Name", "Number"], logic.resolve_fields(["Name", "Gone", "Number", "Name"],
                                                                  ["Number", "Name"]))
        self.assertEqual(["Number", "Level"], logic.resolve_fields(["Gone"], ["Level", "Number"]))
        self.assertEqual(["Element Id"], logic.resolve_fields(["Gone"], ["Other"]))

    def test_column_choices_put_selected_first(self):
        self.assertEqual(["Name", "Area", "level", "Number"],
                         logic.column_choices(["Number", "level", "Name", "Area"], ["Name"]))

    def test_scope_filters(self):
        recs = [{"id": 1, "level": "L1", "visible": True}, {"id": 2, "level": "L2", "visible": False}]
        self.assertEqual(2, len(logic.filter_records(recs, "Whole project")))
        self.assertEqual([1], [r["id"] for r in logic.filter_records(recs, "Current view")])
        self.assertEqual([2], [r["id"] for r in logic.filter_records(recs, "Level", "L2")])
        self.assertEqual([], logic.filter_records(recs, "Level", None))
        self.assertEqual(["L1", "L2"], logic.level_options(recs + [{"level": ""}]))

    def test_missing_fields_ignore_pseudo_fields(self):
        recs = [{"values": {"Name": "A"}}, {"values": {}}]
        self.assertEqual(["Name", "Area"], logic.missing_fields(recs, ["Element Id", "Level", "Name", "Area"]))
        self.assertEqual([], logic.missing_fields([], ["Name"]))

    def test_record_values(self):
        rec = {"id": 2 ** 40, "level": "L1", "category": "Rooms", "values": {"Name": "Office"}}
        self.assertEqual(["1099511627776", "L1", "Rooms", "Office", ""],
                         [logic.record_value(rec, f) for f in ("Element Id", "Level", "Category", "Name", "X")])

    def test_row_filter_escapes_and_requires_all_tokens(self):
        self.assertEqual("", logic.build_row_filter("   "))
        self.assertEqual("[__search] LIKE '%ab%' AND [__search] LIKE '%1%'", logic.build_row_filter("AB 1"))
        self.assertEqual("[__search] LIKE '%o''[*][%][[][]]%'", logic.build_row_filter("o'*%[]"))

    def test_format_parameter_value(self):
        self.assertEqual("", logic.format_parameter_value(None))
        self.assertEqual("12 m²", logic.format_parameter_value(Param("A", "12 m²", "Double", 1.0)))
        self.assertEqual("Office", logic.format_parameter_value(Param("N", None, "String", "Office")))
        self.assertEqual("", logic.format_parameter_value(Param("N", None, "String", None)))
        self.assertEqual("3", logic.format_parameter_value(Param("I", None, "Integer", 3)))
        self.assertEqual("", logic.format_parameter_value(Param("I", None, "Integer", 3, has_value=False)))
        self.assertEqual("Level 1", logic.format_parameter_value(
            Param("L", None, "ElementId", Id(5)), lambda eid: "Level 1"))
        self.assertEqual("5", logic.format_parameter_value(Param("L", None, "ElementId", Id(5)), lambda eid: None))
        self.assertEqual("", logic.format_parameter_value(Param("L", None, "ElementId", Id(-1))))
        self.assertEqual("17", logic.format_parameter_value(
            Param("L", None, "ElementId", SimpleNamespace(IntegerValue=17))))

    def test_action_queue_runs_in_order_and_isolates_errors(self):
        queue, calls, errors = logic.ActionQueue(), [], []
        queue.add("a", lambda app: calls.append(("a", app)))
        queue.add("b", lambda app: 1 / 0)
        queue.add("c", lambda app: calls.append(("c", app)))
        self.assertEqual(3, queue.drain("APP", lambda label, ex: errors.append(label)))
        self.assertEqual([("a", "APP"), ("c", "APP")], calls)
        self.assertEqual(["b"], errors)
        self.assertEqual(0, len(queue))


class XamlWiringTests(unittest.TestCase):
    def test_every_xaml_handler_and_name_exists_in_script(self):
        root = ET.parse(str(BUNDLE / "UI.xaml")).getroot()
        source = (BUNDLE / "script.py").read_text(encoding="utf-8")
        handlers, names = set(), set()
        for el in root.iter():
            for key, value in el.attrib.items():
                if key in ("Click", "SelectionChanged", "TextChanged", "MouseDoubleClick"):
                    handlers.add(value)
                if key.endswith("}Name"):
                    names.add(value)
        self.assertTrue(handlers)
        for handler in handlers:
            self.assertRegex(source, r"def {}\(self, sender, args\)".format(handler))
        for used in set(re.findall(r"self\.((?:cmb|txt|lbl)\w+|grid)\b", source)):
            self.assertIn(used, names)


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
        module("System", Int32=int, Int64=int)
        module("System.Collections")
        module("System.Collections.Generic", List={Id: IdList})
        module("System.Data", DataTable=Mock())
        module("System.Windows", Visibility=SimpleNamespace(Visible="Visible", Collapsed="Collapsed"))
        module("System.Windows.Controls", DataGridTextColumn=Mock(), DataGridLength=Mock(),
               DataGridRow=type("DataGridRow", (), {}), ItemsControl=Mock())
        module("System.Windows.Data", Binding=Mock())
        self.forms = SimpleNamespace(WPFWindow=object, alert=Mock(), SelectFromList=Mock(),
                                     TemplateListItem=Mock())
        self.logger = Mock()
        db = SimpleNamespace(ElementId=Id, FilteredElementCollector=Mock(),
                             BuiltInCategory=SimpleNamespace(OST_MEPSpaces="S", OST_Rooms="R"))
        ui = SimpleNamespace(IExternalEventHandler=object, ExternalEvent=Mock(),
                             ExternalEventRequest=SimpleNamespace(Accepted="Accepted", Pending="Pending"))
        self.revit = SimpleNamespace(doc=None, uidoc=None)
        module("pyrevit", DB=db, UI=ui, HOST_APP=SimpleNamespace(version="2025"), forms=self.forms,
               revit=self.revit, script=SimpleNamespace(get_logger=lambda: self.logger,
                                                        get_bundle_file=lambda n: n))
        modules["parameter_browser_logic"] = logic
        script_spec = importlib.util.spec_from_file_location("parameter_browser_script", BUNDLE / "script.py")
        self.mod = importlib.util.module_from_spec(script_spec)
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {"APPDATA": self.temp.name}):
            script_spec.loader.exec_module(self.mod)

        self.elements = {
            10: Element(10, 1, [Param("Number", None, "String", "101"), Param("Area", "20 m²")]),
            11: Element(11, 2, [Param("Number", None, "String", "102")]),
        }
        self.doc = SimpleNamespace(IsValidObject=True, IsFamilyDocument=False, Title="Proj")
        self.doc.Equals = lambda other: other is self.doc
        self.doc.GetElement = lambda eid: (self.elements.get(eid.Value)
                                           or {1: SimpleNamespace(Name="L1"), 2: SimpleNamespace(Name="L2")}.get(eid.Value))
        self.uidoc = SimpleNamespace(Document=self.doc, Selection=Mock(), ShowElements=Mock())
        self.app = SimpleNamespace(ActiveUIDocument=self.uidoc)

        w = self.mod.ParameterBrowser.__new__(self.mod.ParameterBrowser)
        w.closed = False
        w.initialising = False
        w.queue = logic.ActionQueue()
        w.ext_event = Mock()
        w.ext_event.Raise.return_value = "Accepted"
        w.doc, w.doc_title, w.view_name = self.doc, "Proj", "Plan"
        w.fields = ["Number"]
        w.records = {"Spaces": [{"id": 10, "level": "L1", "visible": True, "category": "", "values": {"Number": "101"}},
                                {"id": 11, "level": "L2", "visible": False, "category": "", "values": {"Number": "102"}}],
                     "Rooms": []}
        w.param_names = {"Spaces": ["Area", "Element Id", "Level", "Number"], "Rooms": []}
        w.pending_fields = set()
        w.current_source = lambda: "Spaces"
        w.current_scope = lambda: "Whole project"
        w.current_level = lambda: None
        w.status = []
        w.set_status = w.status.append
        self.w = w

    def test_no_project_alerts_and_opens_nothing(self):
        self.forms.alert.assert_called_once()
        self.assertIsNone(self.mod.WINDOW)

    def test_handler_delegates_to_owning_window(self):
        owner = Mock()
        self.mod.BrowserEventHandler(owner).Execute(self.app)
        owner.run_queued.assert_called_once_with(self.app)

    def test_selection_is_deferred_until_execute(self):
        self.w.selected_element_ids = lambda: [10, 11]
        self.w.select_clicked(None, None)
        self.uidoc.Selection.SetElementIds.assert_not_called()
        self.w.ext_event.Raise.assert_called_once()
        self.w.run_queued(self.app)
        self.uidoc.Selection.SetElementIds.assert_called_once()
        self.assertEqual(2, len(self.uidoc.Selection.SetElementIds.call_args[0][0]))
        self.uidoc.ShowElements.assert_not_called()

    def test_show_uses_show_elements(self):
        self.w.selected_element_ids = lambda: [10]
        self.w.show_clicked(None, None)
        self.w.run_queued(self.app)
        self.uidoc.ShowElements.assert_called_once()

    def test_other_active_document_is_refused(self):
        self.w.selected_element_ids = lambda: [10]
        self.w.select_clicked(None, None)
        other = SimpleNamespace(Document=SimpleNamespace(), Selection=Mock())
        self.w.run_queued(SimpleNamespace(ActiveUIDocument=other))
        other.Selection.SetElementIds.assert_not_called()
        self.assertIn("Switch back to 'Proj'", self.w.status[-1])

    def test_closed_window_drops_queued_work(self):
        self.w.selected_element_ids = lambda: [10]
        self.w.select_clicked(None, None)
        self.w.closed = True
        self.w.run_queued(self.app)
        self.uidoc.Selection.SetElementIds.assert_not_called()

    def test_rejected_raise_clears_queue(self):
        self.w.ext_event.Raise.return_value = "Denied"
        self.w.selected_element_ids = lambda: [10]
        self.w.select_clicked(None, None)
        self.assertEqual(0, len(self.w.queue))
        self.assertIn("could not queue", self.w.status[-1])

    def test_new_column_values_are_read_only_inside_execute(self):
        self.w.fields = ["Number", "Area"]
        self.w.rebuild_view()
        self.assertEqual([], self.elements[10].lookups)
        self.w.ext_event.Raise.assert_called_once()
        self.w.rebuild_view()
        self.w.ext_event.Raise.assert_called_once()
        self.w.rebuild_view = Mock()
        self.w.run_queued(self.app)
        self.assertEqual("20 m²", self.w.records["Spaces"][0]["values"]["Area"])
        self.assertEqual("", self.w.records["Spaces"][1]["values"]["Area"])
        self.assertEqual(set(), self.w.pending_fields)
        self.w.rebuild_view.assert_called_once()

    def test_ui_handler_reports_instead_of_raising(self):
        self.w.selected_element_ids = Mock(side_effect=RuntimeError("boom"))
        self.w.select_clicked(None, None)
        self.assertIn("boom", self.w.status[-1])

    def test_read_source_returns_plain_data(self):
        collector = self.mod.DB.FilteredElementCollector
        chain = collector.return_value.OfCategory.return_value.WhereElementIsNotElementType.return_value
        chain.ToElements.return_value = [self.elements[10], self.elements[11]]
        chain.ToElementIds.return_value = [Id(10)]
        records, names = self.mod.read_source(self.doc, "S", Id(99), ["Number"])
        self.assertEqual(["Area", "Category", "Element Id", "Level", "Number"], names)
        self.assertEqual([(10, "L1", True, "101"), (11, "L2", False, "102")],
                         [(r["id"], r["level"], r["visible"], r["values"]["Number"]) for r in records])
        self.assertNotIn("Area", records[0]["values"])


if __name__ == "__main__":
    unittest.main()
