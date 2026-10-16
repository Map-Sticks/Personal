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


class NodeList(list):
    def Add(self, item):
        self.append(item)

    def Clear(self):
        del self[:]


class TreeNode:
    def __init__(self):
        self.Items = NodeList()
        self.Header = None
        self.Tag = None
        self.IsExpanded = False


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


class ContentsLogicTests(unittest.TestCase):
    ITEMS = [
        {"id": 3, "category": "Lighting Fixtures", "family": "Downlight", "type": "LED", "mark": "B", "level": "L1", "space": 10},
        {"id": 1, "category": "Lighting Fixtures", "family": "Downlight", "type": "LED", "mark": "A", "level": "L1", "space": 10},
        {"id": 2, "category": "Data Devices", "family": "Outlet", "type": "Double", "mark": "", "level": "", "space": 10},
        {"id": 4, "category": "Electrical Fixtures", "family": "GPO", "type": "Single", "mark": "", "level": "L2", "space": None},
    ]

    def test_index_and_counts(self):
        index = logic.index_contents(self.ITEMS, "space")
        self.assertEqual([3, 1, 2], [i["id"] for i in index[10]])
        self.assertEqual([4], [i["id"] for i in index[None]])
        self.assertEqual({"Lighting Fixtures": 2, "Data Devices": 1}, logic.category_counts(index[10]))

    def test_grouping_orders_categories_types_and_marks(self):
        groups = logic.group_contents(self.ITEMS[:3])
        self.assertEqual(["Lighting Fixtures", "Data Devices"], [g[0] for g in groups])
        name, ids, instances = groups[0][2][0]
        self.assertEqual("Downlight : LED", name)
        self.assertEqual([1, 3], ids)
        self.assertEqual("Mark A \u00b7 L1 \u00b7 Id 1", instances[0][0])
        self.assertEqual([("Id 2", 2)], groups[1][2][0][2])

    def test_grouping_respects_category_filter(self):
        self.assertEqual(["Data Devices"], [g[0] for g in logic.group_contents(self.ITEMS, ["Data Devices"])])
        self.assertEqual([], logic.group_contents(self.ITEMS, []))

    def test_phases_are_ordered_latest_first(self):
        self.assertEqual([30, 10, 99], logic.order_phase_ids([10, 99, 30], [10, 20, 30]))
        self.assertEqual([], logic.order_phase_ids([], [1]))

    def test_content_category_settings(self):
        self.assertEqual(logic.CONTENT_LABELS, logic.normalize_content_categories(None))
        self.assertEqual(["Lighting Fixtures", "Data Devices"],
                         logic.normalize_content_categories(["Data Devices", "Doors", "Lighting Fixtures"]))
        self.assertEqual([], logic.normalize_content_categories([]))

    def test_count_fields_are_numbers_and_never_read_from_model(self):
        field = logic.count_field("Data Devices")
        self.assertTrue(logic.is_count_field(field))
        self.assertIn(field, logic.count_fields())
        rec = {"values": {}, "counts": {"Data Devices": 3}}
        self.assertEqual(3, logic.record_value(rec, field))
        self.assertEqual(0, logic.record_value(rec, logic.count_field("Lighting Fixtures")))
        self.assertEqual([], logic.missing_fields([rec], [field]))


class XamlWiringTests(unittest.TestCase):
    def test_every_xaml_handler_and_name_exists_in_script(self):
        root = ET.parse(str(BUNDLE / "UI.xaml")).getroot()
        source = (BUNDLE / "script.py").read_text(encoding="utf-8")
        handlers, names = set(), set()
        for el in root.iter():
            for key, value in el.attrib.items():
                if key in ("Click", "SelectionChanged", "TextChanged", "MouseDoubleClick", "Checked", "Unchecked"):
                    handlers.add(value)
                if key.endswith("}Name"):
                    names.add(value)
        self.assertTrue(handlers)
        for handler in handlers:
            self.assertRegex(source, r"def {}\(self, sender, args\)".format(handler))
        for used in set(re.findall(r"self\.((?:cmb|txt|lbl|chk|tree)[A-Z]\w*|grid)\b", source)):
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
               DataGridRow=type("DataGridRow", (), {}), ItemsControl=Mock(), TreeViewItem=TreeNode)
        module("System.Windows.Data", Binding=Mock())
        self.forms = SimpleNamespace(WPFWindow=object, alert=Mock(), SelectFromList=Mock(),
                                     TemplateListItem=Mock())
        self.logger = Mock()
        self.FamilyInstance = type("FamilyInstance", (), {})
        db = SimpleNamespace(ElementId=Id, FilteredElementCollector=Mock(), FamilyInstance=self.FamilyInstance,
                             BuiltInParameter=SimpleNamespace(ALL_MODEL_MARK="Mark", ROOM_NAME="Name", ROOM_PHASE="Phase"),
                             BuiltInCategory=SimpleNamespace(OST_MEPSpaces="S", OST_Rooms="R",
                                                             OST_LightingFixtures="LF", OST_DataDevices="DD"))
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
        w.chkUnassigned = SimpleNamespace(IsChecked=False)
        w.lblContents = SimpleNamespace(Text="")
        w.treeContents = SimpleNamespace(Items=NodeList(), SelectedItem=None)
        w.content_categories = list(logic.CONTENT_LABELS)
        w.contents_ids = []
        w.phase_notes = {"Spaces": "Spaces matched in phase: NC"}
        w.contents = {"Spaces": logic.index_contents(ContentsLogicTests.ITEMS, "space"), "Rooms": {}}
        w.records["Spaces"][0]["label"] = "101 Office"
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


    def fixture(self, eid, category_unused, space_id):
        e = self.FamilyInstance()
        e.Id, e.LevelId, e.Name = Id(eid), Id(1), "LED"
        e.Symbol = SimpleNamespace(FamilyName="Downlight")
        e.get_Parameter = lambda bip: Param("Mark", "M{}".format(eid))
        # Only the phase passed in matters; the phase-less .Space/.Room must not be used.
        e.get_Space = lambda phase: SimpleNamespace(Id=Id(space_id)) if (space_id and phase == "NC") else None
        e.get_Room = Mock(side_effect=RuntimeError("not in phase"))
        e.Space = property_trap = SimpleNamespace(Id=Id(-999))
        e.Room = property_trap
        return e

    def test_read_contents_records_space_and_skips_non_instances(self):
        collector = self.mod.DB.FilteredElementCollector
        chain = collector.return_value.OfCategory.return_value.WhereElementIsNotElementType.return_value
        chain.ToElements.side_effect = [[self.fixture(5, "LF", 10), object()], [self.fixture(6, "DD", None)]]
        spaces = self.mod.SpatialLookup("get_Space", ["Existing", "NC"])
        rooms = self.mod.SpatialLookup("get_Room", [SimpleNamespace(Name="NC")])
        items = self.mod.read_contents(self.doc, spaces, rooms)
        self.assertEqual(1, spaces.found)
        self.assertEqual(2, rooms.errors)
        self.assertIn("2 lookup error(s), first: not in phase", rooms.summary("rooms"))
        self.assertEqual("No spaces found in this model.", self.mod.SpatialLookup("get_Space", []).summary("spaces"))
        self.assertEqual([(5, "Lighting Fixtures", 10, None, "M5", "L1", "Downlight", "LED"),
                          (6, "Data Devices", None, None, "M6", "L1", "Downlight", "LED")],
                         [(i["id"], i["category"], i["space"], i["room"], i["mark"], i["level"], i["family"], i["type"])
                          for i in items])

    def test_spatial_phases_use_room_phase_parameter_in_sequence(self):
        def spatial(phase_value):
            return SimpleNamespace(CreatedPhaseId=Id(1), get_Parameter=lambda bip: SimpleNamespace(
                HasValue=True, AsElementId=lambda: Id(phase_value)))
        chain = self.mod.DB.FilteredElementCollector.return_value.OfCategory.return_value.WhereElementIsNotElementType.return_value
        chain.ToElements.return_value = [spatial(20), spatial(30), spatial(20), spatial(-1)]
        self.doc.Phases = [SimpleNamespace(Id=Id(1)), SimpleNamespace(Id=Id(20)), SimpleNamespace(Id=Id(30))]
        self.doc.GetElement = lambda eid: "phase{}".format(eid.Value)
        self.assertEqual(["phase30", "phase20", "phase1"], self.mod.spatial_phases(self.doc, "S"))

    def test_selected_space_lists_its_elements(self):
        self.w.selected_element_ids = lambda: [10]
        self.w.update_contents()
        self.assertEqual("101 Office - 3 element(s)\nSpaces matched in phase: NC", self.w.lblContents.Text)
        self.assertEqual(["Lighting Fixtures (2)", "Data Devices (1)"], [n.Header for n in self.w.treeContents.Items])
        self.assertEqual(["Downlight : LED (2)"], [n.Header for n in self.w.treeContents.Items[0].Items])
        self.assertEqual([1, 3, 2], self.w.contents_ids)

    def test_unassigned_and_category_filter(self):
        self.w.selected_element_ids = lambda: []
        self.w.update_contents()
        self.assertIn("Select a space", self.w.lblContents.Text)
        self.w.chkUnassigned.IsChecked = True
        self.w.update_contents()
        self.assertEqual([4], self.w.contents_ids)
        self.w.content_categories = ["Data Devices"]
        self.w.update_contents()
        self.assertEqual([], self.w.contents_ids)
        self.assertIn("1 hidden by Categories", self.w.lblContents.Text)

    def test_contents_buttons_use_highlighted_node_or_everything(self):
        self.doc.GetElement = lambda eid: SimpleNamespace(Id=eid)
        self.w.selected_element_ids = lambda: [10]
        self.w.update_contents()
        self.w.contents_select_clicked(None, None)
        self.w.run_queued(self.app)
        self.assertEqual(3, len(self.uidoc.Selection.SetElementIds.call_args[0][0]))
        self.w.treeContents.SelectedItem = self.w.treeContents.Items[1]
        self.w.contents_show_clicked(None, None)
        self.w.run_queued(self.app)
        self.assertEqual([Id(2).Value], [i.Value for i in self.uidoc.Selection.SetElementIds.call_args[0][0]])
        self.uidoc.ShowElements.assert_called_once()


if __name__ == "__main__":
    unittest.main()
