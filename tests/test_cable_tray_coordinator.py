"""Cable Tray Coordinator logic, XAML wiring and route-building tests; no Revit/WPF needed."""

import importlib.util
import math
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
          / "Custom Tools.tab" / "Electrical.panel" / "CableTrayCoordinator.pushbutton")

spec = importlib.util.spec_from_file_location("cable_tray_logic", BUNDLE / "lib" / "cable_tray_logic.py")
logic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logic)

ft = logic.mm_to_ft


def mm(value):
    return round(logic.ft_to_mm(value), 3)


def box(x0, x1, y0, y1, z0, z1):
    """World edge segments of an axis-aligned box given in mm."""
    corners = [(ft(x), ft(y), ft(z)) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    return [(corners[i], corners[j]) for i in range(8) for j in range(i + 1, 8) if bin(i ^ j).count("1") == 1]


def box_faces(x0, x1, y0, y1, z0, z1):
    """World face quads of an axis-aligned box given in mm (like the script's bbox fallback)."""
    c = [(ft(x), ft(y), ft(z)) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    faces = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
    return [tuple(c[i] for i in f) for f in faces]


def tray(x0=0, x1=10000, y=0, z=3000, width=300, height=100, z1=None, ends_connected=(False, False), id_value=1):
    start, end = (ft(x0), ft(y), ft(z)), (ft(x1), ft(y), ft(z if z1 is None else z1))
    return {"id_value": id_value, "start": start, "end": end, "width": ft(width), "height": ft(height),
            "ends": [{"point": start, "connected": ends_connected[0]}, {"point": end, "connected": ends_connected[1]}]}


def params(**kw):
    base = {"direction": "Auto", "angle": 45, "clearance_mm": 50, "auto_offset": True, "round_mm": 25,
            "min_straight_mm": 100, "offset_mm": 300}
    base.update(kw)
    return logic.params_in_feet(base)


# A 200 mm beam crossing the tray at x=4900..5100, 3000 wide, bottom at 2950 (clashes with the 3000 tray).
BEAM = box(4900, 5100, -1500, 1500, 2950, 3200)


class GeometryTests(unittest.TestCase):
    def test_frame_round_trip_and_left_axis(self):
        frame, run = logic.horizontal_frame((1.0, 2.0, 3.0), (1.0, 12.0, 3.0))
        self.assertAlmostEqual(10.0, run)
        self.assertEqual((-1.0, 0.0, 0.0), frame.v)  # left of +Y travel is -X
        p = (4.0, 5.0, 6.0)
        back = frame.to_world(frame.to_local(p))
        for a, b in zip(p, back):
            self.assertAlmostEqual(a, b)

    def test_sloped_or_vertical_trays_are_rejected(self):
        with self.assertRaisesRegex(logic.PlanError, "sloped"):
            logic.horizontal_frame((0, 0, 0), (ft(5000), 0, ft(50)))
        with self.assertRaisesRegex(logic.PlanError, "vertical"):
            logic.horizontal_frame((0, 0, 0), (0, 0, ft(500)))

    def test_clip_to_slab(self):
        clipped = logic.clip_to_slab((0, -2, 0), (0, 2, 0), 1, -1, 1)
        self.assertEqual(((0, -1, 0), (0, 1, 0)), clipped)
        self.assertIsNone(logic.clip_to_slab((0, 2, 0), (0, 3, 0), 1, -1, 1))
        self.assertEqual(((0, 0.5, 0), (1, 0.5, 0)), logic.clip_to_slab((0, 0.5, 0), (1, 0.5, 0), 1, -1, 1))

    def test_clip_polygon_to_slab(self):
        square = ((0, -2, -2), (0, 2, -2), (0, 2, 2), (0, -2, 2))
        clipped = logic.clip_primitive(square, 1, -1, 1)
        self.assertEqual([-1, 1], sorted(set(round(p[1], 6) for p in clipped)))
        self.assertIsNone(logic.clip_primitive(square, 1, 3, 4))

    def test_tray_through_big_wall_clashes_even_with_no_edge_in_corridor(self):
        # 200 mm wall, 10 m long and 6 m high: none of its edges come near the tray at z=3000.
        frame, _ = logic.horizontal_frame(tray()["start"], tray()["end"])
        half_w, half_h, c = ft(150), ft(50), ft(50)
        edges = logic._local_obstacles(frame, [box(4900, 5100, -5000, 5000, 0, 6000)])
        self.assertIsNone(logic.obstacle_extents(edges, half_w, half_h, c)["clash"])
        faces = logic._local_obstacles(frame, [box_faces(4900, 5100, -5000, 5000, 0, 6000)])
        ext = logic.obstacle_extents(faces, half_w, half_h, c)
        self.assertEqual([4900, 5100], [round(logic.ft_to_mm(x)) for x in ext["clash"]["s"]])

    def test_required_offsets_for_crossing_beam(self):
        frame, _ = logic.horizontal_frame((0, 0, ft(3000)), (ft(10000), 0, ft(3000)))
        local = [(frame.to_local(a), frame.to_local(b)) for a, b in BEAM]
        ext = logic.obstacle_extents(local, ft(150), ft(50), ft(50))
        self.assertTrue(ext["clash"])
        offsets = logic.required_offsets(ext, ft(150), ft(50), ft(50))
        self.assertAlmostEqual(300, mm(offsets["Over"]))    # beam top 200 above + 50 + half height 50
        self.assertAlmostEqual(150, mm(offsets["Under"]))   # tray top 50 above beam bottom -50, +50 clearance
        self.assertAlmostEqual(1700, mm(offsets["Left"]))
        self.assertEqual("Under", logic.choose_direction(offsets))

    def test_round_up(self):
        self.assertAlmostEqual(150, logic.round_up(126, 25))
        self.assertAlmostEqual(150, logic.round_up(150, 25))
        self.assertAlmostEqual(126, logic.round_up(126, 0))

    def test_segment_normals_follow_the_bend(self):
        self.assertIsNone(logic.segment_normal((1.0, 0.0, 0.0)))
        self.assertIsNone(logic.segment_normal((0.0, 1.0, 0.0)))
        self.assertEqual((-1.0, 0.0, 0.0), logic.segment_normal((0.0, 0.0, 1.0)))  # riser faces back
        self.assertEqual((1.0, 0.0, 0.0), logic.segment_normal((0.0, 0.0, -1.0)))
        n = logic.segment_normal(logic.normalize((1.0, 0.0, 1.0)))
        self.assertAlmostEqual(0.0, logic.dot(n, logic.normalize((1.0, 0.0, 1.0))))
        self.assertGreater(n[2], 0)


class DetourPlanTests(unittest.TestCase):
    def test_auto_detour_drops_under_beam_with_45_degree_bends(self):
        plan = logic.plan_detour(tray(), [BEAM], [], params())
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual("Under", plan["direction"])
        self.assertAlmostEqual(150, mm(plan["offset"]))
        self.assertEqual((4700, 5300), tuple(round(mm(c)) for c in plan["cut"]))
        zs = [round(mm(s["start"][2])) for s in plan["segments"]] + [round(mm(plan["segments"][-1]["end"][2]))]
        self.assertEqual([3000, 2850, 2850, 3000], zs)
        self.assertEqual(3, len(plan["segments"]))
        self.assertIsNotNone(plan["segments"][0]["normal"])
        self.assertIsNone(plan["segments"][1]["normal"])
        self.assertIn("Fittings: 4", "\n".join(plan["summary"]))

    def test_over_90_degree_has_vertical_risers(self):
        plan = logic.plan_detour(tray(), [BEAM], [], params(direction="Over", angle=90))
        self.assertTrue(plan["ok"], plan["error"])
        first = plan["segments"][0]
        self.assertAlmostEqual(first["start"][0], first["end"][0])
        self.assertAlmostEqual(300, mm(first["end"][2] - first["start"][2]))
        self.assertEqual((-1.0, 0.0, 0.0), tuple(round(c, 6) + 0.0 for c in first["normal"]))

    def test_manual_offset_too_small_warns(self):
        plan = logic.plan_detour(tray(), [BEAM], [], params(direction="Over", auto_offset=False, offset_mm=200))
        self.assertTrue(plan["ok"])
        self.assertIn("still clash", plan["warnings"][0])

    def test_left_right_detours_stay_level(self):
        post = box(4950, 5050, -100, 100, 0, 6000)  # a column through the tray
        plan = logic.plan_detour(tray(), [post], [], params(direction="Left"))
        self.assertTrue(plan["ok"], plan["error"])
        self.assertTrue(all(abs(mm(s["start"][2]) - 3000) < 1e-6 for s in plan["segments"]))
        self.assertAlmostEqual(300, mm(plan["offset"]))  # 100 + 50 + 150, rounded to 25
        self.assertIsNone(plan["segments"][0]["normal"])
        auto = logic.plan_detour(tray(), [post], [], params())
        self.assertIn(auto["direction"], ("Left", "Right"))

    def test_cut_points_with_manual_drop(self):
        cuts = [(ft(6000), ft(10), ft(3050)), (ft(3000), 0, ft(3000))]
        plan = logic.plan_detour(tray(), [], cuts, params(direction="Under", auto_offset=False, offset_mm=300))
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual((3000, 6000), tuple(round(mm(c)) for c in plan["cut"]))
        self.assertEqual([3000, 2700, 2700], [round(mm(s["start"][2])) for s in plan["segments"]])
        self.assertEqual(3300, round(mm(plan["segments"][1]["start"][0])))

    def test_auto_with_cut_points_and_no_obstacle_needs_direction(self):
        cuts = [(ft(3000), 0, ft(3000)), (ft(6000), 0, ft(3000))]
        plan = logic.plan_detour(tray(), [], cuts, params(auto_offset=False))
        self.assertFalse(plan["ok"])
        self.assertIn("choose a direction", plan["error"])

    def test_detour_past_tray_end_is_refused(self):
        plan = logic.plan_detour(tray(x1=5200), [BEAM], [], params())
        self.assertFalse(plan["ok"])
        self.assertIn("Bridge", plan["error"])

    def test_obstacle_clear_of_tray(self):
        far_beam = box(4900, 5100, -1500, 1500, 3500, 3700)
        plan = logic.plan_detour(tray(), [far_beam], [], params())
        self.assertFalse(plan["ok"])
        self.assertIn("No clash", plan["error"])
        manual = logic.plan_detour(tray(), [far_beam], [], params(direction="Under", auto_offset=False))
        self.assertTrue(manual["ok"], manual["error"])
        self.assertIn("does not clash", manual["warnings"][0])

    def test_nothing_picked(self):
        plan = logic.plan_detour(tray(), [], [], params())
        self.assertFalse(plan["ok"])
        self.assertIn("obstacle", plan["error"])

    def test_short_segments_are_refused(self):
        plan = logic.plan_detour(tray(), [BEAM], [], params(direction="Under", angle=90, auto_offset=False,
                                                            offset_mm=60, min_straight_mm=100))
        self.assertFalse(plan["ok"])
        self.assertIn("minimum straight", plan["error"])


class BridgePlanTests(unittest.TestCase):
    def trays(self, gap_from=4000, gap_to=6000, **kw):
        return tray(0, gap_from, id_value=1), tray(gap_to, 10000, id_value=2, **kw)

    def test_straight_join_without_obstacle(self):
        a, b = self.trays()
        plan = logic.plan_bridge(a, b, [], params())
        self.assertTrue(plan["ok"], plan["error"])
        self.assertEqual("Straight", plan["direction"])
        self.assertEqual(1, len(plan["segments"]))
        self.assertAlmostEqual(2000, mm(plan["segments"][0]["length"]))

    def test_bridge_over_beam_snaps_slivers(self):
        a, b = self.trays()
        plan = logic.plan_bridge(a, b, [BEAM], params(direction="Over"))
        self.assertTrue(plan["ok"], plan["error"])
        pts = plan["local_points"]
        self.assertAlmostEqual(0, pts[0][0])
        self.assertAlmostEqual(2000, mm(pts[-1][0]))
        for s in plan["segments"]:
            self.assertGreaterEqual(mm(s["length"]), 100 - 1e-6)

    def test_bridge_misaligned_or_different_sizes(self):
        a, _ = self.trays()
        offset_b = tray(6000, 10000, y=500, id_value=2)
        self.assertIn("not in line", logic.plan_bridge(a, offset_b, [], params())["error"])
        nudged = tray(6000, 10000, y=3, id_value=2)
        self.assertIn("not in line", logic.plan_bridge(a, nudged, [], params())["error"])
        higher = tray(6000, 10000, z=3004, id_value=2)
        self.assertIn("not in line", logic.plan_bridge(a, higher, [], params())["error"])
        bigger = tray(6000, 10000, width=450, id_value=2)
        self.assertIn("different sizes", logic.plan_bridge(a, bigger, [], params())["error"])

    def test_bridge_connected_end_and_short_gap(self):
        a = tray(0, 4000, ends_connected=(False, True), id_value=1)
        b = tray(6000, 10000, id_value=2)
        self.assertIn("already connected", logic.plan_bridge(a, b, [], params())["error"])
        a, b = self.trays(4900, 5100)
        plan = logic.plan_bridge(a, b, [BEAM], params(direction="Over"))
        self.assertIn("too short", plan["error"])

    def test_same_tray_twice(self):
        a, _ = self.trays()
        self.assertIn("two different", logic.plan_bridge(a, dict(a), [], params())["error"])


class ParamsAndPresetsTests(unittest.TestCase):
    def test_normalize_params_falls_back_on_bad_values(self):
        p = logic.normalize_params({"direction": "Sideways", "angle": 33, "offset_mm": "abc", "clearance_mm": 75,
                                    "auto_offset": 0})
        self.assertEqual("Auto", p["direction"])
        self.assertEqual(45, p["angle"])
        self.assertEqual(300.0, p["offset_mm"])
        self.assertEqual(75.0, p["clearance_mm"])
        self.assertFalse(p["auto_offset"])
        self.assertEqual(logic.DEFAULT_PARAMS, logic.normalize_params(None))

    def test_parse_mm_messages(self):
        self.assertEqual(12.5, logic.parse_mm("12,5", "offset_mm"))
        with self.assertRaisesRegex(logic.PlanError, "Offset must be a number"):
            logic.parse_mm("x", "offset_mm")
        with self.assertRaisesRegex(logic.PlanError, "between"):
            logic.parse_mm("-1", "clearance_mm")

    def test_presets(self):
        names = [p["name"] for p in logic.BUILTIN_PRESETS]
        self.assertEqual(len(names), len(set(names)))
        for p in logic.BUILTIN_PRESETS:
            self.assertIn(p["direction"], logic.DIRECTIONS)
            self.assertIn(p["angle"], logic.ANGLES)
        user = logic.normalize_user_presets([{"name": names[0]}, {"name": "Mine", "direction": "Over"},
                                             {"name": "Mine"}, "junk"])
        self.assertEqual(["Mine"], [p["name"] for p in user])
        self.assertEqual(len(names) + 1, len(logic.all_presets(user)))
        with self.assertRaisesRegex(logic.PlanError, "built-in"):
            logic.make_user_preset(names[0], {}, [])
        preset, replaces = logic.make_user_preset(" Mine ", {"direction": "Left"}, ["Mine"])
        self.assertTrue(replaces)
        self.assertEqual("Left", preset["direction"])


class PreviewTests(unittest.TestCase):
    def test_elevation_preview_fits_canvas(self):
        plan = logic.plan_detour(tray(), [BEAM], [], params(direction="Over"))
        prim = logic.preview_primitives(plan, 600, 300)
        self.assertEqual("elevation", prim["view"])
        for x, y in prim["route"]:
            self.assertTrue(0 <= x <= 600 and 0 <= y <= 300)
        ys = [y for _, y in prim["route"]]
        self.assertLess(min(ys), ys[0])  # over goes up = smaller screen y
        self.assertEqual(2, len(prim["cuts"]))
        self.assertTrue(prim["obstacles"])
        self.assertTrue(any("+300 mm" in t for _, _, t in prim["labels"]))

    def test_plan_preview_for_sideways_detour(self):
        post = box(4950, 5050, -100, 100, 0, 6000)
        plan = logic.plan_detour(tray(), [post], [], params(direction="Right"))
        prim = logic.preview_primitives(plan, 600, 300)
        self.assertEqual("plan", prim["view"])
        ys = [y for _, y in prim["route"]]
        self.assertGreater(max(ys), ys[0])  # right = down on screen

    def test_failed_plan_still_draws_context(self):
        plan = logic.plan_detour(tray(), [], [], params())
        prim = logic.preview_primitives(plan, 600, 300)
        self.assertFalse(prim["empty"])
        self.assertEqual([], prim["route"])


class XamlWiringTests(unittest.TestCase):
    def test_every_xaml_handler_and_name_exists_in_script(self):
        root = ET.parse(str(BUNDLE / "ui.xaml")).getroot()
        source = (BUNDLE / "script.py").read_text(encoding="utf-8")
        handlers, names = set(), set()
        for el in root.iter():
            for key, value in el.attrib.items():
                if key in ("Click", "SelectionChanged", "TextChanged", "Checked", "Unchecked", "SizeChanged"):
                    handlers.add(value)
                if key.endswith("}Name"):
                    names.add(value)
        self.assertTrue(handlers)
        for handler in handlers:
            self.assertRegex(source, r"def {}\(self, sender, args\)".format(handler))
        used = set(re.findall(r"self\.((?:btn|cmb|txt|lbl|chk|lst|rb|cnv|panel)[A-Z]\w*)\b", source))
        self.assertTrue(used)
        self.assertEqual(set(), used - names)

    def test_modeless_rules(self):
        source = (BUNDLE / "script.py").read_text(encoding="utf-8")
        self.assertIn("__persistentengine__ = True", source)
        self.assertIn("UI.ExternalEvent.Create(self.handler)", source)
        self.assertNotRegex(source, r"f\"|f'")


# --- fake Revit objects for build_route ----------------------------------------------------------
class XYZ(object):
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = float(x), float(y), float(z)

    def DistanceTo(self, o):
        return math.sqrt((self.X - o.X) ** 2 + (self.Y - o.Y) ** 2 + (self.Z - o.Z) ** 2)

    def DotProduct(self, o):
        return self.X * o.X + self.Y * o.Y + self.Z * o.Z


class Line(object):
    def __init__(self, a, b):
        self.a, self.b = a, b

    @staticmethod
    def CreateBound(a, b):
        return Line(a, b)

    def GetEndPoint(self, i):
        return self.a if i == 0 else self.b

    @property
    def Direction(self):
        d = self.a.DistanceTo(self.b)
        return XYZ((self.b.X - self.a.X) / d, (self.b.Y - self.a.Y) / d, (self.b.Z - self.a.Z) / d)


class Connector(object):
    def __init__(self, owner, origin):
        self.Owner, self.Origin, self.ConnectorType, self.refs = owner, origin, "End", []

    @property
    def IsConnected(self):
        return bool(self.refs)

    @property
    def AllRefs(self):
        return list(self.refs)

    def ConnectTo(self, other):
        self.refs.append(other)
        other.refs.append(self)

    def DisconnectFrom(self, other):
        self.refs.remove(other)
        other.refs.remove(self)


class Location(object):
    def __init__(self, owner, curve):
        self.owner, self._curve = owner, curve

    @property
    def Curve(self):
        return self._curve

    @Curve.setter
    def Curve(self, curve):
        self._curve = curve
        self.owner.log.append(("curve", self.owner.Id.Value))
        self.owner.connectors[0].Origin, self.owner.connectors[1].Origin = curve.a, curve.b


class Element(object):
    next_id = [100]

    def __init__(self, doc, log):
        Element.next_id[0] += 1
        self.Id = SimpleNamespace(Value=Element.next_id[0])
        self.IsValidObject = True
        self.Document = doc
        self.log = log
        doc.elements[self.Id.Value] = self


class FakeTray(Element):
    created = []

    def __init__(self, doc, a, b, log):
        Element.__init__(self, doc, log)
        self.connectors = [Connector(self, a), Connector(self, b)]
        self.ConnectorManager = SimpleNamespace(Connectors=self.connectors)
        self.Location = Location(self, Line(a, b))
        self.ReferenceLevel = SimpleNamespace(Id="L1")
        self.Parameters = []
        self.CurveNormal = None

    def GetTypeId(self):
        return "T1"

    def get_Parameter(self, _):
        return None

    @classmethod
    def Create(cls, doc, type_id, a, b, level_id):
        tray_ = cls(doc, a, b, doc.log)
        cls.created.append(tray_)
        doc.log.append(("create", tray_.Id.Value))
        return tray_


class Fitting(Element):
    def __init__(self, doc, kind, c1, c2):
        Element.__init__(self, doc, doc.log)
        self.kind = kind
        self.connectors = [Connector(self, c1.Origin), Connector(self, c2.Origin)]
        self.MEPModel = SimpleNamespace(ConnectorManager=SimpleNamespace(Connectors=self.connectors))
        c1.ConnectTo(self.connectors[0])
        c2.ConnectTo(self.connectors[1])


class FakeDoc(object):
    def __init__(self, fail_elbows=False):
        self.elements, self.log, self.fail_elbows, self.IsWorkshared = {}, [], fail_elbows, False
        self.Create = SimpleNamespace(NewElbowFitting=self.elbow, NewUnionFitting=self.union)

    def elbow(self, c1, c2):
        if self.fail_elbows:
            raise RuntimeError("failed to insert elbow")
        self.log.append(("elbow",))
        return Fitting(self, "elbow", c1, c2)

    def union(self, c1, c2):
        self.log.append(("union",))
        return Fitting(self, "union", c1, c2)

    def GetElement(self, eid):
        return self.elements.get(eid.Value)

    def Regenerate(self):
        self.log.append(("regen",))


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
        module("System")
        module("System.Collections")
        module("System.Collections.Generic", List={})
        module("System.Windows", Point=Mock(), Visibility=SimpleNamespace(Visible="Visible", Collapsed="Collapsed"))
        module("System.Windows.Controls", Canvas=Mock(), TextBlock=Mock())
        module("System.Windows.Media", Color=Mock(), DoubleCollection=Mock(), PenLineCap=Mock(),
               PenLineJoin=Mock(), PointCollection=Mock(), SolidColorBrush=Mock())
        module("System.Windows.Shapes", Line=Mock(), Polyline=Mock(), Rectangle=Mock())
        self.forms = SimpleNamespace(WPFWindow=object, alert=Mock(), ask_for_string=Mock())
        self.logger = Mock()
        bip = SimpleNamespace(RBS_CABLETRAY_WIDTH_PARAM="W", RBS_CABLETRAY_HEIGHT_PARAM="H",
                              ALL_MODEL_INSTANCE_COMMENTS="C", INVALID="INVALID")
        db = SimpleNamespace(XYZ=XYZ, Line=Line, ConnectorType=SimpleNamespace(End="End", Logical="Logical"),
                             BuiltInParameter=bip, InternalDefinition=type("InternalDefinition", (), {}),
                             StorageType=SimpleNamespace(Double=1, Integer=2, String=3, ElementId=4),
                             ElementId=object)
        ui = SimpleNamespace(IExternalEventHandler=object, ExternalEvent=Mock(),
                             ExternalEventRequest=SimpleNamespace(Accepted="Accepted", Pending="Pending"))
        self.revit = SimpleNamespace(doc=None, uidoc=None, Transaction=Mock())
        module("pyrevit", DB=db, UI=ui, HOST_APP=SimpleNamespace(version="2025"), forms=self.forms,
               revit=self.revit, script=SimpleNamespace(get_logger=lambda: self.logger, get_bundle_file=lambda n: n))
        module("Autodesk")
        module("Autodesk.Revit")
        module("Autodesk.Revit.DB")
        module("Autodesk.Revit.DB.Electrical", CableTray=FakeTray)
        module("Autodesk.Revit.UI")
        module("Autodesk.Revit.UI.Selection", ISelectionFilter=object, ObjectType=SimpleNamespace())
        module("Autodesk.Revit.Exceptions", OperationCanceledException=type("Cancelled", (Exception,), {}))
        modules["cable_tray_logic"] = logic
        script_spec = importlib.util.spec_from_file_location("cable_tray_script", BUNDLE / "script.py")
        self.mod = importlib.util.module_from_spec(script_spec)
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {"APPDATA": self.temp.name}):
            script_spec.loader.exec_module(self.mod)
        FakeTray.created = []

        self.doc = SimpleNamespace(IsValidObject=True, Title="Proj")
        self.doc.Equals = lambda other: other is self.doc
        w = self.mod.CableTrayCoordinator.__new__(self.mod.CableTrayCoordinator)
        w.closed = False
        w.initialising = False
        w.queue = logic.ActionQueue()
        w.ext_event = Mock()
        w.ext_event.Raise.return_value = "Accepted"
        w.doc, w.doc_title = self.doc, "Proj"
        w.status = []
        w.set_status = w.status.append
        self.w = w

    def test_no_project_alerts_and_opens_nothing(self):
        self.forms.alert.assert_called_once()
        self.assertIsNone(self.mod.WINDOW)

    def test_handler_delegates_to_owning_window(self):
        owner = Mock()
        self.mod.CoordinatorEventHandler(owner).Execute("app")
        owner.run_queued.assert_called_once_with("app")

    def test_actions_are_queued_not_run_on_click(self):
        action = Mock()
        self.w.run_in_revit("Pick tray", action)
        action.assert_not_called()
        self.w.run_queued("app")
        action.assert_called_once_with("app")

    def test_rejected_raise_clears_queue_and_closed_window_drops_work(self):
        self.w.ext_event.Raise.return_value = "Denied"
        self.w.run_in_revit("Apply", Mock())
        self.assertEqual(0, len(self.w.queue))
        self.assertIn("could not queue", self.w.status[-1])
        self.w.ext_event.Raise.return_value = "Accepted"
        action = Mock()
        self.w.run_in_revit("Apply", action)
        self.w.closed = True
        self.w.run_queued("app")
        action.assert_not_called()

    def test_other_document_is_refused(self):
        other = SimpleNamespace(ActiveUIDocument=SimpleNamespace(Document=SimpleNamespace()))
        with self.assertRaisesRegex(self.mod.UserError, "Switch back"):
            self.w.active_uidoc(other)

    def test_user_errors_go_to_status(self):
        self.w.on_action_error("Apply", self.mod.UserError("nope"))
        self.assertEqual("nope", self.w.status[-1])

    def make_tray(self, doc, x0, x1):
        return FakeTray(doc, XYZ(ft(x0), 0, ft(3000)), XYZ(ft(x1), 0, ft(3000)), doc.log)

    def test_detour_cuts_reconnects_far_end_and_adds_elbows(self):
        doc = FakeDoc()
        source = self.make_tray(doc, 0, 10000)
        neighbour = self.make_tray(doc, 10000, 15000)
        source.connectors[1].ConnectTo(neighbour.connectors[0])
        start_before = source.connectors[0].Origin
        plan = logic.plan_detour(tray(), [BEAM], [], params())
        created, fittings, warnings = self.mod.build_route(doc, plan, [source], False)

        self.assertEqual([], warnings)
        self.assertEqual(4, len(created))          # 3 detour pieces + far piece
        self.assertEqual(4, len(fittings))
        self.assertTrue(all(f.kind == "elbow" for f in fittings))
        self.assertIs(start_before, source.connectors[0].Origin)  # start end untouched
        self.assertAlmostEqual(4700, mm(source.Location.Curve.GetEndPoint(1).X))
        far = created[-1]
        self.assertIn(neighbour.connectors[0], far.connectors[1].refs)  # far neighbour re-attached
        self.assertEqual([], [r for r in source.connectors[1].refs if r.Owner is neighbour])
        # The far end was detached before the tray was shortened.
        self.assertLess(doc.log.index(("curve", source.Id.Value)), doc.log.index(("create", created[0].Id.Value)))
        self.assertIsNotNone(created[0].CurveNormal)
        self.assertIsNone(created[1].CurveNormal)

    def test_elbow_failure_raises_unless_kept(self):
        plan = logic.plan_detour(tray(), [BEAM], [], params())
        doc = FakeDoc(fail_elbows=True)
        with self.assertRaisesRegex(self.mod.UserError, "Nothing was changed"):
            self.mod.build_route(doc, plan, [self.make_tray(doc, 0, 10000)], False)
        doc = FakeDoc(fail_elbows=True)
        created, fittings, warnings = self.mod.build_route(doc, plan, [self.make_tray(doc, 0, 10000)], True)
        self.assertEqual(4, len(created))
        self.assertEqual(4, len(warnings))

    def test_straight_bridge_uses_unions(self):
        doc = FakeDoc()
        a, b = self.make_tray(doc, 0, 4000), self.make_tray(doc, 6000, 10000)
        plan = logic.plan_bridge(tray(0, 4000, id_value=1), tray(6000, 10000, id_value=2), [], params())
        created, fittings, _ = self.mod.build_route(doc, plan, [a, b], False)
        self.assertEqual(1, len(created))
        self.assertEqual(["union", "union"], [f.kind for f in fittings])
        self.assertTrue(a.connectors[1].IsConnected and b.connectors[0].IsConnected)

    def test_join_refuses_near_straight_kink(self):
        doc = FakeDoc()
        a = self.make_tray(doc, 0, 4000)
        b = FakeTray(doc, XYZ(ft(4000), 0, ft(3000)), XYZ(ft(8000), ft(70), ft(3000)), doc.log)  # ~1 deg
        with self.assertRaisesRegex(self.mod.UserError, "less than"):
            self.mod.join(doc, a, b, XYZ(ft(4000), 0, ft(3000)))
        self.assertEqual([], [e for e in doc.log if e[0] in ("union", "elbow")])


if __name__ == "__main__":
    unittest.main()
