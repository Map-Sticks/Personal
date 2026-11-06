# -*- coding: utf-8 -*-
"""NWB Asset ID Generator (pyRevit, IronPython 2.7)

TAB 1 - Asset ID  (unchanged behaviour)
  NWB_AssetID = AssetTypeCode-Building-Level-#### with a per
  (AssetTypeCode-Building-Level) sequential counter.
  - Building: manual placeholder (default "M") OR copied from a param.
  - Level: copied from a param (default NWB_WBS02).
  - Maint. Type 1 & 2 get a real ID; others get "N/A" (when ticked).
  - Invisible copy-guard via Extensible Storage.

TAB 2 - Room ID / Name  (separate function, own Preview/Apply)
  NWB_RoomID   = <Prefix>-<Level>-<Room>   e.g. IP1.04.031 -> OPH-M-04-031
  NWB_RoomName = Name of the host-model MEP Space the element sits in.
  - Space lookup: Association -> Point -> Level point -> Drop test
                  -> Boundary scan (space plan boundary, from the space's
                     level up to the next space level minus a tolerance)
  - Not-found elements get a plain-English diagnosis, and can be selected
    in Revit ("Select in Revit") for visual checking.
  - Room number parsing: last N segments OR last N characters, user delimiter.
  - Level segment taken as-is (GR / LG / RF / 04 ...).
  - Not Type 1/2 (when ticked) or no space found -> "N/A".

Preview + CSV export BEFORE writing. Each Apply runs in one transaction.
"""

__title__ = "NWB\nAsset ID"
__author__ = "ETC / Jacques-Michael Oosthuizen"

import re
import os
import datetime

import clr
# --- .NET assembly references (IronPython does NOT auto-load these) ---
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("System")
clr.AddReference("System.Data")
clr.AddReference("System.Xml")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from Autodesk.Revit.DB import (
    FilteredElementCollector, Transaction, ElementId, StorageType,
    BuiltInParameter, BuiltInCategory, XYZ, LocationPoint, LocationCurve,
    SpatialElementBoundaryOptions, SpatialElementBoundaryLocation,
)
from Autodesk.Revit.DB.ExtensibleStorage import (
    Schema, SchemaBuilder, AccessLevel, Entity,
)

from System import Guid, String
from System.Data import DataTable
from System.IO import StringReader
from System.Xml import XmlReader
from System.Windows.Markup import XamlReader
from System.Collections.Generic import List

from pyrevit import revit, forms, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

MM_TO_FT = 1.0 / 304.8

# ----------------------------------------------------------------------------
# Extensible Storage - the invisible "owner" guard
# ----------------------------------------------------------------------------
SCHEMA_GUID = Guid("9E5D2A14-3B7C-4F81-A6E2-1C0F7B9D4E33")
SCHEMA_NAME = "NWB_AssetIdOwner"
FIELD_OWNER = "OwnerUniqueId"
VENDOR_ID = "NWBHP"

_schema_cache = [None]


def get_schema():
    if _schema_cache[0] is not None:
        return _schema_cache[0]
    existing = Schema.Lookup(SCHEMA_GUID)
    if existing is not None:
        _schema_cache[0] = existing
        return existing
    sb = SchemaBuilder(SCHEMA_GUID)
    sb.SetSchemaName(SCHEMA_NAME)
    sb.SetReadAccessLevel(AccessLevel.Public)
    sb.SetWriteAccessLevel(AccessLevel.Vendor)
    sb.SetVendorId(VENDOR_ID)
    sb.AddSimpleField(FIELD_OWNER, String)
    schema = sb.Finish()
    _schema_cache[0] = schema
    return schema


def read_owner(elem):
    try:
        ent = elem.GetEntity(get_schema())
        if ent is None or not ent.IsValid():
            return None
        val = ent.Get[String](FIELD_OWNER)
        return val if val else None
    except Exception:
        return None


def write_owner(elem):
    try:
        ent = Entity(get_schema())
        ent.Set[String](FIELD_OWNER, elem.UniqueId)
        elem.SetEntity(ent)
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------------
# Parameter helpers
# ----------------------------------------------------------------------------
def num_to_text(v):
    if v is None:
        return u""
    if isinstance(v, bool):
        return u"Yes" if v else u"No"
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return repr(v)
    if isinstance(v, int):
        return str(v)
    return str(v).strip()


def get_param(elem, pname):
    try:
        return elem.LookupParameter(pname)
    except Exception:
        return None


def get_text(elem, pname):
    p = get_param(elem, pname)
    if p is None:
        return u""
    try:
        st = p.StorageType
        if st == StorageType.String:
            s = p.AsString()
            return num_to_text(s) if s else u""
        if st == StorageType.Integer:
            return num_to_text(p.AsInteger())
        if st == StorageType.Double:
            vs = p.AsValueString()
            return vs.strip() if vs else num_to_text(p.AsDouble())
        vs = p.AsValueString()
        return vs.strip() if vs else u""
    except Exception:
        return u""


def set_text(elem, pname, value):
    p = get_param(elem, pname)
    if p is None or p.IsReadOnly:
        return False
    try:
        p.Set(u"" if value is None else value)
        return True
    except Exception:
        return False


def list_text_params(sample_elems, limit=40):
    names = set()
    for e in sample_elems[:limit]:
        try:
            for p in e.Parameters:
                try:
                    if p.StorageType in (StorageType.String, StorageType.Integer):
                        d = p.Definition
                        if d is not None:
                            names.add(d.Name)
                except Exception:
                    continue
        except Exception:
            continue
    return sorted(names)


# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
KEY_PARAM = "NWB_AssetTypeCode"
ID_PARAM = "NWB_AssetID"
MAINT_PARAM = "NWB_MaintainabilityType"
ROOMID_PARAM = "NWB_RoomID"
ROOMNAME_PARAM = "NWB_RoomName"
NA_VALUE = "N/A"
DEFAULT_BUILDING = "M"
DEFAULT_LEVEL_PARAM = "NWB_WBS02"
DEFAULT_BUILDING_PARAM = "NWB_WBS01"
DEFAULT_ROOM_PREFIX = "OPH-M"


# ----------------------------------------------------------------------------
# Collection (shared by both tabs)
# ----------------------------------------------------------------------------
_last_scan = {"scanned": 0}


def no_candidates_message(scope):
    n = _last_scan["scanned"]
    if scope == "selection":
        if n == 0:
            return ("Scope is 'Selected elements' but nothing was selected.\n\n"
                    "Select the elements in Revit BEFORE launching the button, "
                    "or switch scope to 'Active view' / 'Whole model'.")
        return ("You selected %d element(s), but none have %s populated."
                % (n, KEY_PARAM))
    if scope == "view":
        return ("Scanned %d element(s) in the active view '%s', but none have "
                "%s populated." % (n, doc.ActiveView.Name, KEY_PARAM))
    return ("Scanned %d element(s) in the whole model, but none have %s "
            "populated.\n\nRun Part A first." % (n, KEY_PARAM))


def collect_elements(scope):
    if scope == "selection":
        ids = list(uidoc.Selection.GetElementIds())
        elems = [doc.GetElement(i) for i in ids]
    elif scope == "view":
        elems = list(FilteredElementCollector(doc, doc.ActiveView.Id)
                     .WhereElementIsNotElementType().ToElements())
    else:
        elems = list(FilteredElementCollector(doc)
                     .WhereElementIsNotElementType().ToElements())
    elems = [e for e in elems if e is not None]
    _last_scan["scanned"] = len(elems)
    out = []
    for e in elems:
        if get_text(e, KEY_PARAM):
            out.append(e)
    return out


def is_na(txt):
    return txt is not None and txt.strip().upper() in ("N/A", "NA")


def is_type_1_or_2(elem):
    return get_text(elem, MAINT_PARAM) in ("1", "2")


# ============================================================================
# TAB 1 - ASSET ID ENGINE (unchanged)
# ============================================================================
class Row(object):
    __slots__ = ("elem", "eid", "code", "prefix", "existing", "owner",
                 "status", "new_id", "action")

    def __init__(self, elem):
        self.elem = elem
        self.eid = elem.Id.IntegerValue
        self.code = ""
        self.prefix = ""
        self.existing = ""
        self.owner = None
        self.status = ""
        self.new_id = ""
        self.action = ""


def compute_prefix(elem, opts):
    code = get_text(elem, KEY_PARAM)
    if opts["building_mode"] == "manual":
        building = opts["building_manual"]
    else:
        building = get_text(elem, opts["building_param"])
    if opts["level_mode"] == "param":
        level = get_text(elem, opts["level_param"])
    else:
        level = opts["level_manual"]
    return code, u"{0}-{1}-{2}".format(code, building, level)


SEQ_RE = re.compile(r"^(?P<prefix>.+)-(?P<seq>\d+)$")


def parse_existing(existing_id):
    if not existing_id:
        return None, None
    m = SEQ_RE.match(existing_id.strip())
    if not m:
        return None, None
    try:
        return m.group("prefix"), int(m.group("seq"))
    except Exception:
        return None, None


def build_plan(elems, opts):
    maint_only = opts["maint_only"]
    mode = opts["write_mode"]      # blanks / all / dupes
    start = opts["seq_start"]
    pad = opts["seq_pad"]

    rows = []
    seq_rows = []

    for e in elems:
        r = Row(e)
        r.code, r.prefix = compute_prefix(e, opts)
        r.existing = get_text(e, ID_PARAM)
        r.owner = read_owner(e)
        own_uid = e.UniqueId

        if maint_only:
            mt = get_text(e, MAINT_PARAM)
            if mt not in ("1", "2"):
                r.status = "NA"
                if mode == "dupes":
                    r.action = "Skip (N/A pass off)"
                elif mode == "blanks" and r.existing and not is_na(r.existing):
                    r.action = "Keep"
                    r.new_id = r.existing
                elif is_na(r.existing):
                    r.action = "Keep"
                    r.new_id = NA_VALUE
                else:
                    r.new_id = NA_VALUE
                    r.action = "Write N/A"
                rows.append(r)
                continue

        ex_prefix, ex_seq = parse_existing(r.existing)
        if not r.existing or is_na(r.existing):
            r.status = "NEW"
        elif r.owner and r.owner != own_uid:
            r.status = "COPY"
        elif ex_prefix is None:
            r.status = "INVALID"
        elif ex_prefix != r.prefix:
            r.status = "PREFIX_CHANGED"
        else:
            r.status = "OWNED"
        rows.append(r)
        seq_rows.append(r)

    groups = {}
    for r in seq_rows:
        groups.setdefault(r.prefix, []).append(r)

    for prefix, grp in groups.items():
        grp_sorted = sorted(grp, key=lambda x: x.eid)

        if mode == "all":
            n = start
            for r in grp_sorted:
                r.new_id = u"{0}-{1}".format(prefix, str(n).zfill(pad))
                r.action = "Re-sequence (all)"
                n += 1
            continue

        reserved = set()
        owned_by_seq = {}
        for r in grp_sorted:
            if r.status == "OWNED":
                _, seq = parse_existing(r.existing)
                if seq in owned_by_seq:
                    r.status = "DUP"
                else:
                    owned_by_seq[seq] = r
                    reserved.add(seq)
                    r.action = "Keep"
                    r.new_id = r.existing

        next_seq = (max(reserved) + 1) if reserved else start

        def take_next(_n):
            while _n in reserved:
                _n += 1
            reserved.add(_n)
            return _n

        for r in grp_sorted:
            if r.status == "OWNED":
                continue
            if mode == "dupes" and r.status == "NEW":
                r.action = "Skip (blank)"
                continue
            s = take_next(next_seq)
            next_seq = s + 1
            r.new_id = u"{0}-{1}".format(prefix, str(s).zfill(pad))
            r.action = {
                "NEW": "Assign",
                "COPY": "Copy detected -> re-sequence",
                "DUP": "Duplicate -> re-sequence",
                "PREFIX_CHANGED": "Level/Bldg changed -> re-sequence",
                "INVALID": "Invalid id -> re-sequence",
            }.get(r.status, "Assign")

    return rows


def apply_plan(rows):
    written = 0
    stamped = 0
    na_written = 0
    failed = []
    t = Transaction(doc, "NWB Asset ID")
    t.Start()
    try:
        for r in rows:
            if r.action in ("Keep", "") or r.action.startswith("Skip"):
                if r.action == "Keep" and r.status != "NA" and not r.owner:
                    if write_owner(r.elem):
                        stamped += 1
                continue
            if not r.new_id:
                continue
            if set_text(r.elem, ID_PARAM, r.new_id):
                if r.new_id == NA_VALUE:
                    na_written += 1
                else:
                    written += 1
                    if write_owner(r.elem):
                        stamped += 1
            else:
                failed.append(r.eid)
        t.Commit()
    except Exception as ex:
        t.RollBack()
        raise ex
    return written, na_written, stamped, failed


def rows_to_datatable(rows):
    dt = DataTable("preview")
    for col in ("ElementId", "Category", "Prefix", "Existing", "Action", "New ID"):
        dt.Columns.Add(col)
    for r in sorted(rows, key=lambda x: (x.prefix, x.eid)):
        dt.Rows.Add(str(r.eid), cat_name(r.elem), r.prefix, r.existing,
                    r.action, r.new_id)
    return dt


def export_csv(rows):
    path = _csv_path("NWB_AssetID_preview")
    try:
        f = open(path, "w")
        try:
            f.write("ElementId,Category,Prefix,Existing,Action,NewID\n")
            for r in sorted(rows, key=lambda x: (x.prefix, x.eid)):
                f.write(u",".join([
                    str(r.eid), _q(cat_name(r.elem)), r.prefix, r.existing,
                    _q(r.action), r.new_id]) + "\n")
        finally:
            f.close()
        return path
    except Exception:
        return None


# ============================================================================
# TAB 2 - ROOM ID / NAME ENGINE
# ============================================================================
def get_active_phase():
    try:
        p = doc.ActiveView.get_Parameter(BuiltInParameter.VIEW_PHASE)
        if p:
            ph = doc.GetElement(p.AsElementId())
            if ph:
                return ph
    except Exception:
        pass
    try:
        phases = list(doc.Phases)
        if phases:
            return phases[-1]
    except Exception:
        pass
    return None


def representative_point(e):
    """LocationPoint -> midpoint of LocationCurve -> bounding box centre."""
    try:
        loc = e.Location
        if isinstance(loc, LocationPoint):
            return loc.Point
        if isinstance(loc, LocationCurve) and loc.Curve is not None:
            return loc.Curve.Evaluate(0.5, True)
    except Exception:
        pass
    try:
        bb = e.get_BoundingBox(None)
        if bb:
            return XYZ((bb.Min.X + bb.Max.X) * 0.5,
                       (bb.Min.Y + bb.Max.Y) * 0.5,
                       (bb.Min.Z + bb.Max.Z) * 0.5)
    except Exception:
        pass
    return None


_LEVEL_BIPS = (
    BuiltInParameter.FAMILY_LEVEL_PARAM,
    BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM,
    BuiltInParameter.RBS_START_LEVEL_PARAM,
    BuiltInParameter.SCHEDULE_LEVEL_PARAM,
)


def element_level_z(e):
    """Elevation (internal coords, ft) of the element's associated level."""
    lvl = None
    try:
        if e.LevelId and e.LevelId != ElementId.InvalidElementId:
            lvl = doc.GetElement(e.LevelId)
    except Exception:
        lvl = None
    if lvl is None:
        for bip in _LEVEL_BIPS:
            try:
                p = e.get_Parameter(bip)
                if p and p.AsElementId() != ElementId.InvalidElementId:
                    lvl = doc.GetElement(p.AsElementId())
                    if lvl:
                        break
            except Exception:
                continue
    if lvl is None:
        return None
    try:
        return lvl.ProjectElevation
    except Exception:
        try:
            return lvl.Elevation
        except Exception:
            return None


def space_at(pt, phase):
    if pt is None:
        return None
    try:
        if phase is not None:
            return doc.GetSpaceAtPoint(pt, phase)
        return doc.GetSpaceAtPoint(pt)
    except Exception:
        return None


def bbox_centre(e):
    try:
        bb = e.get_BoundingBox(None)
        if bb:
            return XYZ((bb.Min.X + bb.Max.X) * 0.5,
                       (bb.Min.Y + bb.Max.Y) * 0.5,
                       (bb.Min.Z + bb.Max.Z) * 0.5)
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------
# Space index: each space's plan boundary + vertical band
#   band = [space level - 10 mm, next level that HAS spaces - tolerance]
# Used by the boundary scan AND by the "why not found" diagnosis.
# ----------------------------------------------------------------------------
class SpaceBand(object):
    __slots__ = ("space", "number", "level_name", "edges",
                 "minx", "miny", "maxx", "maxy", "z_base", "z_bot", "z_top")


def _level_z(lvl):
    try:
        return lvl.ProjectElevation
    except Exception:
        return lvl.Elevation


def _space_in_phase(sp, phase):
    if phase is None:
        return True
    try:
        p = sp.get_Parameter(BuiltInParameter.ROOM_PHASE)
        if p is None:
            return True
        return p.AsElementId() == phase.Id
    except Exception:
        return True


def build_space_index(phase, top_tol_ft, default_height_ft):
    spaces = list(FilteredElementCollector(doc)
                  .OfCategory(BuiltInCategory.OST_MEPSpaces)
                  .WhereElementIsNotElementType().ToElements())
    placed = []
    for sp in spaces:
        try:
            if sp.Location is None or sp.Area <= 0 or sp.Level is None:
                continue
        except Exception:
            continue
        placed.append(sp)

    in_phase = [sp for sp in placed if _space_in_phase(sp, phase)]
    if in_phase:
        placed = in_phase

    # "level above" = next level that actually has spaces on it
    sorted_z = sorted(set(round(_level_z(sp.Level), 4) for sp in placed))

    opt = SpatialElementBoundaryOptions()
    opt.SpatialElementBoundaryLocation = SpatialElementBoundaryLocation.Finish

    index = []
    for sp in placed:
        try:
            base = _level_z(sp.Level)
        except Exception:
            continue
        above = [z for z in sorted_z if z > base + 0.01]
        top = (above[0] - top_tol_ft) if above else (base + default_height_ft)

        try:
            loops = sp.GetBoundarySegments(opt)
        except Exception:
            loops = None
        if not loops:
            continue
        edges, xs, ys = [], [], []
        for loop in loops:
            pts = []
            for seg in loop:
                try:
                    tess = list(seg.GetCurve().Tessellate())
                except Exception:
                    continue
                pts.extend(tess[:-1] if len(tess) > 1 else tess)
            if len(pts) < 3:
                continue
            for i in range(len(pts)):
                a = pts[i]
                b = pts[(i + 1) % len(pts)]
                edges.append((a.X, a.Y, b.X, b.Y))
                xs.append(a.X)
                ys.append(a.Y)
        if not edges:
            continue

        sb = SpaceBand()
        sb.space = sp
        sb.number, _nm = space_number_name(sp)
        try:
            sb.level_name = sp.Level.Name
        except Exception:
            sb.level_name = u"?"
        sb.edges = edges
        sb.minx, sb.maxx = min(xs), max(xs)
        sb.miny, sb.maxy = min(ys), max(ys)
        sb.z_base = base
        sb.z_bot = base - 0.03          # ~10 mm below level (floor items)
        sb.z_top = top
        index.append(sb)
    return index


def _point_in_edges(x, y, edges):
    """Even-odd ray cast across ALL loops (holes/shafts excluded)."""
    inside = False
    for (x1, y1, x2, y2) in edges:
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xi:
                inside = not inside
    return inside


def _seg_dist(px, py, x1, y1, x2, y2):
    dx = x2 - x1
    dy = y2 - y1
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    cx = x1 + t * dx
    cy = y1 + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def _test_points(e):
    pts = []
    p1 = representative_point(e)
    if p1 is not None:
        pts.append(p1)
    p2 = bbox_centre(e)
    if p2 is not None and (p1 is None or p1.DistanceTo(p2) > 0.01):
        pts.append(p2)
    return pts


def boundary_scan(e, index):
    """Space whose plan boundary contains the element XY AND whose level band
    (level -> next space level - tol) contains the element Z."""
    if not index:
        return None
    for pt in _test_points(e):
        best = None
        for sb in index:
            if pt.X < sb.minx or pt.X > sb.maxx or pt.Y < sb.miny or pt.Y > sb.maxy:
                continue
            if pt.Z < sb.z_bot or pt.Z > sb.z_top:
                continue
            if not _point_in_edges(pt.X, pt.Y, sb.edges):
                continue
            if best is None or sb.z_bot > best.z_bot:
                best = sb
        if best is not None:
            return best.space
    return None


def _mm(ft):
    return int(round(ft * 304.8))


DIAG_RADIUS_FT = 5000.0 * MM_TO_FT     # look for nearby spaces within 5 m


def diagnose_no_space(e, index):
    """Plain-English reason why no space matched. Never writes anything."""
    if not index:
        return u"No placed MEP spaces in model/phase"
    pts = _test_points(e)
    if not pts:
        return u"Element has no location or bounding box"
    pt = pts[0]
    try:
        elev_mm = _mm(pt.Z - min(sb.z_base for sb in index))
    except Exception:
        elev_mm = None

    # A) Inside a space outline in PLAN, but at the wrong height
    hits = [sb for sb in index
            if sb.minx <= pt.X <= sb.maxx and sb.miny <= pt.Y <= sb.maxy
            and _point_in_edges(pt.X, pt.Y, sb.edges)]
    if hits:
        def gap(sb):
            if pt.Z < sb.z_bot:
                return sb.z_bot - pt.Z
            return pt.Z - sb.z_top
        sb = min(hits, key=gap)
        if pt.Z < sb.z_bot:
            return (u"Inside %s (%s) in plan, but %d mm BELOW its level"
                    % (sb.number, sb.level_name, _mm(sb.z_bot - pt.Z)))
        return (u"Inside %s (%s) in plan, but %d mm ABOVE its band "
                u"(gap to level above / outside top-floor height)"
                % (sb.number, sb.level_name, _mm(pt.Z - sb.z_top)))

    # B) Not inside any space outline - find nearest space edge on same band
    same_band = [sb for sb in index if sb.z_bot <= pt.Z <= sb.z_top]
    pool = same_band if same_band else index
    best_d = None
    best_sb = None
    for sb in pool:
        if (pt.X < sb.minx - DIAG_RADIUS_FT or pt.X > sb.maxx + DIAG_RADIUS_FT or
                pt.Y < sb.miny - DIAG_RADIUS_FT or pt.Y > sb.maxy + DIAG_RADIUS_FT):
            continue
        for (x1, y1, x2, y2) in sb.edges:
            d = _seg_dist(pt.X, pt.Y, x1, y1, x2, y2)
            if best_d is None or d < best_d:
                best_d = d
                best_sb = sb
    if not same_band:
        lvl = u" (no spaces at this height)"
    else:
        lvl = u""
    if best_sb is None:
        return u"Outside all spaces in plan - none within 5 m%s" % lvl
    return (u"Outside all spaces in plan - nearest %s (%s) is %d mm away%s"
            % (best_sb.number, best_sb.level_name, _mm(best_d), lvl))


# Method labels (used in the grid AND in the summary counts)
M_ASSOC = u"Association"
M_POINT = u"Point"
M_LVLPT = u"Level point"
M_DROP = u"Drop test"
M_BSCAN = u"Boundary scan"
FOUND_METHODS = (M_ASSOC, M_POINT, M_LVLPT, M_DROP, M_BSCAN)


def find_space(e, phase, index=None, use_bscan=True):
    """Return (space, method) or (None, reason)."""
    # 1) Revit association (matches schedules) - FamilyInstances only
    try:
        if phase is not None:
            sp = e.get_Space(phase)
            if sp:
                return sp, M_ASSOC
    except Exception:
        pass
    try:
        sp = e.get_Space()
        if sp:
            return sp, M_ASSOC
    except Exception:
        pass

    pt = representative_point(e)
    if pt is not None:
        # 2) Point test at the element's own location
        sp = space_at(pt, phase)
        if sp:
            return sp, M_POINT

        # 3) Same XY, 300 mm above the element's level (ceiling-void items)
        lvl_z = element_level_z(e)
        if lvl_z is not None:
            sp = space_at(XYZ(pt.X, pt.Y, lvl_z + 1.0), phase)
            if sp:
                return sp, M_LVLPT

        # 4) Step down from the element, never below its own level
        for d in (1.0, 2.0, 3.0, 4.0, 6.0):
            z = pt.Z - d
            if lvl_z is not None and z < lvl_z:
                break
            sp = space_at(XYZ(pt.X, pt.Y, z), phase)
            if sp:
                return sp, M_DROP

    # 5) Boundary scan: plan boundary + level-to-level-above band
    if use_bscan and index:
        sp = boundary_scan(e, index)
        if sp:
            return sp, M_BSCAN

    if pt is None:
        return None, u"No location"
    return None, u"No space found"


def space_number_name(sp):
    num = u""
    name = u""
    try:
        p = sp.get_Parameter(BuiltInParameter.ROOM_NUMBER)
        num = p.AsString() if p else u""
    except Exception:
        pass
    if not num:
        try:
            num = sp.Number or u""
        except Exception:
            num = u""
    try:
        # ROOM_NAME param = name only (Element.Name returns "Name Number")
        p = sp.get_Parameter(BuiltInParameter.ROOM_NAME)
        name = p.AsString() if p else u""
    except Exception:
        name = u""
    return (num or u"").strip(), (name or u"").strip()


def parse_room_id(number, opts):
    """IP1.04.031 -> OPH-M-04-031. Returns (room_id, error)."""
    s = (number or u"").strip()
    if not s:
        return None, "Space has no number"
    delim = opts["r_delim"] or "."
    n = opts["r_n"]

    if opts["r_take"] == "chars":
        part = s[-n:] if n < len(s) else s
    else:
        segs = s.split(delim)
        if len(segs) < n:
            return None, "Too few segments in '%s'" % s
        part = delim.join(segs[-n:])

    segs = [x.strip() for x in part.split(delim)]
    if len(segs) < 2 or not segs[-2] or not segs[-1]:
        return None, "Cannot split '%s' into level/room" % part
    level, room = segs[-2], segs[-1]

    prefix = (opts["r_prefix"] or u"").strip().rstrip("-")
    if prefix:
        return u"{0}-{1}-{2}".format(prefix, level, room), None
    return u"{0}-{1}".format(level, room), None


# Result categories - every row lands in EXACTLY ONE, so the counts add up
R_FOUND = u"Space found"
R_PARSE = u"Parse fail"        # space found, but its number can't be parsed
R_NOSPACE = u"No space found"  # includes "No location"
R_NOTTYPE = u"Not Type 1/2"


class RoomRow(object):
    def __init__(self, elem):
        self.elem = elem
        self.eid = elem.Id.IntegerValue
        self.space_num = u""
        self.method = u""
        self.result = u""
        self.note = u""
        self.ex_id = u""
        self.ex_name = u""
        self.new_id = None
        self.new_name = None
        self.write_id = None
        self.write_name = None
        self.action = u""       # "Write" / "No change"


def _decide(existing, new, mode):
    """Return the value to write, or None to leave the param alone."""
    if new is None:
        return None
    if mode == "blanks" and existing and not is_na(existing):
        return None
    if existing == new:
        return None
    return new


def build_room_plan(elems, opts):
    phase = get_active_phase()
    mode = opts["r_mode"]          # update / blanks
    # index is always built: boundary scan uses it (if ticked) and the
    # diagnosis of not-found elements always uses it
    index = build_space_index(phase, opts["r_tol_ft"], opts["r_defh_ft"])

    rows = []
    for e in elems:
        rr = RoomRow(e)
        rr.ex_id = get_text(e, ROOMID_PARAM)
        rr.ex_name = get_text(e, ROOMNAME_PARAM)

        if opts["maint_only"] and not is_type_1_or_2(e):
            rr.result = R_NOTTYPE
            rr.method = u"-"
            rr.new_id = NA_VALUE
            rr.new_name = NA_VALUE
        else:
            sp, how = find_space(e, phase, index, opts["r_bscan"])
            rr.method = how
            if sp is None:
                rr.result = R_NOSPACE
                rr.new_id = NA_VALUE
                rr.new_name = NA_VALUE
                try:
                    rr.note = diagnose_no_space(e, index)
                except Exception as ex:
                    rr.note = u"Diagnosis failed: %s" % ex
            else:
                rr.space_num, sp_name = space_number_name(sp)
                rr.new_name = sp_name if sp_name else NA_VALUE
                rid, err = parse_room_id(rr.space_num, opts)
                if err:
                    rr.result = R_PARSE
                    rr.note = err
                    rr.new_id = None          # never write a bad RoomID
                else:
                    rr.result = R_FOUND
                    rr.new_id = rid

        rr.write_id = _decide(rr.ex_id, rr.new_id, mode)
        rr.write_name = _decide(rr.ex_name, rr.new_name, mode)
        rr.action = (u"Write" if (rr.write_id is not None or
                                  rr.write_name is not None) else u"No change")
        rows.append(rr)
    return rows, len(index)


def room_summary(rows, n_idx):
    """Line 1 = where each element ended up (adds up to Total).
    Line 2 = what Apply will actually change."""
    total = len(rows)
    by_res = {}
    by_meth = {}
    for r in rows:
        by_res[r.result] = by_res.get(r.result, 0) + 1
        if r.result in (R_FOUND, R_PARSE):
            by_meth[r.method] = by_meth.get(r.method, 0) + 1
    found = by_res.get(R_FOUND, 0) + by_res.get(R_PARSE, 0)
    meth = u", ".join(u"%s %d" % (m, by_meth[m])
                      for m in FOUND_METHODS if by_meth.get(m))
    line1 = (u"Total %d  =  Space found %d%s  |  No space %d  |  Not Type 1/2 %d"
             % (total, found, (u" [" + meth + u"]") if meth else u"",
                by_res.get(R_NOSPACE, 0), by_res.get(R_NOTTYPE, 0)))

    n_write = sum(1 for r in rows if r.action == u"Write")
    n_id = sum(1 for r in rows if r.write_id is not None)
    n_name = sum(1 for r in rows if r.write_name is not None)
    line2 = (u"Apply will update %d elements (RoomID %d, RoomName %d)  |  "
             u"Already correct / kept %d  |  Parse fail %d  |  %d spaces indexed"
             % (n_write, n_id, n_name, total - n_write,
                by_res.get(R_PARSE, 0), n_idx))
    return line1 + u"\n" + line2


def apply_room_plan(rows):
    n_id = 0
    n_name = 0
    n_elems = 0
    failed = []
    t = Transaction(doc, "NWB Room ID / Name")
    t.Start()
    try:
        for rr in rows:
            ok = True
            if rr.write_id is not None:
                if set_text(rr.elem, ROOMID_PARAM, rr.write_id):
                    n_id += 1
                else:
                    ok = False
            if rr.write_name is not None:
                if set_text(rr.elem, ROOMNAME_PARAM, rr.write_name):
                    n_name += 1
                else:
                    ok = False
            if not ok:
                failed.append(rr.eid)
            elif rr.write_id is not None or rr.write_name is not None:
                n_elems += 1
        t.Commit()
    except Exception as ex:
        t.RollBack()
        raise ex
    return n_elems, n_id, n_name, failed


def room_rows_to_datatable(rows):
    # NB: no "." "/" "[" "]" in column names - WPF binding drops those columns
    dt = DataTable("rooms")
    for col in ("ElementId", "Category", "Result", "Found by", "Diagnosis",
                "Space Number", "RoomID now", "RoomID new", "RoomName now",
                "RoomName new", "Action"):
        dt.Columns.Add(col)
    for rr in sorted(rows, key=lambda x: (x.result, x.note, x.space_num, x.eid)):
        dt.Rows.Add(str(rr.eid), cat_name(rr.elem), rr.result, rr.method,
                    rr.note, rr.space_num, rr.ex_id, rr.new_id or u"",
                    rr.ex_name, rr.new_name or u"", rr.action)
    return dt


def export_room_csv(rows):
    path = _csv_path("NWB_RoomID_preview")
    try:
        f = open(path, "w")
        try:
            f.write("ElementId,Category,Result,FoundBy,Diagnosis,SpaceNumber,"
                    "RoomIDNow,RoomIDNew,RoomNameNow,RoomNameNew,Action\n")
            for rr in sorted(rows, key=lambda x: (x.result, x.note, x.space_num, x.eid)):
                f.write(u",".join([
                    str(rr.eid), _q(cat_name(rr.elem)), _q(rr.result),
                    _q(rr.method), _q(rr.note), _q(rr.space_num),
                    _q(rr.ex_id), _q(rr.new_id or u""), _q(rr.ex_name),
                    _q(rr.new_name or u""), _q(rr.action)]) + "\n")
        finally:
            f.close()
        return path
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Shared small helpers
# ----------------------------------------------------------------------------
def cat_name(elem):
    try:
        return elem.Category.Name if elem.Category else u""
    except Exception:
        return u""


def _q(s):
    return u'"%s"' % (s or u"").replace(u'"', u'""')


def _csv_path(stem):
    fname = "{0}_{1}.csv".format(
        stem, datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    return os.path.join(os.path.expanduser("~"), "Desktop", fname)


# ----------------------------------------------------------------------------
# WPF form (loaded via .NET XamlReader - no 'wpf' module dependency)
# ----------------------------------------------------------------------------
XAML = u"""
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="NWB Asset ID Generator  -  v4 (boundary scan + diagnosis)" Height="760" Width="1000"
        WindowStartupLocation="CenterScreen">
  <Grid Margin="12">
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>

    <!-- SHARED: scope + maintainability filter -->
    <GroupBox Grid.Row="0" Header="Scope (both tabs)">
      <StackPanel Orientation="Horizontal" Margin="6">
        <RadioButton x:Name="scSel" Content="Selected elements" IsChecked="True" Margin="0,0,14,0"/>
        <RadioButton x:Name="scView" Content="Active view" Margin="0,0,14,0"/>
        <RadioButton x:Name="scModel" Content="Whole model" Margin="0,0,30,0"/>
        <CheckBox x:Name="chkMaint" Content="Only Maint. Type 1 &amp; 2 (rest = N/A)" IsChecked="True"/>
      </StackPanel>
    </GroupBox>

    <TabControl Grid.Row="1" Margin="0,8,0,0">

      <!-- ================= TAB 1: ASSET ID ================= -->
      <TabItem Header="  Asset ID  ">
        <Grid Margin="8">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>

          <StackPanel Grid.Row="0" Orientation="Horizontal">
            <GroupBox Header="Building segment" Width="250" Margin="0,0,8,0">
              <StackPanel Margin="6">
                <RadioButton x:Name="bMan" Content="Manual value" IsChecked="True"/>
                <TextBox x:Name="bManVal" Text="M" Margin="18,2,0,6"/>
                <RadioButton x:Name="bPar" Content="Copy from parameter"/>
                <ComboBox x:Name="bParVal" Margin="18,2,0,0"/>
              </StackPanel>
            </GroupBox>
            <GroupBox Header="Level segment" Width="250">
              <StackPanel Margin="6">
                <RadioButton x:Name="lPar" Content="Copy from parameter" IsChecked="True"/>
                <ComboBox x:Name="lParVal" Margin="18,2,0,6"/>
                <RadioButton x:Name="lMan" Content="Manual value"/>
                <TextBox x:Name="lManVal" Text="" Margin="18,2,0,0"/>
              </StackPanel>
            </GroupBox>
          </StackPanel>

          <StackPanel Grid.Row="1" Orientation="Horizontal" Margin="0,8,0,8">
            <GroupBox Header="Sequence" Width="250" Margin="0,0,8,0">
              <StackPanel Orientation="Horizontal" Margin="6">
                <TextBlock Text="Padding:" VerticalAlignment="Center"/>
                <TextBox x:Name="seqPad" Text="4" Width="40" Margin="4,0,16,0"/>
                <TextBlock Text="Start at:" VerticalAlignment="Center"/>
                <TextBox x:Name="seqStart" Text="1" Width="40" Margin="4,0,0,0"/>
              </StackPanel>
            </GroupBox>
            <GroupBox Header="Write mode" Width="400">
              <StackPanel Orientation="Horizontal" Margin="6">
                <RadioButton x:Name="wmBlanks" Content="Fill blanks only" IsChecked="True" Margin="0,0,10,0"/>
                <RadioButton x:Name="wmDupes" Content="Fix dupes only" Margin="0,0,10,0"/>
                <RadioButton x:Name="wmAll" Content="Re-sequence ALL"/>
              </StackPanel>
            </GroupBox>
          </StackPanel>

          <DataGrid Grid.Row="2" x:Name="grid" AutoGenerateColumns="True"
                    IsReadOnly="True" HeadersVisibility="Column" CanUserAddRows="False"/>

          <StackPanel Grid.Row="3" Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,8,0,0">
            <TextBlock x:Name="summary" VerticalAlignment="Center" Margin="0,0,16,0" Foreground="#444"/>
            <Button x:Name="btnPreview" Content="Preview" Width="90" Margin="0,0,8,0"/>
            <Button x:Name="btnCsv" Content="Export CSV" Width="90" Margin="0,0,8,0"/>
            <Button x:Name="btnApply" Content="Apply" Width="90" IsEnabled="False"/>
          </StackPanel>
        </Grid>
      </TabItem>

      <!-- ================= TAB 2: ROOM ID / NAME ================= -->
      <TabItem Header="  Room ID / Name  ">
        <Grid Margin="8">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>

          <StackPanel Grid.Row="0" Orientation="Horizontal">
            <GroupBox Header="Space number parsing" Width="400" Margin="0,0,8,0">
              <StackPanel Margin="6">
                <StackPanel Orientation="Horizontal">
                  <RadioButton x:Name="rSeg" Content="Last N segments" IsChecked="True" Margin="0,0,14,0"/>
                  <RadioButton x:Name="rChr" Content="Last N characters" Margin="0,0,14,0"/>
                  <TextBlock Text="N:" VerticalAlignment="Center"/>
                  <TextBox x:Name="rN" Text="2" Width="36" Margin="4,0,0,0"/>
                </StackPanel>
                <StackPanel Orientation="Horizontal" Margin="0,6,0,0">
                  <TextBlock Text="Delimiter:" VerticalAlignment="Center"/>
                  <TextBox x:Name="rDelim" Text="." Width="30" Margin="4,0,16,0"/>
                  <TextBlock Text="Test:" VerticalAlignment="Center"/>
                  <TextBox x:Name="rTestIn" Text="IP1.04.031" Width="90" Margin="4,0,6,0"/>
                  <TextBlock x:Name="rTestOut" VerticalAlignment="Center" Foreground="#06c" FontWeight="Bold"/>
                </StackPanel>
              </StackPanel>
            </GroupBox>
            <GroupBox Header="RoomID prefix" Width="150">
              <StackPanel Margin="6">
                <TextBox x:Name="rPrefix" Text="OPH-M"/>
                <TextBlock Text="&lt;prefix&gt;-Level-Room" Foreground="#777" FontSize="10" Margin="0,4,0,0"/>
              </StackPanel>
            </GroupBox>
          </StackPanel>

          <StackPanel Grid.Row="1" Orientation="Horizontal" Margin="0,8,0,8">
            <GroupBox Header="Write mode" Width="260" Margin="0,0,8,0">
              <StackPanel Margin="6">
                <RadioButton x:Name="rmUpdate" Content="Update all (follows moved elements)" IsChecked="True"/>
                <RadioButton x:Name="rmBlanks" Content="Fill blanks / N/A only" Margin="0,4,0,0"/>
              </StackPanel>
            </GroupBox>
            <GroupBox Header="Fallback: space boundary scan" Width="420">
              <StackPanel Margin="6">
                <CheckBox x:Name="rBscan" IsChecked="True"
                          Content="Plan boundary, from space level up to level above"/>
                <StackPanel Orientation="Horizontal" Margin="18,6,0,0">
                  <TextBlock Text="Minus tolerance (mm):" VerticalAlignment="Center"/>
                  <TextBox x:Name="rTol" Text="50" Width="45" Margin="4,0,14,0"/>
                  <TextBlock Text="Top floor height (mm):" VerticalAlignment="Center"/>
                  <TextBox x:Name="rDefH" Text="6000" Width="50" Margin="4,0,0,0"/>
                </StackPanel>
              </StackPanel>
            </GroupBox>
          </StackPanel>

          <DataGrid Grid.Row="2" x:Name="rGrid" AutoGenerateColumns="True"
                    IsReadOnly="True" HeadersVisibility="Column" CanUserAddRows="False"
                    SelectionMode="Extended" SelectionUnit="FullRow"/>

          <DockPanel Grid.Row="3" Margin="0,8,0,0" LastChildFill="True">
            <StackPanel DockPanel.Dock="Right" Orientation="Horizontal" VerticalAlignment="Center">
              <Button x:Name="rBtnSelect" Content="Select in Revit" Width="110" Margin="8,0,8,0"
                      ToolTip="Selects the highlighted rows in Revit (or ALL 'No space found' rows if none highlighted), closes this window and zooms to them."/>
              <Button x:Name="rBtnPreview" Content="Preview" Width="90" Margin="0,0,8,0"/>
              <Button x:Name="rBtnCsv" Content="Export CSV" Width="90" Margin="0,0,8,0"/>
              <Button x:Name="rBtnApply" Content="Apply" Width="90" IsEnabled="False"/>
            </StackPanel>
            <TextBlock x:Name="rSummary" TextWrapping="Wrap" VerticalAlignment="Center" Foreground="#444"/>
          </DockPanel>
        </Grid>
      </TabItem>
    </TabControl>

    <StackPanel Grid.Row="2" Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,8,0,0">
      <Button x:Name="btnCancel" Content="Close" Width="90"/>
    </StackPanel>
  </Grid>
</Window>
"""


class AssetIdWindow(object):
    def __init__(self):
        self.window = XamlReader.Load(XmlReader.Create(StringReader(XAML)))
        self._rows = None
        self._room_rows = None
        self.post_select = None
        self._elems = collect_elements("selection")

        n = self.window.FindName
        # shared
        self.scSel = n("scSel"); self.scView = n("scView"); self.scModel = n("scModel")
        self.chkMaint = n("chkMaint")
        self.btnCancel = n("btnCancel")
        # tab 1
        self.bMan = n("bMan"); self.bManVal = n("bManVal")
        self.bPar = n("bPar"); self.bParVal = n("bParVal")
        self.lPar = n("lPar"); self.lParVal = n("lParVal")
        self.lMan = n("lMan"); self.lManVal = n("lManVal")
        self.seqPad = n("seqPad"); self.seqStart = n("seqStart")
        self.wmBlanks = n("wmBlanks"); self.wmDupes = n("wmDupes"); self.wmAll = n("wmAll")
        self.grid = n("grid"); self.summary = n("summary")
        self.btnPreview = n("btnPreview"); self.btnCsv = n("btnCsv")
        self.btnApply = n("btnApply")
        # tab 2
        self.rSeg = n("rSeg"); self.rChr = n("rChr"); self.rN = n("rN")
        self.rDelim = n("rDelim"); self.rPrefix = n("rPrefix")
        self.rTestIn = n("rTestIn"); self.rTestOut = n("rTestOut")
        self.rmUpdate = n("rmUpdate"); self.rmBlanks = n("rmBlanks")
        self.rBscan = n("rBscan"); self.rTol = n("rTol"); self.rDefH = n("rDefH")
        self.rBtnSelect = n("rBtnSelect")
        self.rGrid = n("rGrid"); self.rSummary = n("rSummary")
        self.rBtnPreview = n("rBtnPreview"); self.rBtnCsv = n("rBtnCsv")
        self.rBtnApply = n("rBtnApply")

        params = list_text_params(self._elems) if self._elems else []
        if not params:
            params = [DEFAULT_BUILDING_PARAM, DEFAULT_LEVEL_PARAM]
        for p in params:
            self.bParVal.Items.Add(p)
            self.lParVal.Items.Add(p)
        self._select_combo(self.bParVal, DEFAULT_BUILDING_PARAM)
        self._select_combo(self.lParVal, DEFAULT_LEVEL_PARAM)

        # tab 1 events
        self.btnPreview.Click += self.on_preview
        self.btnApply.Click += self.on_apply
        self.btnCsv.Click += self.on_csv
        self.btnCancel.Click += self.on_cancel

        # tab 2 events
        self.rBtnPreview.Click += self.on_room_preview
        self.rBtnApply.Click += self.on_room_apply
        self.rBtnCsv.Click += self.on_room_csv
        self.rBtnSelect.Click += self.on_room_select
        # live parse test
        for ctl in (self.rN, self.rDelim, self.rPrefix, self.rTestIn):
            ctl.TextChanged += self.on_room_test
        self.rSeg.Checked += self.on_room_test
        self.rChr.Checked += self.on_room_test
        self.on_room_test(None, None)

    def ShowDialog(self):
        return self.window.ShowDialog()

    def Close(self):
        self.window.Close()

    def _select_combo(self, combo, value):
        for i in range(combo.Items.Count):
            if str(combo.Items[i]) == value:
                combo.SelectedIndex = i
                return
        if combo.Items.Count:
            combo.SelectedIndex = 0

    def _scope(self):
        return ("selection" if self.scSel.IsChecked
                else "view" if self.scView.IsChecked else "model")

    # ------------------------------------------------------------------ TAB 1
    def read_opts(self):
        write_mode = ("all" if self.wmAll.IsChecked
                      else "dupes" if self.wmDupes.IsChecked else "blanks")
        try:
            pad = max(1, int(self.seqPad.Text))
        except Exception:
            pad = 4
        try:
            start = max(0, int(self.seqStart.Text))
        except Exception:
            start = 1
        return {
            "scope": self._scope(),
            "maint_only": bool(self.chkMaint.IsChecked),
            "building_mode": "manual" if self.bMan.IsChecked else "param",
            "building_manual": (self.bManVal.Text or "").strip(),
            "building_param": str(self.bParVal.SelectedItem or DEFAULT_BUILDING_PARAM),
            "level_mode": "param" if self.lPar.IsChecked else "manual",
            "level_param": str(self.lParVal.SelectedItem or DEFAULT_LEVEL_PARAM),
            "level_manual": (self.lManVal.Text or "").strip(),
            "seq_pad": pad,
            "seq_start": start,
            "write_mode": write_mode,
        }

    def on_preview(self, sender, args):
        opts = self.read_opts()
        elems = collect_elements(opts["scope"])
        if not elems:
            forms.alert(no_candidates_message(opts["scope"]),
                        title="Nothing to do")
            return
        self._rows = build_plan(elems, opts)
        self.grid.ItemsSource = rows_to_datatable(self._rows).DefaultView

        n_assign = sum(1 for r in self._rows if r.action == "Assign")
        n_na = sum(1 for r in self._rows if r.action == "Write N/A")
        n_keep = sum(1 for r in self._rows if r.action == "Keep")
        n_reseq = sum(1 for r in self._rows if "re-sequence" in r.action.lower())
        n_skip = sum(1 for r in self._rows if r.action.startswith("Skip"))
        self.summary.Text = ("Assign %d | N/A %d | Keep %d | Re-seq %d | Skip %d"
                             % (n_assign, n_na, n_keep, n_reseq, n_skip))
        self.btnApply.IsEnabled = True

    def on_csv(self, sender, args):
        if not self._rows:
            self.on_preview(sender, args)
        if not self._rows:
            return
        path = export_csv(self._rows)
        forms.alert("Saved:\n%s" % path if path else "CSV export failed.",
                    title="Export")

    def on_apply(self, sender, args):
        if not self._rows:
            forms.alert("Run Preview first.", title="Apply")
            return
        written, na_written, stamped, failed = apply_plan(self._rows)
        msg = ("Wrote %d sequential IDs.\nWrote 'N/A' to %d elements.\n"
               "Stamped %d copy-guards." % (written, na_written, stamped))
        if failed:
            msg += "\nFailed on %d elements (read-only param?)." % len(failed)
        forms.alert(msg, title="Done")
        self.Close()

    # ------------------------------------------------------------------ TAB 2
    def read_room_opts(self):
        try:
            n = max(1, int(self.rN.Text))
        except Exception:
            n = 2
        try:
            tol_mm = max(0.0, float(self.rTol.Text))
        except Exception:
            tol_mm = 50.0
        try:
            defh_mm = max(500.0, float(self.rDefH.Text))
        except Exception:
            defh_mm = 6000.0
        return {
            "scope": self._scope(),
            "maint_only": bool(self.chkMaint.IsChecked),
            "r_take": "chars" if self.rChr.IsChecked else "segments",
            "r_n": n,
            "r_delim": (self.rDelim.Text or ".").strip() or ".",
            "r_prefix": self.rPrefix.Text or u"",
            "r_mode": "blanks" if self.rmBlanks.IsChecked else "update",
            "r_bscan": bool(self.rBscan.IsChecked),
            "r_tol_ft": tol_mm * MM_TO_FT,
            "r_defh_ft": defh_mm * MM_TO_FT,
        }

    def on_room_test(self, sender, args):
        try:
            rid, err = parse_room_id(self.rTestIn.Text, self.read_room_opts())
            self.rTestOut.Text = (u"-> " + rid) if rid else (u"-> " + err)
        except Exception:
            pass

    def on_room_preview(self, sender, args):
        opts = self.read_room_opts()
        elems = collect_elements(opts["scope"])
        if not elems:
            forms.alert(no_candidates_message(opts["scope"]),
                        title="Nothing to do")
            return
        self._room_rows, n_idx = build_room_plan(elems, opts)
        self.rGrid.ItemsSource = room_rows_to_datatable(self._room_rows).DefaultView
        self.rSummary.Text = room_summary(self._room_rows, n_idx)
        self.rBtnApply.IsEnabled = True

    def on_room_csv(self, sender, args):
        if not self._room_rows:
            self.on_room_preview(sender, args)
        if not self._room_rows:
            return
        path = export_room_csv(self._room_rows)
        forms.alert("Saved:\n%s" % path if path else "CSV export failed.",
                    title="Export")

    def on_room_apply(self, sender, args):
        if not self._room_rows:
            forms.alert("Run Preview first.", title="Apply")
            return
        n_elems, n_id, n_name, failed = apply_room_plan(self._room_rows)
        msg = ("Updated %d elements.\n  NWB_RoomID written: %d\n"
               "  NWB_RoomName written: %d" % (n_elems, n_id, n_name))
        if failed:
            msg += ("\nFailed on %d elements (param missing / read-only?)."
                    % len(failed))
        forms.alert(msg, title="Done")
        self.Close()

    def on_room_select(self, sender, args):
        """Highlighted rows -> those elements; nothing highlighted -> all
        'No space found' rows. Selection is applied after the window closes."""
        if not self._room_rows:
            forms.alert("Run Preview first.", title="Select in Revit")
            return
        ids = []
        try:
            for item in self.rGrid.SelectedItems:
                ids.append(int(str(item["ElementId"])))
        except Exception:
            ids = []
        if not ids:
            ids = [r.eid for r in self._room_rows if r.result == R_NOSPACE]
        if not ids:
            forms.alert("No 'No space found' elements to select.",
                        title="Select in Revit")
            return
        self.post_select = ids
        self.Close()

    def on_cancel(self, sender, args):
        self.Close()


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    if get_schema() is None:
        forms.alert("Could not initialise Extensible Storage schema.",
                    title="Error")
    else:
        win = AssetIdWindow()
        win.ShowDialog()
        if win.post_select:
            idlist = List[ElementId]()
            for i in win.post_select:
                idlist.Add(ElementId(i))
            uidoc.Selection.SetElementIds(idlist)
            try:
                uidoc.ShowElements(idlist)
            except Exception:
                pass
            forms.alert("%d element(s) selected.\n\nTip: press HI (Isolate "
                        "Element) to view them on their own, then check them "
                        "against the spaces in plan/section." % idlist.Count,
                        title="Select in Revit")
