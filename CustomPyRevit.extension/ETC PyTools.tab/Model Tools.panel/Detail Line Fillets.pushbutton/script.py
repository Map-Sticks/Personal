"""Fillet detail-line corners by one corner click or two line picks."""

from pyrevit import DB, forms, revit, script
from Autodesk.Revit.UI import TaskDialog, TaskDialogCommandLinkId, TaskDialogResult
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException
from System.Collections.Generic import List

from fillet_logic import (JOIN_TOLERANCE, FilletInputError, corner_lines,
                          corner_tolerance, distance, picked_endpoint, plan_fillet,
                          positive_number)


MM_TO_FT = 1.0 / 304.8


def coordinates(point):
    return (point.X, point.Y, point.Z)


def xyz(point):
    return DB.XYZ(*point)


def validate_line(element, view):
    if not isinstance(element, DB.DetailCurve):
        raise FilletInputError("Pick a detail line, not a model line or other element.")
    if element.OwnerViewId not in (view.Id, view.GetPrimaryViewId()):
        raise FilletInputError("Pick a detail line in this view or its primary view.")
    if not isinstance(element.GeometryCurve, DB.Line):
        raise FilletInputError("The selected corner must be on a straight detail line.")
    if element.Pinned or element.GroupId != DB.ElementId.InvalidElementId:
        raise FilletInputError("Unpin or ungroup the detail line before filleting it.")
    if element.LineStyle is None:
        raise FilletInputError("The detail line has no usable line style.")


def snapshot(element, view):
    validate_line(element, view)
    curve = element.GeometryCurve
    return dict(points=(coordinates(curve.GetEndPoint(0)),
                        coordinates(curve.GetEndPoint(1))),
                style=element.LineStyle.Id)


def pick_tolerance(view, points):
    return corner_tolerance(getattr(view, "Scale", 1), distance(points[0], points[1]))


def highlight(uidoc, element=None):
    """Keep the pending first line visibly selected; failures are cosmetic only."""
    ids = List[DB.ElementId]()
    if element is not None:
        ids.Add(element.Id)
    try:
        uidoc.Selection.SetElementIds(ids)
    except Exception:
        pass


def find_corner(doc, view, selected, picked_point, vertex_tolerance):
    """Resolve the clicked endpoint and require exactly two connected details."""
    data = snapshot(selected, view)
    selected_id = selected.UniqueId
    primary_id = view.GetPrimaryViewId()
    candidates = []
    seen = set()
    collector = DB.FilteredElementCollector(doc, view.Id).OfClass(DB.CurveElement)
    for element in collector:
        if not isinstance(element, DB.DetailCurve):
            continue
        if element.OwnerViewId not in (view.Id, primary_id):
            continue
        key = element.UniqueId
        if key in seen:
            continue
        curve = element.GeometryCurve
        if not curve.IsBound:
            continue
        endpoints = (coordinates(curve.GetEndPoint(0)),
                     coordinates(curve.GetEndPoint(1)))
        candidates.append((key, endpoints, isinstance(curve, DB.Line)))
        seen.add(key)
    if selected_id not in seen:
        candidates.append((selected_id, data["points"], True))
    other_id, corner = corner_lines(
        selected_id, data["points"], coordinates(picked_point), candidates,
        vertex_tolerance, pick_tolerance(view, data["points"]))
    other = doc.GetElement(other_id)
    if other is None:
        raise FilletInputError("The adjoining detail line is no longer available.")
    other_data = snapshot(other, view)
    return other_id, corner, data, other_data


class FilletSession(object):
    def __init__(self, uidoc, view, radius_mm):
        self.uidoc, self.doc, self.view = uidoc, uidoc.Document, view
        self.radius_mm = radius_mm
        self.radius = radius_mm * MM_TO_FT
        self.minimum = self.doc.Application.ShortCurveTolerance
        self.vertex_tolerance = max(self.doc.Application.VertexTolerance,
                                    JOIN_TOLERANCE)
        self.count = 0
        self.tx = None

    def start(self):
        if self.tx is not None:
            return
        self.tx = DB.Transaction(self.doc, "Detail Line Fillets")
        options = self.tx.GetFailureHandlingOptions()
        options = options.SetForcedModalHandling(True)
        options = options.SetClearAfterRollback(True)
        self.tx.SetFailureHandlingOptions(options)
        if self.tx.Start() != DB.TransactionStatus.Started:
            raise RuntimeError("Revit could not start the fillet transaction.")

    def fillet(self, selected, picked_point):
        other_id, _, _, _ = find_corner(
            self.doc, self.view, selected, picked_point, self.vertex_tolerance)
        return self.fillet_pair(selected, self.doc.GetElement(other_id))

    def fillet_pair(self, first_element, second_element):
        first = snapshot(first_element, self.view)
        second = snapshot(second_element, self.view)
        first_id, second_id = first_element.UniqueId, second_element.UniqueId
        if first_id == second_id:
            raise FilletInputError("Pick a different second detail line.")
        first_points, second_points, geometry = plan_fillet(
            first["points"], second["points"], self.radius, self.minimum,
            self.vertex_tolerance)
        first_curve = DB.Line.CreateBound(xyz(first_points[0]), xyz(first_points[1]))
        second_curve = DB.Line.CreateBound(xyz(second_points[0]), xyz(second_points[1]))
        arc = DB.Arc.Create(xyz(geometry["start"]), xyz(geometry["end"]),
                            xyz(geometry["middle"]))

        self.start()
        first_element.SetGeometryCurve(first_curve, True)
        second_element.SetGeometryCurve(second_curve, True)
        detail = self.doc.Create.NewDetailCurve(self.view, arc)
        if detail is None:
            raise RuntimeError("Revit could not create the fillet arc.")
        detail.LineStyle = first_element.LineStyle
        self.doc.Regenerate()

        for element_id, points, style_id in (
                (first_id, first_points, first["style"]),
                (second_id, second_points, second["style"])):
            element = self.doc.GetElement(element_id)
            verify_points(element, points, self.vertex_tolerance)
            if element.LineStyle.Id != style_id:
                raise RuntimeError("Revit changed a source line style.")
        verify_points(detail, (geometry["start"], geometry["end"]),
                      self.vertex_tolerance)
        if (not isinstance(detail.GeometryCurve, DB.Arc)
                or abs(detail.GeometryCurve.Radius - self.radius) > self.vertex_tolerance
                or detail.LineStyle.Id != first["style"]):
            raise RuntimeError("Revit changed the fillet radius or line style.")

        self.count += 1
        self.uidoc.RefreshActiveView()
        return geometry["corner"]

    def finish(self):
        if self.tx is None:
            return 0
        if self.tx.Commit() != DB.TransactionStatus.Committed:
            raise RuntimeError("Revit did not commit the fillets.")
        transaction, self.tx = self.tx, None
        transaction.Dispose()
        return self.count

    def cancel(self):
        if self.tx is None:
            return
        if self.tx.GetStatus() == DB.TransactionStatus.Started:
            if self.tx.RollBack() != DB.TransactionStatus.RolledBack:
                raise RuntimeError("Revit could not roll back the fillets.")
        transaction, self.tx = self.tx, None
        transaction.Dispose()
        self.uidoc.RefreshActiveView()


def verify_points(element, points, tolerance):
    curve = element.GeometryCurve
    if any(distance(coordinates(curve.GetEndPoint(index)), points[index])
           > tolerance for index in range(2)):
        raise RuntimeError("Revit adjusted the requested line or fillet geometry.")


def ask_radius():
    while True:
        value = forms.ask_for_string(
            prompt="Enter the fillet radius in model millimetres.",
            title="Detail Line Fillets", default="100")
        if value is None:
            return None
        try:
            return positive_number(value)
        except FilletInputError as error:
            forms.alert(str(error), title="Invalid fillet radius")


def pick_corner(uidoc, radius_mm, pending=False, message=None):
    action = "Pick second line" if pending else "Pick a corner or first line"
    prompt = "FILLET R={:g} mm | {}".format(radius_mm, action)
    if message:
        prompt = "{} | Not accepted: {}".format(prompt, message)
    prompt += (" | Esc = clear first line" if pending
               else " | Esc = Finish / Continue / Cancel")
    reference = uidoc.Selection.PickObject(ObjectType.PointOnElement, prompt)
    return uidoc.Document.GetElement(reference.ElementId), reference.GlobalPoint


def next_action(count, radius_mm):
    dialog = TaskDialog("Detail Line Fillets")
    dialog.MainInstruction = "{} fillet(s) drawn at R {:g} mm.".format(count, radius_mm)
    dialog.MainContent = "Continue picking corners, finish the single transaction, or cancel all."
    dialog.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, "Finish")
    dialog.AddCommandLink(TaskDialogCommandLinkId.CommandLink2, "Continue")
    dialog.AddCommandLink(TaskDialogCommandLinkId.CommandLink3, "Cancel")
    return dialog.Show()


def run_tool(uidoc, view, radius_mm):
    """Corner click = one-click fillet; any other line click = first of a pair.

    Rejected clicks never open a popup: the reason is shown in the status-bar
    prompt of the next pick. Esc clears a pending first line; Esc with nothing
    pending opens Finish / Continue / Cancel.
    """
    session = FilletSession(uidoc, view, radius_mm)
    pending = None
    message = None
    highlight(uidoc)
    try:
        while True:
            try:
                selected, picked_point = pick_corner(
                    uidoc, radius_mm, pending is not None, message)
            except OperationCanceledException:
                message = None
                if pending is not None:
                    pending = None
                    highlight(uidoc)
                    continue
                if session.count == 0:
                    return 0
                choice = next_action(session.count, radius_mm)
                if choice == TaskDialogResult.CommandLink2:
                    continue
                if choice == TaskDialogResult.CommandLink1:
                    return session.finish()
                return 0
            try:
                data = snapshot(selected, view)
                if pending is not None:
                    session.fillet_pair(session.doc.GetElement(pending), selected)
                    pending = None
                    highlight(uidoc)
                    message = None
                    continue
                near = picked_endpoint(data["points"], coordinates(picked_point),
                                       pick_tolerance(view, data["points"]))
                if near is None:
                    pending, message = selected.UniqueId, None
                    highlight(uidoc, selected)
                    continue
                try:
                    other_id, _, _, _ = find_corner(
                        session.doc, view, selected, picked_point,
                        session.vertex_tolerance)
                except FilletInputError as error:
                    pending, message = selected.UniqueId, str(error)
                    highlight(uidoc, selected)
                    continue
                session.fillet_pair(selected, session.doc.GetElement(other_id))
                message = None
            except FilletInputError as error:
                message = str(error)
    finally:
        highlight(uidoc)
        session.cancel()


def main():
    uidoc, doc = revit.uidoc, revit.doc
    if doc is None or uidoc is None or doc.IsFamilyDocument:
        forms.alert("Open a Revit project and a view containing detail lines.",
                    title="Detail Line Fillets")
        return
    if doc.IsReadOnly:
        forms.alert("The document is read-only.", title="Detail Line Fillets")
        return
    view = uidoc.ActiveView
    if view.IsTemplate or view.ViewType in (DB.ViewType.ThreeD, DB.ViewType.DrawingSheet):
        forms.alert("Use a plan, section, elevation, drafting or detail view.",
                    title="Detail Line Fillets")
        return
    radius = ask_radius()
    if radius is None:
        return
    run_tool(uidoc, view, radius)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        script.get_logger().exception("Detail Line Fillets failed")
        forms.alert("Detail Line Fillets failed:\n{}\n\n"
                    "The transaction was rolled back. See the pyRevit output for details."
                    .format(error), title="Detail Line Fillets")
