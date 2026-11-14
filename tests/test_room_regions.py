"""Mocked checks for the Stage 1 Room Regions button; these do not run Revit."""

import importlib.util
import math
import sys
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


BUNDLE = (Path(__file__).resolve().parents[1] / "CustomPyRevit.extension"
          / "ETC PyTools.tab" / "Spaces.panel" / "RoomRegions.pushbutton")
LIB = BUNDLE / "lib"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


logic = load_module("room_regions_logic_test", LIB / "room_regions_logic.py")


def line(start, end):
    return {"kind": "line", "start": tuple(start), "end": tuple(end)}


def arc(start, middle, end):
    return {"kind": "arc", "start": tuple(start), "mid": tuple(middle), "end": tuple(end)}


def polygon(points):
    return [line(points[index], points[(index + 1) % len(points)])
            for index in range(len(points))]


class LogicTests(unittest.TestCase):
    def test_fifo_queue_isolates_errors_and_retains_new_requests(self):
        queue = logic.ActionQueue()
        calls, errors = [], []

        def first(uiapp):
            calls.append(("first", uiapp))
            queue.add("later", lambda app: calls.append(("later", app)))

        def fail(uiapp):
            raise ValueError("bad request")

        queue.add("first", first)
        queue.add("failure", fail)
        queue.add("last", lambda app: calls.append(("last", app)))
        self.assertEqual(3, queue.drain("app", lambda label, ex: errors.append((label, str(ex)))))
        self.assertEqual([("first", "app"), ("last", "app")], calls)
        self.assertEqual([("failure", "bad request")], errors)
        self.assertEqual(1, len(queue))
        queue.drain("app2", lambda *_: self.fail("unexpected error"))
        self.assertEqual(("later", "app2"), calls[-1])
        queue.clear()
        self.assertEqual(0, len(queue))

    def test_element_id_compatibility_and_range_ticking(self):
        self.assertEqual(2**33, logic.element_id_value(types.SimpleNamespace(Value=2**33)))
        self.assertEqual(17, logic.element_id_value(types.SimpleNamespace(IntegerValue=17)))
        keys = ["b", "d", "a", "c"]
        self.assertEqual([1, 2, 3], logic.range_indexes(keys, "c", "d"))
        self.assertEqual([2], logic.range_indexes(keys, "missing", "a"))
        self.assertEqual([], logic.range_indexes(keys, "a", "missing"))

    def test_tick_targets_follow_highlighted_rows_or_shift_range(self):
        keys = ["b", "d", "a", "c"]
        self.assertEqual([0, 2], logic.tick_indexes(keys, ["b", "a", "hidden"], None, "a", False))
        self.assertEqual([1], logic.tick_indexes(keys, ["b", "a"], None, "d", False))
        self.assertEqual([1, 2, 3], logic.tick_indexes(keys, ["b"], "d", "c", True))
        self.assertEqual([], logic.tick_indexes(keys, ["hidden"], None, "hidden", False))

    def test_filter_escapes_literals_and_applies_source_level_and_phase(self):
        query = logic.row_filter(["space", "link"], ["id'1"], False, True, "A[1] 50%*")
        self.assertIn("[__source] = 'space'", query)
        self.assertIn("[__link] = 'id''1'", query)
        self.assertIn("[__level_match] = TRUE", query)
        self.assertIn("[__phase_match] = TRUE", query)
        self.assertIn("A[[]1[]]", query)
        self.assertIn("50[%][*]", query)
        self.assertEqual("1 = 0", logic.row_filter([], [], True, False, ""))
        self.assertNotIn("__source] = 'link'", logic.row_filter(["link"], [], True, False, ""))

    def test_rectangle_chains_reversed_and_unordered_edges(self):
        segments = [line((0, 0), (4, 0)), line((4, 3), (4, 0)),
                    line((0, 3), (0, 0)), line((4, 3), (0, 3))]
        cleaned, warnings = logic.clean_loop(segments, 0.01, 0.011)
        self.assertEqual([], warnings)
        self.assertAlmostEqual(12.0, abs(logic.signed_area(cleaned)))
        for index, segment in enumerate(cleaned):
            self.assertEqual(segment["end"], cleaned[(index + 1) % len(cleaned)]["start"])

    def test_l_shape_island_and_collinear_simplification(self):
        outer = polygon([(0, 0), (3, 0), (6, 0), (6, 2),
                         (2, 2), (2, 6), (0, 6)])
        cleaned, _ = logic.clean_loop(outer, 0.01, 0.011, simplify=True)
        island, _ = logic.clean_loop(polygon([(0.5, 0.5), (1, 0.5),
                                              (1, 1), (0.5, 1)]), 0.01, 0.011)
        self.assertEqual(6, len(cleaned))
        self.assertAlmostEqual(20.0, abs(logic.signed_area(cleaned)))
        self.assertEqual(1, logic.largest_loop_index([island, cleaned]))

    def test_circle_area_and_reverse_arc(self):
        circle = [arc((1, 0), (0, 1), (-1, 0)), arc((-1, 0), (0, -1), (1, 0))]
        cleaned, warnings = logic.clean_loop(circle, 0.001, 0.0011)
        self.assertEqual([], warnings)
        self.assertAlmostEqual(math.pi, logic.signed_area(cleaned))
        reversed_circle = [logic._reverse(item) for item in reversed(cleaned)]
        self.assertAlmostEqual(-math.pi, logic.signed_area(reversed_circle))

    def test_short_segments_are_removed_and_small_gaps_reported(self):
        segments = polygon([(0, 0), (3, 0), (3.005, 0), (3, 2), (0, 2)])
        cleaned, warnings = logic.clean_loop(segments, 0.01, 0.011)
        self.assertEqual(4, len(cleaned))
        self.assertTrue(any("Removed 1" in item for item in warnings))
        self.assertTrue(any("Snapped" in item for item in warnings))
        self.assertAlmostEqual(6.0, abs(logic.signed_area(cleaned)), places=2)

    def test_large_gaps_degenerate_arcs_and_zero_area_are_rejected(self):
        with self.assertRaisesRegex(logic.GeometryError, "gap"):
            logic.clean_loop([line((0, 0), (1, 0)), line((1.1, 0), (0, 0))], 0.01, 0.02)
        with self.assertRaisesRegex(logic.GeometryError, "collinear"):
            logic.segment_length(arc((0, 0), (1, 0), (2, 0)))
        with self.assertRaisesRegex(logic.GeometryError, "area"):
            logic.clean_loop([line((0, 0), (1, 0)), line((1, 0), (0, 0))], 0.01, 0.02)


class NetList(list):
    def __class_getitem__(cls, unused):
        return cls

    def Add(self, item):
        self.append(item)

    @property
    def Count(self):
        return len(self)


class Point:
    def __init__(self, x, y, z=0.0):
        self.X, self.Y, self.Z = float(x), float(y), float(z)


class Transform:
    def __init__(self, x=0, y=0, z=0, angle=0):
        self.x, self.y, self.z, self.angle = x, y, z, angle

    def OfPoint(self, point):
        c, s = math.cos(self.angle), math.sin(self.angle)
        return Point(c * point.X - s * point.Y + self.x,
                     s * point.X + c * point.Y + self.y, point.Z + self.z)


class FakeLine:
    def __init__(self, start, end):
        self.start, self.end = start, end

    @staticmethod
    def CreateBound(start, end):
        return FakeLine(start, end)

    def GetEndPoint(self, index):
        return (self.start, self.end)[index]

    def CreateTransformed(self, transform):
        return FakeLine(transform.OfPoint(self.start), transform.OfPoint(self.end))


class FakeArc:
    IsBound = True
    Normal = Point(0, 0, 1)

    def __init__(self, start, middle, end):
        self.start, self.middle, self.end = start, middle, end

    @staticmethod
    def Create(start, end, middle):
        return FakeArc(start, middle, end)

    def GetEndPoint(self, index):
        return (self.start, self.end)[index]

    def Evaluate(self, value, normalized):
        center, radius, first, sweep = logic._arc_data(
            (self.start.X, self.start.Y), (self.middle.X, self.middle.Y),
            (self.end.X, self.end.Y))
        angle = first + sweep * value
        return Point(center[0] + radius * math.cos(angle),
                     center[1] + radius * math.sin(angle), self.start.Z)

    def CreateTransformed(self, transform):
        return FakeArc(transform.OfPoint(self.start), transform.OfPoint(self.middle),
                       transform.OfPoint(self.end))

    def Tessellate(self):
        return [self.Evaluate(index / 8.0, True) for index in range(9)]


class FakeCyclicCurve:
    IsCyclic = True
    Period = 2.0 * math.pi

    def __init__(self):
        self.IsBound = False
        self.parts = []

    def Clone(self):
        part = FakeCyclicCurve()
        self.parts.append(part)
        return part

    def MakeBound(self, start, end):
        self.start, self.end = start, end
        self.IsBound = True

    def Tessellate(self):
        if not self.IsBound:
            raise RuntimeError("Only bounded curves can be tessellated")
        return [Point(2.0 * math.cos(self.start + (self.end - self.start) * index / 8.0),
                      math.sin(self.start + (self.end - self.start) * index / 8.0))
                for index in range(9)]

    def Dispose(self):
        self.disposed = True


class FakeCurveLoop(list):
    def Append(self, curve):
        self.append(curve)


class BoundaryValidation:
    outcomes = []
    calls = []

    @classmethod
    def IsValidBoundaryOnView(cls, doc, view_id, loops):
        cls.calls.append(loops)
        return cls.outcomes.pop(0)


class BoundaryOptions:
    instances = []

    def __init__(self):
        self.disposed = False
        self.instances.append(self)

    def Dispose(self):
        self.disposed = True


class Segment:
    def __init__(self, curve):
        self.curve = curve

    def GetCurve(self):
        return self.curve


def boundary(points):
    return [Segment(FakeLine(Point(*points[index]), Point(*points[(index + 1) % len(points)])))
            for index in range(len(points))]


class FakeId:
    def __init__(self, value):
        self.Value = value

    def __eq__(self, other):
        return isinstance(other, FakeId) and self.Value == other.Value

    def __hash__(self):
        return hash(self.Value)


class LocationPoint:
    def __init__(self, point):
        self.Point = point


class Parameter:
    def __init__(self, value, formatted=None):
        self.value, self.formatted = value, formatted
        self.HasValue = value is not None

    def AsString(self):
        return self.value

    def AsElementId(self):
        return self.value

    def AsValueString(self):
        return self.formatted


class Spatial:
    def __init__(self, unique_id="room-1", category=-200, level_id=10, phase_id=20,
                 area=12.0, point=None, boundaries=None):
        self.UniqueId, self.Id = unique_id, FakeId(99)
        self.Number, self.Area = "101", area
        self.Category = types.SimpleNamespace(Id=FakeId(category))
        self.LevelId = FakeId(level_id)
        self.Location = LocationPoint(point or Point(0, 0))
        self.params = {1: Parameter(FakeId(phase_id) if phase_id is not None else None),
                       2: Parameter("Office"), 3: Parameter(area, "12 m²")}
        self.boundaries = boundaries if boundaries is not None else []

    def get_Parameter(self, key):
        return self.params.get(key)

    def GetBoundarySegments(self, options):
        return self.boundaries


class RevitLinkType:
    def __init__(self, mapping):
        self.mapping = mapping

    def GetPhaseMap(self):
        return self.mapping


class PhaseMap(dict):
    def ContainsKey(self, key):
        return key in self


class RevitLinkInstance:
    def __init__(self, doc, transform=None, unique_id="link-1", type_id=400):
        self.doc, self.transform = doc, transform or Transform()
        self.UniqueId, self.Id, self.type_id = unique_id, FakeId(401), FakeId(type_id)
        self.name = "Architectural link"

    def GetLinkDocument(self):
        return self.doc

    def GetTotalTransform(self):
        return self.transform

    def GetTypeId(self):
        return self.type_id


class FilledRegionType:
    def __init__(self, unique_id="type-1"):
        self.UniqueId, self.Id, self.name = unique_id, FakeId(50), "Solid"


class RegenerationFailedException(Exception):
    pass


class FakeDoc:
    def __init__(self):
        self.elements = {}
        self.IsFamilyDocument = False
        self.IsReadOnly = False
        self.Title = "Model"
        self.ProjectInformation = types.SimpleNamespace(UniqueId="project-1")
        self.Application = types.SimpleNamespace(ShortCurveTolerance=0.01)

    def GetElement(self, key):
        return self.elements.get(key.Value if isinstance(key, FakeId) else key)

    def GetHashCode(self):
        return 987

    def Regenerate(self):
        pass


def make_view(phase=20, level_id=10, elevation=0):
    level = types.SimpleNamespace(Id=FakeId(level_id), name="Level 1", ProjectElevation=elevation)
    view = types.SimpleNamespace(Id=FakeId(900), UniqueId="view-1", Name="Level 1 Plan",
                                 ViewType="floor", IsTemplate=False, GenLevel=level)
    view.get_Parameter = lambda key: Parameter(FakeId(phase) if phase is not None else None)
    return view


def fake_environment():
    db = types.ModuleType("fake_db")
    db.XYZ, db.Line, db.Arc, db.CurveLoop = Point, FakeLine, FakeArc, FakeCurveLoop
    db.BoundaryValidation = BoundaryValidation
    db.SpatialElementBoundaryOptions = BoundaryOptions
    db.SpatialElementBoundaryLocation = types.SimpleNamespace(
        Finish="finish", Center="center", CoreBoundary="core", CoreCenter="core-center")
    db.ViewType = types.SimpleNamespace(FloorPlan="floor", CeilingPlan="ceiling",
                                        AreaPlan="area", EngineeringPlan="engineering")
    db.BuiltInCategory = types.SimpleNamespace(OST_Rooms=-200, OST_MEPSpaces=-201)
    db.BuiltInParameter = types.SimpleNamespace(ROOM_PHASE=1, ROOM_NAME=2, ROOM_AREA=3, VIEW_PHASE=4)
    db.LocationPoint = LocationPoint
    db.RevitLinkType, db.RevitLinkInstance, db.FilledRegionType = (
        RevitLinkType, RevitLinkInstance, FilledRegionType)
    db.IFailuresPreprocessor = type("IFailuresPreprocessor", (object,), {})
    db.ITransactionFinalizer = type("ITransactionFinalizer", (object,), {})
    db.FailureSeverity = types.SimpleNamespace(Warning="warning", Error="error",
                                                DocumentCorruption="corruption")
    db.FailureProcessingResult = types.SimpleNamespace(Continue="continue", ProceedWithRollBack="rollback")
    db.TransactionStatus = types.SimpleNamespace(Started="started", Committed="committed",
                                                  RolledBack="rolledback", Pending="pending")
    db.ElementId = FakeId
    pyrevit = types.ModuleType("pyrevit")
    pyrevit.DB = db
    pyrevit.forms = types.SimpleNamespace()
    pyrevit.revit = types.SimpleNamespace(query=types.SimpleNamespace(get_name=lambda element: element.name))
    pyrevit.script = types.SimpleNamespace(get_logger=lambda: mock.Mock())
    generic = types.ModuleType("System.Collections.Generic")
    generic.List = NetList
    system = types.ModuleType("System")
    collections = types.ModuleType("System.Collections")
    autodesk = types.ModuleType("Autodesk")
    revit = types.ModuleType("Autodesk.Revit")
    exceptions = types.ModuleType("Autodesk.Revit.Exceptions")
    exceptions.RegenerationFailedException = RegenerationFailedException
    return db, pyrevit, {
        "System": system, "System.Collections": collections,
        "System.Collections.Generic": generic, "pyrevit": pyrevit,
        "Autodesk": autodesk, "Autodesk.Revit": revit, "Autodesk.Revit.Exceptions": exceptions,
        "room_regions_logic": logic,
    }


class RevitMockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.pyrevit, modules = fake_environment()
        cls.patcher = mock.patch.dict(sys.modules, modules)
        cls.patcher.start()
        cls.geometry = load_module("room_regions_geometry", LIB / "room_regions_geometry.py")
        sys.modules["room_regions_geometry"] = cls.geometry
        cls.backend = load_module("room_regions_revit_test", LIB / "room_regions_revit.py")

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()

    def setUp(self):
        BoundaryValidation.calls = []
        BoundaryValidation.outcomes = []
        BoundaryOptions.instances = []

    def test_link_transform_flattening_and_island_validation(self):
        outer = boundary([(0, 0, 2), (4, 0, 2), (4, 3, 2), (0, 3, 2)])
        island = boundary([(1, 1, 2), (2, 1, 2), (2, 2, 2), (1, 2, 2)])
        spatial = Spatial(boundaries=[outer, island])
        BoundaryValidation.outcomes = [True]
        view = make_view(elevation=30)
        loops, warnings = self.geometry.build_boundaries(
            object(), view, spatial, Transform(10, 20, 4, math.pi / 2),
            "Finish", True, False, 0.01)
        self.assertEqual(2, len(loops))
        self.assertEqual([], warnings)
        first = loops[0][0]
        self.assertAlmostEqual(10, first.start.X)
        self.assertAlmostEqual(20, first.start.Y)
        self.assertAlmostEqual(10, first.end.X)
        self.assertAlmostEqual(24, first.end.Y)
        self.assertEqual(30, first.start.Z)
        self.assertTrue(BoundaryOptions.instances[-1].disposed)

    def test_rejected_islands_fall_back_to_outer_with_warning(self):
        outer = boundary([(0, 0), (5, 0), (5, 5), (0, 5)])
        island = boundary([(1, 1), (2, 1), (2, 2), (1, 2)])
        BoundaryValidation.outcomes = [False, True]
        loops, warnings = self.geometry.build_boundaries(
            object(), make_view(), Spatial(boundaries=[outer, island]),
            None, "Finish", True, False, 0.01)
        self.assertEqual(1, len(loops))
        self.assertTrue(any("islands" in item for item in warnings))
        self.assertEqual([2, 1], [len(call) for call in BoundaryValidation.calls])

    def test_tessellated_outer_fallback_and_rejected_boundary(self):
        first = FakeArc(Point(1, 0), Point(0, 1), Point(-1, 0))
        second = FakeArc(Point(-1, 0), Point(0, -1), Point(1, 0))
        spatial = Spatial(boundaries=[[Segment(first), Segment(second)]])
        BoundaryValidation.outcomes = [False, True]
        loops, warnings = self.geometry.build_boundaries(
            object(), make_view(), spatial, None, "Finish", True, False, 0.001)
        self.assertGreater(len(loops[0]), 2)
        self.assertTrue(all(isinstance(item, FakeLine) for item in loops[0]))
        self.assertTrue(any("tessellated outer loop" in item for item in warnings))
        BoundaryValidation.outcomes = [False, False]
        with self.assertRaisesRegex(logic.GeometryError, "rejected"):
            self.geometry.build_boundaries(
                object(), make_view(), spatial, None, "Finish", True, False, 0.001)

    def test_invalid_small_loop_warns_but_invalid_outer_fails(self):
        outer = boundary([(0, 0), (6, 0), (6, 6), (0, 6)])
        broken_inner = [Segment(FakeLine(Point(1, 1), Point(2, 1))),
                        Segment(FakeLine(Point(2, 1), Point(2, 2)))]
        BoundaryValidation.outcomes = [True]
        loops, warnings = self.geometry.build_boundaries(
            object(), make_view(), Spatial(boundaries=[outer, broken_inner]),
            None, "Finish", True, False, 0.01)
        self.assertEqual(1, len(loops))
        self.assertTrue(any("invalid smaller" in item for item in warnings))
        broken_outer = [Segment(FakeLine(Point(0, 0), Point(10, 0))),
                        Segment(FakeLine(Point(10, 0), Point(10, 10)))]
        with self.assertRaisesRegex(logic.GeometryError, "outer loop"):
            self.geometry.build_boundaries(
                object(), make_view(),
                Spatial(boundaries=[boundary([(1, 1), (2, 1), (2, 2), (1, 2)]), broken_outer]),
                None, "Finish", True, False, 0.01)

    def test_empty_boundary_and_unsupported_boundary_are_explicit(self):
        with self.assertRaisesRegex(logic.GeometryError, "no boundary"):
            self.geometry.build_boundaries(
                object(), make_view(), Spatial(), None, "Finish", True, False, 0.01)
        self.assertTrue(BoundaryOptions.instances[-1].disposed)
        with self.assertRaisesRegex(logic.GeometryError, "Unsupported"):
            self.geometry.build_boundaries(
                object(), make_view(), Spatial(), None, "unknown", True, False, 0.01)

    def test_unbounded_cyclic_curve_is_tessellated_in_bounded_halves(self):
        ellipse = FakeCyclicCurve()
        BoundaryValidation.outcomes = [True]
        loops, warnings = self.geometry.build_boundaries(
            object(), make_view(), Spatial(boundaries=[[Segment(ellipse)]]),
            None, "Finish", True, False, 0.01)
        self.assertEqual(16, len(loops[0]))
        self.assertEqual(2, len(ellipse.parts))
        self.assertTrue(all(part.disposed for part in ellipse.parts))
        self.assertTrue(any("tessellated" in warning for warning in warnings))

    def test_unbounded_noncyclic_curve_is_rejected_explicitly(self):
        curve = types.SimpleNamespace(IsBound=False, IsCyclic=False)
        with self.assertRaisesRegex(logic.GeometryError, "unbounded non-cyclic"):
            self.geometry._tessellated_segments(curve)

    def test_levels_transform_and_link_phase_map(self):
        doc, host_level = FakeDoc(), make_view(elevation=10).GenLevel
        linked_level = types.SimpleNamespace(Id=FakeId(11), name="Linked level", ProjectElevation=7)
        doc.elements[11] = linked_level
        spatial = Spatial(level_id=11, point=Point(2, 3))
        self.assertEqual(("Linked level", True),
                         self.backend._level_matches(spatial, doc, Transform(z=3), host_level))
        self.assertFalse(self.backend._level_matches(spatial, doc, Transform(z=3.01), host_level)[1])
        host_doc = FakeDoc()
        link = RevitLinkInstance(doc)
        host_doc.elements[400] = RevitLinkType(PhaseMap({FakeId(20): FakeId(30)}))
        self.assertEqual(FakeId(30), self.backend._mapped_phase(host_doc, link, FakeId(20)))
        with self.assertRaisesRegex(self.backend.UserError, "no mapping"):
            self.backend._mapped_phase(host_doc, link, FakeId(21))

    def test_resolve_guards_unloaded_link_level_and_phase(self):
        doc, linked, view = FakeDoc(), FakeDoc(), make_view()
        spatial = Spatial(unique_id="linked-room", level_id=11, phase_id=30)
        linked.elements["linked-room"] = spatial
        linked.elements[11] = types.SimpleNamespace(Id=FakeId(11), name="Link L1", ProjectElevation=0)
        link = RevitLinkInstance(linked)
        doc.elements["link-1"] = link
        doc.elements[400] = RevitLinkType(PhaseMap({FakeId(20): FakeId(30)}))
        key = ("link", "linked-room", "link-1")
        self.assertIs(spatial, self.backend._resolve(doc, view, key, True)[0])
        spatial.params[1] = Parameter(FakeId(31))
        with self.assertRaisesRegex(self.backend.UserError, "phase"):
            self.backend._resolve(doc, view, key, True)
        self.assertIs(spatial, self.backend._resolve(doc, view, key, False)[0])
        link.transform = Transform(z=3)
        with self.assertRaisesRegex(self.backend.UserError, "level"):
            self.backend._resolve(doc, view, key, False)
        link.doc = None
        with self.assertRaisesRegex(self.backend.UserError, "unloaded"):
            self.backend._resolve(doc, view, key, False)

    def test_context_blocks_non_plans_and_templates(self):
        doc, view = FakeDoc(), make_view()
        uiapp = types.SimpleNamespace(ActiveUIDocument=types.SimpleNamespace(Document=doc, ActiveView=view))
        self.assertIsNone(self.backend._context(uiapp)[3])
        view.ViewType = "3d"
        self.assertIn("unsupported", self.backend._context(uiapp)[3])
        view.ViewType, view.IsTemplate = "floor", True
        self.assertIn("unsupported", self.backend._context(uiapp)[3])
        self.assertIn("Open", self.backend._context(types.SimpleNamespace(ActiveUIDocument=None))[3])

    def test_status_distinguishes_unplaced_redundant_and_unenclosed(self):
        spatial = Spatial(area=0)
        self.assertEqual("Not enclosed", self.backend._status(spatial, BoundaryOptions()))
        spatial.boundaries = [boundary([(0, 0), (1, 0), (0, 1)])]
        self.assertEqual("Redundant", self.backend._status(spatial, BoundaryOptions()))
        spatial.Location = None
        self.assertEqual("Unplaced", self.backend._status(spatial, BoundaryOptions()))
        spatial.Area = 1
        self.assertEqual("Placed", self.backend._status(spatial, BoundaryOptions()))

    def capture_fakes(self, on_update=None):
        doc, linked, view = FakeDoc(), FakeDoc(), make_view()
        doc.elements[10] = view.GenLevel
        linked.elements[11] = types.SimpleNamespace(Id=FakeId(11), name="Link L1", ProjectElevation=0)
        host_room = Spatial(unique_id="host-room")
        host_space = Spatial(unique_id="host-space", category=-201)
        linked_room = Spatial(unique_id="linked-room", level_id=11, phase_id=30)
        link1 = RevitLinkInstance(linked, unique_id="link-1")
        link2 = RevitLinkInstance(linked, Transform(x=15, angle=math.pi / 2), unique_id="link-2")
        link2.Id = FakeId(402)
        unloaded = RevitLinkInstance(None, unique_id="link-unloaded")
        unloaded.Id = FakeId(403)
        doc.elements[400] = RevitLinkType(PhaseMap({FakeId(20): FakeId(30)}))
        doc.categories = {-200: [host_room], -201: [host_space]}
        linked.categories = {-200: [linked_room]}
        doc.classes = {FilledRegionType: [FilledRegionType()],
                       RevitLinkInstance: [link1, link2, unloaded]}
        uiapp = types.SimpleNamespace(ActiveUIDocument=types.SimpleNamespace(Document=doc, ActiveView=view))

        class Collector:
            def __init__(self, source):
                self.source, self.items = source, []

            def OfClass(self, kind):
                self.items = self.source.classes.get(kind, [])
                return self

            def OfCategory(self, category):
                self.items = self.source.categories.get(category, [])
                return self

            def WhereElementIsNotElementType(self):
                return self

            def __iter__(self):
                return iter(self.items)

        class Progress:
            def __init__(self, **kwargs):
                self.cancelled = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def update_progress(self, value, maximum):
                if on_update:
                    on_update(self, uiapp, value)

        return uiapp, Collector, Progress

    def test_capture_keeps_two_placements_distinct_and_reports_unloaded(self):
        uiapp, collector, progress = self.capture_fakes()
        with mock.patch.object(self.db, "FilteredElementCollector", collector, create=True), \
                mock.patch.object(self.pyrevit.forms, "ProgressBar", progress, create=True):
            data = self.backend.capture(uiapp, {
                "token": self.backend._token(uiapp.ActiveUIDocument.Document, uiapp.ActiveUIDocument.ActiveView),
                "sources": ["space", "room", "link"],
                "link_ids": ["link-1", "link-2", "link-unloaded"],
            })
        self.assertEqual(4, len(data["records"]))
        self.assertEqual({"link-1", "link-2", "link-unloaded"}, {link["id"] for link in data["links"]})
        linked = [record for record in data["records"] if record["key"][0] == "link"]
        self.assertEqual({("link", "linked-room", "link-1"), ("link", "linked-room", "link-2")},
                         {record["key"] for record in linked})
        self.assertTrue(all(record["level_match"] and record["phase_match"] for record in linked))
        self.assertTrue(any("unloaded" in warning for warning in data["warnings"]))
        self.assertTrue(BoundaryOptions.instances[-1].disposed)

    def test_capture_cancellation_and_context_switch_keep_previous_list(self):
        def cancel(progress, uiapp, value):
            progress.cancelled = True

        def switch(progress, uiapp, value):
            uiapp.ActiveUIDocument.ActiveView.UniqueId = "other-view"

        for callback, reason in ((cancel, "cancelled"), (switch, "changed")):
            with self.subTest(reason=reason):
                uiapp, collector, progress = self.capture_fakes(callback)
                with mock.patch.object(self.db, "FilteredElementCollector", collector, create=True), \
                        mock.patch.object(self.pyrevit.forms, "ProgressBar", progress, create=True):
                    with self.assertRaisesRegex(self.backend.UserError, reason):
                        self.backend.capture(uiapp, {
                            "token": self.backend._token(
                                uiapp.ActiveUIDocument.Document, uiapp.ActiveUIDocument.ActiveView),
                            "sources": ["space", "room"], "link_ids": [],
                        })
                self.assertTrue(BoundaryOptions.instances[-1].disposed)

    def test_startup_metadata_never_reads_spatial_elements_or_link_documents(self):
        uiapp, collector, _ = self.capture_fakes()
        links = uiapp.ActiveUIDocument.Document.classes[RevitLinkInstance]
        for link in links:
            link.GetLinkDocument = mock.Mock(side_effect=AssertionError("Unrequested linked model read"))
        with mock.patch.object(self.db, "FilteredElementCollector", collector, create=True), \
                mock.patch.object(collector, "OfCategory", side_effect=AssertionError("Spatial scan")), \
                mock.patch.object(self.pyrevit.forms, "ProgressBar",
                                  side_effect=AssertionError("Startup scan"), create=True):
            data = self.backend.capture(uiapp)
        self.assertEqual([], data["records"])
        self.assertEqual(3, len(data["links"]))
        self.assertEqual(1, len(data["types"]))
        for link in links:
            link.GetLinkDocument.assert_not_called()
        self.assertEqual([], BoundaryOptions.instances)

    def test_load_reads_only_the_requested_link_instance(self):
        uiapp, collector, progress = self.capture_fakes()
        doc, view = uiapp.ActiveUIDocument.Document, uiapp.ActiveUIDocument.ActiveView
        links = doc.classes[RevitLinkInstance]
        for link in links[1:]:
            link.GetLinkDocument = mock.Mock(side_effect=AssertionError("Unselected link read"))
        calls = []
        original = collector.OfCategory

        def category(instance, value):
            calls.append((instance.source, value))
            return original(instance, value)

        with mock.patch.object(self.db, "FilteredElementCollector", collector, create=True), \
                mock.patch.object(collector, "OfCategory", category), \
                mock.patch.object(self.pyrevit.forms, "ProgressBar", progress, create=True):
            data = self.backend.capture(uiapp, {
                "token": self.backend._token(doc, view),
                "sources": ["link"], "link_ids": ["link-1"],
            })
        self.assertEqual([("link", "linked-room", "link-1")],
                         [record["key"] for record in data["records"]])
        self.assertEqual([(links[0].doc, -200)], calls)
        for link in links[1:]:
            link.GetLinkDocument.assert_not_called()

    def test_loading_stale_context_is_rejected_without_scanning(self):
        uiapp, collector, _ = self.capture_fakes()
        with mock.patch.object(self.db, "FilteredElementCollector", collector, create=True), \
                mock.patch.object(collector, "OfCategory", side_effect=AssertionError("Spatial scan")):
            with self.assertRaisesRegex(self.backend.UserError, "changed"):
                self.backend.capture(uiapp, {
                    "token": ("old-project", 0, "old-view"),
                    "sources": ["room"], "link_ids": [],
                })

    def test_host_only_loading_does_not_open_any_link(self):
        uiapp, collector, progress = self.capture_fakes()
        doc, view = uiapp.ActiveUIDocument.Document, uiapp.ActiveUIDocument.ActiveView
        for link in doc.classes[RevitLinkInstance]:
            link.GetLinkDocument = mock.Mock(side_effect=AssertionError("Unrequested linked model read"))
        with mock.patch.object(self.db, "FilteredElementCollector", collector, create=True), \
                mock.patch.object(self.pyrevit.forms, "ProgressBar", progress, create=True):
            data = self.backend.capture(uiapp, {
                "token": self.backend._token(doc, view),
                "sources": ["room"], "link_ids": ["link-1"],
            })
        self.assertEqual(["room"], [record["key"][0] for record in data["records"]])

    def test_empty_source_scope_never_falls_back_to_all_sources(self):
        uiapp, collector, _ = self.capture_fakes()
        doc, view = uiapp.ActiveUIDocument.Document, uiapp.ActiveUIDocument.ActiveView
        with mock.patch.object(self.db, "FilteredElementCollector", collector, create=True), \
                mock.patch.object(collector, "OfCategory", side_effect=AssertionError("Spatial scan")):
            for kinds, reason in (([], "Choose"), (["link"], "Select at least one link")):
                with self.subTest(kinds=kinds), self.assertRaisesRegex(self.backend.UserError, reason):
                    self.backend.capture(uiapp, {
                        "token": self.backend._token(doc, view),
                        "sources": kinds, "link_ids": [],
                    })

    def test_failure_processor_deletes_only_warnings_and_rolls_back_errors(self):
        warning = types.SimpleNamespace(GetDescriptionText=lambda: "minor",
                                        GetSeverity=lambda: self.db.FailureSeverity.Warning)
        error = types.SimpleNamespace(GetDescriptionText=lambda: "serious",
                                      GetSeverity=lambda: self.db.FailureSeverity.Error)
        accessor = types.SimpleNamespace(GetFailureMessages=lambda: [warning, error],
                                         DeleteWarning=mock.Mock())
        processor = self.backend.RegionFailures()
        self.assertEqual(self.db.FailureProcessingResult.ProceedWithRollBack,
                         processor.PreprocessFailures(accessor))
        accessor.DeleteWarning.assert_called_once_with(warning)
        self.assertEqual(["minor"], processor.warnings)
        self.assertEqual(["serious"], processor.errors)

    def test_document_corruption_failure_is_marked_fatal(self):
        error = types.SimpleNamespace(GetDescriptionText=lambda: "corrupt",
                                      GetSeverity=lambda: self.db.FailureSeverity.DocumentCorruption)
        accessor = types.SimpleNamespace(GetFailureMessages=lambda: [error], DeleteWarning=mock.Mock())
        processor = self.backend.RegionFailures()
        self.assertEqual(self.db.FailureProcessingResult.ProceedWithRollBack,
                         processor.PreprocessFailures(accessor))
        self.assertTrue(processor.corrupted)

    def batch_fakes(self, commit_statuses, cancel_after=None, assimilate=None,
                    rollback_status=None):
        db = self.db
        statuses, transactions, groups = list(commit_statuses), [], []

        class Options:
            def SetFailuresPreprocessor(self, processor):
                self.processor = processor

            def SetClearAfterRollback(self, value):
                self.clear = value

            def SetForcedModalHandling(self, value):
                self.modal = value

            def SetTransactionFinalizer(self, value):
                self.finalizer = value

            def Dispose(self):
                self.disposed = True

        class Transaction:
            def __init__(self, doc, name):
                self.status, self.options, self.disposed = "new", Options(), False
                transactions.append(self)

            def Start(self):
                self.status = db.TransactionStatus.Started
                return self.status

            def GetFailureHandlingOptions(self):
                return self.options

            def SetFailureHandlingOptions(self, options):
                self.applied = options

            def Commit(self):
                outcome = statuses.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                if outcome == "corruption":
                    failure = types.SimpleNamespace(
                        GetDescriptionText=lambda: "corrupt",
                        GetSeverity=lambda: db.FailureSeverity.DocumentCorruption)
                    accessor = types.SimpleNamespace(GetFailureMessages=lambda: [failure],
                                                     DeleteWarning=mock.Mock())
                    self.options.processor.PreprocessFailures(accessor)
                    outcome = db.TransactionStatus.RolledBack
                elif outcome == "error-with-warning":
                    warning = types.SimpleNamespace(
                        GetDescriptionText=lambda: "minor warning",
                        GetSeverity=lambda: db.FailureSeverity.Warning)
                    failure = types.SimpleNamespace(
                        GetDescriptionText=lambda: "serious error",
                        GetSeverity=lambda: db.FailureSeverity.Error)
                    accessor = types.SimpleNamespace(GetFailureMessages=lambda: [warning, failure],
                                                     DeleteWarning=mock.Mock())
                    self.options.processor.PreprocessFailures(accessor)
                    outcome = db.TransactionStatus.RolledBack
                self.status = outcome
                if outcome == db.TransactionStatus.Committed:
                    self.options.finalizer.OnCommitted(None, "Create Room Region")
                elif outcome == db.TransactionStatus.RolledBack:
                    self.options.finalizer.OnRolledBack(None, "Create Room Region")
                return self.status

            def GetStatus(self):
                return self.status

            def RollBack(self):
                if rollback_status == "raise-pending":
                    self.status = db.TransactionStatus.Pending
                    raise RuntimeError("Rollback entered failure handling")
                self.status = rollback_status or db.TransactionStatus.RolledBack
                return self.status

            def Dispose(self):
                self.disposed = True

        class Group:
            def __init__(self, doc, name):
                self.status, self.assimilated, self.disposed = "new", False, False
                groups.append(self)

            def Start(self):
                self.status = db.TransactionStatus.Started
                return self.status

            def GetStatus(self):
                return self.status

            def Assimilate(self):
                self.assimilated = True
                self.status = assimilate or db.TransactionStatus.Committed
                return self.status

            def RollBack(self):
                self.status = db.TransactionStatus.RolledBack
                return self.status

            def Dispose(self):
                self.disposed = True

        class Progress:
            cancelled = False

            def __init__(self, **kwargs):
                self.cancelled = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def update_progress(self, value, maximum):
                if cancel_after is not None and value > cancel_after:
                    self.cancelled = True

        class FilledRegion:
            @staticmethod
            def Create(doc, type_id, view_id, loops):
                return types.SimpleNamespace(UniqueId="region-{}".format(len(transactions)))

        return transactions, groups, Transaction, Group, Progress, FilledRegion

    def run_batch(self, statuses, cancel_after=None, assimilate=None, resolve=None,
                  switch=False, rollback_status=None, notify=None):
        transactions, groups, transaction, group, progress, region = self.batch_fakes(
            statuses, cancel_after, assimilate, rollback_status)
        doc, view, region_type = FakeDoc(), make_view(), FilledRegionType()
        doc.elements["type-1"] = region_type
        token = self.backend._token(doc, view)
        request = {
            "token": token, "type_id": "type-1", "boundary": "Finish",
            "include_islands": True, "simplify": False, "follow_phase": True,
            "sources": [{"key": ("room", str(index), ""), "label": "Room {}".format(index)}
                        for index in range(3)],
        }
        resolve = resolve or (lambda *args: (Spatial(), None))
        calls = []

        def context(uiapp):
            calls.append(None)
            if switch and len(calls) >= 3:
                new_view = make_view()
                new_view.UniqueId = "other-view"
                return object(), doc, new_view, None
            return object(), doc, view, None

        with mock.patch.object(self.backend, "_context", side_effect=context), \
                mock.patch.object(self.backend, "_resolve", side_effect=resolve), \
                mock.patch.object(self.geometry, "build_boundaries", return_value=([], [])), \
                mock.patch.object(self.db, "Transaction", transaction, create=True), \
                mock.patch.object(self.db, "TransactionGroup", group, create=True), \
                mock.patch.object(self.db, "FilledRegion", region, create=True), \
                mock.patch.object(self.pyrevit.forms, "ProgressBar", progress, create=True):
            try:
                result = self.backend.create(object(), request, notify)
                return result, transactions, groups, None
            except Exception as ex:
                return None, transactions, groups, ex

    def test_per_region_failure_isolated_and_successes_assimilated(self):
        status = self.db.TransactionStatus
        result, transactions, groups, error = self.run_batch(
            [status.Committed, status.RolledBack, status.Committed])
        self.assertIsNone(error)
        self.assertEqual(["region-1", "region-3"], result["created"])
        self.assertEqual(["Created", "Failed", "Created"], [row[0] for row in result["report"]])
        self.assertEqual(3, len(transactions))
        self.assertTrue(all(tx.options.modal and tx.options.clear and tx.disposed for tx in transactions))
        self.assertTrue(groups[0].assimilated)
        self.assertTrue(groups[0].disposed)

    def test_cancellation_keeps_completed_regions_and_skips_remaining(self):
        status = self.db.TransactionStatus
        result, transactions, groups, error = self.run_batch([status.Committed], cancel_after=1)
        self.assertIsNone(error)
        self.assertEqual(1, len(transactions))
        self.assertEqual(["Created", "Skipped", "Skipped"], [row[0] for row in result["report"]])
        self.assertTrue(result["cancelled"])
        self.assertTrue(groups[0].assimilated)

    def test_rolled_back_region_reports_both_errors_and_warnings(self):
        status = self.db.TransactionStatus
        result, _, groups, error = self.run_batch(
            ["error-with-warning", status.Committed, status.Committed])
        self.assertIsNone(error)
        self.assertEqual("Failed", result["report"][0][0])
        self.assertIn("serious error", result["report"][0][2])
        self.assertIn("minor warning", result["report"][0][2])
        self.assertTrue(groups[0].assimilated)

    def test_pending_aborts_without_starting_another_transaction(self):
        status = self.db.TransactionStatus
        notify = mock.Mock()
        result, transactions, groups, error = self.run_batch(
            [status.Pending, status.Committed], notify=notify)
        self.assertIsNone(result)
        self.assertIsInstance(error, self.backend.BatchAbort)
        self.assertEqual(1, len(transactions))
        self.assertFalse(groups[0].assimilated)
        self.assertEqual(status.Started, groups[0].status)
        self.assertFalse(groups[0].disposed)
        self.assertFalse(transactions[0].disposed)
        transactions[0].options.finalizer.OnCommitted(None, "Create Room Region")
        self.assertEqual(status.RolledBack, groups[0].status)
        self.assertTrue(groups[0].disposed)
        self.assertTrue(transactions[0].disposed)
        self.assertIn("rolled back", notify.call_args[0][0])

    def test_pending_rollback_is_deferred_until_revit_finalizes(self):
        status = self.db.TransactionStatus
        result, transactions, groups, error = self.run_batch(
            [RuntimeError("region error"), status.Committed], rollback_status=status.Pending)
        self.assertIsNone(result)
        self.assertIsInstance(error, self.backend.BatchAbort)
        self.assertEqual(1, len(transactions))
        self.assertEqual(status.Started, groups[0].status)
        self.assertFalse(transactions[0].disposed)
        transactions[0].options.finalizer.OnRolledBack(None, "Create Room Region")
        self.assertEqual(status.RolledBack, groups[0].status)
        self.assertTrue(transactions[0].disposed)

    def test_rollback_exception_after_pending_defers_group_cleanup(self):
        status = self.db.TransactionStatus
        notify = mock.Mock()
        result, transactions, groups, error = self.run_batch(
            [RuntimeError("region error"), status.Committed],
            rollback_status="raise-pending", notify=notify)
        self.assertIsNone(result)
        self.assertIsInstance(error, self.backend.BatchAbort)
        self.assertEqual(1, len(transactions))
        self.assertEqual(status.Started, groups[0].status)
        self.assertFalse(groups[0].disposed)
        self.assertFalse(transactions[0].disposed)
        transactions[0].options.finalizer.OnRolledBack(None, "Create Room Region")
        self.assertEqual(status.RolledBack, groups[0].status)
        self.assertTrue(groups[0].disposed)
        self.assertTrue(transactions[0].disposed)
        self.assertIn("rolled back", notify.call_args[0][0])

    def test_corruption_failure_aborts_entire_batch(self):
        status = self.db.TransactionStatus
        result, transactions, groups, error = self.run_batch([status.Committed, "corruption"])
        self.assertIsNone(result)
        self.assertIsInstance(error, self.backend.BatchAbort)
        self.assertEqual(2, len(transactions))
        self.assertEqual(status.RolledBack, groups[0].status)

    def test_pending_finalizer_reports_failed_group_cleanup(self):
        group = types.SimpleNamespace(RollBack=mock.Mock(side_effect=RuntimeError("busy")),
                                      Dispose=mock.Mock())
        transaction, options, notify = (types.SimpleNamespace(Dispose=mock.Mock()),
                                        types.SimpleNamespace(Dispose=mock.Mock()), mock.Mock())
        finalizer = self.backend.PendingGroupFinalizer(group, notify)
        finalizer.OnCommitted(None, "normal")
        group.RollBack.assert_not_called()
        finalizer.defer(transaction, options)
        finalizer.OnRolledBack(None, "pending")
        self.assertIn("could not be rolled back", notify.call_args[0][0])
        self.assertTrue(group.Dispose.called)
        self.assertTrue(transaction.Dispose.called)
        self.assertTrue(options.Dispose.called)

    def test_regeneration_failure_aborts_batch_and_rolls_back(self):
        status = self.db.TransactionStatus
        result, transactions, groups, error = self.run_batch(
            [RegenerationFailedException("corrupt"), status.Committed, status.Committed])
        self.assertIsNone(result)
        self.assertIsInstance(error, self.backend.BatchAbort)
        self.assertEqual(1, len(transactions))
        self.assertEqual(status.RolledBack, transactions[0].status)
        self.assertEqual(status.RolledBack, groups[0].status)

    def test_context_switch_stops_batch_and_keeps_completed_regions(self):
        status = self.db.TransactionStatus
        result, transactions, groups, error = self.run_batch([status.Committed], switch=True)
        self.assertIsNone(error)
        self.assertEqual(1, len(transactions))
        self.assertEqual(["Created", "Skipped", "Skipped"], [row[0] for row in result["report"]])
        self.assertIn("changed", result["stopped_reason"])
        self.assertTrue(groups[0].assimilated)

    def test_cancel_during_first_progress_update_creates_nothing(self):
        result, transactions, groups, error = self.run_batch([], cancel_after=0)
        self.assertIsNone(error)
        self.assertEqual([], transactions)
        self.assertEqual([], result["created"])
        self.assertEqual(3, len(result["report"]))
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.db.TransactionStatus.RolledBack, groups[0].status)

    def test_uncommitted_group_does_not_return_success(self):
        status = self.db.TransactionStatus
        result, _, groups, error = self.run_batch(
            [status.Committed, status.Committed, status.Committed], assimilate=status.RolledBack)
        self.assertIsNone(result)
        self.assertIsInstance(error, self.backend.UserError)
        self.assertTrue(groups[0].disposed)

    def test_document_or_view_switch_is_rejected_before_transaction(self):
        doc, view = FakeDoc(), make_view()
        request = {"token": ("other", 0, "view"), "type_id": "type-1", "sources": [{}]}
        with mock.patch.object(self.backend, "_context", return_value=(object(), doc, view, None)):
            with self.assertRaisesRegex(self.backend.UserError, "changed"):
                self.backend.create(object(), request)


class UiContractTests(unittest.TestCase):
    def test_xaml_handlers_exist_and_required_controls_are_wired(self):
        root = ET.parse(str(BUNDLE / "ui.xaml")).getroot()
        source = (BUNDLE / "script.py").read_text(encoding="utf-8")
        xname = "{http://schemas.microsoft.com/winfx/2006/xaml}Name"
        names = {node.attrib[xname] for node in root.iter() if xname in node.attrib}
        self.assertTrue({"sourceGrid", "typeBox", "linkList", "searchBox", "statusText",
                         "createButton", "refreshButton", "loadSourcesButton", "logText"} <= names)
        for node in root.iter():
            for event in ("Click", "Checked", "Unchecked", "SelectionChanged", "TextChanged",
                          "Closing", "Closed", "PreviewMouseLeftButtonDown"):
                if event in node.attrib:
                    self.assertIn("def {}(".format(node.attrib[event]), source)
        self.assertIn('__persistentengine__ = True', source)
        self.assertEqual(1, source.count("UI.ExternalEvent.Create("))
        self.assertIn('WindowState.Minimized', source)
        self.assertIn('envvars.get_pyrevit_env_var(WINDOW_KEY)', source)


class UiEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clr = types.ModuleType("clr")
        clr.AddReference = lambda name: None
        clr.GetClrType = lambda kind: kind
        system = types.ModuleType("System")
        system.Action, system.Boolean, system.Double = lambda callback: callback, bool, float
        data = types.ModuleType("System.Data")
        data.DataTable = type("DataTable", (object,), {})
        windows = types.ModuleType("System.Windows")
        windows.WindowState = types.SimpleNamespace(Minimized="minimized", Normal="normal")
        controls = types.ModuleType("System.Windows.Controls")
        controls.ComboBoxItem = type("ComboBoxItem", (object,), {})
        controls.ListBoxItem = type("ListBoxItem", (object,), {})
        input_module = types.ModuleType("System.Windows.Input")
        input_module.Keyboard = types.SimpleNamespace(Modifiers=0)
        input_module.ModifierKeys = types.SimpleNamespace(Shift=1)
        pyrevit = types.ModuleType("pyrevit")
        pyrevit.UI = types.SimpleNamespace(
            IExternalEventHandler=type("IExternalEventHandler", (object,), {}),
            ExternalEventRequest=types.SimpleNamespace(Accepted="accepted", Pending="pending", Denied="denied"))
        pyrevit.forms = types.SimpleNamespace(WPFWindow=type("WPFWindow", (object,), {}),
                                             alert=mock.Mock())
        pyrevit.script = types.SimpleNamespace(get_logger=lambda: mock.Mock(),
                                                get_bundle_file=lambda name: str(BUNDLE / name))
        envvars = types.SimpleNamespace(get_pyrevit_env_var=mock.Mock(),
                                        set_pyrevit_env_var=mock.Mock())
        coreutils = types.ModuleType("pyrevit.coreutils")
        coreutils.envvars = envvars
        backend = types.ModuleType("room_regions_revit")
        backend.PendingTransaction = type("PendingTransaction", (Exception,), {})
        backend.create = mock.Mock()
        modules = {"clr": clr, "System": system, "System.Data": data, "System.Windows": windows,
                   "System.Windows.Controls": controls, "System.Windows.Input": input_module,
                   "pyrevit": pyrevit, "pyrevit.coreutils": coreutils,
                   "room_regions_logic": logic, "room_regions_revit": backend}
        cls.patcher = mock.patch.dict(sys.modules, modules)
        cls.patcher.start()
        cls.script = load_module("room_regions_script_test", BUNDLE / "script.py")
        cls.pyrevit, cls.envvars = pyrevit, envvars

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()

    def setUp(self):
        self.pyrevit.forms.alert.reset_mock()
        self.envvars.get_pyrevit_env_var.reset_mock()
        self.envvars.set_pyrevit_env_var.reset_mock()

    def window(self):
        window = self.script.RoomRegionsWindow.__new__(self.script.RoomRegionsWindow)
        window.closed, window.busy, window.pending_transaction = False, False, False
        window.queue = logic.ActionQueue()
        window.update_enabled = mock.Mock()
        window.set_status = mock.Mock()
        window.append_log = mock.Mock()
        window.report_error = mock.Mock()
        return window

    def test_raise_queues_without_executing_until_external_event(self):
        window = self.window()
        window.ext_event = types.SimpleNamespace(Raise=mock.Mock(return_value="accepted"))
        action = mock.Mock()
        self.assertTrue(window.run_in_revit("Read", action))
        self.assertEqual(1, len(window.queue))
        action.assert_not_called()
        self.script.RoomRegionsEventHandler(window).Execute("uiapp")
        action.assert_called_once_with("uiapp")
        self.assertFalse(window.busy)
        self.assertEqual(0, len(window.queue))

    def test_denied_external_event_clears_the_request_and_reports(self):
        window = self.window()
        window.ext_event = types.SimpleNamespace(Raise=mock.Mock(return_value="denied"))
        action = mock.Mock()
        self.assertFalse(window.run_in_revit("Read", action))
        self.assertEqual(0, len(window.queue))
        self.assertFalse(window.busy)
        action.assert_not_called()
        self.assertIn("could not queue", window.append_log.call_args[0][0])

    def test_reclick_activates_existing_minimized_window(self):
        existing = types.SimpleNamespace(closed=False, WindowState="minimized", Activate=mock.Mock())
        self.envvars.get_pyrevit_env_var.return_value = existing
        with mock.patch.object(self.script, "RoomRegionsWindow") as constructor:
            self.script.main()
            constructor.assert_not_called()
        existing.Activate.assert_called_once_with()
        self.assertEqual("normal", existing.WindowState)

    def test_unsupported_view_disables_model_actions_but_not_refresh(self):
        window = self.window()
        window.initialising = False
        window.snapshot = {"error": "Unsupported view", "read_only": False}
        window.workArea = types.SimpleNamespace(IsEnabled=True)
        window.refreshButton = types.SimpleNamespace(IsEnabled=False)
        window.loadSourcesButton = types.SimpleNamespace(IsEnabled=True)
        window.createButton = types.SimpleNamespace(IsEnabled=True)
        window.selectCreatedButton = types.SimpleNamespace(IsEnabled=True)
        window.last_created = ["old-region"]
        self.script.RoomRegionsWindow.update_enabled(window)
        self.assertFalse(window.workArea.IsEnabled)
        self.assertFalse(window.createButton.IsEnabled)
        self.assertFalse(window.selectCreatedButton.IsEnabled)
        self.assertTrue(window.refreshButton.IsEnabled)
        self.assertFalse(window.loadSourcesButton.IsEnabled)

    def tick_window(self):
        class Selection(list):
            def Clear(self):
                del self[:]

            def Add(self, row):
                self.append(row)

        window = self.window()
        rows = [{"__key": key, "__checked": False} for key in ("a", "b", "c")]
        window.table = types.SimpleNamespace(DefaultView=rows)
        window.sourceGrid = types.SimpleNamespace(SelectedItems=Selection(rows[:2]))
        window.selection_callbacks = []
        window.Dispatcher = types.SimpleNamespace(BeginInvoke=window.selection_callbacks.append)
        window.anchor = None
        window.update_count = mock.Mock()
        return window, rows

    def test_ticking_and_unticking_applies_to_highlighted_rows(self):
        window, rows = self.tick_window()
        sender = types.SimpleNamespace(DataContext=rows[0], IsChecked=True)
        window.row_tick_clicked(sender, None)
        self.assertEqual([True, True, False], [row["__checked"] for row in rows])
        sender.IsChecked = False
        window.row_tick_clicked(sender, None)
        self.assertEqual([False, False, False], [row["__checked"] for row in rows])

    def test_mouse_checkbox_preserves_highlighted_selection_and_handles_input(self):
        window, rows = self.tick_window()
        sender = types.SimpleNamespace(DataContext=rows[0])
        args = types.SimpleNamespace(Handled=False)
        window.row_tick_mouse_down(sender, args)
        self.assertTrue(args.Handled)
        self.assertEqual([True, True, False], [row["__checked"] for row in rows])
        self.assertEqual([rows[0], rows[1]], window.sourceGrid.SelectedItems)
        window.row_tick_mouse_down(sender, args)
        self.assertEqual([False, False, False], [row["__checked"] for row in rows])

    def test_checkbox_restores_highlights_after_datagrid_handles_the_bubbling_event(self):
        window, rows = self.tick_window()
        window.row_tick_mouse_down(types.SimpleNamespace(DataContext=rows[0]),
                                   types.SimpleNamespace(Handled=False))
        window.sourceGrid.SelectedItems[:] = [rows[0]]
        window.selection_callbacks[0]()
        self.assertEqual(rows[:2], window.sourceGrid.SelectedItems)

    def test_shift_checkbox_keeps_visible_range_behavior(self):
        window, rows = self.tick_window()
        window.anchor = "a"
        with mock.patch.object(self.script.Keyboard, "Modifiers", self.script.ModifierKeys.Shift):
            window.row_tick_clicked(types.SimpleNamespace(DataContext=rows[2], IsChecked=True), None)
        self.assertEqual([True, True, True], [row["__checked"] for row in rows])

    def test_new_link_list_does_not_select_every_link(self):
        window = self.window()
        items = []
        window.linkList = types.SimpleNamespace(Items=types.SimpleNamespace(
            Clear=lambda: items.clear(), Add=items.append))
        data = [{"id": "link-1", "name": "First"}, {"id": "link-2", "name": "Second"}]
        window.populate_links(data, None)
        self.assertFalse(any(item.IsSelected for item in items))
        window.populate_links(data, {"link-2"})
        self.assertEqual([False, True], [item.IsSelected for item in items])

    def test_clicking_unhighlighted_row_changes_only_that_row(self):
        window, rows = self.tick_window()
        window.row_tick_clicked(types.SimpleNamespace(DataContext=rows[2], IsChecked=True), None)
        self.assertEqual([False, False, True], [row["__checked"] for row in rows])

    def test_load_button_queues_only_selected_sources_and_links(self):
        window = self.window()
        window.snapshot = {"token": ("project", 1, "view")}
        window.selected_source_kinds = mock.Mock(return_value=["link"])
        window.selected_links = mock.Mock(return_value={"link-2"})
        window.run_in_revit = mock.Mock()
        window.refresh_from_revit = mock.Mock()
        window.load_sources_clicked(None, None)
        label, action = window.run_in_revit.call_args[0]
        self.assertEqual("Load selected sources", label)
        action("uiapp")
        window.refresh_from_revit.assert_called_once_with("uiapp", {
            "token": ("project", 1, "view"), "sources": ["link"], "link_ids": ["link-2"],
        })

    def test_load_without_chosen_sources_reports_without_queueing(self):
        window = self.window()
        window.snapshot = {"token": ("project", 1, "view")}
        window.selected_links = mock.Mock(return_value=set())
        window.run_in_revit = mock.Mock()
        for sources in ([], ["link"]):
            window.selected_source_kinds = mock.Mock(return_value=sources)
            window.load_sources_clicked(None, None)
            window.run_in_revit.assert_not_called()
            self.assertIn("Choose", window.set_status.call_args[0][0])

    def test_pending_completion_is_dispatched_to_an_open_window(self):
        window = self.window()
        window.pending_transaction = True
        window.Dispatcher = types.SimpleNamespace(BeginInvoke=lambda action: action())
        window.pending_finished("Rollback complete.")
        window.set_status.assert_called_once_with("Rollback complete.")
        window.append_log.assert_called_once_with("Rollback complete.")
        self.assertFalse(window.pending_transaction)
        window.update_enabled.assert_called_once()
        window.closed = True
        window.pending_finished("Ignored after close.")
        window.set_status.assert_called_once()

    def test_pending_failure_blocks_new_requests_and_close_until_finalized(self):
        window = self.window()
        self.script.backend.create.side_effect = self.script.backend.PendingTransaction("pending")
        with self.assertRaises(self.script.backend.PendingTransaction):
            window.create_from_revit("uiapp", {})
        window.ext_event = types.SimpleNamespace(Raise=mock.Mock())
        self.assertTrue(window.pending_transaction)
        self.assertFalse(window.run_in_revit("Refresh", mock.Mock()))
        window.ext_event.Raise.assert_not_called()
        args = types.SimpleNamespace(Cancel=False)
        window.window_closing(None, args)
        self.assertTrue(args.Cancel)
        self.assertFalse(window.closed)
        self.script.backend.create.side_effect = None

    def test_create_requires_confirmation_before_queuing_plain_request(self):
        window = self.window()
        window.visible_ticked = mock.Mock(return_value=[
            {"key": ("room", "source-1", ""), "number": "101",
             "name": "Office", "source": "Host"}])
        window.selected_type_id = mock.Mock(return_value="type-1")
        window.snapshot = {"token": ("project", 1, "view")}
        window.boundaryBox = types.SimpleNamespace(SelectedItem="Finish")
        window.islandsCheck = types.SimpleNamespace(IsChecked=True)
        window.simplifyCheck = types.SimpleNamespace(IsChecked=False)
        window.phaseCheck = types.SimpleNamespace(IsChecked=True)
        window.save_settings = mock.Mock()
        window.run_in_revit = mock.Mock()
        window.create_from_revit = mock.Mock()
        self.pyrevit.forms.alert.return_value = False
        window.create_clicked(None, None)
        window.run_in_revit.assert_not_called()
        self.pyrevit.forms.alert.return_value = True
        window.create_clicked(None, None)
        label, action = window.run_in_revit.call_args[0]
        self.assertEqual("Create", label)
        action("uiapp")
        uiapp, request = window.create_from_revit.call_args[0]
        self.assertEqual("uiapp", uiapp)
        self.assertEqual(("project", 1, "view"), request["token"])
        self.assertEqual(("room", "source-1", ""), request["sources"][0]["key"])
        self.assertTrue(request["include_islands"])


if __name__ == "__main__":
    unittest.main()
