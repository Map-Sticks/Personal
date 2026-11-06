"""Exercise the actual button functions with Revit/WPF boundaries mocked."""
import ast
import json
import io
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET


BUNDLE = (Path(__file__).resolve().parents[1] / "CustomPyRevit.extension"
          / "Custom Tools.tab" / "Electrical.panel" / "FindLinkedFixtures.pushbutton")
SOURCE = (BUNDLE / "script.py").read_text()


class Items(list):
    def Add(self, value):
        self.append(value)

    def Clear(self):
        self.clear()

    @property
    def Count(self):
        return len(self)


class Combo:
    def __init__(self, value=None):
        self.Items = Items()
        self.SelectedItem = value
        self._index = -1

    @property
    def SelectedIndex(self):
        return self._index

    @SelectedIndex.setter
    def SelectedIndex(self, value):
        self._index = value
        self.SelectedItem = self.Items[value]


class Id:
    def __init__(self, value):
        self.Value = value


class Point:
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = x, y, z


class Location:
    def __init__(self, point):
        self.Point = point


class ClrLong:
    """Non-JSON-native integer proxy like a boxed CLR Int64."""
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return str(self.value)

    def __repr__(self):
        return "{}L".format(self.value)


def load_button():
    tree = ast.parse(SOURCE)
    category_names = [
        "ElectricalFixtures", "DataDevices", "CommunicationDevices", "LightingFixtures",
        "LightingDevices", "FireAlarmDevices", "SecurityDevices", "ElectricalEquipment",
        "GenericModel", "SpecialityEquipment",
    ]
    bic = NS(**{"OST_" + name: -(i + 1) for i, name in enumerate(category_names)})
    env = dict(
        math=math, _re=re, json=json, io=io, shutil=shutil, os=os,
        BuiltInCategory=bic,
        forms=NS(WPFWindow=object, alert=Mock(), SelectFromList=NS(show=Mock()),
                 TemplateListItem=lambda item, checked: NS(item=item, checked=checked)),
        IExternalEventHandler=object, logger=Mock(),
        FamilyPlacementType=NS(OneLevelBased="level", WorkPlaneBased="plane"),
        TransactionStatus=NS(Started="started", Committed="committed"),
        ElementId=Id, XYZ=Point, LocationPoint=Location,
        traceback=__import__("traceback"),
    )
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            nodes.append(node)
        elif isinstance(node, ast.Assign) and all(
                isinstance(t, ast.Name) and t.id.isupper() and t.id != "SETTINGS_FILE"
                for t in node.targets):
            nodes.append(node)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(BUNDLE / "script.py"), "exec"), env)
    return env


class GenericMethod:
    def __init__(self, function):
        self.function = function

    def __getitem__(self, field_type):
        if field_type is not str:
            raise TypeError("Expected System.String")
        return self.function


class StorageAPI:
    """Mock only the API boundary; exercise production storage and transaction logic."""
    def __init__(self, env):
        self.document = NS(IsReadOnly=False, storages=[], Regenerate=Mock())
        self.schema = None
        self.transactions = []
        self.commit_status = "committed"
        self.start_status = "started"
        self.write_error = None
        self.corrupt_write = False
        self.after_commit = None
        api = self

        class FakeSchema:
            def __init__(self, guid):
                self.guid, self.fields = guid, {}

            def GetField(self, name):
                return self.fields.get(name)

            @staticmethod
            def Lookup(guid):
                return api.schema

        class Builder:
            def __init__(self, guid):
                self.schema = FakeSchema(guid)

            def SetSchemaName(self, value):
                self.schema.name = value

            def SetReadAccessLevel(self, value):
                self.schema.read = value

            def SetWriteAccessLevel(self, value):
                self.schema.write = value

            def AddSimpleField(self, name, field_type):
                if field_type is not str:
                    raise TypeError("Expected System.String")
                self.schema.fields[name] = name

            def Finish(self):
                api.schema = self.schema
                return self.schema

        class FakeEntity:
            def __init__(self, schema, valid=True, values=None):
                self.schema, self.valid = schema, valid
                self.values = dict(values or {})
                self.Set = GenericMethod(lambda field, value: self.values.__setitem__(field, value))
                self.Get = GenericMethod(lambda field: self.values.get(field, ""))

            def IsValid(self):
                return self.valid

        class Storage:
            def __init__(self):
                self.values = {}

            @staticmethod
            def Create(document):
                storage = Storage()
                document.storages.append(storage)
                return storage

            def SetEntity(self, entity):
                if api.write_error:
                    raise api.write_error
                self.values[entity.schema.guid] = dict(entity.values)
                if api.corrupt_write:
                    self.values[entity.schema.guid]["Payload"] = "damaged"

            def GetEntity(self, schema):
                return FakeEntity(schema, schema.guid in self.values, self.values.get(schema.guid))

        class Collector:
            def __init__(self, document):
                self.document = document

            def OfClass(self, cls):
                if cls is not Storage:
                    raise TypeError("Expected DataStorage")
                return self.document.storages

        class Tx:
            def __init__(self, document, name):
                self.document, self.name, self.status = document, name, "uninitialized"
                api.transactions.append(self)

            def Start(self):
                self.backup = [(s, {k: dict(v) for k, v in s.values.items()})
                               for s in self.document.storages]
                self.status = api.start_status
                return self.status

            def Commit(self):
                if api.commit_status != "committed":
                    self.RollBack()
                    return api.commit_status
                self.status = "committed"
                if api.after_commit:
                    api.after_commit()
                return self.status

            def RollBack(self):
                self.document.storages[:] = [s for s, values in self.backup]
                for storage, values in self.backup:
                    storage.values = values
                self.status = "rolledback"

            def GetStatus(self):
                return self.status

        self.Storage = Storage
        env.update(System=NS(Guid=lambda value: value, String=str, Action=lambda callback: callback),
                   Schema=FakeSchema, SchemaBuilder=Builder, Entity=FakeEntity,
                   DataStorage=Storage, FilteredElementCollector=Collector, Transaction=Tx,
                   AccessLevel=NS(Public="public"), doc=self.document)


class ButtonTests(unittest.TestCase):
    def setUp(self):
        self.env = load_button()
        self.Win = self.env["Win"]
        self.win = self.Win.__new__(self.Win)
        self.win.txtStatus = NS(Text="")
        self.win._closed = False
        self.win._applying_settings = False
        self.win._settings_ready = True

    def test_inner_boundary_is_not_filled_space(self):
        outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
        hole = [(4, 4), (6, 4), (6, 6), (4, 6)]
        self.assertFalse(self.env["inside_polys"](Point(5, 5, 2), [outer, hole]))
        self.assertTrue(self.env["inside_polys"](Point(2, 2, 2), [outer, hole]))

    def test_nested_island_and_disjoint_regions_are_inside(self):
        outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
        hole = [(2, 2), (8, 2), (8, 8), (2, 8)]
        island = [(4, 4), (6, 4), (6, 6), (4, 6)]
        other = [(20, 0), (30, 0), (30, 10), (20, 10)]
        self.assertTrue(self.env["inside_polys"](Point(5, 5, 2), [outer, hole, island]))
        self.assertTrue(self.env["inside_polys"](Point(25, 5, 2), [outer, other]))

    def test_space_name_uses_name_parameter_not_decorated_element_name(self):
        sp = NS(Id=Id(81), Number="101", Name="101", LevelId=Id(11),
                get_Parameter=lambda bip: NS(HasValue=True, AsString=lambda: "Nurse Station"))
        with patch.dict(self.env, BuiltInParameter=NS(ROOM_NAME="name"),
                        doc=NS(GetElement=lambda eid: NS(Name="Level 1"))):
            si = self.env["SI"](sp)
        self.assertEqual("Nurse Station", si.name)
        self.assertIn("101 - Nurse Station", si.disp)
        self.assertIn("Level 1", si.disp)

    def test_horizontal_tolerance_cannot_cross_an_inner_void(self):
        outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
        hole = [(4, 4), (6, 4), (6, 6), (4, 6)]
        sp = NS(IsPointInSpace=lambda point: True)
        self.assertFalse(self.env["inside_space"](sp, Point(5, 5, 2), [outer, hole], 100))

    def test_outer_boundary_tolerance_requires_real_space_volume_at_same_z(self):
        outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
        def contains(p):
            return 0 < p.X < 10 and 0 < p.Y < 10 and 0 < p.Z < 3
        sp = NS(IsPointInSpace=Mock(side_effect=contains))
        check = self.env["inside_space"]
        self.assertTrue(check(sp, Point(-0.2, 5, 2), [outer], 0.3))
        self.assertFalse(check(sp, Point(-0.2, 5, 4), [outer], 0.3))
        self.assertFalse(check(sp, Point(5, 5, 4), [outer], 0.3))
        self.assertFalse(check(sp, Point(-0.2, 5, 2), [outer], 0.1))
        self.assertFalse(check(sp, Point(-0.2, 5, 2), [outer], 0))
        self.assertTrue(all(call.args[0].Z in (2, 4) for call in sp.IsPointInSpace.call_args_list))

    def test_space_volume_checks_actual_slope_not_only_bounding_box(self):
        outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
        sp = NS(IsPointInSpace=lambda p: p.Z < p.X / 10 + 1)
        self.assertFalse(self.env["inside_space"](sp, Point(1, 5, 1.5), [outer], 10))
        self.assertTrue(self.env["inside_space"](sp, Point(9, 5, 1.5), [outer], 10))

    def test_containment_api_errors_are_not_silently_accepted(self):
        outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
        sp = NS(IsPointInSpace=Mock(side_effect=RuntimeError("Space is invalid")))
        with self.assertRaisesRegex(RuntimeError, "Space is invalid"):
            self.env["inside_space"](sp, Point(1, 1, 1), [outer], 0)

    def test_hole_and_island_classification_is_winding_and_order_independent(self):
        outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
        hole = [(2, 2), (8, 2), (8, 8), (2, 8)]
        island = [(4, 4), (6, 4), (6, 6), (4, 6)]
        check = self.env["inside_polys"]
        for loops in ([outer, hole, island], [list(reversed(hole)), island, outer]):
            self.assertFalse(check(Point(3, 3, 1), loops))
            self.assertTrue(check(Point(5, 5, 1), loops))
            self.assertFalse(check(Point(12, 12, 1), loops))

    def test_space_boundaries_use_revit_tessellation_not_eight_fixed_samples(self):
        coords = [(0, 0), (4, 0), (5, 1), (6, 2), (6, 3), (6, 4),
                  (5, 5), (4, 6), (3, 6), (2, 6), (1, 5), (0, 4), (0, 0)]
        curve = NS(Tessellate=Mock(return_value=[Point(x, y, 0) for x, y in coords]),
                   Evaluate=Mock(side_effect=AssertionError("Must not use fixed samples")))
        sp = NS(GetBoundarySegments=lambda options: [[NS(GetCurve=lambda: curve)]])
        with patch.dict(self.env, SpatialElementBoundaryOptions=lambda: object()):
            loops = self.env["space_polys"](sp)
        self.assertEqual(coords[:-1], loops[0])
        curve.Tessellate.assert_called_once()
        curve.Evaluate.assert_not_called()

    def test_missing_or_failed_boundary_stops_scan_instead_of_using_partial_loop(self):
        with patch.dict(self.env, SpatialElementBoundaryOptions=lambda: object()):
            with self.assertRaisesRegex(ValueError, "no boundary"):
                self.env["space_polys"](NS(GetBoundarySegments=lambda options: []))
            bad = NS(Tessellate=Mock(side_effect=RuntimeError("curve error")))
            sp = NS(GetBoundarySegments=lambda options: [[NS(GetCurve=lambda: bad)]])
            with self.assertRaisesRegex(RuntimeError, "curve error"):
                self.env["space_polys"](sp)

    def test_linked_height_uses_signed_host_z_and_project_level_not_shared_elevation(self):
        level = NS(ProjectElevation=20, Elevation=1020)
        self.assertAlmostEqual(609.6, self.env["linked_height_mm"](Point(1, 2, 22), level))
        self.assertAlmostEqual(-304.8, self.env["linked_height_mm"](Point(1, 2, 19), level))

    def geometry_env(self):
        class Solid:
            def __init__(self, faces):
                self.Faces = Items(faces)
                self.Faces.Size = len(faces)

        class Mesh:
            def __init__(self, triangles):
                self.triangles = triangles
                self.NumTriangles = len(triangles)

            def get_Triangle(self, i):
                return NS(get_Vertex=lambda k, t=self.triangles[i]: t[k])

        class GeometryInstance:
            def __init__(self, items):
                self.items = items

            def GetInstanceGeometry(self):
                return self.items

        self.env.update(Solid=Solid, Mesh=Mesh, GeometryInstance=GeometryInstance,
                        Options=lambda: NS(DetailLevel=None),
                        ViewDetailLevel=NS(Fine="fine"))
        return Solid, Mesh, GeometryInstance

    def test_geometry_height_includes_offset_built_into_family(self):
        Solid, Mesh, GeometryInstance = self.geometry_env()
        # Insertion point at the level (Z=0) but the family's 3D sits 700-780 mm up.
        lo, hi = 700 / 304.8, 780 / 304.8
        face = NS(Triangulate=lambda: Mesh([(Point(0, 0, lo), Point(1, 0, lo), Point(0, 0, hi))]))
        options = []
        element = NS(get_Geometry=lambda o: options.append(o) or [GeometryInstance([Solid([face])])],
                     get_BoundingBox=Mock())
        transform = NS(OfPoint=lambda p: Point(p.X + 5, p.Y, p.Z + 10))
        zmin, zmax = self.env["geometry_z_range"](element, transform)
        self.assertAlmostEqual(lo + 10, zmin)
        self.assertAlmostEqual(hi + 10, zmax)
        self.assertEqual("fine", options[0].DetailLevel)
        element.get_BoundingBox.assert_not_called()
        level = NS(ProjectElevation=10)
        self.assertAlmostEqual(0.0, self.env["linked_height_mm"](Point(0, 0, 10), level))
        self.assertAlmostEqual(700.0, self.env["ft2mm"](zmin - level.ProjectElevation))

    def test_geometry_height_skips_empty_solids_and_reads_meshes(self):
        Solid, Mesh, GeometryInstance = self.geometry_env()
        mesh = Mesh([(Point(0, 0, 2), Point(0, 0, 3), Point(0, 0, 4))])
        element = NS(get_Geometry=lambda o: [Solid([]), mesh], get_BoundingBox=Mock())
        self.assertEqual((2, 4), self.env["geometry_z_range"](element, NS(OfPoint=lambda p: p)))

    def test_geometry_height_falls_back_to_bounding_box(self):
        self.geometry_env()
        box = NS(Min=Point(0, 0, 1), Max=Point(1, 1, 2), Transform=NS(OfPoint=lambda p: p))
        element = NS(get_Geometry=lambda o: None, get_BoundingBox=lambda view: box)
        self.assertEqual((1, 2), self.env["geometry_z_range"](element, NS(OfPoint=lambda p: p)))
        element = NS(get_Geometry=lambda o: [], get_BoundingBox=lambda view: None)
        self.assertIsNone(self.env["geometry_z_range"](element, NS(OfPoint=lambda p: p)))

    def test_linked_height_text_shows_insertion_and_3d_extent(self):
        text, tip = self.env["linked_height_text"](0.0, 700.0, 780.0, "L01")
        self.assertEqual(u"Ins 0.0\n3D 700.0 / 780.0", text)
        self.assertIn("L01", tip)
        self.assertIn("3D centre = 740.0", tip)
        text, tip = self.env["linked_height_text"](None, None, None, "L01")
        self.assertEqual(u"Ins n/a\n3D n/a", text)

    def test_linked_level_match_includes_link_translation(self):
        transform = NS(OfPoint=lambda p: Point(-p.Y, p.X, p.Z + 100))
        linked, host = NS(ProjectElevation=10), NS(ProjectElevation=110)
        self.assertTrue(self.env["linked_level_matches"](transform, Point(3, 4, 12), linked, host))
        host.ProjectElevation = 110.01
        self.assertFalse(self.env["linked_level_matches"](transform, Point(3, 4, 12), linked, host))

    def test_old_level_settings_are_explicitly_migrated(self):
        for value in ("Same Level Name", None, "Match linked level elevation"):
            self.assertEqual("Match linked level elevation", self.env["level_mode"](value))
        for value in ("Ignore Level", "Space volume only"):
            self.assertEqual("Space volume only", self.env["level_mode"](value))

    def test_space_search_matches_name_number_level_and_multiple_terms(self):
        self.win._sps = [
            NS(disp="101 - Nurse Station [Level 1] (81)", search="101 nurse station level 1"),
            NS(disp="102 - Treatment [Level 1] (82)", search="102 treatment level 1"),
            NS(disp="101 - Nurse Station [Level 2] (83)", search="101 nurse station level 2"),
        ]
        self.win.lbSpaces = NS(Items=Items())
        self.win.tbSpaceSearch = NS(Text=" nurse   station  1 ")
        self.win._rebuild()
        self.assertEqual(2, len(self.win.lbSpaces.Items))
        self.win.tbSpaceSearch.Text = "nurse level 2"
        self.win._rebuild()
        self.assertEqual(["101 - Nurse Station [Level 2] (83)"], self.win.lbSpaces.Items)
        self.win.tbSpaceSearch.Text = "treatment"
        self.win._rebuild()
        self.assertEqual(["102 - Treatment [Level 1] (82)"], self.win.lbSpaces.Items)
        self.win.tbSpaceSearch.Text = ""
        self.win._rebuild()
        self.assertEqual(3, len(self.win._smap))

    def test_same_number_name_level_spaces_have_distinct_search_results(self):
        def space(eid):
            return NS(Id=Id(eid), Number="101", Name="101", LevelId=Id(11),
                      get_Parameter=lambda bip: NS(HasValue=True, AsString=lambda: "Nurse Station"))
        with patch.dict(self.env, BuiltInParameter=NS(ROOM_NAME="name"),
                        doc=NS(GetElement=lambda eid: NS(Name="Level 1"))):
            first, second = self.env["SI"](space(81)), self.env["SI"](space(82))
        self.assertNotEqual(first.disp, second.disp)
        self.assertEqual(first.search, second.search)

    def prepare_categories(self):
        self.win.cmbLinkInstance = Combo("Linked Model")
        self.win._scan_category_link = "Linked Model"
        self.win._scan_categories = [("-1", "Electrical Fixtures", Id(-1)),
                                     ("-99", "Nurse Call Devices", Id(-99))]
        self.win._target_categories = list(self.win._scan_categories)
        self.win._scan_category_keys = {"-1"}
        self.win._target_category_keys = {"-1"}
        self.win._visible_families = {"Old electrical family"}
        self.win._clear = Mock()
        self.win._rebuild_sbd = Mock()
        self.win._update_category_summary = Mock()
        self.win._on_save = Mock()

    def test_existing_default_scan_categories_are_preserved(self):
        self.assertEqual({"-1", "-2", "-3"}, self.env["selected_category_keys"]({}))

    def test_legacy_checkbox_settings_are_migrated(self):
        settings = {"cats": {"catElectricalFixtures": False, "catGenericModel": True}}
        self.assertEqual({"-2", "-3", "-9"}, self.env["selected_category_keys"](settings))

    def test_explicit_category_selection_overrides_legacy_settings(self):
        self.assertEqual({"-99"}, self.env["selected_category_keys"](
            {"scan_categories": ["-99"], "cats": {"catElectricalFixtures": True}}))

    def test_explicit_empty_selection_is_not_reset_to_defaults(self):
        self.assertEqual(set(), self.env["selected_category_keys"]({"scan_categories": []}))

    def test_nurse_call_category_can_be_selected_and_saved(self):
        self.prepare_categories()
        self.env["forms"].SelectFromList.show.return_value = [
            NS(item="Nurse Call Devices", checked=True)]
        self.win._choose_categories(False)
        self.assertEqual({"-99"}, self.win._scan_category_keys)
        self.assertEqual([-99], [cid.Value for cid in self.win._sel_cats()])
        self.win._clear.assert_called_once()
        self.win._on_save.assert_not_called()

    def test_picker_prechecks_previous_choices(self):
        self.prepare_categories()
        self.env["forms"].SelectFromList.show.return_value = None
        self.win._choose_categories(False)
        options = self.env["forms"].SelectFromList.show.call_args.args[0]
        self.assertEqual([True, False], [opt.checked for opt in options])
        self.win._on_save.assert_not_called()
        self.assertEqual({"-1"}, self.win._scan_category_keys)

    def test_host_category_change_removes_old_family_filter(self):
        self.prepare_categories()
        self.env["forms"].SelectFromList.show.return_value = [
            NS(item="Nurse Call Devices", checked=True)]
        self.win._choose_categories(True)
        self.assertEqual({"-99"}, self.win._target_category_keys)
        self.assertEqual(set(), self.win._visible_families)
        self.win._rebuild_sbd.assert_called_once()
        self.win._clear.assert_not_called()

    def test_no_categories_is_reported(self):
        self.prepare_categories()
        self.win._scan_categories = []
        self.win._choose_categories(False)
        self.env["forms"].alert.assert_called_once()
        self.env["forms"].SelectFromList.show.assert_not_called()

    def test_unchecked_but_highlighted_category_is_not_selected(self):
        self.prepare_categories()
        self.env["forms"].SelectFromList.show.return_value = [
            NS(item="Electrical Fixtures", checked=False),
            NS(item="Nurse Call Devices", checked=False)]
        self.win._choose_categories(False)
        self.assertEqual(set(), self.win._scan_category_keys)
        self.assertTrue(self.env["forms"].SelectFromList.show.call_args.kwargs["return_all"])

    def test_stale_link_catalog_requests_refresh_before_showing_picker(self):
        self.prepare_categories()
        self.win.cmbLinkInstance.SelectedItem = "Other Link"
        self.win._request = Mock()
        self.win._choose_categories(False)
        self.win._request.assert_called_once()
        self.env["forms"].SelectFromList.show.assert_not_called()

    def test_link_catalog_is_dynamic_not_a_fixed_enum(self):
        self.prepare_categories()
        cats = [NS(Id=Id(-99), Name="Nurse Call Devices"),
                NS(Id=Id(-123), Name="Another Category")]
        self.win._sel_link = lambda: NS(GetLinkDocument=lambda: NS(Settings=NS(Categories=cats)))
        self.win._load_scan_categories()
        self.assertEqual(["Another Category", "Nurse Call Devices"],
                         [name for key, name, cid in self.win._scan_categories])
        self.win._clear.assert_called_once()

    def test_unloaded_link_clears_catalog_and_stale_results(self):
        self.prepare_categories()
        self.win._sel_link = lambda: NS(GetLinkDocument=lambda: None)
        self.win._load_scan_categories()
        self.assertEqual([], self.win._scan_categories)
        self.win._clear.assert_called_once()

    def prepare_scan(self):
        self.prepare_categories()
        self.win._scan_category_keys = {"-99"}
        point = Point(2, 3, 0)
        transformed = Point(102, -3, 10)
        transform = NS(OfPoint=Mock(return_value=transformed))
        device = NS(Location=Location(point))
        sp = NS(LevelId=Id(10), IsPointInSpace=lambda p: 100 < p.X < 110 and -5 < p.Y < 0 and 9 < p.Z < 11)
        link_doc = object()
        lk = NS(GetLinkDocument=lambda: link_doc,
                GetTotalTransform=lambda: transform)
        self.win._val_scan = lambda: (True, "", dict(sp=sp, lk=lk, st=0, dh=300))
        self.win.tbIncludeKeywords = NS(Text="")
        self.win.tbExcludeKeywords = NS(Text="")
        self.win.cmbLevelMode = Combo("Ignore Level")
        self.win.cbIgnoreDuplicates = NS(IsChecked=True)
        self.win.cbVerboseDiag = NS(IsChecked=False)
        self.win._amap = Mock()
        self.win._render = Mock()
        self.win._apply_rules_to_rows = Mock()
        self.win._summ = Mock()
        collector = Mock()
        collector.OfCategoryId.return_value.WhereElementIsNotElementType.return_value = [device]
        def row(*args):
            return NS(fam="Nurse", typ="Call", cat="Nurse Call Devices", pt=args[2],
                      le_id=1, geom_lo_mm=None, geom_hi_mm=None)
        self.env.update(FilteredElementCollector=lambda document: collector,
                        space_polys=lambda space: [[(100, -5), (110, -5), (110, 0), (100, 0)]],
                        FR=row, doc=NS(GetElement=lambda eid: NS(Name="Space Level", ProjectElevation=8)))
        return collector, device, point, transformed, transform

    def test_scan_passes_selected_category_id_to_collector_and_transforms_point(self):
        collector, device, point, transformed, transform = self.prepare_scan()
        self.win._scan()
        self.assertEqual(-99, collector.OfCategoryId.call_args.args[0].Value)
        self.assertEqual(1, len(self.win._rows))
        self.assertEqual("Accepted", self.win._rows[0].status)
        self.assertIs(transformed, self.win._rows[0].pt)
        self.assertAlmostEqual(609.6, self.win._rows[0].linked_h_mm)
        self.assertEqual(300, self.win._rows[0].h_mm)
        self.assertEqual("Space Level", self.win._rows[0].space_level_name)
        transform.OfPoint.assert_called_once_with(point)

    def test_scan_reports_3d_geometry_height_above_space_level(self):
        collector, device, point, transformed, transform = self.prepare_scan()
        calls = []
        def zrange(element, trf):
            calls.append((element, trf))
            return 8 + 700 / 304.8, 8 + 780 / 304.8
        self.env["geometry_z_range"] = zrange
        self.win._scan()
        row = self.win._rows[0]
        self.assertEqual([(device, transform)], calls)
        self.assertAlmostEqual(700.0, row.geom_lo_mm)
        self.assertAlmostEqual(780.0, row.geom_hi_mm)
        self.assertEqual(300, row.h_mm)

    def test_scan_geometry_failure_keeps_row_with_unknown_3d_height(self):
        self.prepare_scan()
        self.env["geometry_z_range"] = Mock(side_effect=RuntimeError("bad geometry"))
        self.win._scan()
        self.assertEqual(1, len(self.win._rows))
        self.assertIsNone(self.win._rows[0].geom_lo_mm)
        self.env["logger"].warning.assert_called()

    def test_nonpoint_elements_are_rejected_without_attempting_transform(self):
        collector, device, point, transformed, transform = self.prepare_scan()
        device.Location = None
        self.win._scan()
        self.assertEqual([], self.win._rows)
        transform.OfPoint.assert_not_called()
        self.win._summ.assert_called_once_with({"Rejected - no LocationPoint": 1})

    def test_scan_duplicate_filter_is_preserved(self):
        collector, device, point, transformed, transform = self.prepare_scan()
        collector.OfCategoryId.return_value.WhereElementIsNotElementType.return_value = [device, device]
        self.win._scan()
        self.assertEqual(1, len(self.win._rows))
        self.win._summ.assert_called_once_with({"Accepted": 1, "Rejected - duplicate": 1})

    def test_scan_name_exclusion_is_preserved(self):
        self.prepare_scan()
        self.win.tbExcludeKeywords.Text = "nurse"
        self.win._scan()
        self.assertEqual([], self.win._rows)
        self.win._summ.assert_called_once_with({"Rejected - name filter": 1})

    def test_scan_space_rejection_is_preserved(self):
        self.prepare_scan()
        self.env["inside_space"] = lambda *args: False
        self.win._scan()
        self.assertEqual([], self.win._rows)
        self.win._summ.assert_called_once_with({"Rejected - outside space/tolerance": 1})

    def test_scan_level_filter_uses_transformed_elevation_not_name(self):
        collector, device, point, transformed, transform = self.prepare_scan()
        device.LevelId = Id(22)
        device_doc = NS(GetElement=lambda eid: NS(Name="Same name", ProjectElevation=0))
        link = NS(GetLinkDocument=lambda: device_doc, GetTotalTransform=lambda: transform)
        space = NS(LevelId=Id(11))
        self.win._val_scan = lambda: (True, "", dict(sp=space, lk=link, st=0, dh=300))
        self.env["doc"] = NS(GetElement=lambda eid: NS(Name="Same name", ProjectElevation=8))
        self.win.cmbLevelMode.SelectedItem = "Same Level Name"
        self.win._scan()
        self.assertEqual([], self.win._rows)
        self.win._summ.assert_called_once_with({"Rejected - wrong level": 1})

    def test_invalid_scan_clears_previous_results_before_reporting_error(self):
        self.win._rows = [object()]
        self.win._clear = Mock(side_effect=lambda: setattr(self.win, "_rows", []))
        self.win._val_scan = lambda: (False, "No boundary segments.", None)
        self.win._scan()
        self.assertEqual([], self.win._rows)
        self.env["forms"].alert.assert_called_once()
        self.assertEqual("Scan cancelled.", self.win.txtStatus.Text)

    def test_boundary_geometry_failure_cannot_leave_previous_scan_results_placeable(self):
        self.prepare_scan()
        self.win._rows = [object()]
        self.win._clear = Mock(side_effect=lambda: setattr(self.win, "_rows", []))
        self.env["space_polys"] = Mock(side_effect=RuntimeError("Cannot read boundary"))
        with self.assertRaisesRegex(RuntimeError, "Cannot read boundary"):
            self.win._scan()
        self.assertEqual([], self.win._rows)
        self.win._render.assert_not_called()

    def test_scan_volume_only_rejects_another_storey(self):
        collector, device, point, transformed, transform = self.prepare_scan()
        transformed.Z = 20
        self.win._scan()
        self.assertEqual([], self.win._rows)
        self.win._summ.assert_called_once_with({"Rejected - outside space/tolerance": 1})

    def test_scan_level_match_accepts_different_names_at_same_transformed_elevation(self):
        collector, device, point, transformed, transform = self.prepare_scan()
        device.LevelId = Id(22)
        level = NS(Name="Different linked name", ProjectElevation=0)
        transform.OfPoint.side_effect = lambda p: transformed if p is point else Point(p.X, p.Y, 8)
        lk = NS(GetLinkDocument=lambda: NS(GetElement=lambda eid: level),
                GetTotalTransform=lambda: transform)
        sp = NS(LevelId=Id(10), IsPointInSpace=lambda p: True)
        self.win._val_scan = lambda: (True, "", dict(sp=sp, lk=lk, st=0, dh=300))
        self.win.cmbLevelMode.SelectedItem = "Match linked level elevation"
        self.win._scan()
        self.assertEqual(1, len(self.win._rows))
        self.assertEqual("Accepted", self.win._rows[0].status)

    def test_linked_device_without_level_requires_explicit_volume_only_mode(self):
        collector, device, point, transformed, transform = self.prepare_scan()
        device.LevelId = Id(-1)
        lk = NS(GetLinkDocument=lambda: NS(GetElement=lambda eid: None),
                GetTotalTransform=lambda: transform)
        self.win._val_scan = lambda: (True, "", dict(
            sp=NS(LevelId=Id(10)), lk=lk, st=0, dh=300))
        self.win.cmbLevelMode.SelectedItem = "Match linked level elevation"
        self.win._scan()
        self.assertEqual([], self.win._rows)
        self.win._summ.assert_called_once_with({"Rejected - wrong level": 1})

    def test_target_filter_refresh_preserves_row_edits_and_rule_choice(self):
        self.win._target_category_keys = {"-99"}
        self.win._visible_families = set()
        self.win._symbol_data = {
            "nurse": dict(id=101, family="Nurse", category="-99", symbol="nurse-symbol"),
            "electrical": dict(id=102, family="Power", category="-1", symbol="power-symbol"),
        }
        self.win._sbd = {"electrical": "power-symbol"}
        cmb = Combo("nurse")
        rule = NS(sym_id=101, ctrls={"cmb": Combo("nurse")})
        row = NS(sym_id=101, ctrls={"cmb": cmb, "ht": NS(Text="777"),
                                  "mode": Combo("Ceiling")})
        self.win._rules, self.win._rows = [rule], [row]
        self.win.txtFamilyCount = NS(Text="")
        self.win._update_category_summary = Mock()
        self.win._rebuild_sbd()
        self.assertEqual({"nurse": "nurse-symbol"}, self.win._sbd)
        self.assertEqual("nurse", cmb.SelectedItem)
        self.assertEqual("nurse", rule.ctrls["cmb"].SelectedItem)
        self.assertEqual("777", row.ctrls["ht"].Text)
        self.assertEqual("Ceiling", row.ctrls["mode"].SelectedItem)
        self.assertEqual("Showing 1 of 1 families", self.win.txtFamilyCount.Text)

    def test_symbol_loader_collects_all_loaded_family_categories(self):
        self.prepare_categories()
        self.win.S = {}
        self.win._rebuild_sbd = Mock()
        symbols = [NS(Id=Id(101), Category=NS(Id=Id(-99), Name="Nurse Call Devices"),
                      Family=NS(Name="Nurse"), Name="Call")]
        collector = Mock()
        collector.OfClass.return_value.WhereElementIsElementType.return_value = symbols
        with patch.dict(self.env, FilteredElementCollector=lambda document: collector,
                        FamilySymbol=object, doc=object(),
                        sdisplay=lambda symbol: "Nurse :: Call"):
            self.win._load_sym()
        collector.OfCategory.assert_not_called()
        self.assertEqual("-99", self.win._target_categories[0][0])
        self.assertEqual(101, next(iter(self.win._symbol_data.values()))["id"])

    def test_modern_and_legacy_element_ids(self):
        value = 2 ** 40
        self.assertEqual(value, self.env["id_value"](Id(value)))
        self.assertEqual(42, self.env["id_value"](NS(IntegerValue=42)))

    def test_mode_guards_allow_only_supported_placement_types(self):
        check = self.env["placement_error"]
        for mode in ("Wall", "Ceiling"):
            self.assertIsNone(check("level", mode))
            self.assertIn("not supported", check("plane", mode))
        self.assertIsNone(check("plane", "Ceiling (Face)"))
        for kind in ("level", "hosted", "curve", "view", "adaptive"):
            self.assertIn("not supported", check(kind, "Ceiling (Face)"))

    def test_nonfinite_numbers_are_rejected(self):
        for text in ("nan", "inf", "-inf"):
            with self.assertRaisesRegex(ValueError, "finite"):
                self.env["pfloat"](text, -5000, 10000)
        self.assertEqual(300, self.env["pfloat"](" 300 ", 0, 5000))

    def test_pending_action_is_not_overwritten(self):
        first, second = Mock(), Mock()
        self.win.handler = NS(action=first)
        self.win.ext_event = Mock()
        self.win._request(second)
        self.assertIs(first, self.win.handler.action)
        self.win.ext_event.Raise.assert_not_called()
        self.assertIn("pending", self.win.txtStatus.Text)

    def test_denied_external_event_is_reported_and_cleared(self):
        self.win.handler = NS(action=None)
        self.win.ext_event = Mock()
        self.win.ext_event.Raise.return_value = "Denied"
        self.win._request(Mock())
        self.assertIsNone(self.win.handler.action)
        self.env["forms"].alert.assert_called_once()

    def test_handler_blocks_wrong_or_closed_document(self):
        action = Mock()
        handler = self.env["Hnd"](self.win)
        document = NS(IsValidObject=True)
        with patch.dict(self.env, doc=document):
            for active in (None, NS(Document=NS(Equals=lambda doc: False))):
                handler.action = action
                handler.Execute(NS(ActiveUIDocument=active))
            document.IsValidObject = False
            handler.action = action
            handler.Execute(NS(ActiveUIDocument=NS(Document=NS(Equals=lambda doc: True))))
        action.assert_not_called()
        self.assertEqual(3, self.env["forms"].alert.call_count)
        self.assertIsNone(handler.action)

    def test_handler_executes_in_correct_document(self):
        action = Mock()
        handler = self.env["Hnd"](self.win)
        handler.action = action
        with patch.dict(self.env, doc=NS(IsValidObject=True)):
            handler.Execute(NS(ActiveUIDocument=NS(Document=NS(Equals=lambda doc: True))))
        action.assert_called_once_with(self.win)
        self.assertIsNone(handler.action)

    def test_closed_window_does_not_execute_pending_action(self):
        action = Mock()
        handler = self.env["Hnd"](self.win)
        handler.action = action
        self.win._closed = True
        handler.Execute(NS())
        action.assert_not_called()

    def test_failed_placement_rolls_back_its_subtransaction(self):
        tx = Mock()
        self.win._place1 = Mock(return_value=(None, None))
        with patch.dict(self.env, SubTransaction=lambda document: tx, doc=object()):
            self.assertEqual((None, None), self.win._place_isolated(None, None, 600, False))
        tx.RollBack.assert_called_once()
        tx.Commit.assert_not_called()

    def test_exception_after_creation_rolls_back_its_subtransaction(self):
        tx = Mock()
        tx.GetStatus.return_value = "started"
        self.win._place1 = Mock(side_effect=RuntimeError("rotation failed"))
        with patch.dict(self.env, SubTransaction=lambda document: tx, doc=object()):
            with self.assertRaisesRegex(RuntimeError, "rotation"):
                self.win._place_isolated(None, None, 600, False)
        tx.RollBack.assert_called_once()

    def test_successful_placement_commits_its_subtransaction(self):
        tx = Mock()
        tx.Commit.return_value = "committed"
        self.win._place1 = Mock(return_value=(Id(5), {"mode": "Wall"}))
        with patch.dict(self.env, SubTransaction=lambda document: tx, doc=object()):
            result = self.win._place_isolated(None, None, 600, False)
        self.assertEqual(5, result[0].Value)
        tx.Commit.assert_called_once()
        tx.RollBack.assert_not_called()

    def test_rolled_back_subtransaction_is_not_reported_as_success(self):
        tx = Mock()
        tx.Commit.return_value = "rolled-back"
        tx.GetStatus.return_value = "rolled-back"
        self.win._place1 = Mock(return_value=(Id(5), {}))
        with patch.dict(self.env, SubTransaction=lambda document: tx, doc=object()):
            with self.assertRaisesRegex(RuntimeError, "did not commit"):
                self.win._place_isolated(None, None, 600, False)

    def test_new_controls_exist_and_are_wired(self):
        root = ET.parse(BUNDLE / "ui.xaml").getroot()
        names = {node.attrib.get("Name") for node in root.iter()}
        for name in ("btnScanCategories", "btnTargetCategories", "txtCategoryCount"):
            self.assertIn(name, names)
            self.assertIn("self." + name, SOURCE)
        self.assertNotIn("catElectricalFixtures", names)

    def test_settings_round_trip_preserves_categories_maps_and_rules(self):
        self.prepare_categories()
        self.win._scan_category_keys = {"-99"}
        self.win._target_category_keys = {"-99", "-1"}
        self.win.S = {"maps": {"Source :: Type": 123}, "cats": {"catDataDevices": True}}
        self.win._collect_rules_data = lambda: [{"kw": "nurse", "target": 123, "h": 600}]
        for name, value in (("tbScanTolerance", "300"), ("tbWallSearchDistance", "600"),
                            ("tbDefaultHeight", "450"), ("tbIncludeKeywords", "call"),
                            ("tbExcludeKeywords", "")):
            setattr(self.win, name, NS(Text=value))
        for name in ("cbActiveView", "cbIgnoreDuplicates", "cbVerboseDiag",
                     "cbAutoSelectZoom", "cbTagPlaced"):
            setattr(self.win, name, NS(IsChecked=True))
        self.win.cmbLevelMode = Combo("Ignore Level")
        self.win.cmbLinkInstance = Combo("Linked Model")
        data = self.win._coll_s()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            with patch.dict(self.env, os=os, SETTINGS_FILE=path):
                self.assertEqual((True, None), self.env["save_s"](data,path))
                restored = self.env["load_s"]()
        self.assertEqual(["-99"], restored["scan_categories"])
        self.assertEqual(["-1", "-99"], restored["target_categories"])
        self.assertEqual({"Source :: Type": 123}, restored["maps"])
        self.assertEqual(123, restored["rules"][0]["target"])
        self.assertEqual("Space volume only", restored["lvl_mode"])

    def test_boxed_clr_mapping_and_rule_ids_save_as_exact_decimal_strings(self):
        huge = 9223372036854775807
        data = {"maps": {"Source :: Type": ClrLong(483488), "Large :: Type": ClrLong(huge)},
                "rules": [{"target": ClrLong(huge), "kw": "call"}, {"target": None}],
                "scan_categories": ["-2001060"], "def_h": 600.0}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            with patch.dict(self.env, SETTINGS_FILE=path):
                ok, error = self.env["save_s"](data,path)
                self.assertTrue(ok, error)
                raw = json.loads(Path(path).read_text())
                restored = self.env["load_s"]()
        self.assertEqual("483488", raw["maps"]["Source :: Type"])
        self.assertEqual(str(huge), raw["maps"]["Large :: Type"])
        self.assertEqual(str(huge), raw["rules"][0]["target"])
        self.assertIsNone(raw["rules"][1]["target"])
        self.assertEqual(huge, restored["maps"]["Large :: Type"])
        self.assertEqual(483488, restored["maps"]["Source :: Type"])
        self.assertEqual(huge, restored["rules"][0]["target"])
        self.assertIsInstance(data["maps"]["Source :: Type"], ClrLong)
        self.assertIsInstance(data["rules"][0]["target"], ClrLong)

    def test_numeric_and_string_target_ids_load_to_same_in_memory_values(self):
        for value in (483488, "483488", 9223372036854775807, "9223372036854775807"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "settings.json")
                Path(path).write_text(json.dumps({"maps": {"Source": value},
                                                 "rules": [{"target": value}]}))
                restored = self.env["load_s"](path)
            self.assertEqual(int(value), restored["maps"]["Source"])
            self.assertEqual(int(value), restored["rules"][0]["target"])

    def test_invalid_target_ids_fail_without_overwriting_existing_settings(self):
        for value in (483488.5, True, "483488L", "bad", ""):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "settings.json")
                original = '{"maps":{"Original":123}}'
                Path(path).write_text(original)
                with patch.dict(self.env, SETTINGS_FILE=path):
                    ok, error = self.env["save_s"]({"maps": {"Source": value}},path)
                self.assertFalse(ok)
                self.assertIn("Invalid saved target ID", error)
                self.assertEqual(original, Path(path).read_text())

    def test_bad_json_is_reported_instead_of_loading_empty_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            Path(path).write_text('{"maps":')
            with patch.dict(self.env, SETTINGS_FILE=path):
                with self.assertRaises(ValueError):
                    self.env["load_s"]()

    def test_closing_before_initial_load_does_not_save_empty_rules(self):
        self.win._settings_ready = False
        self.win._coll_s = Mock(return_value={"maps": {}, "rules": []})
        save = Mock(return_value=(True, None))
        with patch.dict(self.env, save_s=save):
            self.win._on_closed(None, None)
        save.assert_not_called()
        self.win._coll_s.assert_not_called()

    def test_utf8_bom_json_loads_and_preserves_legacy_mappings(self):
        data = {"maps": {"Source :: Type": 123}, "rules": [{"kw": "call", "target": 123}],
                "cats": {"catGenericModel": True}, "def_h": 600}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "old.json")
            Path(path).write_bytes(b"\xef\xbb\xbf" + json.dumps(data).encode("utf-8"))
            loaded = self.env["load_s"](path)
        self.assertEqual(data["maps"], loaded["maps"])
        self.assertEqual(data["rules"], loaded["rules"])
        self.assertEqual(600, loaded["def_h"])
        self.assertTrue(loaded["cats"]["catGenericModel"])

    def test_invalid_settings_shapes_fail_before_replacing_current_settings(self):
        for data in ([], {"maps": []}, {"rules": {}}, {"rules": [5]},
                     {"scan_categories": {}}, {"visible_families": {}}, {"def_h": "nan"}):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "bad.json")
                Path(path).write_text(json.dumps(data))
                self.win.S = {"maps": {"Original": 5}}
                with self.assertRaises(ValueError):
                    self.win._import_settings(path)
                self.assertEqual({"Original": 5}, self.win.S["maps"])
                self.env["forms"].alert.assert_not_called()

    def test_missing_default_settings_are_defaults_but_missing_import_is_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "missing.json")
            with patch.dict(self.env, SETTINGS_FILE=path):
                self.assertEqual({}, self.env["load_s"]()["maps"])
                with self.assertRaisesRegex(ValueError, "has not been overwritten"):
                    self.env["load_s"](path)

    def test_first_save_backs_up_original_and_never_replaces_that_backup(self):
        original = b'{"maps":{"Original":123},"rules":[]}'
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            Path(path).write_bytes(original)
            with patch.dict(self.env, SETTINGS_FILE=path):
                self.assertEqual((True, None), self.env["save_s"]({"maps": {"Next": 456}},path))
                self.assertEqual(original, Path(path + ".bak").read_bytes())
                self.assertEqual((True, None), self.env["save_s"]({"maps": {}},path))
                self.assertEqual(original, Path(path + ".bak").read_bytes())

    def test_nonserializable_settings_do_not_touch_original_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            Path(path).write_text('{"maps":{"Original":123}}')
            before = Path(path).read_bytes()
            with patch.dict(self.env, SETTINGS_FILE=path):
                ok, error = self.env["save_s"]({"def_h": float("nan")},path)
            self.assertFalse(ok)
            self.assertTrue(error)
            self.assertEqual(before, Path(path).read_bytes())

    def test_save_button_queues_api_action_without_touching_legacy_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            Path(path).write_text('{"maps":{}}')
            self.win._coll_s = Mock(return_value={"maps": {}})
            self.win._request = Mock()
            with patch.dict(self.env, SETTINGS_FILE=path):
                copied = '{"maps":{"Recovered :: Type":123}}'
                Path(path).write_text(copied)
                self.Win._on_save(self.win)
            self.assertEqual(copied, Path(path).read_text())
            self.win._coll_s.assert_not_called()
            self.win._request.assert_called_once()

    def test_save_to_model_updates_snapshot_only_after_verified_save(self):
        data={"maps": {"Source :: Type": 123}, "rules": []}
        self.win._coll_s=Mock(return_value=data)
        self.win._settings_token="old"
        self.win._settings_summary=Mock()
        saver=Mock(return_value="new")
        with patch.dict(self.env, save_model_settings=saver, doc=object()):
            self.win._save_to_model()
        self.assertEqual("new", self.win._settings_token)
        self.assertEqual(self.env["settings_json"](data), self.win._saved_settings)
        self.assertEqual(data,self.win.S)
        self.assertIn("Verified in RVT: 1 mappings",self.win.txtStatus.Text)
        self.assertIn("Save/sync",self.win.txtStatus.Text)

    def test_failed_initialization_disables_save_without_collecting_incomplete_ui(self):
        self.win._load_sym = Mock(side_effect=RuntimeError("Could not read families"))
        with self.assertRaisesRegex(RuntimeError, "families"):
            self.win._init_load()
        self.assertFalse(self.win._settings_ready)
        self.win._coll_s = Mock()
        self.win._on_save()
        self.win._coll_s.assert_not_called()
        self.env["forms"].alert.assert_called_once()

    def test_successful_initialization_reports_loaded_counts_and_enables_save(self):
        self.win.S = {"maps": {"A": 1, "B": 2}, "rules": [{}]}
        self.win._rules, self.win._ref_planes = [object()], []
        for name in ("_load_sym", "_load_lnk", "_load_sp", "_load_scan_categories",
                     "_load_ref_planes", "_load_rules_from_settings", "_settings_summary"):
            setattr(self.win, name, Mock())
        self.win._init_load()
        self.assertTrue(self.win._settings_ready)
        self.assertIn("2 mappings, 1 rules", self.win.txtStatus.Text)
        self.win._settings_summary.assert_called_once()

    def test_import_confirmation_and_read_only_source_then_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "old.json")
            source = '{"maps":{"Source :: Type":123},"rules":[{"kw":"nurse","target":123}],"def_h":600}'
            Path(path).write_text(source)
            self.win._clear = Mock()
            self.win.spRules = NS(Children=Items())
            self.win._apply_s = Mock()
            self.win._init_load = Mock()
            self.env["forms"].alert.return_value = True
            with patch.dict(self.env, SETTINGS_FILE=os.path.join(directory, "new.json")):
                self.win._import_settings(path)
            self.assertEqual({"Source :: Type": 123}, self.win.S["maps"])
            self.assertEqual(600, self.win.S["def_h"])
            self.assertEqual("JSON draft: "+path, self.win._settings_source)
            self.assertFalse(self.win._settings_ready)
            self.win._init_load.assert_called_once()
            self.assertEqual(source, Path(path).read_text())
            self.assertIn("1 mappings and 1 rules", self.env["forms"].alert.call_args.args[0])

    def test_import_cancel_keeps_existing_mappings_and_does_not_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "empty.json")
            Path(path).write_text('{"maps":{},"rules":[]}')
            self.win.S = {"maps": {"Original": 7}}
            self.env["forms"].alert.return_value = False
            self.win._import_settings(path)
            self.assertEqual({"Original": 7}, self.win.S["maps"])
            self.assertTrue(self.win._settings_ready)
            self.assertIn("0 mappings and 0 rules", self.env["forms"].alert.call_args.args[0])

    def test_rule_target_ids_survive_hidden_family_filters_and_missing_project_ids(self):
        self.win._symbol_data = {"Visible": {"id": 9}}
        self.win._sbd = {"Visible": object()}
        rule = NS(sym_id=123, ctrls={"cmb": Combo("<not set>"), "kw": NS(Text="nurse"),
                                    "h": NS(Text="600"), "mode": Combo("Wall"),
                                    "rp": Combo("Auto-match")})
        self.win._rules = [rule]
        self.assertEqual(123, self.win._collect_rules_data()[0]["target"])
        rule.ctrls["cmb"].SelectedItem = "Visible"
        self.assertEqual(9, self.win._collect_rules_data()[0]["target"])
        rule.ctrls["cmb"].SelectedItem = "<not set>"
        self.assertIsNone(self.win._collect_rules_data()[0]["target"])

    def test_settings_summary_distinguishes_missing_targets_from_missing_mappings(self):
        self.win.S = {"maps": {"SourceA": 5, "SourceB": 6}, "rules": [{}]}
        self.win._symbol_data = {"Found": {"id": 5}}
        self.win._settings_source = "old.json"
        self.win._coll_s=Mock(return_value=self.win.S)
        self.win.txtSettingsInfo = NS(Text="", ToolTip="")
        with patch.dict(self.env, SETTINGS_FILE="new.json"):
            self.win._settings_summary()
        self.assertIn("2 mappings (1 target IDs found)", self.win.txtSettingsInfo.Text)
        self.assertIn("Source: old.json", self.win.txtSettingsInfo.Text)
        self.assertIn("Save to RVT", self.win.txtSettingsInfo.Text)

    def test_existing_legacy_settings_load_without_dropping_maps_or_rules(self):
        source = BUNDLE / "find_linked_fixtures_settings.json"
        original = json.loads(source.read_text())
        data = self.env["load_s"](str(source))
        self.assertEqual(original["maps"], data["maps"])
        self.assertEqual(original["rules"], data["rules"])
        self.assertEqual(original["def_h"], data["def_h"])

    def prepare_model_settings(self):
        api = StorageAPI(self.env)
        self.win._settings_token = None
        self.win._saved_settings = None
        self.win._discard_close = False
        self.win._settings_summary = Mock()
        return api

    def prepare_startup(self):
        self.win._clear = Mock()
        self.win.spRules = NS(Children=Items())
        self.win._apply_s = Mock()
        self.win._init_load = Mock()

    def test_model_save_reload_preserves_mappings_rules_categories_and_large_ids(self):
        api = self.prepare_model_settings()
        data = {"maps": {"Nurse :: Call": ClrLong(9223372036854775807)},
                "rules": [{"kw": "nurse", "target": ClrLong(483488), "h": 600}],
                "scan_categories": ["-2001060"], "target_categories": ["-123"],
                "visible_families": ["Nurse call"], "def_h": 600}
        token = self.env["save_model_settings"](api.document, data, None)
        self.assertEqual("9223372036854775807", json.loads(token)["settings"]["maps"]["Nurse :: Call"])
        self.assertEqual("committed", api.transactions[0].status)
        self.assertEqual("public", api.schema.read)
        self.assertEqual("public", api.schema.write)
        restored, snapshot = self.env["load_model_settings"](api.document)
        self.assertEqual(token, snapshot)
        self.assertEqual(9223372036854775807, restored["maps"]["Nurse :: Call"])
        self.assertEqual(483488, restored["rules"][0]["target"])
        for key in ("scan_categories", "target_categories", "visible_families", "def_h"):
            self.assertEqual(data[key], restored[key])
        token2 = self.env["save_model_settings"](api.document, restored, token)
        self.assertEqual(token, token2)
        self.assertEqual(1, len(api.document.storages))
        self.prepare_startup()
        self.win._startup_load()
        self.assertEqual(9223372036854775807, self.win.S["maps"]["Nurse :: Call"])
        self.win._add_rule = Mock()
        self.win._load_rules_from_settings()
        self.win._add_rule.assert_called_once_with(
            kw="nurse", sym_id=483488, h=600, mode="Wall", rp="Auto-match")

    def test_empty_model_settings_win_over_legacy_json_without_touching_file(self):
        api = self.prepare_model_settings()
        self.prepare_startup()
        self.env["save_model_settings"](api.document, {"maps": {}, "rules": []}, None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            original = '{"maps":{"Legacy":22}}'
            path.write_text(original)
            with patch.dict(self.env, SETTINGS_FILE=str(path)):
                self.win._startup_load()
            self.assertEqual(original, path.read_text())
        self.assertEqual({}, self.win.S["maps"])
        self.assertEqual([], self.win.S["rules"])
        self.assertEqual("RVT Extensible Storage", self.win._settings_source)
        self.assertIsNotNone(self.win._saved_settings)

    def test_legacy_migration_is_draft_until_explicit_save_and_source_unchanged(self):
        api = self.prepare_model_settings()
        self.prepare_startup()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            original = '{"maps":{"Legacy":22},"rules":[{"kw":"nurse","target":22}]}'
            path.write_text(original)
            with patch.dict(self.env, SETTINGS_FILE=str(path)):
                self.win._startup_load()
                self.assertEqual({"Legacy": 22}, self.win.S["maps"])
                self.assertIn("Legacy JSON draft", self.win._settings_source)
                self.assertIsNone(self.win._saved_settings)
                self.assertEqual([], api.transactions)
                self.assertEqual([], api.document.storages)
                self.win._settings_ready = True
                self.win._coll_s = Mock(return_value=self.win.S)
                self.win._save_to_model()
            self.assertEqual(original, path.read_text())
        self.assertEqual({"Legacy": 22}, self.env["load_model_settings"](api.document)[0]["maps"])

    def test_model_settings_are_isolated_to_document_and_existing_schema_can_be_reused(self):
        api = self.prepare_model_settings()
        self.env["save_model_settings"](api.document, {"maps": {"Host A": 1}}, None)
        other = NS(IsReadOnly=False, storages=[], Regenerate=Mock())
        self.assertEqual((None, None), self.env["load_model_settings"](other))
        self.env["save_model_settings"](other, {"maps": {"Host B": 2}}, None)
        self.assertEqual({"Host A": 1}, self.env["load_model_settings"](api.document)[0]["maps"])
        self.assertEqual({"Host B": 2}, self.env["load_model_settings"](other)[0]["maps"])

    def test_stale_model_snapshot_conflicts_then_explicit_reload_allows_save(self):
        api = self.prepare_model_settings()
        original = self.env["save_model_settings"](api.document, {"maps": {"A": 1}}, None)
        updated = self.env["save_model_settings"](api.document, {"maps": {"B": 2}}, original)
        for snapshot in (original, None):
            with self.subTest(snapshot=snapshot), self.assertRaisesRegex(ValueError, "Reload RVT"):
                self.env["save_model_settings"](api.document, {"maps": {}}, snapshot)
            self.assertEqual("rolledback", api.transactions[-1].status)
            self.assertEqual(updated, self.env["load_model_settings"](api.document)[1])
        self.env["save_model_settings"](api.document, {"maps": {"C": 3}}, updated)
        self.assertEqual({"C": 3}, self.env["load_model_settings"](api.document)[0]["maps"])

    def test_write_failures_and_read_back_mismatches_roll_back(self):
        for problem in ("ownership", "mismatch", "commit"):
            with self.subTest(problem=problem):
                api = self.prepare_model_settings()
                original = self.env["save_model_settings"](api.document, {"maps": {"A": 1}}, None)
                if problem == "ownership":
                    api.write_error = RuntimeError("Owned by another user")
                elif problem == "mismatch":
                    api.corrupt_write = True
                else:
                    api.commit_status = "rolledback"
                with self.assertRaises((RuntimeError, ValueError)):
                    self.env["save_model_settings"](api.document, {"maps": {"B": 2}}, original)
                self.assertEqual("rolledback", api.transactions[-1].status)
                self.assertEqual(original, self.env["load_model_settings"](api.document)[1])

    def test_first_write_verification_failure_removes_new_storage_on_rollback(self):
        api = self.prepare_model_settings()
        api.corrupt_write = True
        with self.assertRaisesRegex(ValueError, "before commit"):
            self.env["save_model_settings"](api.document, {"maps": {"A": 1}}, None)
        self.assertEqual([], api.document.storages)
        self.assertEqual("rolledback", api.transactions[-1].status)

    def test_postcommit_verification_failure_is_not_reported_as_success(self):
        api = self.prepare_model_settings()
        self.win.S = {"maps": {"A": 1}, "rules": []}
        self.win._coll_s = Mock(return_value=self.win.S)
        self.win.Dispatcher = NS(BeginInvoke=Mock())
        api.after_commit = lambda: api.document.storages.clear()
        self.win._save_to_model(close_after=True)
        self.assertIn("NOT SAVED", self.win.txtStatus.Text)
        self.assertIsNone(self.win._settings_token)
        self.assertIsNone(self.win._saved_settings)
        self.assertFalse(self.win._discard_close)
        self.win.Dispatcher.BeginInvoke.assert_not_called()
        self.env["forms"].alert.assert_called_once()

    def test_read_only_and_failed_transaction_start_do_not_write(self):
        api = self.prepare_model_settings()
        api.document.IsReadOnly = True
        with self.assertRaisesRegex(ValueError, "read-only"):
            self.env["save_model_settings"](api.document, {}, None)
        self.assertEqual([], api.transactions)
        api.document.IsReadOnly = False
        api.start_status = "uninitialized"
        with self.assertRaisesRegex(ValueError, "start"):
            self.env["save_model_settings"](api.document, {}, None)
        self.assertEqual([], api.document.storages)

    def test_duplicate_records_and_damaged_schema_fail_explicitly(self):
        api = self.prepare_model_settings()
        token = self.env["save_model_settings"](api.document, {"maps": {"A": 1}}, None)
        duplicate = api.Storage.Create(api.document)
        duplicate.values = {k: dict(v) for k, v in api.document.storages[0].values.items()}
        with self.assertRaisesRegex(ValueError, "Multiple"):
            self.env["load_model_settings"](api.document)
        with self.assertRaisesRegex(ValueError, "Multiple"):
            self.env["save_model_settings"](api.document, {}, token)
        api.document.storages.remove(duplicate)
        api.schema.fields.clear()
        with self.assertRaisesRegex(ValueError, "Payload field"):
            self.env["load_model_settings"](api.document)

    def test_corrupt_or_future_model_settings_never_fall_back_to_legacy(self):
        for payload in ("", "broken", "{}", '{"version":2,"settings":{}}',
                        '{"version":true,"settings":{}}', '{"version":1,"settings":null}',
                        '{"version":1,"settings":{"maps":[]}}'):
            with self.subTest(payload=payload):
                api = self.prepare_model_settings()
                self.prepare_startup()
                self.env["save_model_settings"](api.document, {"maps": {"A": 1}}, None)
                api.document.storages[0].values[api.schema.guid]["Payload"] = payload
                self.win._settings_ready = False
                with self.assertRaises(ValueError):
                    self.win._startup_load()
                self.assertFalse(self.win._settings_ready)
                self.win._init_load.assert_not_called()
                self.assertEqual(payload, api.document.storages[0].values[api.schema.guid]["Payload"])

    def test_failed_model_save_retains_edits_and_snapshot_and_reports_error(self):
        api = self.prepare_model_settings()
        self.win._settings_token = self.env["save_model_settings"](api.document, {"maps": {"Old": 1}}, None)
        self.win.S = {"maps": {"Edit": 2}, "rules": []}
        self.win._saved_settings = "original snapshot"
        self.win._coll_s = Mock(return_value=self.win.S)
        api.write_error = RuntimeError("Worksharing ownership denied")
        self.win._save_to_model(close_after=True)
        self.assertEqual("original snapshot", self.win._saved_settings)
        self.assertEqual({"Edit": 2}, self.win.S["maps"])
        self.assertFalse(self.win._discard_close)
        self.assertIn("NOT SAVED", self.win.txtStatus.Text)
        self.assertIn("ownership", self.env["forms"].alert.call_args.args[0])

    def test_close_save_discard_cancel_and_unchanged_do_not_autosave(self):
        for choice in ("Save to RVT and close", "Discard window changes", "Cancel", None):
            with self.subTest(choice=choice):
                self.prepare_model_settings()
                self.win._coll_s = Mock(return_value={"maps": {"Edit": 2}})
                self.win._request = Mock()
                self.env["forms"].alert.return_value = choice
                event = NS(Cancel=False)
                self.win._on_closing(None, event)
                self.assertEqual(choice != "Discard window changes", event.Cancel)
                self.assertEqual(choice == "Discard window changes", self.win._discard_close)
                if choice == "Save to RVT and close":
                    self.win._request.assert_called_once()
                else:
                    self.win._request.assert_not_called()
        self.win._discard_close = False
        self.win._saved_settings = self.env["settings_json"]({"maps": {"Edit": 2}})
        self.env["forms"].alert.reset_mock()
        event = NS(Cancel=False)
        self.win._on_closing(None, event)
        self.assertFalse(event.Cancel)
        self.env["forms"].alert.assert_not_called()

    def test_successful_save_and_close_is_dispatched_only_after_read_back(self):
        api = self.prepare_model_settings()
        self.win._coll_s = Mock(return_value={"maps": {"Edit": 2}, "rules": []})
        self.win.Close = Mock()
        self.win.Dispatcher = NS(BeginInvoke=Mock())
        self.win._save_to_model(close_after=True)
        self.assertEqual("committed", api.transactions[-1].status)
        self.assertTrue(self.win._discard_close)
        self.win.Close.assert_not_called()
        self.win.Dispatcher.BeginInvoke.assert_called_once()
        self.win.Dispatcher.BeginInvoke.call_args.args[0]()
        self.win.Close.assert_called_once()

    def test_invalid_ui_can_be_corrected_or_explicitly_discarded_on_close(self):
        self.prepare_model_settings()
        self.win._coll_s = Mock(side_effect=ValueError("Invalid height"))
        for choice in ("Cancel", "Discard window changes"):
            self.env["forms"].alert.return_value = choice
            event = NS(Cancel=False)
            self.win._on_closing(None, event)
            self.assertEqual(choice == "Cancel", event.Cancel)

    def test_reload_cancel_preserves_window_edits_and_accept_reads_model(self):
        api = self.prepare_model_settings()
        self.prepare_startup()
        token = self.env["save_model_settings"](api.document, {"maps": {"Stored": 1}}, None)
        self.win._coll_s = Mock(return_value={"maps": {"Edit": 2}})
        self.env["forms"].alert.return_value = False
        self.win._reload_settings()
        self.win._init_load.assert_not_called()
        self.env["forms"].alert.return_value = True
        self.win._reload_settings()
        self.assertEqual({"Stored": 1}, self.win.S["maps"])
        self.assertEqual(token, self.win._settings_token)
        self.win._init_load.assert_called_once()

    def test_explicit_export_only_writes_chosen_json_not_model_or_legacy(self):
        api = self.prepare_model_settings()
        data = {"maps": {"Nurse": ClrLong(483488)}, "rules": [{"target": ClrLong(483488)}]}
        self.win._coll_s = Mock(return_value=data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.json"
            legacy = Path(directory) / "legacy.json"
            legacy.write_text('{"maps":{"Old":1}}')
            self.env["forms"].save_file = Mock(return_value=str(path))
            with patch.dict(self.env, SETTINGS_FILE=str(legacy)):
                self.win._on_export()
            self.assertEqual({"Nurse": 483488}, self.env["load_s"](str(path))["maps"])
            self.assertEqual('{"maps":{"Old":1}}', legacy.read_text())
        self.assertEqual([], api.transactions)
        self.assertIsNone(self.win._saved_settings)
        self.assertIn("have not been saved by export", self.win.txtStatus.Text)

    def test_invalid_numeric_ui_and_rule_heights_are_not_silently_saved(self):
        for data in ({"def_h": "not a height"}, {"rules": [{"h": "nan"}]},
                     {"rules": [{"h": 10001}]}, {"scan_tol": -1}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.env["model_settings_payload"](data)

    def test_new_model_without_legacy_file_starts_unsaved_defaults_without_writes(self):
        api = self.prepare_model_settings()
        self.prepare_startup()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            with patch.dict(self.env, SETTINGS_FILE=str(path)):
                self.win._startup_load()
            self.assertFalse(path.exists())
        self.assertEqual({}, self.win.S["maps"])
        self.assertIsNone(self.win._settings_token)
        self.assertIsNone(self.win._saved_settings)
        self.assertEqual([], api.transactions)
        self.assertIn("not yet in RVT", self.win._settings_source)

    def test_export_cancel_and_export_failure_do_not_mark_model_saved(self):
        self.prepare_model_settings()
        self.win._coll_s = Mock(return_value={"maps": {"A": 1}})
        self.env["forms"].save_file = Mock(return_value=None)
        self.win._on_export()
        self.win._coll_s.assert_not_called()
        self.env["forms"].save_file.return_value = "chosen.json"
        with patch.dict(self.env, save_s=Mock(return_value=(False, "Permission denied"))):
            self.win._on_export()
        self.assertIsNone(self.win._saved_settings)
        self.assertIn("Permission denied", self.env["forms"].alert.call_args.args[0])

    def test_reset_requires_confirmation_and_never_implicitly_saves(self):
        self.prepare_model_settings()
        self.win.S = {"maps": {"Original": 5}}
        self.env["forms"].alert.return_value = False
        self.win._on_reset(None, None)
        self.assertEqual({"Original": 5}, self.win.S["maps"])
        self.env["forms"].alert.return_value = True
        self.win._apply_s = Mock()
        self.win._clear = Mock()
        self.win._rebuild_sbd = Mock()
        self.win._update_category_summary = Mock()
        self.win.spRules = NS(Children=Items())
        self.win._on_reset(None, None)
        self.assertEqual({}, self.win.S["maps"])
        self.assertEqual([], self.win.S["rules"])
        self.assertIsNone(self.win._saved_settings)
        self.assertIn("Click Save to RVT", self.win.txtStatus.Text)

    def test_no_button_workflow_calls_old_automatic_save_handler(self):
        calls = [node for node in ast.walk(ast.parse(SOURCE))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "_on_save"]
        self.assertEqual([], calls)

    def prepare_placement(self, mode):
        level = NS(Id=Id(10), Name="Level 1", ProjectElevation=12, Elevation=1012)
        link = NS(GetLinkDocument=lambda: object())
        create = Mock()
        instance = NS(Id=Id(900), get_Parameter=Mock(return_value=Mock(IsReadOnly=False)),
                      Location=Location(Point(2, 3, 12 + 3000 / 304.8)))
        create.NewFamilyInstance.return_value = instance
        document = NS(ActiveView=NS(), Create=create, Regenerate=Mock(),
                      GetElement=lambda eid: link if eid.Value == 1 else level)
        self.win._csi = NS(space=NS(LevelId=Id(10)), disp="Space 1")
        row = NS(li_id=1, le_id=2, mode=mode, rp_name="Plane 3000", h_mm=3000,
                 pt=Point(2, 3, 0), err=None)
        symbol = NS(IsActive=True)
        self.env.update(doc=document, StructuralType=NS(NonStructural="nonstructural"),
                        BuiltInParameter=NS(INSTANCE_ELEVATION_PARAM="elevation"))
        Point.BasisX = object()
        return row, symbol, create, instance, level

    def test_face_mode_uses_existing_reference_plane_not_new_sketch_plane(self):
        row, symbol, create, instance, level = self.prepare_placement("Ceiling (Face)")
        plane = NS(Origin=Point(0, 0, 20), Normal=Point(0, 0, 1))
        reference = object()
        self.win._find_rp_by_name = lambda name: NS(GetPlane=lambda: plane,
                                                   GetReference=lambda: reference)
        eid, details = self.win._place1(row, symbol, 600, False)
        args = create.NewFamilyInstance.call_args.args
        self.assertEqual(4, len(args))
        self.assertIs(reference, args[0])
        self.assertEqual((2, 3, 20), (args[1].X, args[1].Y, args[1].Z))
        self.assertIs(symbol, args[3])
        self.assertEqual(900, eid.Value)
        self.assertEqual("refplane", details["source"])

    def test_face_mode_rejects_nonhorizontal_plane_before_creation(self):
        row, symbol, create, instance, level = self.prepare_placement("Ceiling (Face)")
        self.win._find_rp_by_name = lambda name: NS(
            GetPlane=lambda: NS(Normal=Point(1, 0, 0)))
        self.assertEqual((None, None), self.win._place1(row, symbol, 600, False))
        self.assertIn("horizontal", row.err)
        create.NewFamilyInstance.assert_not_called()

    def test_ceiling_mode_retains_level_based_signature_and_height(self):
        row, symbol, create, instance, level = self.prepare_placement("Ceiling")
        eid, details = self.win._place1(row, symbol, 600, False)
        args = create.NewFamilyInstance.call_args.args
        self.assertEqual((2, 3, 12), (args[0].X, args[0].Y, args[0].Z))
        self.assertEqual((symbol, level, "nonstructural"), args[1:])
        instance.get_Parameter.return_value.Set.assert_called_once_with(3000 / 304.8)
        self.assertEqual("Ceiling", details["mode"])

    def test_wall_mode_retains_boundary_projection_height_and_rotation(self):
        row, symbol, create, instance, level = self.prepare_placement("Wall")
        self.win._find_wall = Mock(return_value=(object(), Point(5, 6, 0), None, 1, "boundary"))
        inward = Point(0, 1, 0)
        self.win._calc_inward = Mock(return_value=inward)
        self.win._rotate_to_face_inward = Mock()
        with patch.dict(self.env, space_center=lambda space: Point(0, 0, 0)):
            eid, details = self.win._place1(row, symbol, 600, False)
        args = create.NewFamilyInstance.call_args.args
        self.assertEqual((5, 6, 12), (args[0].X, args[0].Y, args[0].Z))
        self.win._rotate_to_face_inward.assert_called_once_with(instance, args[0], inward)
        instance.get_Parameter.return_value.Set.assert_called_once_with(3000 / 304.8)
        self.assertEqual("boundary", details["source"])

    def test_placement_uses_space_level_even_when_active_plan_level_is_different(self):
        row, symbol, create, instance, level = self.prepare_placement("Ceiling")
        self.env["doc"].ActiveView = NS(GenLevel=NS(Id=Id(999), ProjectElevation=200))
        self.win._place1(row, symbol, 600, False)
        self.assertIs(level, create.NewFamilyInstance.call_args.args[2])
        self.assertEqual(12, create.NewFamilyInstance.call_args.args[0].Z)

    def test_placement_without_space_level_reports_failure(self):
        row, symbol, create, instance, level = self.prepare_placement("Ceiling")
        link = NS(GetLinkDocument=lambda: object())
        self.env["doc"].GetElement = lambda eid: link if eid.Value == 1 else None
        self.assertEqual((None, None), self.win._place1(row, symbol, 600, False))
        self.assertIn("Space has no valid level", row.err)
        create.NewFamilyInstance.assert_not_called()

    def test_wrong_actual_placement_height_is_not_silently_reported_as_success(self):
        row, symbol, create, instance, level = self.prepare_placement("Ceiling")
        instance.Location.Point.Z = 12
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.win._place1(row, symbol, 600, False)
        self.env["doc"].Regenerate.assert_called_once()

    def test_wrong_height_rolls_back_created_instance_via_real_placement_path(self):
        row, symbol, create, instance, level = self.prepare_placement("Ceiling")
        instance.Location.Point.Z = 12
        tx = Mock()
        tx.GetStatus.return_value = "started"
        with patch.dict(self.env, SubTransaction=lambda document: tx):
            with self.assertRaisesRegex(ValueError, "does not match"):
                self.win._place_isolated(row, symbol, 600, False)
        create.NewFamilyInstance.assert_called_once()
        tx.RollBack.assert_called_once()
        tx.Commit.assert_not_called()

    def test_height_verification_handles_missing_or_readonly_offset_parameter(self):
        row, symbol, create, instance, level = self.prepare_placement("Ceiling")
        for parameter in (None, NS(IsReadOnly=True)):
            instance.get_Parameter.return_value = parameter
            self.env["set_placement_height"](instance, level, 3000)
            instance.Location.Point.Z = 12
            with self.assertRaisesRegex(ValueError, "does not match"):
                self.env["set_placement_height"](instance, level, 3000)
            instance.Location.Point.Z = 12 + 3000 / 304.8

    def test_unchanged_offset_parameter_is_accepted_when_actual_height_is_correct(self):
        row, symbol, create, instance, level = self.prepare_placement("Ceiling")
        instance.get_Parameter.return_value.Set.return_value = False
        self.env["set_placement_height"](instance, level, 3000)

    def test_height_set_exception_propagates_for_row_rollback(self):
        row, symbol, create, instance, level = self.prepare_placement("Ceiling")
        instance.get_Parameter.return_value.Set.side_effect = RuntimeError("Read-only in this context")
        with self.assertRaisesRegex(RuntimeError, "Read-only"):
            self.win._place1(row, symbol, 600, False)

    def test_linked_height_column_is_display_only_and_explains_reference_level(self):
        class Event:
            def __iadd__(self, value):
                return self
        def combo(**kwargs):
            return NS(Items=Items(), SelectedItem=None, SelectedIndex=0, SelectionChanged=Event(), **kwargs)
        def panel(**kwargs):
            return NS(Children=Items(), **kwargs)
        self.win._sbd = {}
        self.win._rp_combo_items = lambda: ["Auto-match"]
        row = NS(chk=True, status="Accepted", dname="Nurse :: Call", cat="Nurse Call Devices",
                 le_id=12, linked_h_mm=-42.75, space_level_name="Level 2", sym_id=None,
                 h_mm=600, mode="Ceiling", rp_name="Auto-match", reason="")
        with patch.dict(self.env, StackPanel=panel, Orientation=NS(Horizontal="horizontal"),
                        Thickness=lambda *args: 0, CheckBox=lambda **kwargs: NS(**kwargs),
                        TextBlock=lambda **kwargs: NS(**kwargs), TextBox=lambda **kwargs: NS(**kwargs),
                        ComboBox=combo, stcol=lambda *args: None,
                        System=NS(Windows=NS(TextWrapping=NS(Wrap="wrap")))):
            p = self.win._rpanel(row)
            self.assertEqual("Ins -42.8\n3D n/a", p.Children[2].Text)
            self.assertIn("Level 2", p.Children[2].ToolTip)
            self.assertIn("display only", p.Children[2].ToolTip)
            self.assertEqual("600", p.Children[4].Text)
            row.geom_lo_mm, row.geom_hi_mm = 700.0, 780.0
            self.assertEqual("Ins -42.8\n3D 700.0 / 780.0", self.win._rpanel(row).Children[2].Text)
            self.assertEqual("600", self.win._rpanel(row).Children[4].Text)
            row.linked_h_mm = None
            self.assertEqual("Ins n/a\n3D 700.0 / 780.0", self.win._rpanel(row).Children[2].Text)

    def prepare_placement_batch(self, commit_status):
        row = NS(ctrls={"cmb": Combo("target"), "cb": NS(IsChecked=True)}, placed=None)
        self.win._rows = [row]
        self.win._sbd = {"target": object()}
        self.win.tbWallSearchDistance = NS(Text="600")
        self.win.cbTagPlaced = NS(IsChecked=False)
        self.win.cbAutoSelectZoom = NS(IsChecked=False)
        self.win.btnUndoLast = NS(IsEnabled=False)
        self.win.btnSelectLast = NS(IsEnabled=False)
        self.win._vrow = Mock(return_value=(True, ""))
        self.win._place_isolated = Mock(return_value=(Id(5), {"source": "ceiling"}))
        self.win._rok = Mock()
        self.win._on_save = Mock()
        self.win._report = Mock()
        tx = Mock()
        tx.Commit.return_value = commit_status
        tx.GetStatus.return_value = "rolled-back"
        self.env.update(Transaction=lambda document, name: tx, doc=object(),
                        List={Id: Items})
        return row, tx

    def test_outer_transaction_failure_never_marks_rows_placed(self):
        row, tx = self.prepare_placement_batch("rolled-back")
        with self.assertRaisesRegex(RuntimeError, "did not commit"):
            self.win._place()
        self.assertIsNone(row.placed)
        self.assertTrue(row.ctrls["cb"].IsChecked)
        self.win._rok.assert_not_called()
        self.win._report.assert_not_called()

    def test_outer_commit_marks_rows_and_keeps_select_undo_ids(self):
        row, tx = self.prepare_placement_batch("committed")
        self.win._place()
        self.assertEqual(5, row.placed)
        self.assertFalse(row.ctrls["cb"].IsChecked)
        self.assertEqual([5], [eid.Value for eid in self.win._lpi])
        self.assertTrue(self.win.btnUndoLast.IsEnabled)
        self.assertTrue(self.win.btnSelectLast.IsEnabled)
        self.win._report.assert_called_once()

    def test_keyword_filter_and_first_matching_rule_behavior_is_preserved(self):
        self.assertEqual(["NURSE", "CALL"], self.env["pkw"](" nurse, call, "))
        self.assertTrue(self.env["has_any"]("Nurse Call", ["NURSE"]))
        self.assertFalse(self.env["has_none"]("Nurse Call", ["CALL"]))
        first = NS(ctrls={"kw": NS(Text="nurse")})
        second = NS(ctrls={"kw": NS(Text="call")})
        self.win._rules = [first, second]
        self.assertIs(first, self.win._match_rule("Nurse Call Device"))


if __name__ == "__main__":
    unittest.main()
