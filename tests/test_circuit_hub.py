"""CircuitHub logic and modeless action tests; no Revit/WPF installation needed."""

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET


BUNDLE = (Path(__file__).resolve().parents[1] / "CustomPyRevit.extension"
          / "Custom Tools.tab" / "Electrical.panel" / "CircuitHub.pushbutton")
LIB = BUNDLE / "lib"

spec = importlib.util.spec_from_file_location("circuit_hub_logic", LIB / "circuit_hub_logic.py")
logic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logic)


class Id:
    def __init__(self, value):
        self.Value = value


class Panel:
    def __init__(self, panel_id, name, readonly=False):
        self.Id = Id(panel_id)
        self.Name = "Family type, not Panel Name"
        self.value = name
        self.param = Mock(IsReadOnly=readonly, StorageType="String")
        self.param.AsString.side_effect = lambda: self.value
        self.param.Set.side_effect = self.set_name

    def set_name(self, value):
        self.value = value
        return True

    def get_Parameter(self, parameter):
        return self.param


class RenameLogicTests(unittest.TestCase):
    def test_plan_trims_names_and_skips_unchanged(self):
        self.assertEqual([(2, "B", "New B")], logic.panel_rename_plan(
            {1: "A", 2: "B"}, [(1, "A"), (2, " New B ")]))

    def test_case_only_rename_and_unicode(self):
        self.assertEqual([(1, "Panel", "panel")],
                         logic.panel_rename_plan({1: "Panel"}, [(1, "panel")]))
        self.assertEqual("Pr\u00e9-\u00c9tage", logic.bulk_panel_name("\u00c9tage", prefix="Pr\u00e9-"))

    def test_replacement_then_prefix_suffix(self):
        self.assertEqual("X-DB-DB-1-Z",
                         logic.bulk_panel_name("PB-PB-1", "PB", "DB", "X-", "-Z"))
        self.assertEqual("X-PB", logic.bulk_panel_name("PB", "", "ignored", "X-"))
        self.assertEqual("PB", logic.bulk_panel_name("PB-A", "-A", ""))

    def test_invalid_plans_reject_all_changes(self):
        cases = [
            [(1, "")], [(1, " \t ")], [(1, "A\nB")],
            [(1, "B")], [(1, " b ")],
            [(1, "C"), (2, "c")], [(1, "C"), (1, "D")],
            [(3, "C")], [(1, "B"), (2, "A")],
        ]
        for proposals in cases:
            with self.subTest(proposals=proposals), self.assertRaises(ValueError):
                logic.panel_rename_plan({1: "A", 2: "B"}, proposals)

    def test_large_and_legacy_ids(self):
        self.assertEqual(2 ** 40, logic.element_id_value(Id(2 ** 40)))
        self.assertEqual(17, logic.element_id_value(SimpleNamespace(IntegerValue=17)))

    def test_id_read_failure_not_silently_downgraded(self):
        class Broken:
            @property
            def Value(self):
                raise RuntimeError("Disposed")
        with self.assertRaises(RuntimeError):
            logic.element_id_value(Broken())


class FamilyTypeLogicTests(unittest.TestCase):
    PAIRS = [("Panelboard", "225A"), ("panelboard MLO", "100A"), ("Panelboard", "400A"),
             ("Switchboard", "225A")]

    def test_options_are_sorted_and_types_follow_family(self):
        families, types = logic.family_type_options(self.PAIRS)
        self.assertEqual(["Panelboard", "panelboard MLO", "Switchboard"], families)
        self.assertEqual(["100A", "225A", "400A"], types)
        self.assertEqual(["225A", "400A"], logic.family_type_options(self.PAIRS, "Panelboard")[1])
        self.assertEqual([], logic.family_type_options(self.PAIRS, "Missing")[1])

    def test_matching_and_active_state(self):
        all_f, all_t = logic.ALL_FAMILIES, logic.ALL_TYPES
        self.assertFalse(logic.family_type_filter_active(all_f, all_t))
        self.assertFalse(logic.family_type_filter_active(None, ""))
        self.assertTrue(logic.family_type_filter_active(all_f, "225A"))
        self.assertTrue(logic.matches_family_type("Panelboard", "225A", all_f, all_t))
        self.assertTrue(logic.matches_family_type("Switchboard", "225A", all_f, "225A"))
        self.assertFalse(logic.matches_family_type("Panelboard", "400A", "Panelboard", "225A"))
        self.assertFalse(logic.matches_family_type("Switchboard", "225A", "Panelboard", all_t))

    def test_panel_row_text_includes_name_count_family_and_type(self):
        self.assertEqual("+ PANEL  DB-1 (3)   |   Panelboard : 225A",
                         logic.panel_row_text("+", "DB-1", 3, "Panelboard", "225A"))


class CircuitHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.panels = {1: Panel(1, "A"), 2: Panel(2, "B")}
        self.doc = SimpleNamespace(IsValidObject=True, IsFamilyDocument=False)
        self.doc.Equals = lambda other: self.doc is other
        self.doc.GetElement = lambda eid: self.panels.get(eid.Value)
        self.uidoc = SimpleNamespace(Document=self.doc, Selection=Mock())
        self.uidoc.Selection.GetElementIds.return_value = []
        self.app = SimpleNamespace(ActiveUIDocument=self.uidoc)
        self.native = Mock()
        self.native.Commit.return_value = "Committed"
        self.native.GetStatus.return_value = "Started"
        native = self.native
        panels = self.panels

        class Transaction:
            def __init__(self, name, **kwargs):
                self._rvtxn = native
                self.snapshot = {pid: panel.value for pid, panel in panels.items()}
                native.RollBack.side_effect = self.rollback

            def rollback(self):
                for pid, value in self.snapshot.items():
                    panels[pid].value = value

            def __enter__(self):
                native.Start()
                return self

        modules = {}

        def module(name, **members):
            result = ModuleType(name)
            result.__dict__.update(members)
            modules[name] = result
            return result

        module("clr", AddReference=Mock())
        module("System", Int32=int, Int64=int)
        module("System.Collections")
        module("System.Collections.Generic", List=SimpleNamespace(__unused__=True))
        windows = module("System.Windows")
        for name in ("MessageBox", "MessageBoxButton", "MessageBoxImage", "Thickness",
                     "FontWeights", "VerticalAlignment", "DragDrop", "DragDropEffects",
                     "DataObject"):
            setattr(windows, name, Mock())
        controls = module("System.Windows.Controls")
        windows.Controls = controls
        for name in ("ListBoxItem", "StackPanel", "Button", "TextBlock", "ItemsControl"):
            setattr(controls, name, Mock())
        inputs = module("System.Windows.Input")
        for name in ("Keyboard", "Key", "ModifierKeys", "MouseButtonState"):
            setattr(inputs, name, Mock())
        module("System.Windows.Media", Brushes=Mock(), VisualTreeHelper=Mock())
        self.forms = SimpleNamespace(WPFWindow=object, alert=Mock(return_value=True),
                                     ask_for_string=Mock(), SelectFromList=Mock())
        self.logger = Mock()
        self.revit = SimpleNamespace(doc=self.doc, uidoc=self.uidoc, Transaction=Transaction)
        module("pyrevit", revit=self.revit, forms=self.forms,
               HOST_APP=SimpleNamespace(version="2025"),
               script=SimpleNamespace(get_logger=lambda: self.logger))
        module("Autodesk")
        module("Autodesk.Revit")
        db = module("Autodesk.Revit.DB", ElementId=Id,
                    TransactionStatus=SimpleNamespace(Committed="Committed", Started="Started"),
                    StorageType=SimpleNamespace(String="String"),
                    BuiltInCategory=SimpleNamespace(OST_ElectricalEquipment=-2001040),
                    BuiltInParameter=SimpleNamespace(RBS_ELEC_PANEL_NAME="Panel Name"),
                    Level=type("Level", (), {}), ElementSet=Mock,
                    FilteredElementCollector=Mock())
        module("Autodesk.Revit.DB.Electrical",
               ElectricalSystem=type("ElectricalSystem", (), {}),
               ElectricalSystemType=SimpleNamespace(PowerCircuit="Power"),
               PanelScheduleView=Mock(), PanelScheduleTemplate=type("Template", (), {}))
        module("Autodesk.Revit.UI", IExternalEventHandler=object, ExternalEvent=Mock(),
               ExternalEventRequest=SimpleNamespace(Accepted="Accepted", Pending="Pending"),
               RevitCommandId=Mock(), PostableCommand=Mock())
        module("Autodesk.Revit.UI.Selection", ISelectionFilter=object, ObjectType=Mock())
        module("Autodesk.Revit.Exceptions",
               OperationCanceledException=type("OperationCanceledException", (Exception,), {}))
        modules["circuit_hub_logic"] = logic
        spec = importlib.util.spec_from_file_location("circuit_hub_test_script", BUNDLE / "script.py")
        self.mod = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {"APPDATA": self.temp.name}):
            spec.loader.exec_module(self.mod)
        self.mod.CACHE.panels = self.panels
        self.mod.CACHE.dirty = False
        self.window = self.mod.CircuitHub.__new__(self.mod.CircuitHub)
        for name, value in dict(
                building=False, closed=False, executing=False, document=self.doc,
                document_change_subscribed=True, pending_external_action=None,
                external_event=Mock(), auto_refresh_enabled=True).items():
            setattr(self.window, name, value)
        self.window.external_event.Raise.return_value = "Accepted"
        self.window.log = Mock()
        self.window.refresh = Mock()
        self.window.selected_tags = Mock(return_value=[("panel", 1), ("panel", 2)])
        self.mod.show_warning = Mock()
        self.mod.show_error = Mock()
        self.db = db

    def test_panel_labels_use_parameter_not_family_name(self):
        self.assertEqual("A", self.mod.get_panel_name(self.panels[1]))

    def test_rename_is_one_transaction_and_preserves_both_panels(self):
        self.window.executing = True
        self.window.rename_panels([(1, "DB-A"), (2, "DB-B")])
        self.assertEqual(["DB-A", "DB-B"], [self.panels[i].value for i in (1, 2)])
        self.native.Start.assert_called_once()
        self.native.Commit.assert_called_once()
        self.native.Dispose.assert_called_once()
        self.window.refresh.assert_called_once()
        self.assertIn("A (id 1) -> DB-A", self.forms.alert.call_args.kwargs["expanded"])

    def test_rename_cancel_and_noop_do_not_start_transaction(self):
        self.window.executing = True
        self.forms.alert.return_value = False
        self.window.rename_panels([(1, "New")])
        self.window.rename_panels([(1, "A")])
        self.native.Start.assert_not_called()
        self.assertEqual("A", self.panels[1].value)

    def test_duplicate_and_readonly_preflight_do_not_write(self):
        self.window.executing = True
        with self.assertRaises(self.mod.UserFacingError):
            self.window.rename_panels([(1, "B")])
        self.panels[2].param.IsReadOnly = True
        with self.assertRaises(self.mod.UserFacingError):
            self.window.panels_for_rename()
        self.native.Start.assert_not_called()
        self.panels[1].param.Set.assert_not_called()

    def test_failed_second_write_rolls_back_first_and_never_reports_success(self):
        self.window.executing = True
        self.panels[2].param.Set.side_effect = lambda name: False
        with self.assertRaises(self.mod.UserFacingError):
            self.window.rename_panels([(1, "New A"), (2, "New B")])
        self.assertEqual("A", self.panels[1].value)
        self.native.RollBack.assert_called_once()
        self.native.Commit.assert_not_called()
        self.window.refresh.assert_not_called()

    def test_noncommitted_status_and_commit_exception_are_not_success(self):
        for result in ("RolledBack", "Pending"):
            with self.subTest(result=result):
                self.native.Commit.return_value = result
                self.native.GetStatus.return_value = result
                with self.assertRaises(self.mod.UserFacingError):
                    self.mod.run_transaction("test", lambda: "Success")
        self.native.Commit.side_effect = RuntimeError("Commit failed")
        self.native.GetStatus.return_value = "Started"
        with self.assertRaisesRegex(RuntimeError, "Commit failed"):
            self.mod.run_transaction("test", lambda: "Success")
        self.native.RollBack.assert_called_once()

    def test_callback_defers_all_reads_until_execute(self):
        self.window.on_rename_panel(None, None)
        self.window.selected_tags.assert_not_called()
        self.forms.ask_for_string.assert_not_called()
        self.window.external_event.Raise.assert_called_once()
        self.window.selected_tags.return_value = [("panel", 1)]
        self.forms.ask_for_string.return_value = "New"
        self.window.execute_pending_external_event(self.app)
        self.assertEqual("New", self.panels[1].value)

    def test_external_event_uses_owning_window_and_passes_current_application(self):
        owner = Mock()
        self.mod.APP = Mock()
        handler = self.mod.CircuitHubExternalEventHandler(owner)
        handler.Execute(self.app)
        owner.execute_pending_external_event.assert_called_once_with(self.app)
        self.mod.APP.execute_pending_external_event.assert_not_called()

    def test_document_listener_ignores_other_projects(self):
        self.mod.APP = self.window
        self.mod.on_revit_document_changed(
            None, SimpleNamespace(GetDocument=lambda: object()))
        self.assertFalse(self.mod.CACHE.dirty)
        self.window.external_event.Raise.assert_not_called()
        self.mod.on_revit_document_changed(
            None, SimpleNamespace(GetDocument=lambda: self.doc))
        self.assertTrue(self.mod.CACHE.dirty)
        self.window.external_event.Raise.assert_called_once()

    def test_id_construction_uses_explicit_revit_year_overload(self):
        for use_64 in (False, True):
            with self.subTest(use_64=use_64):
                self.mod.USE_64_BIT_IDS = use_64
                self.mod.Int32 = Mock(side_effect=int)
                self.mod.Int64 = Mock(side_effect=int)
                value = 2 ** 40 if use_64 else 17
                self.assertEqual(value, self.mod.as_eid(value).Value)
                used = self.mod.Int64 if use_64 else self.mod.Int32
                unused = self.mod.Int32 if use_64 else self.mod.Int64
                used.assert_called_once_with(value)
                unused.assert_not_called()

    def test_highlighting_rows_does_not_block_next_button_with_pending_event(self):
        self.window.target_circuit_id = None
        self.window.update_details = Mock()
        self.window.selected_tags.return_value = [("circuit", 20)]
        self.window.on_list_selection_changed(None, None)
        self.assertEqual(20, self.window.target_circuit_id)
        self.window.external_event.Raise.assert_not_called()

    def test_details_are_plain_cached_text_not_model_reads(self):
        self.mod.CACHE.circuit_details = {20: ("A", "2", "Lighting")}
        self.window.target_circuit_id = 20
        self.window.txtPanel = SimpleNamespace(Text="")
        self.window.txtCircuit = SimpleNamespace(Text="")
        self.window.txtLoad = SimpleNamespace(Text="")
        self.window.txtSelectedSummary = SimpleNamespace(Text="")
        self.doc.GetElement = Mock(side_effect=AssertionError("Model read outside ExternalEvent"))
        self.window.update_details()
        self.assertEqual("Target panel: A", self.window.txtPanel.Text)
        self.doc.GetElement.assert_not_called()

    def test_bulk_callback_wiring_uses_form_values(self):
        self.window.tbPanelFind = SimpleNamespace(Text="A")
        self.window.tbPanelReplace = SimpleNamespace(Text="Z")
        self.window.tbPanelPrefix = SimpleNamespace(Text="DB-")
        self.window.tbPanelSuffix = SimpleNamespace(Text="-1")
        self.window.on_bulk_rename_panels(None, None)
        self.window.execute_pending_external_event(self.app)
        self.assertEqual(["DB-Z-1", "DB-B-1"], [self.panels[i].value for i in (1, 2)])
        self.native.Commit.assert_called_once()

    def test_document_switch_close_and_window_close_block_queued_work(self):
        work = Mock()
        for state in ("switch", "closed-document", "closed-window", "no-document"):
            with self.subTest(state=state):
                self.window.closed = state == "closed-window"
                self.doc.IsValidObject = state != "closed-document"
                self.app.ActiveUIDocument = (
                    None if state == "no-document" else
                    SimpleNamespace(Document=object()) if state == "switch" else self.uidoc)
                self.window.pending_external_action = ("test", work, False, False)
                self.window.execute_pending_external_event(self.app)
        work.assert_not_called()
        self.native.Start.assert_not_called()

    def test_dirty_cache_rebuilt_before_work_even_with_auto_refresh_off(self):
        self.window.auto_refresh_enabled = False
        self.window.queue_refresh_from_doc_changed()
        self.assertTrue(self.mod.CACHE.dirty)
        self.window.external_event.Raise.assert_not_called()
        calls = []
        self.mod.CACHE.build = lambda: calls.append("build")
        self.window.run_direct("test", lambda: calls.append("work"), False, False)
        self.window.execute_pending_external_event(self.app)
        self.assertEqual(["build", "work"], calls)

    def test_document_changes_coalesce_with_action_refresh(self):
        self.window.executing = True
        self.window.queue_refresh_from_doc_changed()
        self.window.queue_refresh_from_doc_changed()
        self.window.external_event.Raise.assert_not_called()
        self.window.executing = False
        self.window.queue_refresh_from_doc_changed()
        self.window.queue_refresh_from_doc_changed()
        self.window.external_event.Raise.assert_called_once()

    def test_rejected_raise_clears_pending_action_and_reports_error(self):
        self.window.external_event.Raise.return_value = "Denied"
        self.window.run_direct("test", Mock(), False, False)
        self.assertIsNone(self.window.pending_external_action)
        self.mod.show_error.assert_called_once()

    def test_busy_action_is_not_overwritten(self):
        first = Mock()
        self.window.run_direct("first", first, False, False)
        self.window.run_direct("second", Mock(), False, False)
        self.assertEqual("first", self.window.pending_external_action[0])
        self.window.external_event.Raise.assert_called_once()
        self.mod.show_warning.assert_called_once()

    def test_selection_validation_error_is_caught_by_action_runner(self):
        self.window.on_remove_revit_selection_from_all_circuits(None, None)
        self.window.execute_pending_external_event(self.app)
        self.mod.show_warning.assert_called_once()
        self.native.Start.assert_not_called()

    def test_none_level_and_membership_lookup_do_not_scan_circuit_elements(self):
        self.assertEqual("<No Level>", self.mod.element_level_name(None))
        circuit = Mock()
        self.mod.CACHE.memberships = {99: [circuit]}
        for _ in range(100):
            self.assertEqual([circuit], self.mod.find_circuits_containing(99))
        self.assertEqual([], self.mod.find_circuits_containing(100))
        self.assertEqual([], circuit.mock_calls)

    def test_cache_build_indexes_power_memberships_once_and_skips_unknown_systems(self):
        fixture = SimpleNamespace(Id=Id(2 ** 40), Category=SimpleNamespace(Name="Fixtures"))

        class Circuit:
            def __init__(self, system_type, panel, cid):
                self.Id = Id(cid)
                self.SystemType = system_type
                self.BaseEquipment = panel
                self.reads = 0

            @property
            def Elements(self):
                self.reads += 1
                return [fixture, fixture]

        class Unreadable(Circuit):
            @property
            def SystemType(self):
                raise RuntimeError("Unreadable")

            @SystemType.setter
            def SystemType(self, value):
                pass

        power = Circuit("Power", self.panels[1], 20)
        data = Circuit("Data", self.panels[1], 30)
        unknown = Unreadable("Unknown", self.panels[1], 40)
        collector = Mock()
        collector.OfClass.side_effect = lambda kind: (
            [power, data, unknown] if kind is self.mod.ElectricalSystem else [])
        collector.OfCategory.return_value.WhereElementIsNotElementType.return_value = list(self.panels.values())
        self.mod.FilteredElementCollector = Mock(return_value=collector)
        self.mod.panel_level_name = lambda panel: "<No Level>"
        self.mod.CACHE.build()
        self.assertFalse(self.mod.CACHE.dirty)
        self.assertEqual([power], self.mod.CACHE.circuits)
        for _ in range(100):
            self.assertEqual([power], self.mod.find_circuits_containing(2 ** 40))
        self.assertEqual(1, power.reads)
        self.assertEqual(0, data.reads)
        self.assertEqual(0, unknown.reads)
        self.logger.warning.assert_called_once()

    def test_row_removal_only_removes_displayed_membership(self):
        self.window.executing = True
        fixture = SimpleNamespace(Id=Id(10))
        first, second = Mock(Id=Id(20)), Mock(Id=Id(30))
        elements = {10: fixture, 20: first, 30: second}
        self.doc.GetElement = lambda eid: elements[eid.Value]
        self.mod.CACHE.memberships = {10: [first, second]}
        self.window.on_inline_remove_fixture(SimpleNamespace(Tag=("element", 10, 20)), None)
        first.RemoveFromCircuit.assert_called_once()
        second.RemoveFromCircuit.assert_not_called()

    def test_target_only_expands_unassigned_level_group(self):
        circuit = SimpleNamespace(Id=Id(20), BaseEquipment=None, Elements=[])
        self.doc.GetElement = lambda eid: circuit
        self.window.executing = True
        self.window.target_circuit_id = 20
        self.window.expanded_panel_ids = set()
        self.window.expanded_circuit_ids = set()
        self.window.build_list = Mock()
        self.window.on_show_target_only(None, None)
        self.assertEqual({("unassigned", "<No Level>")}, self.window.expanded_panel_ids)

    def use_fake_id_list(self):
        class FakeList(list):
            def Add(self, item):
                self.append(item)
        self.mod.List = {self.mod.ElementId: FakeList}

    def test_panel_family_and_type_prefers_type_and_falls_back_to_parameters(self):
        symbol = SimpleNamespace(FamilyName="Panelboard", Name="225A", Id=Id(9))
        panel = SimpleNamespace(Id=Id(1), GetTypeId=lambda: Id(9))
        self.doc.GetElement = lambda eid: symbol
        self.assertEqual(("Panelboard", "225A"), self.mod.panel_family_and_type(panel))
        self.doc.GetElement = lambda eid: None
        self.mod.BuiltInParameter.ELEM_FAMILY_PARAM = "family"
        self.mod.BuiltInParameter.ELEM_TYPE_PARAM = "type"
        params = {"family": Mock(**{"AsValueString.return_value": "Fam"}), "type": None}
        panel.get_Parameter = lambda bip: params[bip]
        self.assertEqual(("Fam", "<No Type>"), self.mod.panel_family_and_type(panel))

    def build_rows(self, family_filter, type_filter, search=""):
        class Items(list):
            Clear = list.clear
            Add = list.append
        unassigned = SimpleNamespace(Id=Id(50), BaseEquipment=None, CircuitNumber="1",
                                     Elements=[], get_Parameter=lambda bip: None)
        on_a = SimpleNamespace(Id=Id(60), BaseEquipment=self.panels[1], CircuitNumber="2",
                               Elements=[], get_Parameter=lambda bip: None)
        self.mod.CACHE.circuits = [unassigned, on_a]
        self.mod.CACHE.panel_family_types = {1: ("Panelboard", "225A"), 2: ("Switchboard", "400A")}
        self.mod.CACHE.memberships = {}
        self.mod.panel_level_name = lambda panel: "L1"
        self.mod.circuit_level_name = lambda circuit: "L1"
        w = self.window
        w.lbItems = SimpleNamespace(Items=Items(), SelectedItems=[])
        w.make_item = lambda text, tag: SimpleNamespace(text=text, Tag=tag)
        w.tbSearch = SimpleNamespace(Text=search)
        for name in ("txtPanel", "txtCircuit", "txtLoad", "txtSelectedSummary"):
            setattr(w, name, SimpleNamespace(Text=""))
        w.selected_tags = Mock(return_value=[])
        w.pending_select_tags = []
        w.target_circuit_id = None
        w.expanded_panel_ids = set()
        w.expanded_circuit_ids = set()
        w.level_filter = "<All Levels>"
        w.show_empty_panels = True
        w.family_filter = family_filter
        w.type_filter = type_filter
        w.build_list()
        return [item.text for item in w.lbItems.Items]

    def test_panel_rows_show_family_type_and_filters_hide_other_panels(self):
        rows = self.build_rows(self.mod.ALL_FAMILIES, self.mod.ALL_TYPES)
        self.assertEqual(["+ PANEL  A (1)   |   Panelboard : 225A",
                          "+ PANEL  B (0)   |   Switchboard : 400A",
                          "+ PANEL  UNASSIGNED / NO PANEL - L1 (1)"], rows)
        self.assertEqual(["+ PANEL  B (0)   |   Switchboard : 400A"],
                         self.build_rows("Switchboard", self.mod.ALL_TYPES))
        self.assertEqual(["+ PANEL  A (1)   |   Panelboard : 225A"],
                         self.build_rows(self.mod.ALL_FAMILIES, "225A"))
        self.assertEqual([], self.build_rows("Panelboard", "400A"))

    def test_search_matches_panel_family_or_type(self):
        rows = self.build_rows(self.mod.ALL_FAMILIES, self.mod.ALL_TYPES, search="switchboard")
        self.assertEqual(["- PANEL  B (0)   |   Switchboard : 400A"], rows)

    def test_select_panels_in_model_selects_real_panels_without_moving_view(self):
        self.use_fake_id_list()
        self.uidoc.ShowElements = Mock()
        self.window.selected_tags = Mock(return_value=[
            ("panel", 2), ("unassigned_panel", ("unassigned", "L1")), ("circuit", 20), ("panel", 2)])
        self.window.executing = True
        self.window.on_select_panels_in_model(None, None)
        ids = self.uidoc.Selection.SetElementIds.call_args.args[0]
        self.assertEqual([2], [x.Value for x in ids])
        self.uidoc.ShowElements.assert_not_called()

    def test_show_panels_in_view_selects_and_shows(self):
        self.use_fake_id_list()
        self.uidoc.ShowElements = Mock()
        self.window.executing = True
        self.window.on_show_panels_in_view(None, None)
        shown = self.uidoc.ShowElements.call_args.args[0]
        self.assertEqual([1, 2], [x.Value for x in shown])
        self.uidoc.Selection.SetElementIds.assert_called_once_with(shown)

    def test_panel_actions_without_panel_rows_warn_and_do_nothing(self):
        self.window.selected_tags = Mock(return_value=[("unassigned_panel", ("unassigned", "L1"))])
        self.window.on_select_panels_in_model(None, None)
        self.window.execute_pending_external_event(self.app)
        self.mod.show_warning.assert_called_once()
        self.uidoc.Selection.SetElementIds.assert_not_called()

    def test_rows_select_in_revit_includes_highlighted_panels(self):
        self.use_fake_id_list()
        self.uidoc.ShowElements = Mock()
        self.window.selected_tags = Mock(return_value=[("panel", 1)])
        self.window.executing = True
        self.window.on_select_rows_in_view(None, None)
        self.assertEqual([1], [x.Value for x in self.uidoc.Selection.SetElementIds.call_args.args[0]])
        self.uidoc.ShowElements.assert_called_once()

    def test_type_combo_follows_family_and_resets_missing_saved_type(self):
        class Items(list):
            Clear = list.clear
            Add = list.append

            @property
            def Count(self):
                return len(self)

        class Combo:
            def __init__(self):
                self.Items = Items()
                self.SelectedIndex = 0

            @property
            def SelectedItem(self):
                return self.Items[self.SelectedIndex]
        families, types = Combo(), Combo()
        self.window.cmbFamilyFilter, self.window.cmbTypeFilter = families, types
        self.window.building = False
        self.mod.CACHE.panel_family_types = {1: ("Panelboard", "225A"), 2: ("Switchboard", "400A")}
        self.window.family_filter, self.window.type_filter = "Panelboard", "400A"
        self.window.populate_family_type_filters()
        self.assertEqual(["<All Families>", "Panelboard", "Switchboard"], families.Items)
        self.assertEqual(["<All Types>", "225A"], types.Items)
        self.assertEqual(1, families.SelectedIndex)
        self.assertEqual(("Panelboard", "<All Types>"), (self.window.family_filter, self.window.type_filter))
        self.assertFalse(self.window.building)

    def test_xaml_has_all_wired_controls_and_handlers(self):
        root = ET.parse(BUNDLE / "ui.xaml").getroot()
        names = {element.get("Name") for element in root.iter() if element.get("Name")}
        source = (BUNDLE / "script.py").read_text()
        import re
        for control, handler in re.findall(r"self\.(\w+)\.\w+ \+= self\.(on_\w+)", source):
            self.assertIn(control, names)
            self.assertTrue(hasattr(self.mod.CircuitHub, handler), handler)


if __name__ == "__main__":
    unittest.main()
