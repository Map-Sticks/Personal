# -*- coding: utf-8 -*-
"""Cable Tray Coordinator - re-route cable trays over, under or around obstacles.

Two modes:
  * Detour: pick one tray and the obstacle (or two cut points). The tray is cut, the middle is
    lifted/dropped/jogged with the chosen preset and re-joined with fittings.
  * Bridge: pick two parallel/perpendicular horizontal tray ends (and optionally obstacles). The tool draws the
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
import time
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
# ft^3 (~0.3 cm^3): ignores touching faces and boolean noise when checking real clashes.
CLASH_VOLUME = 1e-5
TRANSACTION_PREFIX = u"Cable Tray Coordinator"
# Length of the throwaway trays used to measure fitting take-up; longer than any elbow take-up.
TAKEUP_PROBE_MM = 3000.0
# Elbow instance parameters the tool may reduce (Revit's channel/ladder bends); the measured take-up
# must follow the geometric rule within this tolerance or the size is treated as not adjustable.
BEND_RADIUS_PARAM = u"Bend Radius"
FITTING_LEG_PARAM = u"Tray Length"
FITTING_CHECK_MM = 3.0
# A straight whose end is further than this from its resized fitting's connector is pulled onto it.
FITTING_GAP_MM = 0.5
OPTIONS_HINT = (u"Regenerate tries every direction and bend angle (and grouped/individual obstacles) with your "
                u"clearance and fitting limits, test-builds the best ones and lists them here.")

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
            "width": width, "height": height, "ends": ends, "takeup_key": takeup_key(tray)}


def end_connectors(element):
    manager = connector_manager(element)
    if manager is None:
        return []
    return [c for c in manager.Connectors if c.ConnectorType == DB.ConnectorType.End]


def read_run(tray, end_point, max_straights=logic.MAX_RUN_REACH):
    """The connected run beyond one end of a tray, nearest first, for a run re-route.

    Returns {"items": [...], "stop": text or None}: two-connector fittings become
    {"kind": "fitting", ...}; straights become read_tray dicts with "kind": "tray" and "near" (their
    end facing the picked tray). The walk stops at a free end, a tee/cross or anything else.
    """
    items, stop = [], None
    seen = set([id_value(tray.Id)])
    current, conn, straights = tray, connector_near(tray, to_xyz(end_point)), 0
    while True:
        others = [o for o in (list(conn.AllRefs) if conn is not None and conn.IsConnected else [])
                  if o.Owner.Id != current.Id and o.ConnectorType == DB.ConnectorType.End]
        if not others:
            stop = u"a free end"
            break
        other = others[0]
        owner = other.Owner
        if id_value(owner.Id) in seen:
            stop = u"a loop"
            break
        seen.add(id_value(owner.Id))
        ends = end_connectors(owner)
        if isinstance(owner, CableTray):
            try:
                info = read_tray(owner)
            except UserError:
                stop = u"a curved tray"
                break
            info["kind"], info["near"] = "tray", xyz_tuple(other.Origin)
            items.append(info)
            straights += 1
            if straights >= max_straights:
                break
            current, conn = owner, max(ends, key=lambda c: c.Origin.DistanceTo(other.Origin))
            continue
        if len(ends) != 2:
            stop = u"a tee or cross ({})".format(element_name(owner))
            break
        items.append({"kind": "fitting", "id": owner.Id, "id_value": id_value(owner.Id),
                      "label": u"{} [{}]".format(element_name(owner), id_value(owner.Id))})
        # A union's two connectors can coincide: the exit is the one not attached to where we came from.
        entry_id = id_value(current.Id)
        exits = [c for c in ends if not any(id_value(r.Owner.Id) == entry_id for r in list(c.AllRefs))]
        if not exits:
            stop = u"a loop"
            break
        current, conn = owner, exits[0]
    return {"items": items, "stop": stop}


def read_detour_tray(tray):
    """read_tray plus the connected run beyond each connected end (for a run re-route)."""
    info = read_tray(tray)
    runs = {}
    free = logic.free_ends(info)
    for index, key in enumerate(("start", "end")):
        if not free[index]:
            try:
                runs[key] = read_run(tray, info[key])
            except Exception:
                logger.debug(u"Connected run could not be read:\n{}".format(traceback.format_exc()))
    info["runs"] = runs
    return info


def with_picks(infos, previous):
    """Carry the clicked points (end/tee choice) over to freshly read tray dicts."""
    for info, old in zip(infos, previous or ()):
        if old and old.get("pick_point") is not None:
            info["pick_point"] = old["pick_point"]
    return infos


def bridge_status(trays):
    """Status text after picking bridge trays: how they will connect, or why they cannot."""
    try:
        request = logic.tee_request(trays[0], trays[1])
        if request is not None:
            return u"Tee into the side of {}{}.".format(
                request["main"]["label"], u" (its free end points at that side)" if request["auto"] else u"")
        logic.bridge_ends(trays[0], trays[1])
        return u"Trays picked: end-to-end join. Click along the second tray instead to tee into its side."
    except logic.PlanError as ex:
        return logic.to_text(ex)


def takeup_key(tray):
    """Fitting take-up depends on the tray type (its routing preferences) and size."""
    width, height = tray_size(tray)
    return (id_value(tray.GetTypeId()), int(round(logic.ft_to_mm(width))), int(round(logic.ft_to_mm(height))))


def fitting_size_params(fitting):
    """(bend radius, leg) instance parameters of an elbow the tool may change, or None each."""
    found = []
    for name in (BEND_RADIUS_PARAM, FITTING_LEG_PARAM):
        try:
            param = fitting.LookupParameter(name)
            usable = (param is not None and not param.IsReadOnly and
                      param.StorageType == DB.StorageType.Double)
        except Exception:
            usable = False
        found.append(param if usable else None)
    return found[0], found[1]


def adjustable_elbow(doc, elbow, a, b, length, takeup):
    """{"bend_radius", "bend_leg"} (ft) of a probe 90-degree elbow whose size can be reduced.

    Halves the radius (and sets the shortest leg) in a sub-transaction that is rolled back, and only
    reports the sizes when the take-up then drops as logic.adjusted_takeup predicts.
    """
    radius_param, leg_param = fitting_size_params(elbow)
    if radius_param is None:
        return {}
    radius = radius_param.AsDouble()
    leg = leg_param.AsDouble() if leg_param is not None else None
    sizes = {"bend_radius": radius}
    if leg is not None:
        sizes["bend_leg"] = leg
    option = {"radius": radius / 2.0, "leg": logic.mm_to_ft(logic.MIN_FITTING_LEG_MM) if leg is not None else None,
              "standard_radius": radius, "standard_leg": leg}
    expected = logic.adjusted_takeup({90: takeup}, option)[90]
    sub = DB.SubTransaction(doc)
    sub.Start()
    try:
        radius_param.Set(option["radius"])
        if leg_param is not None:
            leg_param.Set(option["leg"])
        doc.Regenerate()
        measured = max(length - curve_length(a), length - curve_length(b), 0.0)
    except Exception:
        logger.debug(u"Elbow size could not be changed:\n{}".format(traceback.format_exc()))
        return {}
    finally:
        sub.RollBack()
    if abs(measured - expected) > logic.mm_to_ft(FITTING_CHECK_MM):
        logger.debug(u"Elbow take-up {} after resizing, expected {}: size not adjusted.".format(
            logic.fmt_mm(measured), logic.fmt_mm(expected)))
        return {}
    return sizes


def close_fitting_gaps(fitting):
    """Extend connected trays that Revit left short of a resized fitting's connectors.

    After a smaller leg/radius Revit moves only one of the straights (in sloped routes it often
    keeps the slope's length), leaving a connected-but-open gap; the left-behind end is moved onto
    the fitting connector so the straight follows.
    """
    manager = connector_manager(fitting)
    if manager is None:
        return
    for conn in manager.Connectors:
        for other in list(conn.AllRefs):
            owner = other.Owner
            if owner.Id == fitting.Id or not isinstance(owner, CableTray):
                continue
            if other.Origin.DistanceTo(conn.Origin) < logic.mm_to_ft(FITTING_GAP_MM):
                continue
            curve = owner.Location.Curve
            p0, p1 = curve.GetEndPoint(0), curve.GetEndPoint(1)
            if p0.DistanceTo(other.Origin) <= p1.DistanceTo(other.Origin):
                owner.Location.Curve = DB.Line.CreateBound(conn.Origin, p1)
            else:
                owner.Location.Curve = DB.Line.CreateBound(p0, conn.Origin)


def apply_fitting_option(doc, fittings, option, warnings):
    """Set a plan's reduced bend radius / leg on the elbows it created (tees and unions are left)."""
    if not option:
        return
    for fitting in fittings:
        radius_param, leg_param = fitting_size_params(fitting)
        if radius_param is None:
            continue
        try:
            if option.get("radius"):
                radius_param.Set(option["radius"])
            if option.get("leg") and leg_param is not None:
                leg_param.Set(option["leg"])
            doc.Regenerate()
            close_fitting_gaps(fitting)
        except Exception as ex:
            warnings.append(u"Could not resize elbow {} ({})".format(id_value(fitting.Id), ex))
    doc.Regenerate()


def measure_takeup(doc, source):
    """{bend angle: largest per-fitting take-up (ft)} for this tray type and size.

    Test-places horizontal, upward and downward elbows of every angle next to the source tray in a
    transaction that is always rolled back. Angles whose fitting cannot be placed are left out.
    """
    curve = source.Location.Curve
    flat = DB.XYZ(curve.Direction.X, curve.Direction.Y, 0.0)
    if flat.GetLength() < 1e-6:
        return {}
    d = flat.Normalize()
    z = DB.XYZ.BasisZ
    side = z.CrossProduct(d)
    length = logic.mm_to_ft(TAKEUP_PROBE_MM)
    origin = curve.GetEndPoint(0)
    result = {}
    transaction = DB.Transaction(doc, TRANSACTION_PREFIX + u": measure fittings")
    transaction.Start()
    try:
        index = 0
        for angle in logic.ANGLES:
            r = math.radians(angle)
            for kind in ("side", "up", "down"):
                index += 1
                if kind == "side":
                    d2, normal = d * math.cos(r) + side * math.sin(r), None
                else:
                    s = 1.0 if kind == "up" else -1.0
                    d2 = d * math.cos(r) + z * (s * math.sin(r))
                    normal = z * math.cos(r) - d * (s * math.sin(r))
                base = origin + side * (index * 2.0 * length)
                joint = base + d * length
                a = create_segment(doc, source, {"start": xyz_tuple(base), "end": xyz_tuple(joint), "normal": None})
                b = create_segment(doc, source, {"start": xyz_tuple(joint), "end": xyz_tuple(joint + d2 * length),
                                                 "normal": xyz_tuple(normal) if normal is not None else None})
                doc.Regenerate()
                try:
                    elbow = doc.Create.NewElbowFitting(connector_near(a, joint), connector_near(b, joint))
                    doc.Regenerate()
                except Exception:
                    continue
                takeup = max(length - curve_length(a), length - curve_length(b), 0.0)
                result[angle] = max(result.get(angle, 0.0), takeup)
                if kind != "side":
                    result[("v", angle)] = max(result.get(("v", angle), 0.0), takeup)
                if angle == 90 and kind == "side":
                    result.update(adjustable_elbow(doc, elbow, a, b, length, takeup))
        # Tee into the side: two main halves meeting at the joint plus a branch square to them.
        index += 1
        base = origin + side * (index * 2.0 * length)
        joint = base + d * length
        try:
            a = create_segment(doc, source, {"start": xyz_tuple(base), "end": xyz_tuple(joint), "normal": None})
            b = create_segment(doc, source, {"start": xyz_tuple(joint), "end": xyz_tuple(joint + d * length),
                                             "normal": None})
            c = create_segment(doc, source, {"start": xyz_tuple(joint + side * length), "end": xyz_tuple(joint),
                                             "normal": None})
            doc.Regenerate()
            doc.Create.NewTeeFitting(connector_near(a, joint), connector_near(b, joint), connector_near(c, joint))
            doc.Regenerate()
            result["tee_main"] = max(length - curve_length(a), length - curve_length(b), 0.0)
            result["tee_branch"] = max(length - curve_length(c), 0.0)
        except Exception:
            logger.debug(u"Tee take-up could not be measured:\n{}".format(traceback.format_exc()))
        # Union between two straights in line (bridges meeting a tray end-on use one).
        index += 1
        base = origin + side * (index * 2.0 * length)
        joint = base + d * length
        try:
            a = create_segment(doc, source, {"start": xyz_tuple(base), "end": xyz_tuple(joint), "normal": None})
            b = create_segment(doc, source, {"start": xyz_tuple(joint), "end": xyz_tuple(joint + d * length),
                                             "normal": None})
            doc.Regenerate()
            doc.Create.NewUnionFitting(connector_near(a, joint), connector_near(b, joint))
            doc.Regenerate()
            result["union"] = max(length - curve_length(a), length - curve_length(b), 0.0)
        except Exception:
            logger.debug(u"Union take-up could not be measured:\n{}".format(traceback.format_exc()))
    finally:
        transaction.RollBack()
    return result


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
    if len(segments) >= MAX_OBSTACLE_SEGMENTS:
        logger.warning("Obstacle geometry limit reached; using its complete bounding-box envelope.")
        segments = bbox_segments(element, transform)
    if not segments:
        segments = bbox_segments(element, transform)
    if not segments:
        raise UserError(u"Could not read the geometry of element {}.".format(id_value(element.Id)))
    category = element.Category.Name if element.Category is not None else u"Element"
    label = u"{}: {} [{}]".format(category, element_name(element), id_value(element.Id))
    if link_name:
        label += u" (link: {})".format(link_name)
    return {"key": (link_id, id_value(element.Id)), "label": label, "segments": segments,
            "element_id": element.Id, "link_id": DB.ElementId(link_id) if link_id is not None else None}


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


def curve_length(element):
    curve = element.Location.Curve
    return curve.GetEndPoint(0).DistanceTo(curve.GetEndPoint(1))


def fittings_on(element, fitting_ids):
    """How many of the given fittings are connected to this tray."""
    manager = connector_manager(element)
    if manager is None:
        return 0
    count = 0
    for conn in manager.Connectors:
        if conn.IsConnected and any(id_value(o.Owner.Id) in fitting_ids for o in conn.AllRefs):
            count += 1
    return count


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


def move_free_end(tray, original, point):
    """Move the (unconnected) end of ``tray`` that was at ``original`` to ``point``."""
    if logic.distance(original, point) < 1e-9:
        return
    curve = tray.Location.Curve
    p0, p1 = curve.GetEndPoint(0), curve.GetEndPoint(1)
    old, new = to_xyz(original), to_xyz(point)
    if p0.DistanceTo(old) <= p1.DistanceTo(old):
        tray.Location.Curve = DB.Line.CreateBound(new, p1)
    else:
        tray.Location.Curve = DB.Line.CreateBound(p0, new)


def remove_run(doc, source, cut_tray, reroute):
    """Delete the connected run between the picked tray and the re-route's cut straight."""
    detach_end(doc, source, to_xyz(reroute["tray_end"]))
    detach_end(doc, cut_tray, to_xyz(reroute["cut_near"]))
    ids = List[DB.ElementId]()
    for item in reroute["removed"]:
        element = doc.GetElement(item["id"])
        if element is None or not element.IsValidObject:
            raise UserError(u"The connected run changed ({} is missing). Pick the tray again.".format(
                item.get("label", u"an element")))
        ids.Add(element.Id)
    if ids.Count:
        deleted = doc.Delete(ids)
        if any(id_value(eid) in (id_value(source.Id), id_value(cut_tray.Id)) for eid in deleted):
            raise UserError("Removing the connected run would delete a tray that is kept. Nothing was changed.")


def split_for_tee(doc, main, point, warnings):
    """Cut the main tray at the tee point; return the new far half (re-connected at the far end)."""
    curve = main.Location.Curve
    p0, p1 = curve.GetEndPoint(0), curve.GetEndPoint(1)
    attached = detach_end(doc, main, p1)
    main.Location.Curve = DB.Line.CreateBound(p0, point)
    second = create_segment(doc, main, {"start": xyz_tuple(point), "end": xyz_tuple(p1), "normal": None})
    doc.Regenerate()
    reattach(doc, second, p1, attached, warnings)
    return second


def build_route(doc, plan, trays, keep_if_fails, pre_fit_lengths=None):
    """Create the planned route. Must run inside an open transaction.

    trays: [picked tray] (detour), [tray A, tray B] (bridge), or [picked tray, cut straight] (run
    re-route). pre_fit_lengths (dict, optional) receives {element id: length before fittings} so the
    caller can measure fitting take-up.
    """
    segs = plan["segments"]
    warnings = []
    tee = plan.get("tee")
    reroute = plan.get("run_reroute")
    starts = [to_xyz(s["start"]) for s in segs]
    last = to_xyz(segs[-1]["end"])
    extra = []
    main = second = None
    if reroute:
        source, cut_tray = trays[0], trays[1]
        remove_run(doc, source, cut_tray, reroute)
        move_free_end(source, reroute["tray_end"], segs[0]["start"])
        move_free_end(cut_tray, reroute["cut_near"], segs[-1]["end"])
        new_trays = [create_segment(doc, source, s) for s in segs]
        chain, joints = [source] + new_trays + [cut_tray], starts + [last]
    elif plan["mode"] == logic.MODE_DETOUR:
        tray = trays[0]
        curve = tray.Location.Curve
        p_start, p_end = curve.GetEndPoint(0), curve.GetEndPoint(1)
        open_start, open_end = plan.get("open_ends") or (False, False)
        if open_start and open_end:
            # The whole (free) tray moves sideways: no new pieces.
            tray.Location.Curve = DB.Line.CreateBound(starts[0], last)
            new_trays, chain, joints = [], [tray], []
        elif open_end:
            tray.Location.Curve = DB.Line.CreateBound(p_start, starts[0])
            new_trays = [create_segment(doc, tray, s) for s in segs]
            chain, joints = [tray] + new_trays, starts
        elif open_start:
            tray.Location.Curve = DB.Line.CreateBound(last, p_end)
            new_trays = [create_segment(doc, tray, s) for s in segs]
            chain, joints = new_trays + [tray], starts[1:] + [last]
        else:
            attached = detach_end(doc, tray, p_end)
            tray.Location.Curve = DB.Line.CreateBound(p_start, starts[0])
            new_trays = [create_segment(doc, tray, s) for s in segs]
            far = create_segment(doc, tray, {"start": segs[-1]["end"], "end": xyz_tuple(p_end), "normal": None})
            doc.Regenerate()
            reattach(doc, far, p_end, attached, warnings)
            new_trays.append(far)
            chain, joints = [tray] + new_trays, starts + [last]
    elif tee:
        branch, main = (trays[1], trays[0]) if tee.get("swapped") else (trays[0], trays[1])
        move_free_end(branch, plan["original_ends"][0], segs[0]["start"])
        new_trays = [create_segment(doc, branch, s) for s in segs]
        second = split_for_tee(doc, main, last, warnings)
        extra.append(second)
        chain, joints = [branch] + new_trays, starts
    else:
        # A spatial bridge may pull a free source end back to make room for its fittings.
        for tray, original, point in zip(trays, plan.get("original_ends") or (),
                                         (segs[0]["start"], segs[-1]["end"])):
            move_free_end(tray, original, point)
        new_trays = [create_segment(doc, trays[0], s) for s in segs]
        chain, joints = [trays[0]] + new_trays + [trays[1]], starts + [last]
    doc.Regenerate()
    if pre_fit_lengths is not None:
        for element in chain + extra + ([main] if main is not None else []):
            pre_fit_lengths[id_value(element.Id)] = curve_length(element)

    def failed(message):
        if not keep_if_fails:
            raise UserError(message + u". Nothing was changed. Check the cable tray type has bends "
                            u"assigned, try another angle, or tick 'Keep route if a fitting fails'.")
        warnings.append(message)

    fittings = []
    option = plan.get("fitting_option")
    for first, other, joint in zip(chain[:-1], chain[1:], joints):
        try:
            fitting = join(doc, first, other, joint)
            if fitting is not None:
                fittings.append(fitting)
                # Shrink each elbow before the next is placed: Revit inserts standard-size elbows,
                # and two of them may not fit on a straight planned for the smaller ones.
                apply_fitting_option(doc, [fitting], option, warnings)
        except Exception as ex:
            failed(u"No fitting between trays {} and {} ({})".format(id_value(first.Id), id_value(other.Id), ex))
    if tee:
        connectors = [connector_near(e, last) for e in (main, second, chain[-1])]
        try:
            if any(c is None for c in connectors):
                raise UserError("the tray ends do not meet at the tee point")
            fittings.append(doc.Create.NewTeeFitting(connectors[0], connectors[1], connectors[2]))
        except Exception as ex:
            failed(u"No tee on tray {} ({}): check its type has a tee assigned in its routing preferences".format(
                id_value(main.Id), ex))
    return new_trays + extra, fittings, warnings


def end_connections(tray, point):
    conn = connector_near(tray, point)
    return [(other.Owner.Id, other.Origin) for other in list(conn.AllRefs)
            if other.Owner.Id != tray.Id and other.ConnectorType != DB.ConnectorType.Logical] if conn else []


def element_solids(element, transform=None):
    """World-coordinate solids of an element (``transform`` maps linked geometry into the host)."""
    options = DB.Options()
    options.DetailLevel = DB.ViewDetailLevel.Fine
    solids = []

    def walk(geometry):
        for obj in geometry:
            if isinstance(obj, DB.Solid):
                if abs(obj.Volume) > CLASH_VOLUME:
                    solids.append(DB.SolidUtils.CreateTransformed(obj, transform) if transform is not None else obj)
            elif isinstance(obj, DB.GeometryInstance):
                walk(obj.GetInstanceGeometry())

    geometry = element.get_Geometry(options)
    if geometry is not None:
        walk(geometry)
    return solids


def solid_bounds(solid):
    box = solid.GetBoundingBox()
    points = [box.Transform.OfPoint(DB.XYZ(x, y, z)) for x in (box.Min.X, box.Max.X)
              for y in (box.Min.Y, box.Max.Y) for z in (box.Min.Z, box.Max.Z)]
    return [(min(getattr(p, a) for p in points), max(getattr(p, a) for p in points)) for a in ("X", "Y", "Z")]


def route_clashes(elements, obstacle_sources):
    """Labels of selected obstacles whose solids really intersect the new trays/fittings.

    Works in any grid rotation (no axis-aligned envelopes). Returns (labels, unchecked pair count).
    """
    obstacles = [(label, [(s, solid_bounds(s)) for s in element_solids(element, transform)])
                 for label, element, transform in obstacle_sources]
    clashes, unchecked = [], 0
    for element in elements:
        for solid in element_solids(element):
            bounds = solid_bounds(solid)
            for label, solids in obstacles:
                if label in clashes:
                    continue
                for other, other_bounds in solids:
                    if any(bounds[i][1] < other_bounds[i][0] or other_bounds[i][1] < bounds[i][0]
                           for i in range(3)):
                        continue
                    try:
                        common = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
                            solid, other, DB.BooleanOperationsType.Intersect)
                    except Exception:
                        unchecked += 1
                        continue
                    if common is not None and abs(common.Volume) > CLASH_VOLUME:
                        clashes.append(label)
                        break
    return clashes, unchecked


def tray_and_fitting_ids(doc, created):
    """Ids of every element sharing a category with the route's own trays/fittings."""
    ids, seen = set(), set()
    for element in created:
        category = element.Category
        if category is None or id_value(category.Id) in seen:
            continue
        seen.add(id_value(category.Id))
        collector = DB.FilteredElementCollector(doc).OfCategoryId(category.Id)
        ids.update(id_value(eid) for eid in collector.WhereElementIsNotElementType().ToElementIds())
    return ids


def revert_route(doc, record):
    """Inside a transaction: disconnect the trial, delete only its elements, restore the source."""
    created = record["created"]
    if not all(e.IsValidObject for e in created + record["sources"]):
        raise UserError("The last route no longer exists unchanged. Use Revit Undo instead.")
    allowed = set(id_value(e.Id) for e in created)
    # Revit may also drop internal bookkeeping (e.g. run elements); only real user elements are protected.
    protected = tray_and_fitting_ids(doc, created) | set(id_value(e.Id) for e in record["sources"])
    protected.update(id_value(owner_id) for ends in record["connections"] for end in ends for owner_id, _ in end)
    protected -= allowed
    for element in created:
        manager = connector_manager(element)
        if manager:
            for conn in manager.Connectors:
                if conn.ConnectorType == DB.ConnectorType.Logical:
                    continue
                for other in list(conn.AllRefs):
                    if other.Owner.Id != element.Id and other.ConnectorType != DB.ConnectorType.Logical:
                        conn.DisconnectFrom(other)
    ids = List[DB.ElementId]()
    for element in created:
        ids.Add(element.Id)
    deleted = doc.Delete(ids)
    if any(id_value(eid) in protected for eid in deleted):
        raise UserError("Revert would delete elements outside the last route. Nothing was changed.")
    for source in record["sources"]:
        curve = source.Location.Curve
        detach_end(doc, source, curve.GetEndPoint(0))
        detach_end(doc, source, curve.GetEndPoint(1))
    for source, info in zip(record["sources"], record["infos"]):
        source.Location.Curve = DB.Line.CreateBound(to_xyz(info["start"]), to_xyz(info["end"]))
    doc.Regenerate()
    warnings = []
    for source, info, connections in zip(record["sources"], record["infos"], record["connections"]):
        for key, attached in zip(("start", "end"), connections):
            reattach(doc, source, to_xyz(info[key]), attached, warnings)
    if warnings:
        raise UserError(u"Revert could not restore connections: " + u"; ".join(warnings))
    doc.Regenerate()


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
        self.short_curve_tolerance = doc.Application.ShortCurveTolerance
        self.mode = logic.MODE_DETOUR
        self.tray = None
        self.bridge = []
        self.obstacles = []
        self.cut_points = []
        self.plan = None
        self.last_created = []
        self.revert_record = None
        self.pending_undo = None
        self.change_application = None
        self.own_change = False
        self.takeup = {}
        self.options = []
        self.options_key = None

        settings = load_settings()
        self.user_presets = logic.normalize_user_presets(settings.get("user_presets"))
        for name in logic.DIRECTIONS:
            self.cmbDirection.Items.Add(name)
        for label in ANGLE_LABELS:
            self.cmbAngle.Items.Add(label)
        for radius in logic.BEND_RADII_MM:
            self.cmbMinRadius.Items.Add(u"{:g}".format(radius))
        self.set_params(logic.normalize_params(settings.get("params")))
        self.refresh_presets()
        self.Closed += self.on_closed
        self.lblOptions.Text = OPTIONS_HINT
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
            self.cmbMinRadius.Text = u"{:g}".format(params["min_bend_radius_mm"])
            self.chkShortLegs.IsChecked = params["shorten_fitting_legs"]
            self.chkShallowerBends.IsChecked = params["shallower_bends"]
            self.chkAutoOffset.IsChecked = params["auto_offset"]
            self.chkKeepIfFittingFails.IsChecked = params["keep_if_fitting_fails"]
            self.chkIndividualObstacles.IsChecked = params["individual_obstacles"]
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
            "individual_obstacles": bool(self.chkIndividualObstacles.IsChecked),
            "shorten_fitting_legs": bool(self.chkShortLegs.IsChecked),
            "shallower_bends": bool(self.chkShallowerBends.IsChecked),
        }
        for key, box in (("offset_mm", self.txtOffset), ("clearance_mm", self.txtClearance),
                         ("round_mm", self.txtRound), ("min_straight_mm", self.txtMinStraight)):
            params[key] = logic.parse_mm(box.Text, key)
        # An editable combo box updates Text only after SelectionChanged has fired.
        radius = self.cmbMinRadius.SelectedItem
        params["min_bend_radius_mm"] = logic.parse_mm(radius if radius is not None else self.cmbMinRadius.Text,
                                                      "min_bend_radius_mm")
        return params

    def persist_settings(self):
        data = {"user_presets": self.user_presets}
        try:
            data["params"] = self.read_params()
            if self.active_relaxed(data["params"]):
                data["params"] = logic.keep_own_limits(data["params"], self.own_limits)
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

    def short_straight_error(self, params, short, angles, infos, learn=True):
        """UserError for straights the fittings made too short.

        The measured per-fitting take-up is learned for this tray type/size and the plan's bend
        angles, so the refreshed preview reserves enough and the next Apply fits. Min. straight is
        not changed. Test builds (learn=False) leave the take-up alone so their options stay valid.
        """
        planned = [s for s in short if s[3]]
        existing = [s for s in short if not s[3]]
        parts = []
        learned = False
        if planned:
            per_fitting = max(s[2] for s in planned)
            for info in infos if learn else ():
                table = self.takeup.setdefault(info.get("takeup_key"), {})
                for angle in angles:
                    if per_fitting > table.get(angle, 0.0) + logic.EPS:
                        table[angle] = per_fitting
                        learned = True
            advice = (u"The preview now allows for this measured take-up: check it and Apply again." if learned
                      else u"Choose another bend angle or move the route.")
            parts.append(u"Each {} fitting takes up to {} off a straight, leaving {} (less than Min. "
                         u"straight {:g} mm). {}".format(u"/".join(u"{}{}".format(a, logic.DEG) for a in angles),
                                                         logic.fmt_mm(per_fitting),
                                                         logic.fmt_mm(min(s[1] for s in planned)),
                                                         params["min_straight_mm"], advice))
        for element, length, takeup, _ in existing:
            parts.append(u"Existing tray {} would be left {} after its fitting (take-up {}): lengthen it, "
                         u"or lower Min. straight to {:g} mm or less if that is acceptable.".format(
                             id_value(element.Id), logic.fmt_mm(length), logic.fmt_mm(takeup),
                             math.floor(logic.ft_to_mm(length))))
        error = UserError(u"Nothing was changed. " + u" ".join(parts))
        error.refresh_preview = learned
        return error

    def on_action_error(self, label, ex):
        if label == "Regenerate options":
            self.lblOptions.Text = u"Regenerate stopped: see the message at the bottom of the window."
        if isinstance(ex, (UserError, logic.PlanError)):
            self.set_status(logic.to_text(ex))
            if getattr(ex, "refresh_preview", False):
                self.update_preview()
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
            self.tray, self.bridge, self.cut_points = read_detour_tray(trays[0]), [], []
            self.mode = logic.MODE_DETOUR
        elif len(trays) == 2:
            self.bridge, self.tray = [read_tray(trays[0]), read_tray(trays[1])], None
            self.mode = logic.MODE_BRIDGE
        self.ensure_takeup(doc, trays)
        if others:
            self.obstacles = [read_obstacle(e) for e in others]
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
        self.tray = read_detour_tray(uidoc.Document.GetElement(ref))
        self.ensure_takeup(uidoc.Document, [uidoc.Document.GetElement(ref)])
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
            "Pick the SECOND tray: click near the end to join, or along its side to tee into it"))
        tray_b = read_tray(doc.GetElement(second))
        # Where each tray was clicked decides which end connects, or a tee into the side.
        for info, ref in ((tray_a, first), (tray_b, second)):
            if ref.GlobalPoint is not None:
                info["pick_point"] = xyz_tuple(ref.GlobalPoint)
        self.bridge = [tray_a, tray_b]
        self.ensure_takeup(doc, [doc.GetElement(first), doc.GetElement(second)])
        self.update_mode_ui()
        self.set_status(bridge_status(self.bridge))

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
                    raise UserError("A selected obstacle link is unloaded. Load it and select obstacles again.")
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

    def ensure_takeup(self, doc, trays):
        """Measure fitting take-up once per tray type/size (API context only; always rolled back)."""
        for tray in trays:
            try:
                key = takeup_key(tray)
            except Exception:
                continue
            if key in self.takeup:
                continue
            self.own_change = True
            try:
                self.takeup[key] = measure_takeup(doc, tray)
            except Exception:
                logger.debug(u"Fitting take-up could not be measured:\n{}".format(traceback.format_exc()))
                self.takeup[key] = {}
            finally:
                self.own_change = False

    def takeup_for(self, infos):
        """{angle: per-fitting take-up (ft)}: the larger value of the trays involved."""
        merged = {}
        for info in infos:
            for angle, value in self.takeup.get(info.get("takeup_key"), {}).items():
                merged[angle] = max(merged.get(angle, 0.0), value)
        return merged

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

    def obstacle_source(self, doc, info):
        """(element, link transform or None, link instance or None) for a cached obstacle."""
        link = doc.GetElement(info["link_id"]) if info.get("link_id") is not None else None
        obstacle_doc = link.GetLinkDocument() if link is not None else doc
        if info.get("link_id") is not None and (link is None or obstacle_doc is None):
            raise UserError("A selected obstacle link is missing or unloaded. Select obstacles again.")
        element = obstacle_doc.GetElement(info["element_id"])
        if element is None:
            raise UserError("A selected obstacle no longer exists. Select obstacles again.")
        return element, (link.GetTotalTransform() if link else None), link

    def refresh_obstacles(self, doc):
        refreshed = []
        for info in self.obstacles:
            element, transform, link = self.obstacle_source(doc, info)
            refreshed.append(read_obstacle(element, transform, link.GetLinkDocument().Title if link else None,
                                           id_value(link.Id) if link else None))
        self.obstacles = refreshed

    def on_document_changed(self, sender, args):
        if self.closed or self.own_change or not self.doc.IsValidObject or not self.doc.Equals(args.GetDocument()):
            return
        operation = str(args.Operation)
        if operation.endswith("RolledBack"):
            return
        if getattr(self, "pending_undo", None) is not None:
            record, self.pending_undo = self.pending_undo, None
            if operation.endswith("TransactionUndone"):
                try:
                    self.restore_after_revert(args.GetDocument(), record)
                except Exception:
                    logger.debug(traceback.format_exc())
                    self.set_status("Revit Undo ran, but the trays could not be re-read. Pick them again.")
                return
        names = [logic.to_text(n) for n in args.GetTransactionNames()]
        if operation.endswith("TransactionCommitted") and names and \
                all(n.startswith(TRANSACTION_PREFIX) for n in names):
            return
        self.revert_record = None
        self.btnRevert.IsEnabled = False
        self.set_status("Model changed. Last-route Revert disabled for safety; use Revit Undo if needed.")

    def restore_after_revert(self, doc, record):
        """Window state after a revert: the same trays, picks, obstacles and cut points as before Apply."""
        trays = [doc.GetElement(info["id"]) for info in record["infos"]]
        if any(t is None or not t.IsValidObject for t in trays):
            raise UserError("A reverted tray could not be found.")
        self.mode = record["mode"]
        self.tray = read_detour_tray(trays[0]) if self.mode == logic.MODE_DETOUR else None
        self.bridge = with_picks([read_tray(t) for t in trays], record["infos"]) \
            if self.mode == logic.MODE_BRIDGE else []
        self.cut_points = record["cut_points"]
        self.obstacles = record["obstacles"]
        self.last_created = []
        self.revert_record = None
        self.btnRevert.IsEnabled = False
        self.update_mode_ui()
        self.set_options_enabled(True)
        self.update_preview()
        self.set_status("Last route reverted. Same trays, obstacles and cut points retained; adjust and Apply again.")

    def do_revert(self, uiapp):
        uidoc = self.active_uidoc(uiapp)
        record = self.revert_record
        if record is None:
            raise UserError("No unchanged last route to revert. Use Revit Undo for earlier work.")
        if not all(e.IsValidObject for e in record["created"]):
            raise UserError("The last route no longer exists unchanged. Use Revit Undo instead.")
        if record.get("undo"):
            # A run re-route deleted part of the run; Revit's own Undo restores it exactly.
            command = UI.RevitCommandId.LookupPostableCommandId(UI.PostableCommand.Undo)
            if command is None or not uiapp.CanPostCommand(command):
                raise UserError("Revit cannot undo right now. Use Revit Undo (Ctrl+Z) instead.")
            self.pending_undo = record
            uiapp.PostCommand(command)
            self.set_status("Reverting the re-routed run with Revit Undo...")
            return
        created_ids = [e.Id for e in record["created"]]
        self.own_change = True
        try:
            with revit.Transaction("Cable Tray Coordinator: Revert last route", doc=uidoc.Document):
                revert_route(uidoc.Document, record)
        finally:
            self.own_change = False
        if any(uidoc.Document.GetElement(eid) is not None for eid in created_ids):
            raise UserError("Revit rolled back Revert. The last route is still in place.")
        self.restore_after_revert(uidoc.Document, record)

    def do_apply(self, uiapp, params):
        uidoc = self.active_uidoc(uiapp)
        doc = uidoc.Document
        trays = self.fresh_trays(doc)
        self.refresh_obstacles(doc)
        self.ensure_takeup(doc, trays)
        if self.mode == logic.MODE_DETOUR:
            infos = [read_detour_tray(trays[0])]
        else:
            infos = with_picks([read_tray(t) for t in trays], self.bridge)
        plan = self.compute_plan(params, infos)
        if not plan or not plan["ok"]:
            raise UserError(plan["error"] if plan else "Pick the tray(s) first.")
        reroute = plan.get("run_reroute")
        if reroute:
            cut_tray = doc.GetElement(reroute["cut_tray"]["id"])
            if cut_tray is None or not cut_tray.IsValidObject:
                raise UserError("The connected run changed. Pick the tray again.")
        record = {"mode": self.mode, "sources": trays, "infos": infos, "undo": bool(reroute),
                  "cut_points": list(self.cut_points), "obstacles": list(self.obstacles),
                  "connections": [[end_connections(t, to_xyz(info[k])) for k in ("start", "end")]
                                  for t, info in zip(trays, infos)]}
        if self.change_application is None:
            self.change_application = uiapp.Application
            self.change_application.DocumentChanged += self.on_document_changed
        if reroute:
            kind = u"run re-route"
        elif plan.get("tee"):
            kind = u"tee"
        elif plan.get("spatial_bridge"):
            kind = u"spatial bridge"
        else:
            kind = logic.DIRECTION_LABELS.get(plan["direction"], u"route")
        name = u"Cable Tray Coordinator: {}".format(kind)
        self.own_change = True
        try:
            with revit.Transaction(name, doc=doc):
                created, fittings, warnings = self.checked_build(doc, plan, params, trays, infos)
        finally:
            self.own_change = False
        if not all(e.IsValidObject for e in list(created) + list(fittings)):
            raise UserError("Revit rolled the change back (see the Revit warning). Nothing was changed.")
        self.last_created = list(created) + list(fittings)
        record["created"] = self.last_created
        self.revert_record = record
        self.btnRevert.IsEnabled = True
        self.persist_settings()
        ids = List[DB.ElementId]()
        for element in self.last_created:
            ids.Add(element.Id)
        uidoc.Selection.SetElementIds(ids)
        if self.mode == logic.MODE_DETOUR:
            self.tray = read_detour_tray(trays[0])
            self.cut_points = []
        else:
            self.bridge = with_picks([read_tray(t) for t in trays], self.bridge)
        self.set_options_enabled(False)
        self.update_preview()
        text = u"Done: {} tray segment(s) and {} fitting(s) created (one Undo).".format(len(created), len(fittings))
        if warnings:
            text += u" Warnings: " + u"; ".join(warnings)
        self.set_status(text)

    def checked_build(self, doc, plan, params, trays, infos, learn=True):
        """Build the plan inside the open transaction and check it; raises UserError if a straight ends up
        short or the built route clashes. Returns (created, fittings, warnings)."""
        reroute = plan.get("run_reroute")
        build_trays = list(trays)
        if reroute:
            cut_tray = doc.GetElement(reroute["cut_tray"]["id"])
            if cut_tray is None or not cut_tray.IsValidObject:
                raise UserError("The connected run changed. Pick the tray again.")
            build_trays.append(cut_tray)
        lengths_before = [curve_length(t) for t in build_trays]
        pre_fit = {}
        created, fittings, warnings = build_route(doc, plan, build_trays, params["keep_if_fitting_fails"], pre_fit)
        doc.Regenerate()
        minimum = max(logic.params_in_feet(params)["min_straight"],
                      getattr(self, "short_curve_tolerance", 0.0) * 1.01)
        # Existing trays that were already short are not this route's fault.
        checked = [(e, None) for e in created] + list(zip(build_trays, lengths_before))
        short = []
        fitting_ids = set(id_value(f.Id) for f in fittings)
        for element, before in checked:
            length = curve_length(element)
            if length < minimum - logic.EPS and (before is None or length < before - logic.EPS):
                takeup = max(pre_fit.get(id_value(element.Id), length) - length, 0.0)
                # Bridge source trays keep their length whatever the plan reserves.
                planned = before is None or self.mode == logic.MODE_DETOUR
                short.append((element, length, takeup / max(1, fittings_on(element, fitting_ids)), planned))
        if short:
            raise self.short_straight_error(params, short, plan.get("bend_angles") or [plan["angle"]], infos,
                                            learn)
        if self.obstacles:
            # A shifted free end is the picked tray itself, so check it too.
            moved = list(trays) if any(plan.get("open_ends") or ()) else []
            clashes, unchecked = route_clashes(list(created) + list(fittings) + moved,
                                               [(o["label"],) + self.obstacle_source(doc, o)[:2]
                                                for o in self.obstacles])
            if clashes and not any("will still clash" in w for w in plan["warnings"]):
                raise UserError(u"The built route clashes with: {}. Nothing was changed. Increase "
                                u"clearance or choose another direction.".format(u"; ".join(clashes)))
            if unchecked:
                warnings.append(u"{} obstacle solid check(s) could not be computed; check those "
                                u"clashes visually".format(unchecked))
        return created, fittings, warnings

    # --- Regenerate: ranked route options -----------------------------------------------------
    def selection_key(self):
        """What the options were generated for; a different pick invalidates them."""
        trays = [self.tray] if self.mode == logic.MODE_DETOUR else list(self.bridge)
        return (self.mode, tuple(t["id_value"] for t in trays if t),
                tuple(sorted(o["key"] for o in self.obstacles)),
                tuple(tuple(round(c, 4) for c in p) for p in self.cut_points or ()))

    def clear_options(self, note=None):
        self.options = []
        self.options_key = None
        was = self.initialising
        self.initialising = True
        try:
            self.lstOptions.Items.Clear()
            self.lstOptions.IsEnabled = True
        finally:
            self.initialising = was
        self.lblOptions.Text = note or OPTIONS_HINT

    def set_options_enabled(self, enabled):
        if not getattr(self, "options", None):
            return
        self.lstOptions.IsEnabled = enabled
        self.lblOptions.Text = (u"Pick an option to preview it, then Apply." if enabled else
                                u"Route applied. 'Revert last route' to try another option, or Regenerate.")

    def trial_build(self, doc, candidate, trays, infos):
        """None if the option builds cleanly in a transaction that is always rolled back, else the reason."""
        transaction = DB.Transaction(doc, TRANSACTION_PREFIX + u": test option")
        self.own_change = True
        transaction.Start()
        try:
            self.checked_build(doc, candidate["plan"], candidate["params"], trays, infos, learn=False)
            return None
        except (UserError, logic.PlanError) as ex:
            return logic.to_text(ex)
        except Exception as ex:
            logger.debug(u"Test build failed:\n{}".format(traceback.format_exc()))
            return u"Revit could not build it ({})".format(ex)
        finally:
            transaction.RollBack()
            self.own_change = False

    def do_regenerate(self, uiapp, params):
        doc = self.active_uidoc(uiapp).Document
        self.own_limits = logic.own_limits(params)
        # Re-read everything from the model so the options match what is there now.
        trays = self.fresh_trays(doc)
        self.refresh_obstacles(doc)
        self.ensure_takeup(doc, trays)
        if self.mode == logic.MODE_DETOUR:
            self.tray = read_detour_tray(trays[0])
            infos = [self.tray]
        else:
            self.bridge = with_picks([read_tray(t) for t in trays], self.bridge)
            infos = self.bridge
        self.update_mode_ui()

        def plan_fn(variant):
            try:
                return self.compute_plan(variant, infos)
            except logic.PlanError:
                raise
            except Exception:
                logger.debug(u"Option planning failed:\n{}".format(traceback.format_exc()))
                return None

        def verify(candidate):
            return self.trial_build(doc, candidate, trays, infos)

        started = time.time()
        variants = logic.option_variants(self.mode, params, len(self.obstacles), bool(self.cut_points))
        candidates, stats = logic.evaluate_options(plan_fn, variants, time.time)
        options, rejected = logic.choose_options(candidates, verify)
        relaxed = u""
        if not options:
            more, relaxed = logic.relaxed_variants(variants, params)
            if more:
                budget = max(5.0, logic.OPTION_BUDGET_S - (time.time() - started))
                extra, extra_stats = logic.evaluate_options(plan_fn, more, time.time, budget, relaxed)
                options, extra_rejected = logic.choose_options(extra, verify)
                rejected += extra_rejected
                stats["stopped"] = stats["stopped"] or extra_stats["stopped"]
                stats["error"] = stats["error"] or extra_stats["error"]
        self.clear_options()
        self.options = options
        self.options_key = self.selection_key()
        was = self.initialising
        self.initialising = True
        try:
            for option in options:
                self.lstOptions.Items.Add(logic.option_list_text(option))
        finally:
            self.initialising = was
        notes = []
        if stats["stopped"]:
            notes.append(u"search stopped after {:.0f} s".format(logic.OPTION_BUDGET_S))
        if rejected:
            notes.append(u"{} route(s) dropped after a test build ({})".format(
                len(rejected), rejected[0]["rejected"][:160]))
        if not options:
            self.lblOptions.Text = u"No route found{}. {}".format(
                u" even with " + relaxed if relaxed else u"",
                u"; ".join(notes + ([stats["error"]] if stats["error"] else [])))
            self.set_status(u"Regenerate found no route; the note above the option list says why.")
            return
        head = (u"Nothing fits your settings. These RELAXED options go below your limits ({}); picking one "
                u"loads those values and Apply asks to confirm. ".format(relaxed) if relaxed else u"")
        self.lblOptions.Text = head + u"{} option(s), each test-built in Revit (nothing changed){}. Pick one to " \
            u"preview it, then Apply.".format(len(options), u"; " + u"; ".join(notes) if notes else u"")
        self.lstOptions.SelectedIndex = 0
        self.set_status(u"Regenerate: {} route(s) planned in {:.1f} s; best option selected.".format(
            stats["planned"], time.time() - started))

    def select_option(self, index):
        if index < 0 or index >= len(self.options or ()):
            return
        option = self.options[index]
        self.set_params(logic.normalize_params(option["params"]))
        self.update_preview()
        if option.get("relaxed"):
            self.set_status(logic.relaxed_notice(option, getattr(self, "own_limits", None)) +
                            u" These values are now in Parameters; Apply asks first and your own values stay "
                            u"remembered.")
            return
        self.set_status(u"Option {} in the preview. Apply to build it.".format(option["number"]))

    def active_relaxed(self, params):
        """The selected RELAXED option if the Parameters still hold its values, else None."""
        options = getattr(self, "options", None) or []
        index = self.lstOptions.SelectedIndex if options else -1
        option = options[index] if 0 <= index < len(options) else None
        return option if logic.relaxed_in_use(params, option) else None

    # --- planning (pure; safe on the UI thread) ---------------------------------------------
    def compute_plan(self, params, infos=None):
        p = logic.params_in_feet(params)
        p["min_straight"] = max(p["min_straight"], getattr(self, "short_curve_tolerance", 0.0) * 1.01)
        segments = [o["segments"] for o in self.obstacles]
        if self.mode == logic.MODE_DETOUR:
            tray = infos[0] if infos else self.tray
            if tray is None:
                return None
            p["takeup"] = self.takeup_for([tray])
            plan = logic.plan_with_fittings(lambda q: logic.plan_detour(tray, segments, self.cut_points, q), p)
            if plan.get("mixed_routes") or plan.get("spatial_bridge"):
                plan["preview_view"] = "elevation" if self.chkElevationPreview.IsChecked else "plan"
            return plan
        trays = infos if infos else self.bridge
        if len(trays) != 2:
            return None
        p["takeup"] = self.takeup_for(trays)
        plan = logic.plan_with_fittings(lambda q: logic.plan_bridge(trays[0], trays[1], segments, q), p)
        if plan.get("spatial_bridge"):
            plan["preview_view"] = "elevation" if self.chkElevationPreview.IsChecked else "plan"
        return plan

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
        if getattr(self, "options", None) and self.options_key != self.selection_key():
            self.clear_options(u"The picks changed: Regenerate for new options.")
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
        keys = ("direction", "angle", "offset_mm", "clearance_mm", "auto_offset")
        if not preset.get("builtin"):
            keys += ("min_straight_mm", "round_mm", "individual_obstacles", "keep_if_fitting_fails",
                     "min_bend_radius_mm", "shorten_fitting_legs", "shallower_bends")
        for key in keys:
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
    def regenerate_clicked(self, sender, args):
        params = self.read_params()
        if (self.mode == logic.MODE_DETOUR and not self.tray) or (
                self.mode == logic.MODE_BRIDGE and len(self.bridge) != 2):
            self.set_status("Pick the tray(s) first.")
            return
        self.lblOptions.Text = u"Regenerating: planning and test-building routes (a few seconds)..."
        self.run_in_revit("Regenerate options", lambda uiapp: self.do_regenerate(uiapp, params))

    @ui_handler
    def option_selected(self, sender, args):
        if self.initialising:
            return
        self.select_option(self.lstOptions.SelectedIndex)

    @ui_handler
    def show_clicked(self, sender, args):
        self.run_in_revit("Show in Revit", self.do_show)

    @ui_handler
    def apply_clicked(self, sender, args):
        params = self.read_params()
        if not (self.plan and self.plan["ok"]):
            self.set_status("Fix the preview message first.")
            return
        relaxed = self.active_relaxed(params)
        if relaxed and not forms.alert(logic.relaxed_notice(relaxed, getattr(self, "own_limits", None)) +
                                       u"\n\nBuild this route anyway? Your own settings stay remembered.",
                                       title="Cable Tray Coordinator", yes=True, no=True):
            self.set_status(u"Relaxed option not applied. Pick another option or change the Parameters.")
            return
        self.run_in_revit("Apply", lambda uiapp: self.do_apply(uiapp, params))

    @ui_handler
    def revert_clicked(self, sender, args):
        self.run_in_revit("Revert last route", self.do_revert)

    @ui_handler
    def close_clicked(self, sender, args):
        self.Close()

    def on_closed(self, sender, args):
        global WINDOW
        self.closed = True
        self.queue.clear()
        self.persist_settings()
        if self.change_application is not None:
            self.change_application.DocumentChanged -= self.on_document_changed
            self.change_application = None
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
