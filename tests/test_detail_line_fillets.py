"""Verify detail-line corner detection and one-transaction fillets."""

import copy
import importlib.util
import math
from pathlib import Path
import runpy
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


BUNDLE = (Path(__file__).resolve().parents[1] / "CustomPyRevit.extension"
          / "ETC PyTools.tab" / "Model Tools.panel" / "Detail Line Fillets.pushbutton")
spec = importlib.util.spec_from_file_location("fillet_logic", BUNDLE / "lib" / "fillet_logic.py")
logic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logic)


def sources():
    return {
        "a": ((0., 0., 0.), (10., 0., 0.)),
        "b": ((0., 0., 0.), (0., 10., 0.)),
        "c": ((10., 0., 0.), (10., 10., 0.)),
    }


class GeometryTests(unittest.TestCase):
    def test_right_angle_radius_tangency_and_trim(self):
        a, b, arc = logic.plan_fillet(sources()["a"], sources()["b"], 2., .001)
        for actual, expected in ((a[0], (2., 0., 0.)),
                                 (b[0], (0., 2., 0.)),
                                 (arc["center"], (2., 2., 0.))):
            for value, target in zip(actual, expected):
                self.assertAlmostEqual(target, value)
        self.assertAlmostEqual(2., logic.distance(arc["start"], arc["center"]))
        self.assertAlmostEqual(2., logic.distance(arc["end"], arc["center"]))

    def test_corner_selection_finds_exactly_one_adjoining_line(self):
        candidate_list = [(key, points, True) for key, points in sources().items()]
        other, corner = logic.corner_lines(
            "a", sources()["a"], (0., 0., 0.), candidate_list, 1e-6, .02)
        self.assertEqual("b", other)
        self.assertEqual((0., 0., 0.), corner)

    def test_corner_selection_rejects_noncorner_and_ambiguous_corner(self):
        candidate_list = [(key, points, True) for key, points in sources().items()]
        with self.assertRaisesRegex(ValueError, "endpoint"):
            logic.corner_lines("a", sources()["a"], (5., 0., 0.),
                               candidate_list, 1e-6, .02)
        extra = ("d", ((0., 0., 0.), (-10., 0., 0.)), True)
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            logic.corner_lines("a", sources()["a"], (0., 0., 0.),
                               candidate_list + [extra], 1e-6, .02)

    def test_corner_pick_tolerance_boundary_and_both_endpoints(self):
        points = sources()["a"]
        self.assertEqual(0, logic.picked_endpoint(points, (.02, 0, 0), .02))
        self.assertIsNone(logic.picked_endpoint(points, (.020001, 0, 0), .02))
        self.assertEqual(1, logic.picked_endpoint(points, (9.99, 0, 0), .02))
        self.assertEqual(1, logic.picked_endpoint(points[::-1], (0, 0, 0), .02))

    def test_corner_tolerance_is_paper_size_times_scale_capped_by_length(self):
        self.assertAlmostEqual(4. / 304.8, logic.corner_tolerance(1, 10.))
        self.assertAlmostEqual(200. / 304.8, logic.corner_tolerance(50, 10.))
        self.assertAlmostEqual(.25, logic.corner_tolerance(500, 1.))
        self.assertAlmostEqual(4. / 304.8, logic.corner_tolerance(None, 10.))

    def test_pair_join_uses_vertex_tolerance_not_pick_tolerance(self):
        near = ((0, 5e-7, 0), (0, 10, 0))
        self.assertEqual((0, 0), logic.joined_ends(sources()["a"], near, 1e-6))
        _, _, arc = logic.plan_fillet(sources()["a"], near, 1, .001, 1e-6)
        self.assertAlmostEqual(1, logic.distance(arc["start"], arc["center"]))
        with self.assertRaisesRegex(ValueError, "shared endpoint"):
            logic.plan_fillet(sources()["a"], ((0, .001, 0), (0, 10, 0)),
                              1, .001, 1e-6)

    def test_bad_radius_and_impossible_trim_are_rejected(self):
        for value in ("", "abc", 0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                logic.positive_number(value)
        with self.assertRaisesRegex(ValueError, "consumed"):
            logic.plan_fillet(sources()["a"], sources()["b"], 10., .001)


class Point:
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = x, y, z


class CurveElement:
    pass


class Curve:
    IsBound = True

    def __init__(self, a, b):
        self.points = (a, b)

    def GetEndPoint(self, index):
        return self.points[index]


class Line(Curve):
    @staticmethod
    def CreateBound(a, b):
        return Line(a, b)


class Arc(Curve):
    @staticmethod
    def Create(a, b, middle):
        points = [(p.X, p.Y, p.Z) for p in (a, b, middle)]
        ab, am, bm = (logic.distance(points[i], points[j])
                      for i, j in ((0, 1), (0, 2), (1, 2)))
        semi = (ab + am + bm) / 2.
        area = math.sqrt(semi * (semi - ab) * (semi - am) * (semi - bm))
        arc = Arc(a, b)
        arc.Radius = ab * am * bm / (4. * area)
        return arc


class DetailCurve(CurveElement):
    def __init__(self, key, points, style, view):
        self.GeometryCurve = Line(*(Point(*p) for p in points))
        self.LineStyle = SimpleNamespace(Id=style)
        self.OwnerViewId = view.Id
        self.Id = key
        self.Pinned = False
        self.GroupId = -1
        self.UniqueId = key

    def SetGeometryCurve(self, curve, override):
        self.GeometryCurve = curve


class Collector(list):
    def OfClass(self, element_type):
        return self


class StrictTransaction:
    """Reject transaction API access after Dispose, as Revit does."""

    def __init__(self, doc, name):
        self.doc = doc
        self.disposed = False
        self._status = "Uninitialized"
        doc.transactions.append(self)

    def check(self, operation):
        if self.disposed:
            raise RuntimeError("The managed object is not valid.")
        self.doc.events.append(operation)

    def GetFailureHandlingOptions(self):
        self.check("options")
        options = Mock()
        options.SetForcedModalHandling.return_value = options
        options.SetClearAfterRollback.return_value = options
        return options

    def SetFailureHandlingOptions(self, options):
        self.check("set-options")

    def Start(self):
        self.check("start")
        self.before = copy.deepcopy(self.doc.elements)
        self._status = self.doc.start_result
        self.doc.IsModifiable = self._status == "Started"
        return self._status

    def GetStatus(self):
        self.check("status")
        return self._status

    def Commit(self):
        self.check("commit")
        if self.doc.commit_error:
            raise self.doc.commit_error
        self._status = self.doc.commit_result
        if self._status == "RolledBack":
            self.doc.elements = self.before
        self.doc.IsModifiable = False
        return self._status

    def RollBack(self):
        self.check("rollback")
        if self.doc.rollback_result == "RolledBack":
            self.doc.elements = self.before
            self.doc.IsModifiable = False
            self._status = "RolledBack"
        return self.doc.rollback_result

    def Dispose(self):
        self.check("dispose")
        if self._status == "Started":
            raise AssertionError("Disposing an open transaction")
        self.disposed = True


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.view = SimpleNamespace(Id=42, GetPrimaryViewId=lambda: -1,
                                    IsTemplate=False, ViewType=1)
        self.doc = SimpleNamespace(
            elements={}, transactions=[], events=[], start_result="Started",
            commit_result="Committed", rollback_result="RolledBack",
            commit_error=None, IsModifiable=False, IsFamilyDocument=False,
            IsReadOnly=False,
            Application=SimpleNamespace(ShortCurveTolerance=.001,
                                        VertexTolerance=1e-6))
        self.doc.GetElement = lambda key: self.doc.elements.get(key)
        for index, (key, points) in enumerate(sources().items(), 1):
            self.doc.elements[key] = DetailCurve(key, points, index, self.view)
        self.doc.Create = SimpleNamespace(NewDetailCurve=Mock(side_effect=self.create_detail))
        self.doc.Regenerate = Mock()
        self.uidoc = SimpleNamespace(Document=self.doc, ActiveView=self.view,
                                     RefreshActiveView=Mock())
        self.uidoc.Selection = SimpleNamespace(PickObject=Mock(),
                                               SetElementIds=Mock())
        self.pick_points = []
        self.pick_responses = []
        self.dialog_responses = []
        self.dialogs = []

        class FakeTaskDialog:
            def __init__(inner_self, title):
                inner_self.title = title
                self.dialogs.append(inner_self)

            def AddCommandLink(inner_self, link_id, text):
                inner_self.links = getattr(inner_self, "links", [])
                inner_self.links.append((link_id, text))

            def Show(inner_self):
                return self.dialog_responses.pop(0)

        ui = ModuleType("Autodesk.Revit.UI")
        ui.TaskDialog = FakeTaskDialog
        ui.TaskDialogCommandLinkId = SimpleNamespace(
            CommandLink1=1, CommandLink2=2, CommandLink3=3)
        ui.TaskDialogResult = SimpleNamespace(
            CommandLink1=1, CommandLink2=2, CommandLink3=3)
        selection = ModuleType("Autodesk.Revit.UI.Selection")
        selection.ObjectType = SimpleNamespace(PointOnElement=7)
        generic = ModuleType("System.Collections.Generic")

        class IdList(list):
            def Add(inner_self, value):
                inner_self.append(value)

        generic.List = type("ListFactory", (), {
            "__class_getitem__": classmethod(lambda cls, item: IdList)})
        exceptions = ModuleType("Autodesk.Revit.Exceptions")
        exceptions.OperationCanceledException = type(
            "OperationCanceledException", (Exception,), {})
        pyrevit = ModuleType("pyrevit")
        pyrevit.DB = SimpleNamespace(
            CurveElement=CurveElement, DetailCurve=DetailCurve, Line=Line,
            Arc=Arc, XYZ=Point, FilteredElementCollector=self.collector,
            ElementId=SimpleNamespace(InvalidElementId=-1),
            Transaction=StrictTransaction,
            TransactionStatus=SimpleNamespace(Committed="Committed",
                                               Started="Started",
                                               RolledBack="RolledBack"),
            ViewType=SimpleNamespace(ThreeD=10, DrawingSheet=11))
        pyrevit.forms = SimpleNamespace(
            ask_for_string=Mock(return_value="100"),
            alert=Mock(return_value=True))
        pyrevit.revit = SimpleNamespace(doc=self.doc, uidoc=self.uidoc)
        pyrevit.script = SimpleNamespace(get_logger=lambda: SimpleNamespace(
            exception=Mock()))
        with patch.dict(sys.modules, {
                "pyrevit": pyrevit, "fillet_logic": logic,
                ui.__name__: ui, selection.__name__: selection,
                generic.__name__: generic,
                exceptions.__name__: exceptions}):
            self.button = runpy.run_path(str(BUNDLE / "script.py"))
        self.session = self.button["FilletSession"](self.uidoc, self.view, 100.)

    def collector(self, doc, view_id):
        return Collector(list(doc.elements.values()))

    def create_detail(self, view, arc):
        self.assertTrue(self.doc.IsModifiable)
        key = "arc{}".format(len(self.doc.elements))
        element = DetailCurve(key, ((0, 0, 0), (1, 0, 0)), 99, view)
        element.GeometryCurve = arc
        self.doc.elements[key] = element
        return element

    def configure_picks(self, choices):
        for unique_id, point in choices:
            if unique_id is None:
                self.pick_responses.append(
                    self.button["OperationCanceledException"]())
            else:
                self.pick_responses.append(SimpleNamespace(
                    ElementId=unique_id, GlobalPoint=Point(*point)))
        self.uidoc.Selection.PickObject.side_effect = self.next_pick

    def next_pick(self, object_type, prompt):
        self.assertEqual(7, object_type)
        self.pick_points.append(prompt)
        response = self.pick_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def assert_original(self):
        self.assertEqual({"a", "b", "c"}, set(self.doc.elements))
        for key, points in sources().items():
            curve = self.doc.elements[key].GeometryCurve
            actual = tuple(self.button["coordinates"](curve.GetEndPoint(i))
                           for i in range(2))
            self.assertEqual(points, actual)

    def test_corner_fillet_draws_immediately_with_selected_line_style(self):
        corner = self.session.fillet(self.doc.elements["a"], Point(0, 0, 0))
        self.assertEqual((0., 0., 0.), corner)
        arc = self.doc.elements["arc3"]
        self.assertIsInstance(arc.GeometryCurve, Arc)
        self.assertEqual(1, arc.LineStyle.Id)
        self.assertAlmostEqual(100. / 304.8, arc.GeometryCurve.Radius)
        self.assertTrue(self.doc.IsModifiable)
        self.uidoc.RefreshActiveView.assert_called_once()
        self.session.cancel()
        self.assert_original()

    def test_repeated_fillet_commits_once_without_redrawing(self):
        self.session.fillet(self.doc.elements["a"], Point(0, 0, 0))
        first_arc = self.doc.elements["arc3"]
        self.session.fillet(self.doc.elements["a"], Point(10, 0, 0))
        second_arc = self.doc.elements["arc4"]
        self.assertEqual(1, self.doc.events.count("start"))
        self.assertEqual(0, self.doc.events.count("commit"))
        self.assertEqual(2, self.session.finish())
        self.assertEqual(1, self.doc.events.count("commit"))
        self.assertEqual(0, self.doc.events.count("rollback"))
        self.assertIs(first_arc, self.doc.elements["arc3"])
        self.assertIs(second_arc, self.doc.elements["arc4"])
        self.assertEqual(2, self.doc.Create.NewDetailCurve.call_count)
        self.assertEqual("dispose", self.doc.events[-1])

    def test_cancel_rolls_back_every_fillet_and_disposes_once(self):
        self.session.fillet(self.doc.elements["a"], Point(0, 0, 0))
        self.session.fillet(self.doc.elements["a"], Point(10, 0, 0))
        self.session.cancel()
        self.session.cancel()
        self.assert_original()
        self.assertEqual(1, self.doc.events.count("rollback"))
        self.assertEqual(1, self.doc.events.count("dispose"))
        self.assertEqual(0, self.doc.events.count("commit"))

    def test_pick_uses_native_point_on_element_and_finish_commits(self):
        self.configure_picks([("a", (0, 0, 0)), (None, None)])
        self.dialog_responses.append(1)
        self.assertEqual(1, self.button["run_tool"](self.uidoc, self.view, 100.))
        self.assertEqual(1, self.doc.events.count("commit"))
        self.assertIn("Pick a corner or first line", self.pick_points[0])
        self.assertIn("R=100 mm", self.pick_points[0])
        self.assertEqual([(1, "Finish"), (2, "Continue"), (3, "Cancel")],
                         self.dialogs[0].links)
        self.button["forms"].alert.assert_not_called()

    def test_far_from_corner_click_starts_two_line_selection(self):
        self.configure_picks([("a", (5, 0, 0)), ("b", (0, 5, 0)), (None, None)])
        self.dialog_responses.append(1)
        self.assertEqual(1, self.button["run_tool"](self.uidoc, self.view, 100.))
        self.assertIn("Pick second line", self.pick_points[1])
        self.assertEqual(1, self.doc.events.count("commit"))
        self.assertEqual(1, self.doc.elements["arc3"].LineStyle.Id)
        self.button["forms"].alert.assert_not_called()

    def test_second_pick_at_another_corner_still_uses_first_line(self):
        self.configure_picks([("b", (0, 5, 0)), ("a", (10, 0, 0)), (None, None)])
        self.dialog_responses.append(1)
        self.assertEqual(1, self.button["run_tool"](self.uidoc, self.view, 100.))
        self.assertEqual(2, self.doc.elements["arc3"].LineStyle.Id)
        self.assertEqual(sources()["c"][0],
                         self.button["coordinates"](
                             self.doc.elements["c"].GeometryCurve.GetEndPoint(0)))
        self.button["forms"].alert.assert_not_called()

    def test_invalid_second_picks_keep_first_line_and_use_status_prompt(self):
        self.configure_picks([("b", (0, 5, 0)), ("b", (0, 4, 0)),
                              ("c", (10, 5, 0)), ("missing", (3, 3, 0)),
                              ("a", (5, 0, 0)), (None, None)])
        self.dialog_responses.append(1)
        self.assertEqual(1, self.button["run_tool"](self.uidoc, self.view, 100.))
        for index in (2, 3, 4):
            self.assertIn("Pick second line", self.pick_points[index])
        self.assertIn("different", self.pick_points[2])
        self.assertIn("shared endpoint", self.pick_points[3])
        self.assertIn("detail line", self.pick_points[4])
        self.assertEqual(2, self.doc.elements["arc3"].LineStyle.Id)
        self.button["forms"].alert.assert_not_called()

    def test_ambiguous_near_corner_allows_explicit_second_line(self):
        self.doc.elements["d"] = DetailCurve(
            "d", ((0, 0, 0), (-10, 0, 0)), 4, self.view)
        self.configure_picks([("a", (0, 0, 0)), ("b", (0, 5, 0)), (None, None)])
        self.dialog_responses.append(1)
        self.assertEqual(1, self.button["run_tool"](self.uidoc, self.view, 100.))
        self.assertIn("Ambiguous", self.pick_points[1])
        self.assertIn("Pick second line", self.pick_points[1])
        self.assertEqual(1, self.doc.Create.NewDetailCurve.call_count)
        self.button["forms"].alert.assert_not_called()

    def test_rejected_direct_corner_uses_status_and_next_pick_starts_fresh(self):
        self.configure_picks([("missing", (3, 3, 0)), ("a", (0, 0, 0)), (None, None)])
        self.dialog_responses.append(1)
        self.assertEqual(1, self.button["run_tool"](self.uidoc, self.view, 100.))
        self.assertIn("detail line", self.pick_points[1])
        self.assertIn("Pick a corner or first line", self.pick_points[1])
        self.button["forms"].alert.assert_not_called()

    def test_mixed_corner_and_pair_picks_share_one_transaction(self):
        self.configure_picks([("a", (0, 0, 0)), ("c", (10, 5, 0)),
                              ("a", (5, 0, 0)), (None, None)])
        self.dialog_responses.append(1)
        self.assertEqual(2, self.button["run_tool"](self.uidoc, self.view, 100.))
        self.assertEqual(1, self.doc.events.count("start"))
        self.assertEqual(1, self.doc.events.count("commit"))
        self.assertEqual(3, self.doc.elements["arc4"].LineStyle.Id)
        self.button["forms"].alert.assert_not_called()

    def test_esc_with_pending_line_only_clears_it_without_a_dialog(self):
        self.configure_picks([("a", (0, 0, 0)), ("b", (0, 5, 0)),
                              (None, None), ("a", (10, 0, 0)), (None, None)])
        self.dialog_responses.extend([3])
        self.assertEqual(0, self.button["run_tool"](self.uidoc, self.view, 100.))
        self.assertEqual(1, len(self.dialogs))
        self.assertIn("Esc = clear first line", self.pick_points[2])
        self.assertIn("Pick a corner or first line", self.pick_points[3])
        self.assertEqual(2, self.doc.Create.NewDetailCurve.call_count)
        self.assertEqual(1, self.doc.events.count("rollback"))
        self.assert_original()
        self.button["forms"].alert.assert_not_called()

    def test_pending_first_line_is_highlighted_then_cleared(self):
        self.configure_picks([("a", (5, 0, 0)), ("b", (0, 5, 0)), (None, None)])
        self.dialog_responses.append(1)
        self.button["run_tool"](self.uidoc, self.view, 100.)
        calls = [list(call.args[0]) for call in
                 self.uidoc.Selection.SetElementIds.call_args_list]
        self.assertIn(["a"], calls)
        self.assertEqual([], calls[-1])

    def test_corner_capture_scales_with_view_scale(self):
        self.view.Scale = 50
        self.configure_picks([("a", (.5, 0, 0)), (None, None)])
        self.dialog_responses.append(1)
        self.assertEqual(1, self.button["run_tool"](self.uidoc, self.view, 100.))
        self.assertIsInstance(self.doc.elements["arc3"].GeometryCurve, Arc)

    def test_same_click_at_full_scale_starts_two_line_mode(self):
        self.view.Scale = 1
        self.configure_picks([("a", (.5, 0, 0)), (None, None), (None, None)])
        self.assertEqual(0, self.button["run_tool"](self.uidoc, self.view, 100.))
        self.assertIn("Pick second line", self.pick_points[1])
        self.assertEqual([], self.doc.transactions)

    def test_impossible_radius_in_either_mode_reports_status_without_writes(self):
        self.configure_picks([("a", (0, 0, 0)), ("a", (5, 0, 0)),
                              ("b", (0, 5, 0)), (None, None), (None, None)])
        self.assertEqual(0, self.button["run_tool"](self.uidoc, self.view, 3048.))
        self.assertIn("consumed", self.pick_points[1])
        self.assertIn("consumed", self.pick_points[3])
        self.assertIn("Pick second line", self.pick_points[3])
        self.assertEqual([], self.doc.transactions)
        self.button["forms"].alert.assert_not_called()
        self.assert_original()

    def test_curved_pick_is_ignored_without_losing_pending_first_line(self):
        curved = DetailCurve("d", ((0, 0, 0), (1, 1, 0)), 4, self.view)
        curved.GeometryCurve = Arc(Point(0, 0, 0), Point(1, 1, 0))
        self.doc.elements["d"] = curved
        self.configure_picks([("b", (0, 5, 0)), ("d", (.5, .5, 0)),
                              ("a", (5, 0, 0)), (None, None)])
        self.dialog_responses.append(1)
        self.assertEqual(1, self.button["run_tool"](self.uidoc, self.view, 100.))
        self.assertIn("straight detail line", self.pick_points[2])
        self.assertIn("Pick second line", self.pick_points[2])
        self.assertEqual(2, self.doc.elements["arc4"].LineStyle.Id)
        self.button["forms"].alert.assert_not_called()

    def test_second_mode_api_failure_rolls_back_earlier_corner_and_partial_pair(self):
        def create(view, arc):
            if "arc3" in self.doc.elements:
                raise RuntimeError("Arc failed")
            return self.create_detail(view, arc)

        self.doc.Create.NewDetailCurve.side_effect = create
        self.configure_picks([("a", (0, 0, 0)), ("c", (10, 5, 0)),
                              ("a", (5, 0, 0))])
        with self.assertRaisesRegex(RuntimeError, "Arc failed"):
            self.button["run_tool"](self.uidoc, self.view, 100.)
        self.assertEqual(1, self.doc.events.count("rollback"))
        self.assert_original()

    def test_api_value_error_is_not_mistaken_for_a_rejected_pick(self):
        self.doc.Create.NewDetailCurve.side_effect = ValueError("API geometry failure")
        self.configure_picks([("a", (5, 0, 0)), ("b", (0, 5, 0))])
        with self.assertRaisesRegex(ValueError, "API geometry failure"):
            self.button["run_tool"](self.uidoc, self.view, 100.)
        self.assertEqual(1, self.doc.events.count("rollback"))
        self.assert_original()

    def test_continue_restarts_pick_and_cancel_rolls_back(self):
        self.configure_picks([("a", (0, 0, 0)), (None, None),
                              ("a", (10, 0, 0)), (None, None)])
        self.dialog_responses.extend([2, 3])
        self.assertEqual(0, self.button["run_tool"](self.uidoc, self.view, 100.))
        self.assertEqual(4, len(self.pick_points))
        self.assertEqual(0, self.doc.events.count("commit"))
        self.assertEqual(1, self.doc.events.count("rollback"))
        self.assert_original()

    def test_invalid_corner_does_not_open_transaction(self):
        with self.assertRaisesRegex(ValueError, "Click near"):
            self.session.fillet(self.doc.elements["a"], Point(5, 0, 0))
        self.assertEqual([], self.doc.transactions)

    def test_creation_failure_rolls_back_partial_geometry(self):
        self.doc.Create.NewDetailCurve.side_effect = RuntimeError("Arc failed")
        with self.assertRaisesRegex(RuntimeError, "Arc failed"):
            self.session.fillet(self.doc.elements["a"], Point(0, 0, 0))
        self.session.cancel()
        self.assert_original()

    def test_transaction_commit_rejection_is_reported_then_rolled_back(self):
        self.session.fillet(self.doc.elements["a"], Point(0, 0, 0))
        self.doc.commit_result = "RolledBack"
        with self.assertRaisesRegex(RuntimeError, "did not commit"):
            self.session.finish()
        self.session.cancel()
        self.assert_original()
        self.assertEqual("dispose", self.doc.events[-1])


if __name__ == "__main__":
    unittest.main()
