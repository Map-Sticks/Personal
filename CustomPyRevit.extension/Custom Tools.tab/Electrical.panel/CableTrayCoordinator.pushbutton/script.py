# -*- coding: utf-8 -*-
"""Cable Tray Coordinator - re-route cable trays over, under or around obstacles.

Two modes:
  * Detour: pick one tray and the obstacle (or two cut points). The tray is cut, the middle is
    lifted/dropped/jogged with the chosen preset and re-joined with fittings.
  * Bridge: pick two in-line tray ends with a gap (and optionally the obstacle). The tool draws the
    connecting run around the obstacle and joins both ends.

Modeless rules (see docs/pyrevit-patterns.md):
  * __persistentengine__ keeps the engine alive so the window's handlers keep working.
  * The window only works on cached plain-Python data. Every pick, model read and the model change
    run in ExternalEvent.Execute; the change is one transaction (one Undo).
"""
__title__ = "Cable Tray\nCoordinator"
__doc__ = "Re-route a cable tray over, under or around an obstacle using presets, with a live preview."
__author__ = "Map-Sticks / GitHub Copilot"
__persistentengine__ = True

import math
import os
import json
import traceback
from functools import wraps

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
from System.Collections.Generic import List
from System.Windows import Point, Visibility
from System.Windows.Controls import Canvas, TextBlock
from System.Windows.Media import Color, DoubleCollection, PenLineCap, PenLineJoin, PointCollection, SolidColorBrush
from System.Windows.Shapes import Line as WpfLine, Polyline, Rectangle

from pyrevit import DB, UI, HOST_APP, forms, revit, script
from Autodesk.Revit.DB.Electrical import CableTray
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

import cable_tray_logic as logic

logger = script.get_logger()
XAML_PATH = script.get_bundle_file("ui.xaml")
USE_64_BIT_IDS = int(HOST_APP.version) >= 2024
SETTINGS_FILE = os.path.join(
    os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming"),
    "pyRevit", "CableTrayCoordinator.settings.json")
MAX_OBSTACLE_SEGMENTS = 20000
# Joints straighter than COLLINEAR_COS (~0.25 deg) get a union; bends must be at least MIN_BEND_DEG.
COLLINEAR_COS = math.cos(math.radians(0.25))
MIN_BEND_DEG = 5.0
ELBOW_MAX_COS = math.cos(math.radians(MIN_BEND_DEG))
# Instance values carried over to new segments (geometry-driven values such as offsets are not).
COPIED_BUILTINS = ("RBS_CABLETRAY_WIDTH_PARAM", "RBS_CABLETRAY_HEIGHT_PARAM", "ALL_MODEL_INSTANCE_COMMENTS",
                   "RBS_CTC_SERVICE_TYPE")
ANGLE_LABELS = [u"{}{}".format(a, logic.DEG) for a in logic.ANGLES]

WINDOW = globals().get("WINDOW")


class UserError(Exception):
    pass


def load_settings():
    try:
        with open(SETTINGS_FILE, "r") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data):
    try:
        folder = os.path.dirname(SETTINGS_FILE)
        if not os.path.exists(folder):
            os.makedirs(folder)
        with open(SETTINGS_FILE, "w") as fp:
            json.dump(data, fp, indent=2)
    except Exception:
        logger.debug("Could not save settings: {}".format(traceback.format_exc()))


def ui_handler(func):
    """Never let an exception escape a WPF event handler of a modeless window."""
    @wraps(func)
    def wrapper(self, *args):
        try:
            return func(self, *args)
        except Exception as ex:
            logger.error(traceback.format_exc())
            self.set_status(u"ERROR: {}".format(ex))
    return wrapper


# --- Revit helpers: only call from the command itself or ExternalEvent.Execute --------------------
def id_value(element_id):
    return element_id.Value if USE_64_BIT_IDS else element_id.IntegerValue


def xyz_tuple(p):
    return (p.X, p.Y, p.Z)


def to_xyz(t):
    return DB.XYZ(t[0], t[1], t[2])


def element_name(element):
    try:
        return element.Name
    except Exception:
        try:
            return DB.Element.Name.__get__(element)
        except Exception:
            return u"?"


def connector_manager(element):
    manager = getattr(element, "ConnectorManager", None)
    if manager is None:
        mep_model = getattr(element, "MEPModel", None)
        manager = getattr(mep_model, "ConnectorManager", None) if mep_model is not None else None
    return manager


def connector_near(element, point, tolerance=0.01):
    manager = connector_manager(element)
    if manager is None:
        return None
    best = None
    for conn in manager.Connectors:
        if conn.ConnectorType == DB.ConnectorType.Logical:
            continue
        d = conn.Origin.DistanceTo(point)
        if d <= tolerance and (best is None or d < best[0]):
            best = (d, conn)
    return best[1] if best else None


def tray_size(tray):
    width = tray.get_Parameter(DB.BuiltInParameter.RBS_CABLETRAY_WIDTH_PARAM)
    height = tray.get_Parameter(DB.BuiltInParameter.RBS_CABLETRAY_HEIGHT_PARAM)
    return (width.AsDouble() if width is not None else tray.Width,
            height.AsDouble() if height is not None else tray.Height)


def read_tray(tray):
    """Plain-data snapshot of a straight cable tray (ElementId kept for later lookups)."""
    if not isinstance(tray, CableTray):
        raise UserError("That is not a cable tray (fittings cannot be re-routed; pick a straight segment).")
    curve = getattr(tray.Location, "Curve", None)
    if not isinstance(curve, DB.Line):
        raise UserError("Only straight cable tray segments are supported.")
    width, height = tray_size(tray)
    ends = []
    manager = connector_manager(tray)
    for conn in manager.Connectors:
        if conn.ConnectorType == DB.ConnectorType.End:
            ends.append({"point": xyz_tuple(conn.Origin), "connected": bool(conn.IsConnected)})
    type_name = element_name(tray.Document.GetElement(tray.GetTypeId()))
    label = u"{} {:.0f}x{:.0f} [{}]".format(type_name, logic.ft_to_mm(width), logic.ft_to_mm(height),
                                           id_value(tray.Id))
    return {"id": tray.Id, "id_value": id_value(tray.Id), "label": label,
            "start": xyz_tuple(curve.GetEndPoint(0)), "end": xyz_tuple(curve.GetEndPoint(1)),
            "width": width, "height": height, "ends": ends}


def _point(p, transform):
    return xyz_tuple(transform.OfPoint(p) if transform is not None else p)


def _add_mesh(mesh, transform, out):
    for i in range(mesh.NumTriangles):
        if len(out) >= MAX_OBSTACLE_SEGMENTS:
            return
        tri = mesh.get_Triangle(i)
        out.append(tuple(_point(tri.get_Vertex(k), transform) for k in range(3)))


def collect_segments(geometry, transform, out):
    """Append obstacle face triangles (world coordinates) to ``out``, up to MAX_OBSTACLE_SEGMENTS.

    Faces, not just edges: a tray through the middle of a big wall/slab crosses no edge.
    """
    for obj in geometry:
        if len(out) >= MAX_OBSTACLE_SEGMENTS:
            return
        if isinstance(obj, DB.Solid):
            if obj.Faces.Size == 0:
                continue
            for face in obj.Faces:
                if len(out) >= MAX_OBSTACLE_SEGMENTS:
                    return
                mesh = face.Triangulate()
                if mesh is not None:
                    _add_mesh(mesh, transform, out)
        elif isinstance(obj, DB.GeometryInstance):
            collect_segments(obj.GetInstanceGeometry(), transform, out)
        elif isinstance(obj, DB.Mesh):
            _add_mesh(obj, transform, out)


def bbox_segments(element, transform):
    box = element.get_BoundingBox(None)
    if box is None:
        return []
    corners = []
    for x in (box.Min.X, box.Max.X):
        for y in (box.Min.Y, box.Max.Y):
            for z in (box.Min.Z, box.Max.Z):
                corners.append(_point(box.Transform.OfPoint(DB.XYZ(x, y, z)), transform))
    # Corner index bits: 4 = max X, 2 = max Y, 1 = max Z. Six quad faces.
    faces = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
    return [tuple(corners[i] for i in face) for face in faces]


def read_obstacle(element, transform=None, link_name=None, link_id=None):
    options = DB.Options()
    options.DetailLevel = DB.ViewDetailLevel.Fine
    segments = []
    geometry = element.get_Geometry(options)
    if geometry is not None:
        collect_segments(geometry, transform, segments)
    if not segments:
        segments = bbox_segments(element, transform)
    if not segments:
        raise UserError(u"Could not read the geometry of element {}.".format(id_value(element.Id)))
    category = element.Category.Name if element.Category is not None else u"Element"
    label = u"{}: {} [{}]".format(category, element_name(element), id_value(element.Id))
    if link_name:
        label += u" (link: {})".format(link_name)
    return {"key": (link_id, id_value(element.Id)), "label": label, "segments": segments}


def copy_instance_data(source, target, doc):
    """Copy size, comments, service type, workset and project/shared parameter values."""
    names = list(COPIED_BUILTINS) + (["ELEM_PARTITION_PARAM"] if doc.IsWorkshared else [])
    builtins = [getattr(DB.BuiltInParameter, n, None) for n in names]
    pairs = [(source.get_Parameter(b), target.get_Parameter(b)) for b in builtins if b is not None]
    for param in source.Parameters:
        definition = param.Definition
        if isinstance(definition, DB.InternalDefinition) and \
                definition.BuiltInParameter != DB.BuiltInParameter.INVALID:
            continue
        pairs.append((param, target.get_Parameter(definition)))
    for src, dst in pairs:
        if src is None or dst is None or dst.IsReadOnly or not src.HasValue:
            continue
        try:
            st = src.StorageType
            if st == DB.StorageType.Double:
                dst.Set(src.AsDouble())
            elif st == DB.StorageType.Integer:
                dst.Set(src.AsInteger())
            elif st == DB.StorageType.String:
                dst.Set(src.AsString() or u"")
            elif st == DB.StorageType.ElementId:
                dst.Set(src.AsElementId())
        except Exception:
            logger.debug(u"Could not copy {}".format(src.Definition.Name))


def create_segment(doc, source, seg):
    tray = CableTray.Create(doc, source.GetTypeId(), to_xyz(seg["start"]), to_xyz(seg["end"]),
                            source.ReferenceLevel.Id)
    copy_instance_data(source, tray, doc)
    if seg.get("normal"):
        # Without this, sloped/vertical trays get a random 'up' and NewElbowFitting fails.
        tray.CurveNormal = to_xyz(seg["normal"])
    return tray


def detach_end(doc, tray, point):
    """Disconnect whatever is attached at one end; return (owner id, origin) to re-attach later."""
    conn = connector_near(tray, point)
    attached = []
    if conn is None or not conn.IsConnected:
        return attached
    for other in list(conn.AllRefs):
        if other.Owner.Id == tray.Id or other.ConnectorType == DB.ConnectorType.Logical:
            continue
        attached.append((other.Owner.Id, other.Origin))
        conn.DisconnectFrom(other)
    return attached


def reattach(doc, tray, point, attached, warnings):
    conn = connector_near(tray, point)
    for owner_id, origin in attached:
        other = connector_near(doc.GetElement(owner_id), origin)
        if conn is None or other is None:
            warnings.append(u"Could not reconnect element {} at the far end.".format(id_value(owner_id)))
            continue
        conn.ConnectTo(other)


def line_direction(tray):
    return tray.Location.Curve.Direction


def join(doc, first, second, joint):
    c1, c2 = connector_near(first, joint), connector_near(second, joint)
    if c1 is None or c2 is None:
        raise UserError("Segment ends do not meet.")
    cos_angle = abs(line_direction(first).DotProduct(line_direction(second)))
    if cos_angle > COLLINEAR_COS:
        try:
            return doc.Create.NewUnionFitting(c1, c2)
        except Exception:
            # Many tray types have no union: genuinely collinear straights connect directly.
            c1.ConnectTo(c2)
            return None
    if cos_angle > ELBOW_MAX_COS:
        raise UserError(u"Segments meet at less than {} degrees; cannot place a fitting.".format(
            MIN_BEND_DEG))
    return doc.Create.NewElbowFitting(c1, c2)


def build_route(doc, plan, trays, keep_if_fails):
    """Create the planned route. Must run inside an open transaction."""
    segs = plan["segments"]
    warnings = []
    if plan["mode"] == logic.MODE_DETOUR:
        tray = trays[0]
        curve = tray.Location.Curve
        p_start, p_end = curve.GetEndPoint(0), curve.GetEndPoint(1)
        attached = detach_end(doc, tray, p_end)
        tray.Location.Curve = DB.Line.CreateBound(p_start, to_xyz(segs[0]["start"]))
        new_trays = [create_segment(doc, tray, s) for s in segs]
        far = create_segment(doc, tray, {"start": segs[-1]["end"], "end": xyz_tuple(p_end), "normal": None})
        doc.Regenerate()
        reattach(doc, far, p_end, attached, warnings)
        new_trays.append(far)
        chain = [tray] + new_trays
    else:
        new_trays = [create_segment(doc, trays[0], s) for s in segs]
        chain = [trays[0]] + new_trays + [trays[1]]
    doc.Regenerate()

    joints = [to_xyz(s["start"]) for s in segs] + [to_xyz(segs[-1]["end"])]
    fittings = []
    for first, second, joint in zip(chain[:-1], chain[1:], joints):
        try:
            fitting = join(doc, first, second, joint)
            if fitting is not None:
                fittings.append(fitting)
        except Exception as ex:
            message = u"No fitting between trays {} and {} ({})".format(
                id_value(first.Id), id_value(second.Id), ex)
            if not keep_if_fails:
                raise UserError(message + u". Nothing was changed. Check the cable tray type has bends "
                                u"assigned, try another angle, or tick 'Keep route if a fitting fails'.")
            warnings.append(message)
    return new_trays, fittings, warnings


class PredicateFilter(ISelectionFilter):
    def __init__(self, predicate):
        self.predicate = predicate

    def AllowElement(self, element):
        try:
            return bool(self.predicate(element))
        except Exception:
            return False

    def AllowReference(self, reference, position):
        return True


class CoordinatorEventHandler(UI.IExternalEventHandler):
    def __init__(self, window):
        self.window = window

    def Execute(self, uiapp):
        try:
            self.window.run_queued(uiapp)
        except Exception:
            logger.error(traceback.format_exc())

    def GetName(self):
        return "Cable Tray Coordinator"


class CableTrayCoordinator(forms.WPFWindow):
    def __init__(self, doc):
        self.initialising = True
        self.closed = False
        forms.WPFWindow.__init__(self, XAML_PATH)
        self.queue = logic.ActionQueue()
        self.handler = CoordinatorEventHandler(self)
        # Must be created while the command still has a valid API context.
        self.ext_event = UI.ExternalEvent.Create(self.handler)

        self.doc = doc
        self.doc_title = doc.Title
        self.mode = logic.MODE_DETOUR
        self.tray = None
        self.bridge = []
        self.obstacles = []
        self.cut_points = []
        self.plan = None
        self.last_created = []

        settings = load_settings()
        self.user_presets = logic.normalize_user_presets(settings.get("user_presets"))
        for name in logic.DIRECTIONS:
            self.cmbDirection.Items.Add(name)
        for label in ANGLE_LABELS:
            self.cmbAngle.Items.Add(label)
        self.set_params(logic.normalize_params(settings.get("params")))
        self.refresh_presets()
        self.Closed += self.on_closed
        self.initialising = False
        self.update_mode_ui()

    # --- state helpers ----------------------------------------------------------------------
    def set_status(self, text):
        try:
            self.lblStatus.Text = text
        except Exception:
            pass

    def presets(self):
        return logic.all_presets(self.user_presets)

    def refresh_presets(self, select=None):
        self.lstPresets.Items.Clear()
        for preset in self.presets():
            self.lstPresets.Items.Add(preset["name"])
        if select is not None:
            self.lstPresets.SelectedItem = select

    def set_params(self, params):
        was = self.initialising
        self.initialising = True
        try:
            self.cmbDirection.SelectedItem = params["direction"]
            self.cmbAngle.SelectedIndex = logic.ANGLES.index(params["angle"])
            self.txtOffset.Text = u"{:g}".format(params["offset_mm"])
            self.txtClearance.Text = u"{:g}".format(params["clearance_mm"])
            self.txtRound.Text = u"{:g}".format(params["round_mm"])
            self.txtMinStraight.Text = u"{:g}".format(params["min_straight_mm"])
            self.chkAutoOffset.IsChecked = params["auto_offset"]
            self.chkKeepIfFittingFails.IsChecked = params["keep_if_fitting_fails"]
            self.txtOffset.IsEnabled = not params["auto_offset"]
        finally:
            self.initialising = was

    def read_params(self):
        """UI values in mm; raises PlanError with a friendly message for bad input."""
        angle_index = self.cmbAngle.SelectedIndex
        params = {
            "direction": logic.to_text(self.cmbDirection.SelectedItem or logic.AUTO),
            "angle": logic.ANGLES[angle_index] if angle_index >= 0 else 45,
            "auto_offset": bool(self.chkAutoOffset.IsChecked),
            "keep_if_fitting_fails": bool(self.chkKeepIfFittingFails.IsChecked),
        }
        for key, box in (("offset_mm", self.txtOffset), ("clearance_mm", self.txtClearance),
                         ("round_mm", self.txtRound), ("min_straight_mm", self.txtMinStraight)):
            params[key] = logic.parse_mm(box.Text, key)
        return params

    def persist_settings(self):
        data = {"user_presets": self.user_presets}
        try:
            data["params"] = self.read_params()
        except logic.PlanError:
            data["params"] = load_settings().get("params")
        save_settings(data)

    # --- ExternalEvent plumbing -------------------------------------------------------------
    def run_in_revit(self, label, action):
        """Queue action(uiapp); it runs in Revit's API context once Revit is idle."""
        if self.closed:
            return False
        self.queue.add(label, action)
        result = self.ext_event.Raise()
        if result not in (UI.ExternalEventRequest.Accepted, UI.ExternalEventRequest.Pending):
            self.queue.clear()
            self.set_status(u"Revit could not queue '{}' ({}). Try again.".format(label, result))
            return False
        self.set_status(u"{}...".format(label))
        return True

    def run_queued(self, uiapp):
        if self.closed:
            self.queue.clear()
            return
        self.queue.drain(uiapp, self.on_action_error)

    def on_action_error(self, label, ex):
        if isinstance(ex, (UserError, logic.PlanError)):
            self.set_status(logic.to_text(ex))
            return
        if isinstance(ex, OperationCanceledException):
            self.set_status(u"{} cancelled.".format(label))
            return
        logger.error(u"{} failed:\n{}".format(label, traceback.format_exc()))
        self.set_status(u"ERROR in {}: {}".format(label, ex))

    def active_uidoc(self, uiapp):
        uidoc = uiapp.ActiveUIDocument
        if self.doc is None or not self.doc.IsValidObject:
            raise UserError("The project this window was opened for is closed. Close and reopen the tool.")
        if uidoc is None or not self.doc.Equals(uidoc.Document):
            raise UserError(u"Switch back to '{}' to use this window.".format(self.doc_title))
        return uidoc

    def picking(self, func):
        """Hide the window while the user picks in Revit (API context only)."""
        self.Hide()
        try:
            return func()
        finally:
            self.Show()
            self.Activate()

    def excluded_ids(self):
        ids = set()
        if self.tray:
            ids.add(self.tray["id_value"])
        for t in self.bridge:
            ids.add(t["id_value"])
        return ids

    # --- Model reads / picks (API context only) ---------------------------------------------
    def load_selection(self, uidoc):
        doc = uidoc.Document
        trays, others = [], []
        for eid in uidoc.Selection.GetElementIds():
            element = doc.GetElement(eid)
            if isinstance(element, CableTray):
                trays.append(element)
            elif element is not None and element.Category is not None:
                others.append(element)
        if len(trays) > 2:
            raise UserError("Select one tray (detour) or two trays (bridge), plus the obstacle(s).")
        if len(trays) == 1:
            self.tray, self.bridge, self.cut_points = read_tray(trays[0]), [], []
            self.mode = logic.MODE_DETOUR
        elif len(trays) == 2:
            self.bridge, self.tray = [read_tray(trays[0]), read_tray(trays[1])], None
            self.mode = logic.MODE_BRIDGE
        if others:
            self.obstacles = [read_obstacle(e) for e in others[:20]]
        return len(trays), len(others)

    def do_use_selection(self, uiapp):
        trays, others = self.load_selection(self.active_uidoc(uiapp))
        self.update_mode_ui()
        if not trays and not others:
            self.set_status("Nothing selected in Revit. Select the tray(s) and the obstacle, then click again.")
        else:
            self.set_status(u"Loaded {} tray(s) and {} obstacle(s) from the Revit selection.".format(trays, others))

    def do_pick_tray(self, uiapp):
        uidoc = self.active_uidoc(uiapp)
        tray_filter = PredicateFilter(lambda e: isinstance(e, CableTray))
        ref = self.picking(lambda: uidoc.Selection.PickObject(
            ObjectType.Element, tray_filter, "Pick the cable tray segment to re-route"))
        self.tray = read_tray(uidoc.Document.GetElement(ref))
        self.cut_points = []
        self.update_mode_ui()
        self.set_status(u"Tray picked: {}".format(self.tray["label"]))

    def do_pick_ends(self, uiapp):
        uidoc = self.active_uidoc(uiapp)
        doc = uidoc.Document
        first = self.picking(lambda: uidoc.Selection.PickObject(
            ObjectType.Element, PredicateFilter(lambda e: isinstance(e, CableTray)),
            "Pick the FIRST tray (the segment on one side of the gap)"))
        tray_a = read_tray(doc.GetElement(first))
        second = self.picking(lambda: uidoc.Selection.PickObject(
            ObjectType.Element,
            PredicateFilter(lambda e: isinstance(e, CableTray) and id_value(e.Id) != tray_a["id_value"]),
            "Pick the SECOND tray (the segment on the other side of the gap)"))
        self.bridge = [tray_a, read_tray(doc.GetElement(second))]
        self.update_mode_ui()
        try:
            logic.nearest_free_ends(self.bridge[0], self.bridge[1])
            self.set_status("Tray ends picked.")
        except logic.PlanError as ex:
            self.set_status(logic.to_text(ex))

    def do_add_obstacles(self, uiapp, linked):
        uidoc = self.active_uidoc(uiapp)
        doc = uidoc.Document
        excluded = self.excluded_ids()
        if linked:
            refs = self.picking(lambda: uidoc.Selection.PickObjects(
                ObjectType.LinkedElement, "Pick obstacle(s) in a linked model, then click Finish"))
        else:
            refs = self.picking(lambda: uidoc.Selection.PickObjects(
                ObjectType.Element,
                PredicateFilter(lambda e: e.Category is not None and id_value(e.Id) not in excluded),
                "Pick the obstacle(s) to avoid, then click Finish"))
        known = set(o["key"] for o in self.obstacles)
        added = 0
        for ref in refs:
            if linked:
                link = doc.GetElement(ref.ElementId)
                link_doc = link.GetLinkDocument()
                if link_doc is None:
                    continue
                obstacle = read_obstacle(link_doc.GetElement(ref.LinkedElementId), link.GetTotalTransform(),
                                         link_doc.Title, id_value(link.Id))
            else:
                obstacle = read_obstacle(doc.GetElement(ref))
            if obstacle["key"] not in known:
                known.add(obstacle["key"])
                self.obstacles.append(obstacle)
                added += 1
        self.update_mode_ui()
        self.set_status(u"Added {} obstacle(s).".format(added))

    def do_pick_cuts(self, uiapp):
        uidoc = self.active_uidoc(uiapp)
        tray_id = self.tray["id_value"]
        only_tray = PredicateFilter(lambda e: id_value(e.Id) == tray_id)
        points = []
        for prompt in ("Pick where the detour should LEAVE the tray",
                       "Pick where the detour should REJOIN the tray"):
            ref = self.picking(lambda: uidoc.Selection.PickObject(ObjectType.PointOnElement, only_tray, prompt))
            points.append(xyz_tuple(ref.GlobalPoint))
        self.cut_points = points
        self.update_mode_ui()
        self.set_status("Cut points picked.")

    def do_show(self, uiapp):
        uidoc = self.active_uidoc(uiapp)
        ids = List[DB.ElementId]()
        for tray in ([self.tray] if self.tray else []) + list(self.bridge):
            ids.Add(tray["id"])
        for element in self.last_created:
            if element.IsValidObject:
                ids.Add(element.Id)
        if ids.Count == 0:
            raise UserError("Pick a tray first.")
        uidoc.Selection.SetElementIds(ids)
        uidoc.ShowElements(ids)
        self.set_status("Shown in Revit.")

    def fresh_trays(self, doc):
        sources = [self.tray] if self.mode == logic.MODE_DETOUR else list(self.bridge)
        trays = []
        for info in sources:
            element = doc.GetElement(info["id"]) if info else None
            if element is None or not element.IsValidObject:
                raise UserError("A picked tray no longer exists. Pick it again.")
            trays.append(element)
        if not trays:
            raise UserError("Pick the tray(s) first.")
        return trays

    def do_apply(self, uiapp, params):
        uidoc = self.active_uidoc(uiapp)
        doc = uidoc.Document
        trays = self.fresh_trays(doc)
        infos = [read_tray(t) for t in trays]
        plan = self.compute_plan(params, infos)
        if not plan["ok"]:
            raise UserError(plan["error"])
        name = u"Cable Tray Coordinator: {}".format(logic.DIRECTION_LABELS.get(plan["direction"], u"route"))
        with revit.Transaction(name, doc=doc):
            created, fittings, warnings = build_route(doc, plan, trays, params["keep_if_fitting_fails"])
        if not all(e.IsValidObject for e in created):
            raise UserError("Revit rolled the change back (see the Revit warning). Nothing was changed.")
        self.last_created = list(created) + list(fittings)
        ids = List[DB.ElementId]()
        for element in self.last_created:
            ids.Add(element.Id)
        uidoc.Selection.SetElementIds(ids)
        if self.mode == logic.MODE_DETOUR:
            self.tray = read_tray(trays[0])
            self.cut_points = []
        else:
            self.bridge = [read_tray(t) for t in trays]
        self.update_preview()
        text = u"Done: {} tray segment(s) and {} fitting(s) created (one Undo).".format(len(created), len(fittings))
        if warnings:
            text += u" Warnings: " + u"; ".join(warnings)
        self.set_status(text)

    # --- planning (pure; safe on the UI thread) ---------------------------------------------
    def compute_plan(self, params, infos=None):
        p = logic.params_in_feet(params)
        segments = [o["segments"] for o in self.obstacles]
        if self.mode == logic.MODE_DETOUR:
            tray = infos[0] if infos else self.tray
            if tray is None:
                return None
            return logic.plan_detour(tray, segments, self.cut_points, p)
        trays = infos if infos else self.bridge
        if len(trays) != 2:
            return None
        return logic.plan_bridge(trays[0], trays[1], segments, p)

    def update_mode_ui(self):
        detour = self.mode == logic.MODE_DETOUR
        was = self.initialising
        self.initialising = True
        try:
            self.rbDetour.IsChecked = detour
            self.rbBridge.IsChecked = not detour
        finally:
            self.initialising = was
        self.panelDetour.Visibility = Visibility.Visible if detour else Visibility.Collapsed
        self.panelBridge.Visibility = Visibility.Collapsed if detour else Visibility.Visible
        self.panelCuts.Visibility = self.panelDetour.Visibility
        self.lblTray.Text = self.tray["label"] if self.tray else u"No tray picked."
        if len(self.bridge) == 2:
            self.lblEnds.Text = u"A: {}\nB: {}".format(self.bridge[0]["label"], self.bridge[1]["label"])
        else:
            self.lblEnds.Text = u"No tray ends picked."
        if self.obstacles:
            self.lblObstacles.Text = u"\n".join(o["label"] for o in self.obstacles)
        else:
            self.lblObstacles.Text = u"No obstacle (optional for manual offsets)."
        self.lblCuts.Text = (u"Cut points picked - the detour starts and ends there." if self.cut_points
                             else u"Optional: pick where to cut instead of using an obstacle.")
        self.update_preview()

    def update_preview(self):
        if self.initialising:
            return
        self.plan = None
        message = None
        try:
            params = self.read_params()
            self.txtOffset.IsEnabled = not params["auto_offset"]
            self.plan = self.compute_plan(params)
        except logic.PlanError as ex:
            message = logic.to_text(ex)
        if self.plan is None and message is None:
            message = (u"Pick a tray and an obstacle (or two cut points) to see the preview."
                       if self.mode == logic.MODE_DETOUR else
                       u"Pick the two tray ends on each side of the gap to see the preview.")
        ok = bool(self.plan and self.plan["ok"])
        if self.plan is not None and not ok:
            message = self.plan["error"]
        warnings = list(self.plan["warnings"]) if self.plan else []
        self.txtSummary.Text = u"\n".join(self.plan["summary"]) if ok else u""
        self.lblMessage.Text = u"\n".join(([message] if message else []) + warnings)
        self.btnApply.IsEnabled = ok
        self.draw_preview()

    # --- preview drawing (WPF only) ----------------------------------------------------------
    @staticmethod
    def brush(a, r, g, b):
        return SolidColorBrush(Color.FromArgb(a, r, g, b))

    def add_line(self, p0, p1, brush, thickness, dashed=False):
        line = WpfLine()
        line.X1, line.Y1, line.X2, line.Y2 = p0[0], p0[1], p1[0], p1[1]
        line.Stroke = brush
        line.StrokeThickness = thickness
        line.StrokeStartLineCap = line.StrokeEndLineCap = PenLineCap.Flat
        if dashed:
            dashes = DoubleCollection()
            dashes.Add(4.0)
            dashes.Add(3.0)
            line.StrokeDashArray = dashes
        self.cnvPreview.Children.Add(line)

    def add_rect(self, rect, fill, stroke, dashed=False):
        shape = Rectangle()
        shape.Width, shape.Height = max(1.0, rect[2]), max(1.0, rect[3])
        shape.Fill = fill
        shape.Stroke = stroke
        shape.StrokeThickness = 1.0
        if dashed:
            dashes = DoubleCollection()
            dashes.Add(4.0)
            dashes.Add(3.0)
            shape.StrokeDashArray = dashes
        Canvas.SetLeft(shape, rect[0])
        Canvas.SetTop(shape, rect[1])
        self.cnvPreview.Children.Add(shape)

    def add_polyline(self, points, brush, thickness):
        line = Polyline()
        pts = PointCollection()
        for x, y in points:
            pts.Add(Point(x, y))
        line.Points = pts
        line.Stroke = brush
        line.StrokeThickness = thickness
        line.StrokeLineJoin = PenLineJoin.Round
        self.cnvPreview.Children.Add(line)

    def add_text(self, x, y, text, brush):
        label = TextBlock()
        label.Text = text
        label.Foreground = brush
        label.FontSize = 11
        Canvas.SetLeft(label, x)
        Canvas.SetTop(label, y)
        self.cnvPreview.Children.Add(label)

    def draw_preview(self):
        canvas = self.cnvPreview
        canvas.Children.Clear()
        width = canvas.ActualWidth if canvas.ActualWidth > 50 else 640.0
        height = canvas.ActualHeight if canvas.ActualHeight > 50 else 320.0
        plan = self.plan
        if not plan or plan.get("frame") is None:
            self.lblPreviewTitle.Text = u"Preview"
            return
        prim = logic.preview_primitives(plan, width, height)
        self.lblPreviewTitle.Text = (u"Preview - side view (elevation)" if prim["view"] == "elevation"
                                     else u"Preview - top view (plan)")
        if prim.get("empty"):
            return
        grey = self.brush(255, 150, 150, 150)
        for rect in prim["clearance"]:
            self.add_rect(rect, None, self.brush(255, 220, 60, 60), dashed=True)
        for rect in prim["obstacles"]:
            self.add_rect(rect, self.brush(110, 220, 60, 60), self.brush(255, 180, 30, 30))
        for p0, p1 in prim["stubs"]:
            self.add_line(p0, p1, grey, prim["thickness_px"])
        for p0, p1 in prim["ghost"]:
            self.add_line(p0, p1, grey, 1.5, dashed=True)
        if plan.get("ok") and len(prim["route"]) > 1:
            self.add_polyline(prim["route"], self.brush(140, 30, 110, 220), prim["thickness_px"])
            self.add_polyline(prim["route"], self.brush(255, 20, 60, 140), 1.5)
        for x, y_top, y_bottom in prim["cuts"]:
            self.add_line((x, y_top), (x, y_bottom), self.brush(255, 240, 140, 0), 2.0)
        for x, y, text in prim["labels"]:
            self.add_text(x, y, text, self.brush(255, 40, 40, 40))

    # --- UI event handlers (names referenced from ui.xaml) -----------------------------------
    @ui_handler
    def mode_changed(self, sender, args):
        if self.initialising:
            return
        self.mode = logic.MODE_BRIDGE if getattr(sender, "Name", "") == "rbBridge" else logic.MODE_DETOUR
        self.update_mode_ui()

    @ui_handler
    def use_selection_clicked(self, sender, args):
        self.run_in_revit("Read Revit selection", self.do_use_selection)

    @ui_handler
    def pick_tray_clicked(self, sender, args):
        self.run_in_revit("Pick tray", self.do_pick_tray)

    @ui_handler
    def pick_ends_clicked(self, sender, args):
        self.run_in_revit("Pick tray ends", self.do_pick_ends)

    @ui_handler
    def add_obstacle_clicked(self, sender, args):
        self.run_in_revit("Pick obstacles", lambda uiapp: self.do_add_obstacles(uiapp, False))

    @ui_handler
    def add_linked_obstacle_clicked(self, sender, args):
        self.run_in_revit("Pick linked obstacles", lambda uiapp: self.do_add_obstacles(uiapp, True))

    @ui_handler
    def clear_obstacles_clicked(self, sender, args):
        self.obstacles = []
        self.update_mode_ui()

    @ui_handler
    def pick_cuts_clicked(self, sender, args):
        if not self.tray:
            self.set_status("Pick the tray first.")
            return
        self.run_in_revit("Pick cut points", self.do_pick_cuts)

    @ui_handler
    def clear_cuts_clicked(self, sender, args):
        self.cut_points = []
        self.update_mode_ui()

    @ui_handler
    def preset_changed(self, sender, args):
        name = self.lstPresets.SelectedItem
        preset = next((p for p in self.presets() if p["name"] == name), None)
        if preset is None:
            self.lblPresetInfo.Text = u""
            return
        self.lblPresetInfo.Text = preset.get("description", u"")
        current = logic.DEFAULT_PARAMS
        try:
            current = self.read_params()
        except logic.PlanError:
            pass
        merged = dict(current)
        for key in ("direction", "angle", "offset_mm", "clearance_mm", "auto_offset"):
            merged[key] = preset[key]
        self.set_params(logic.normalize_params(merged))
        self.update_preview()

    @ui_handler
    def param_changed(self, sender, args):
        self.update_preview()

    @ui_handler
    def preview_resized(self, sender, args):
        self.draw_preview()

    @ui_handler
    def save_preset_clicked(self, sender, args):
        params = self.read_params()
        name = forms.ask_for_string(prompt="Name for this preset:", title="Save preset")
        if not name:
            return
        existing = [p["name"] for p in self.user_presets]
        preset, replaces = logic.make_user_preset(name, params, existing)
        if replaces and not forms.alert(u"Replace preset '{}'?".format(preset["name"]), yes=True, no=True):
            return
        self.user_presets = [p for p in self.user_presets if p["name"] != preset["name"]] + [preset]
        self.persist_settings()
        self.refresh_presets(select=preset["name"])
        self.set_status(u"Preset '{}' saved.".format(preset["name"]))

    @ui_handler
    def delete_preset_clicked(self, sender, args):
        name = self.lstPresets.SelectedItem
        if not any(p["name"] == name for p in self.user_presets):
            self.set_status("Built-in presets cannot be deleted.")
            return
        self.user_presets = [p for p in self.user_presets if p["name"] != name]
        self.persist_settings()
        self.refresh_presets()
        self.set_status(u"Preset '{}' deleted.".format(name))

    @ui_handler
    def show_clicked(self, sender, args):
        self.run_in_revit("Show in Revit", self.do_show)

    @ui_handler
    def apply_clicked(self, sender, args):
        params = self.read_params()
        if not (self.plan and self.plan["ok"]):
            self.set_status("Fix the preview message first.")
            return
        self.run_in_revit("Apply", lambda uiapp: self.do_apply(uiapp, params))

    @ui_handler
    def close_clicked(self, sender, args):
        self.Close()

    def on_closed(self, sender, args):
        global WINDOW
        self.closed = True
        self.queue.clear()
        self.persist_settings()
        try:
            self.ext_event.Dispose()
        except Exception:
            pass
        if WINDOW is self:
            WINDOW = None


def main():
    global WINDOW
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Open a Revit project before running Cable Tray Coordinator.", title="Cable Tray Coordinator")
        return
    if WINDOW is not None and not WINDOW.closed:
        try:
            WINDOW.Close()
        except Exception:
            pass
    WINDOW = CableTrayCoordinator(doc)
    try:
        # Still inside the command's API context: pre-load a tray/obstacle the user already selected.
        WINDOW.load_selection(revit.uidoc)
        WINDOW.update_mode_ui()
    except Exception as ex:
        WINDOW.set_status(logic.to_text(ex))
    WINDOW.show(modal=False)


main()
