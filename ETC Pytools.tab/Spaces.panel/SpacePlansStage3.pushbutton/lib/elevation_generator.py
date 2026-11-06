# -*- coding: utf-8 -*-
"""STEP 1h - Elevation height driven by the space's Unbounded Height.

MODAL, one file, no lib folder, no sheets, no storage.

WHAT 1h CHANGES
---------------
The crop height is no longer a fixed 2700. It is now:

    height = max(space Unbounded Height, MIN_ELEV_HEIGHT_MM) + OFFSET_TOP_MM

read per space from Space.UnboundedHeight (a real API property), falling back
to the ROOM_HEIGHT built-in parameter ("Unbounded Height"), then to
MIN_ELEV_HEIGHT_MM if neither is available. So a tall plant room gets a tall
elevation and a normal room does not get a short one.

Set MIN_ELEV_HEIGHT_MM to 0 to use the space height exactly.

IronPython 2.7 - no f-strings, no max(default=), no py3-only syntax.
"""

import math
import traceback

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter,
    ViewFamilyType, ViewFamily, ElevationMarker, Transaction, XYZ, Line,
    SpatialElementBoundaryOptions, SpatialElementBoundaryLocation,
    ElementTransformUtils, ElementId, View, ViewType, BoundingBoxXYZ,
    SubTransaction,
)
from Autodesk.Revit.UI.Selection import ObjectType

from pyrevit import forms, script

doc = None
uidoc = None
output = None

MM = 304.8


def mm2ft(mm):
    return float(mm) / MM


def ft2mm(ft):
    return float(ft) * MM


# ===========================================================================
# TUNING
# ===========================================================================

# Elevation height comes from the space's Unbounded Height, but never less
# than this. Set to 0.0 to always use the space height exactly.
MIN_ELEV_HEIGHT_MM = 2700.0

# Faces parallel and within this perpendicular distance are the SAME wall
# (a wall behind the visible one). The nearer face is kept.
DUPLICATE_WALL_TOL_MM = 400.0

# How far PAST the wall face the elevation looks (crop depth + far clip).
VIEW_DEPTH_MM = 400.0

# minimum overlap along the wall for two faces to count as duplicates (0..1)
DUPLICATE_OVERLAP_FRAC = 0.30

# ---- merging --------------------------------------------------------------
COLLINEAR_TOL_MM = 25.0
ANGLE_TOL_DEG = 1.5
MERGE_GAP_MM = 2500.0       # 0 = split at every opening

# ---- placement / view -----------------------------------------------------
PROBE_MM = [600.0, 450.0, 300.0, 200.0, 150.0]
MIN_WALL_MM = 300.0
VIEW_SCALE = 50

OFFSET_SIDE_MM = 100.0
OFFSET_BOTTOM_MM = 100.0
OFFSET_TOP_MM = 100.0

BBOX_SLACK_MM = 500.0
VERBOSE = True


# ============================================================================
# Space selection
# ============================================================================
def _cat_matches(el, bic):
    try:
        a = el.Category.Id
        b = ElementId(bic)
        if hasattr(a, "Value"):
            return a.Value == b.Value
        return a.IntegerValue == b.IntegerValue
    except Exception:
        return False


def get_space():
    for eid in uidoc.Selection.GetElementIds():
        el = doc.GetElement(eid)
        if el is not None and _cat_matches(el, BuiltInCategory.OST_MEPSpaces):
            return el
    try:
        ref = uidoc.Selection.PickObject(ObjectType.Element, "Pick a Space")
        el = doc.GetElement(ref.ElementId)
        if el is not None and _cat_matches(el, BuiltInCategory.OST_MEPSpaces):
            return el
        forms.alert("That is not an MEP Space.", title="Elev Test")
        return None
    except Exception:
        return None


# ============================================================================
# NEW - space height
# ============================================================================
def space_height_ft(space):
    """Return (height_ft, source_text).

    Space.UnboundedHeight is the direct API property. ROOM_HEIGHT is the
    matching built-in parameter, labelled "Unbounded Height" in the UI.
    """
    try:
        h = space.UnboundedHeight
        if h and h > 0:
            return h, "Space.UnboundedHeight"
    except Exception:
        pass
    try:
        p = space.get_Parameter(BuiltInParameter.ROOM_HEIGHT)
        if p is not None:
            h = p.AsDouble()
            if h and h > 0:
                return h, "ROOM_HEIGHT parameter"
    except Exception:
        pass
    return 0.0, "not available"


def elevation_height_ft(space):
    """Height used for the crop: space height, floored at MIN_ELEV_HEIGHT_MM."""
    h, src = space_height_ft(space)
    floor_h = mm2ft(MIN_ELEV_HEIGHT_MM)
    if h < floor_h:
        used = floor_h
        note = "min %.0f mm" % MIN_ELEV_HEIGHT_MM
    else:
        used = h
        note = src
    return used, h, note


# ============================================================================
# Point-in-space probe
# ============================================================================
class SpaceProbe(object):
    def __init__(self, space, sample_pt):
        self.space = space
        self.usable = False
        self.error = ""
        try:
            space.IsPointInSpace(sample_pt)
            self.usable = True
        except Exception:
            self.error = traceback.format_exc().splitlines()[-1]

    def inside(self, pt):
        if not self.usable:
            return None
        try:
            return bool(self.space.IsPointInSpace(pt))
        except Exception:
            return None


# ============================================================================
# Boundary -> straight segments
# ============================================================================
def raw_segments(space, bb):
    opts = SpatialElementBoundaryOptions()
    opts.SpatialElementBoundaryLocation = \
        SpatialElementBoundaryLocation.Finish
    loops = space.GetBoundarySegments(opts)
    if not loops:
        return [], 0, 0

    slack = mm2ft(BBOX_SLACK_MM)
    min_x = bb.Min.X - slack
    max_x = bb.Max.X + slack
    min_y = bb.Min.Y - slack
    max_y = bb.Max.Y + slack

    segs = []
    rejected = 0
    curved = 0
    for bseg in loops[0]:
        c = bseg.GetCurve()
        p0 = c.GetEndPoint(0)
        p1 = c.GetEndPoint(1)
        m = (p0 + p1) * 0.5
        if m.X < min_x or m.X > max_x or m.Y < min_y or m.Y > max_y:
            rejected += 1
            continue
        if not isinstance(c, Line):
            curved += 1
            continue
        segs.append((p0, p1))
    return segs, rejected, curved


# ============================================================================
# Merge collinear fragments
# ============================================================================
def _dir2(p0, p1):
    v = XYZ(p1.X - p0.X, p1.Y - p0.Y, 0.0)
    if v.GetLength() < 1.0e-9:
        return None
    return v.Normalize()


def _canonical(d):
    if d.X < -1.0e-9:
        return XYZ(-d.X, -d.Y, 0.0)
    if abs(d.X) <= 1.0e-9 and d.Y < 0:
        return XYZ(-d.X, -d.Y, 0.0)
    return d


def _perp_dist(pt, origin, d):
    vx = pt.X - origin.X
    vy = pt.Y - origin.Y
    return abs(vx * d.Y - vy * d.X)


def merge_collinear(segs):
    ang_tol = math.cos(math.radians(ANGLE_TOL_DEG))
    perp_tol = mm2ft(COLLINEAR_TOL_MM)
    gap_tol = mm2ft(MERGE_GAP_MM)

    groups = []
    for p0, p1 in segs:
        d = _dir2(p0, p1)
        if d is None:
            continue
        cd = _canonical(d)
        placed = False
        for g in groups:
            gd = g["dir"]
            if abs(cd.X * gd.X + cd.Y * gd.Y) < ang_tol:
                continue
            if _perp_dist(p0, g["origin"], gd) > perp_tol:
                continue
            if _perp_dist(p1, g["origin"], gd) > perp_tol:
                continue
            t0 = (p0.X - g["origin"].X) * gd.X + (p0.Y - g["origin"].Y) * gd.Y
            t1 = (p1.X - g["origin"].X) * gd.X + (p1.Y - g["origin"].Y) * gd.Y
            if t0 > t1:
                t0, t1 = t1, t0
            g["items"].append((t0, t1))
            placed = True
            break
        if not placed:
            t1 = (p1.X - p0.X) * cd.X + (p1.Y - p0.Y) * cd.Y
            a, b = (0.0, t1) if t1 >= 0 else (t1, 0.0)
            groups.append({"origin": p0, "dir": cd, "z": p0.Z,
                           "items": [(a, b)]})

    merged = []
    for g in groups:
        items = g["items"]
        items.sort()
        runs = []
        cur_a, cur_b = items[0]
        for a, b in items[1:]:
            if a - cur_b <= gap_tol:
                if b > cur_b:
                    cur_b = b
            else:
                runs.append((cur_a, cur_b))
                cur_a, cur_b = a, b
        runs.append((cur_a, cur_b))

        o = g["origin"]
        d = g["dir"]
        z = g["z"]
        for a, b in runs:
            pa = XYZ(o.X + d.X * a, o.Y + d.Y * a, z)
            pb = XYZ(o.X + d.X * b, o.Y + d.Y * b, z)
            merged.append((pa, pb))
    return merged


# ============================================================================
# Drop duplicate faces from walls behind the visible one
# ============================================================================
def dedupe_parallel(faces, center):
    ang_tol = math.cos(math.radians(ANGLE_TOL_DEG))
    dup_tol = mm2ft(DUPLICATE_WALL_TOL_MM)

    items = []
    for p0, p1 in faces:
        d = _dir2(p0, p1)
        if d is None:
            continue
        cd = _canonical(d)
        mid = XYZ((p0.X + p1.X) * 0.5, (p0.Y + p1.Y) * 0.5, p0.Z)
        dist = _perp_dist(XYZ(center.X, center.Y, p0.Z), p0, cd)
        length = XYZ(p1.X - p0.X, p1.Y - p0.Y, 0.0).GetLength()
        items.append({"p0": p0, "p1": p1, "dir": cd, "mid": mid,
                      "dist": dist, "len": length, "dead": False})

    dropped = 0
    for i in range(len(items)):
        if items[i]["dead"]:
            continue
        for j in range(i + 1, len(items)):
            if items[j]["dead"]:
                continue
            a = items[i]
            b = items[j]
            if abs(a["dir"].X * b["dir"].X + a["dir"].Y * b["dir"].Y) < ang_tol:
                continue
            gap = _perp_dist(b["mid"], a["p0"], a["dir"])
            if gap > dup_tol:
                continue
            d = a["dir"]
            ta1 = (a["p1"].X - a["p0"].X) * d.X + (a["p1"].Y - a["p0"].Y) * d.Y
            tb0 = (b["p0"].X - a["p0"].X) * d.X + (b["p0"].Y - a["p0"].Y) * d.Y
            tb1 = (b["p1"].X - a["p0"].X) * d.X + (b["p1"].Y - a["p0"].Y) * d.Y
            lo_a, hi_a = (0.0, ta1) if ta1 >= 0 else (ta1, 0.0)
            lo_b, hi_b = (tb0, tb1) if tb0 <= tb1 else (tb1, tb0)
            ov = min(hi_a, hi_b) - max(lo_a, lo_b)
            if ov <= 0:
                continue
            shorter = a["len"] if a["len"] <= b["len"] else b["len"]
            if shorter <= 0 or (ov / shorter) < DUPLICATE_OVERLAP_FRAC:
                continue
            if a["dist"] <= b["dist"]:
                items[j]["dead"] = True
                dropped += 1
            else:
                items[i]["dead"] = True
                dropped += 1
                break

    out = []
    for it in items:
        if not it["dead"]:
            out.append((it["p0"], it["p1"]))
    return out, dropped


# ============================================================================
# Which side is inside?
# ============================================================================
def resolve_inward(probe, mid, normal, center, z_list):
    if probe.usable:
        for z in z_list:
            for mm in PROBE_MM:
                d = mm2ft(mm)
                p_pos = XYZ(mid.X + normal.X * d, mid.Y + normal.Y * d, z)
                if probe.inside(p_pos):
                    return normal, p_pos
                neg = normal.Negate()
                p_neg = XYZ(mid.X + neg.X * d, mid.Y + neg.Y * d, z)
                if probe.inside(p_neg):
                    return neg, p_neg
        return None, None

    to_ctr = XYZ(center.X, center.Y, mid.Z) - mid
    inward = normal
    if inward.DotProduct(to_ctr) < 0:
        inward = inward.Negate()
    d = mm2ft(PROBE_MM[0])
    return inward, XYZ(mid.X + inward.X * d, mid.Y + inward.Y * d, z_list[0])


# ============================================================================
# Pickers
# ============================================================================
def _type_name(el):
    try:
        n = el.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM).AsString()
        if n:
            return n
    except Exception:
        pass
    try:
        return el.Name
    except Exception:
        return "<unnamed>"


def pick_elevation_vft():
    opts = []
    for v in FilteredElementCollector(doc).OfClass(ViewFamilyType).ToElements():
        try:
            if v.ViewFamily == ViewFamily.Elevation:
                opts.append((_type_name(v), v))
        except Exception:
            continue
    if not opts:
        return None
    if len(opts) == 1:
        return opts[0][1]
    opts.sort(key=lambda p: p[0].lower())
    names = []
    for nm, _v in opts:
        names.append(nm)
    chosen = forms.SelectFromList.show(names, title="Elevation TYPE",
                                       button_name="Use this type")
    if not chosen:
        return None
    for nm, v in opts:
        if nm == chosen:
            return v
    return None


def pick_elevation_template():
    opts = []
    for v in FilteredElementCollector(doc).OfClass(View).ToElements():
        try:
            if v.IsTemplate and v.ViewType == ViewType.Elevation:
                opts.append((v.Name, v))
        except Exception:
            continue
    if not opts:
        return None
    opts.sort(key=lambda p: p[0].lower())
    names = ["<no template>"]
    for nm, _v in opts:
        names.append(nm)
    chosen = forms.SelectFromList.show(names, title="Elevation VIEW TEMPLATE",
                                       button_name="Apply")
    if not chosen or chosen == "<no template>":
        return None
    for nm, v in opts:
        if nm == chosen:
            return v
    return None


# ============================================================================
# Aim
# ============================================================================
def looks_at_wall(view, origin, wall_mid):
    try:
        vd = view.ViewDirection
        v2 = XYZ(vd.X, vd.Y, 0.0)
        to_wall = XYZ(wall_mid.X - origin.X, wall_mid.Y - origin.Y, 0.0)
        if v2.GetLength() < 1.0e-9 or to_wall.GetLength() < 1.0e-9:
            return True
        return v2.Normalize().DotProduct(to_wall.Normalize()) < 0
    except Exception:
        return True


def rotate_marker(marker, origin, angle):
    axis = Line.CreateBound(origin, XYZ(origin.X, origin.Y, origin.Z + 1.0))
    ElementTransformUtils.RotateElement(doc, marker.Id, axis, angle)
    doc.Regenerate()


def aim_at_wall(marker, view, origin, wall_mid, want_dir):
    try:
        cur = view.ViewDirection
        c2 = XYZ(cur.X, cur.Y, 0.0)
        w2 = XYZ(want_dir.X, want_dir.Y, 0.0)
        if c2.GetLength() > 1.0e-9 and w2.GetLength() > 1.0e-9:
            c2 = c2.Normalize()
            w2 = w2.Normalize()
            dot = c2.DotProduct(w2)
            if dot > 1.0:
                dot = 1.0
            if dot < -1.0:
                dot = -1.0
            ang = math.acos(dot)
            if ang > 1.0e-6:
                crossz = c2.X * w2.Y - c2.Y * w2.X
                if crossz < 0:
                    ang = -ang
                rotate_marker(marker, origin, ang)
    except Exception:
        pass

    if not looks_at_wall(view, origin, wall_mid):
        try:
            rotate_marker(marker, origin, math.pi)
        except Exception:
            pass
        return looks_at_wall(view, origin, wall_mid)
    return True


# ============================================================================
# Crop - height now driven by the space
# ============================================================================
def crop_to_wall(view, p_start, p_end, floor_z, height_ft):
    bb = view.CropBox
    tr = bb.Transform
    inv = tr.Inverse

    z_bot = floor_z - mm2ft(OFFSET_BOTTOM_MM)
    z_top = floor_z + height_ft + mm2ft(OFFSET_TOP_MM)

    corners = [
        XYZ(p_start.X, p_start.Y, z_bot),
        XYZ(p_end.X, p_end.Y, z_bot),
        XYZ(p_start.X, p_start.Y, z_top),
        XYZ(p_end.X, p_end.Y, z_top),
    ]

    xs = []
    ys = []
    zs = []
    for c in corners:
        l = inv.OfPoint(c)
        xs.append(l.X)
        ys.append(l.Y)
        zs.append(l.Z)

    side = mm2ft(OFFSET_SIDE_MM)
    x_min = min(xs) - side
    x_max = max(xs) + side
    y_min = min(ys)
    y_max = max(ys)

    wall_z = min(zs)
    depth = mm2ft(VIEW_DEPTH_MM)
    z_far = wall_z - depth
    z_near = max(zs) + mm2ft(50.0)
    if z_near <= z_far:
        z_near = z_far + mm2ft(100.0)

    new = BoundingBoxXYZ()
    new.Transform = tr
    new.Min = XYZ(x_min, y_min, z_far)
    new.Max = XYZ(x_max, y_max, z_near)
    view.CropBox = new
    view.CropBoxActive = True
    view.CropBoxVisible = True

    try:
        p = view.get_Parameter(BuiltInParameter.VIEWER_BOUND_FAR_CLIPPING)
        if p is not None and not p.IsReadOnly:
            p.Set(1)
    except Exception:
        pass
    try:
        p = view.get_Parameter(BuiltInParameter.VIEWER_BOUND_OFFSET_FAR)
        if p is not None and not p.IsReadOnly:
            p.Set(abs(wall_z) + depth)
    except Exception:
        pass

    return ("w %.0f mm, h %.0f mm, depth %.0f past face"
            % (ft2mm(x_max - x_min), ft2mm(y_max - y_min), VIEW_DEPTH_MM))



def _txt(space,bip):
    try:
        p=space.get_Parameter(bip)
        return p.AsString() or '' if p else ''
    except:return ''

def generate_space_elevations(document,host_view,space,elevation_type,elevation_template=None,settings=None,output_writer=None):
    global doc,output,MIN_ELEV_HEIGHT_MM,DUPLICATE_WALL_TOL_MM,VIEW_DEPTH_MM
    global DUPLICATE_OVERLAP_FRAC,COLLINEAR_TOL_MM,ANGLE_TOL_DEG,MERGE_GAP_MM
    global MIN_WALL_MM,VIEW_SCALE,OFFSET_SIDE_MM,OFFSET_BOTTOM_MM,OFFSET_TOP_MM,BBOX_SLACK_MM,VERBOSE
    doc=document;output=output_writer;settings=settings or {}
    MIN_ELEV_HEIGHT_MM=float(settings.get('min_elev_height_mm',2700));DUPLICATE_WALL_TOL_MM=float(settings.get('duplicate_wall_tol_mm',400));VIEW_DEPTH_MM=float(settings.get('view_depth_mm',400));DUPLICATE_OVERLAP_FRAC=float(settings.get('duplicate_overlap_frac',0.30));COLLINEAR_TOL_MM=float(settings.get('collinear_tol_mm',25));ANGLE_TOL_DEG=float(settings.get('angle_tol_deg',1.5));MERGE_GAP_MM=float(settings.get('merge_gap_mm',2500));MIN_WALL_MM=float(settings.get('min_wall_mm',300));VIEW_SCALE=int(settings.get('view_scale',50));OFFSET_SIDE_MM=float(settings.get('offset_side_mm',100));OFFSET_BOTTOM_MM=float(settings.get('offset_bottom_mm',100));OFFSET_TOP_MM=float(settings.get('offset_top_mm',100));BBOX_SLACK_MM=float(settings.get('bbox_slack_mm',500));VERBOSE=False
    result={'space_id':space.Id,'space_number':_txt(space,BuiltInParameter.ROOM_NUMBER),'space_name':_txt(space,BuiltInParameter.ROOM_NAME),'created_views':[],'skipped_short':0,'skipped_no_inside':0,'aim_unverified':0,'skipped_curved':0,'duplicates_dropped':0,'errors':[]}
    bb=space.get_BoundingBox(None)
    if bb is None:result['errors'].append('No bounding box');return result
    center=(bb.Min+bb.Max)*0.5;floor_z=bb.Min.Z;zlist=[center.Z,floor_z+mm2ft(1200),floor_z+mm2ft(600)];height,raw,note=elevation_height_ft(space);probe=SpaceProbe(space,XYZ(center.X,center.Y,center.Z));segs,rej,curved=raw_segments(space,bb);merged=merge_collinear(segs);faces,dropped=dedupe_parallel(merged,center)
    faces=sorted(faces,key=lambda f:-math.atan2(((f[0]+f[1])*0.5).Y-center.Y,((f[0]+f[1])*0.5).X-center.X));result['skipped_curved']=curved;result['duplicates_dropped']=dropped
    for idx,(p0,p1) in enumerate(faces):
        along=p1-p0;length=along.GetLength()
        if length<mm2ft(MIN_WALL_MM):result['skipped_short']+=1;continue
        along=along.Normalize();mid=(p0+p1)*0.5;normal=XYZ(along.Y,-along.X,0).Normalize();inward,origin=resolve_inward(probe,mid,normal,center,zlist)
        if inward is None:result['skipped_no_inside']+=1;continue
        sub=SubTransaction(document)
        try:
            sub.Start();marker=ElevationMarker.CreateElevationMarker(document,elevation_type.Id,origin,VIEW_SCALE);view=marker.CreateElevation(document,host_view.Id,0);document.Regenerate();ok=aim_at_wall(marker,view,origin,mid,inward);crop_to_wall(view,p0,p1,floor_z,height);view.Scale=VIEW_SCALE
            if elevation_template:view.ViewTemplateId=elevation_template.Id
            seq=len(result['created_views'])+1
            try:view.Name='%s - Elevation %02d'%(result['space_number'] or 'Space',seq)
            except:pass
            result['created_views'].append({'view':view,'view_id':view.Id,'marker_id':marker.Id,'sequence':seq,'wall_length_ft':length,'aim_verified':ok})
            if not ok:result['aim_unverified']+=1
            sub.Commit()
        except Exception:
            try:sub.RollBack()
            except:pass
            result['errors'].append('face %d: %s'%(idx+1,traceback.format_exc().splitlines()[-1]))
    return result
