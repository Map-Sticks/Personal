# -*- coding: utf-8 -*-
__persistentengine__ = True

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

import math
import System
from System.Collections.Generic import List
from System.Windows.Controls import TreeViewItem, CheckBox, StackPanel, TextBlock, TextBox, Orientation, ComboBox, ComboBoxItem, Button, Canvas
from System.Windows.Shapes import Polyline, Ellipse
from System.Windows.Media import Brushes, PointCollection
from System.Windows import Thickness, TextWrapping, Point

from pyrevit import revit, forms, script
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, FamilySymbol, Transaction,
    HostObjectUtils, ShellLayerType, RevitLinkInstance,
    XYZ, BuiltInParameter, LocationCurve, LocationPoint, ElementId, 
    SpatialElementBoundaryOptions, Reference, Line, ElementTransformUtils, Level
)
from Autodesk.Revit.DB.Structure import StructuralType

doc = revit.doc
uidoc = revit.uidoc
XAML_PATH = None

# -----------------------------
# Units & Helpers
# -----------------------------
FT_PER_MM = 1.0 / 304.8
def mm_to_ft(mm): return mm * FT_PER_MM

class LinkedWallReferenceFilter(ISelectionFilter):
    def AllowElement(self, e):
        return isinstance(e, RevitLinkInstance)
    def AllowReference(self, r, p):
        try:
            link_inst = doc.GetElement(r.ElementId)
            link_doc = link_inst.GetLinkDocument()
            linked_elem = link_doc.GetElement(r.LinkedElementId)
            return linked_elem and linked_elem.GetType().Name == "Wall"
        except:
            return False

def param_str(elem, bip):
    try:
        p = elem.get_Parameter(bip)
        if p and p.HasValue: return p.AsString()
    except: pass
    return None

def safe_elem_name(elem):
    return (getattr(elem, 'Name', None) or 
            param_str(elem, BuiltInParameter.ALL_MODEL_TYPE_NAME) or 
            str(elem.Id.IntegerValue))

def symbol_display(sym):
    fname = getattr(sym.Family, 'Name', '<Family?>')
    tname = param_str(sym, BuiltInParameter.SYMBOL_NAME_PARAM) or "<Type?>"
    return u"{} :: {}".format(fname, tname)

def collect_workplane_symbols():
    cats = [BuiltInCategory.OST_ElectricalFixtures]
    syms = []
    for cat in cats:
        for s in FilteredElementCollector(doc).OfClass(FamilySymbol).OfCategory(cat).WhereElementIsElementType():
            syms.append(s)
    return list({s.Id.IntegerValue: s for s in syms}.values())

def ensure_symbol_active(sym):
    if not sym.IsActive:
        sym.Activate()
        doc.Regenerate()

def get_space_point(space):
    try:
        loc = space.Location
        if isinstance(loc, LocationPoint): return loc.Point
    except: pass
    bb = space.get_BoundingBox(None)
    return (bb.Min + bb.Max) * 0.5 if bb else XYZ(0, 0, 0)

# --- NEW: Automatically name walls by Compass Direction ---
def compute_friendly_wall_name(link_inst, linked_wall, space):
    options = SpatialElementBoundaryOptions()
    segments_list = space.GetBoundarySegments(options)
    
    matching_curves = []
    if segments_list:
        for b_list in segments_list:
            for seg in b_list:
                if seg.LinkElementId == linked_wall.Id:
                    matching_curves.append(seg.GetCurve())
                    
    if not matching_curves:
        return u"Unbounded Wall ({})".format(linked_wall.Id.IntegerValue)

    matching_curves.sort(key=lambda x: x.Length, reverse=True)
    c = matching_curves[0]
    length_mm = int(c.Length * 304.8)

    space_pt = get_space_point(space)
    mid_pt = c.Evaluate(0.5, True)
    
    v = (mid_pt - space_pt).Normalize()
    angle = math.atan2(v.X, v.Y) * 180 / math.pi
    
    direction = ""
    if -22.5 <= angle <= 22.5: direction = "North"
    elif 22.5 < angle <= 67.5: direction = "North-East"
    elif 67.5 < angle <= 112.5: direction = "East"
    elif 112.5 < angle <= 157.5: direction = "South-East"
    elif angle > 157.5 or angle <= -157.5: direction = "South"
    elif -157.5 < angle <= -112.5: direction = "South-West"
    elif -112.5 < angle <= -67.5: direction = "West"
    elif -67.5 < angle < -22.5: direction = "North-West"
    
    return u"{} Wall (L: {}mm)".format(direction, length_mm)

# -----------------------------
# Data Models
# -----------------------------
class SpaceData(object):
    def __init__(self, space):
        self.space = space
        self.idint = space.Id.IntegerValue
        self.name = safe_elem_name(space)
        self.number = getattr(space, 'Number', '')
        lvl = doc.GetElement(space.LevelId) if space.LevelId else None
        self.level = lvl.Name if lvl else ""
        self.walls = []  
        # Link-mode cache: which link was last scanned, and the resulting fixtures.
        # Keyed only by link instance id so swapping links forces a fresh scan.
        self.link_scanned_id = None     # int — RevitLinkInstance.Id.IntegerValue
        self.link_fixtures = []         # list[LinkFixtureData]

    def display(self):
        return u"{} - {} ({})".format(self.number, self.name, self.level)

class WallData(object):
    def __init__(self, ref_obj, link_inst_id_int, link_wall_id_int, friendly_name):
        self.ref = ref_obj 
        self.link_inst_id = link_inst_id_int
        self.link_wall_id = link_wall_id_int
        self.friendly_name = friendly_name
        self.checked = True
        self.types = []  

    def display(self):
        return self.friendly_name

class TypeData(object):
    def __init__(self, sym_id_int, label, qty, elev_mm, offset_mm, comments):
        self.symbol_id = sym_id_int
        self.label = label
        self.qty = qty
        self.elev = elev_mm
        self.offset = offset_mm
        self.comments = comments

# -----------------------------
# Link-Fixture Data Model + Helpers
# -----------------------------
# Architects file outlets/switches/data points under various Built-In Categories.
# Cast a wide net so we don't miss the architect's intent.
LINK_FIXTURE_BICS = [
    BuiltInCategory.OST_ElectricalFixtures,
    BuiltInCategory.OST_ElectricalEquipment,
    BuiltInCategory.OST_LightingFixtures,
    BuiltInCategory.OST_LightingDevices,
    BuiltInCategory.OST_DataDevices,
    BuiltInCategory.OST_CommunicationDevices,
    BuiltInCategory.OST_SecurityDevices,
    BuiltInCategory.OST_FireAlarmDevices,
    BuiltInCategory.OST_GenericModel,         # architects sometimes use this
    BuiltInCategory.OST_SpecialityEquipment,  # ditto
]

def _bic_short_name(bic):
    """'OST_ElectricalFixtures' from BuiltInCategory.OST_ElectricalFixtures."""
    return str(bic).rsplit('.', 1)[-1]

class LinkFixtureData(object):
    """One linked-model fixture instance discovered inside a space, with its
    location already transformed into host (this model's) coordinates.

    Z handling: rather than rely on absolute Z (which often differs between
    link and host coordinate systems), we store the host-doc Level we matched
    to (by name) plus the fixture's mounting offset above its floor in the
    LINK doc. Placement uses host_level_id + mounting_offset_ft directly.
    """
    def __init__(self, link_inst_id_int, linked_elem_id_int,
                 family_name, type_name,
                 point_host, facing_host,
                 host_level_id_int, mounting_offset_ft,
                 near_boundary=False, suggested_elev_mm=0.0):
        self.link_inst_id = link_inst_id_int
        self.linked_elem_id = linked_elem_id_int
        self.family_name = family_name
        self.type_name = type_name
        self.point = point_host          # XYZ in host coords (ft) — XY meaningful, Z informational
        self.facing = facing_host        # XYZ direction vector in host coords
        self.host_level_id = host_level_id_int   # matched level in HOST doc
        self.mounting_offset_ft = mounting_offset_ft  # height above floor (ft)
        self.near_boundary = near_boundary  # matched via tolerance, not strict containment
        self.checked = True              # per-instance include/exclude
        self.placed_id = None            # filled in after placement
        self.override_symbol_id = None    # per-fixture mapped FamilySymbol id
        self.override_elev_mm = None      # per-fixture placement height in mm
        self.suggested_elev_mm = suggested_elev_mm  # link fixture height above floor (mm)

    def type_key(self):
        return (self.family_name, self.type_name)

    def short(self):
        return u"{} :: {}".format(self.family_name, self.type_name)

    def display(self):
        return u"{} :: {}  [XY projects to nearest bounding wall; individual height]".format(
            self.family_name, self.type_name)


def get_loaded_link_instances():
    """All RevitLinkInstances whose document is actually loaded."""
    out = []
    for li in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
        try:
            if li.GetLinkDocument() is not None:
                out.append(li)
        except Exception:
            pass
    return out


def link_instance_display(link_inst):
    """Friendly label for the link picker dropdown."""
    name = safe_elem_name(link_inst)
    try:
        ld = link_inst.GetLinkDocument()
        if ld:
            return u"{}  [{}]".format(name, ld.Title)
    except Exception:
        pass
    return name


def _space_vertical_extent_ft(space):
    """Return (floor_z_ft, upper_z_ft) world Z range that defines the space's
    vertical volume. Defaults to space level + 3 m if upper limit is missing."""
    lvl = doc.GetElement(space.LevelId) if space.LevelId else None
    if not lvl:
        return None
    floor_z = lvl.Elevation
    upper_z = floor_z + mm_to_ft(3000.0)

    p_offset = space.get_Parameter(BuiltInParameter.ROOM_UPPER_OFFSET)
    p_upper = space.get_Parameter(BuiltInParameter.ROOM_UPPER_LEVEL)
    try:
        upper_lvl = None
        if p_upper:
            up_id = p_upper.AsElementId()
            if up_id and up_id != ElementId.InvalidElementId:
                upper_lvl = doc.GetElement(up_id)
        if upper_lvl is not None:
            upper_z = upper_lvl.Elevation
            if p_offset and p_offset.HasValue:
                upper_z += p_offset.AsDouble()
        else:
            if p_offset and p_offset.HasValue:
                upper_z = floor_z + p_offset.AsDouble()
    except Exception:
        pass

    if upper_z < floor_z:
        upper_z = floor_z + mm_to_ft(3000.0)
    return floor_z, upper_z


def _space_boundary_polygons_xy(space):
    """Return a list of 2D polygon loops (each polygon = list of (x, y) tuples)
    for the space's boundary in host XY coordinates. Used as a fallback when
    Space.IsPointInSpace fails. Curves are sampled so arcs become polylines."""
    polygons = []
    try:
        loops = space.GetBoundarySegments(SpatialElementBoundaryOptions())
    except Exception:
        return polygons
    if not loops:
        return polygons
    for loop in loops:
        pts = []
        for seg in loop:
            try:
                c = seg.GetCurve()
            except Exception:
                continue
            # Sample 8 points along the curve (handles arcs)
            for i in range(8):
                try:
                    p = c.Evaluate(i / 8.0, True)
                    pts.append((p.X, p.Y))
                except Exception:
                    pass
        if len(pts) >= 3:
            polygons.append(pts)
    return polygons


def _point_in_polygon_xy(px, py, polygon):
    """Standard ray-cast point-in-polygon test in 2D. polygon = list of (x, y)."""
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)):
            denom = (yj - yi)
            if abs(denom) > 1e-12:
                x_cross = (xj - xi) * (py - yi) / denom + xi
                if px < x_cross:
                    inside = not inside
        j = i
    return inside


def _point_in_space_xy_fallback(p_host, polygons):
    """True iff p_host's XY is inside any of the space's boundary polygons."""
    px, py = p_host.X, p_host.Y
    for poly in polygons:
        if _point_in_polygon_xy(px, py, poly):
            return True
    return False


# Tolerance used to catch wall-hosted families whose insertion point sits at
# the wall centerline (or on the far face) — slightly outside the boundary
# polygon. 300 mm covers most realistic wall thicknesses.
BOUNDARY_TOLERANCE_MM = 300.0


def _distance_point_to_segment_xy(px, py, x1, y1, x2, y2):
    """Euclidean distance from (px,py) to the segment (x1,y1)-(x2,y2)."""
    dx = x2 - x1
    dy = y2 - y1
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-12:
        return math.sqrt((px - x1) ** 2 + (py - y1) ** 2)
    t = ((px - x1) * dx + (py - y1) * dy) / len_sq
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return math.sqrt((px - proj_x) ** 2 + (py - proj_y) ** 2)


def _min_distance_point_to_polygons(px, py, polygons):
    """Minimum distance from (px,py) to any edge of any polygon. inf if none."""
    best = float('inf')
    for poly in polygons:
        n = len(poly)
        if n < 2:
            continue
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            d = _distance_point_to_segment_xy(px, py, x1, y1, x2, y2)
            if d < best:
                best = d
                if best <= 0.0:
                    return 0.0
    return best


def _point_near_space_xy(p_host, polygons, tol_ft):
    """True if p_host's XY is within `tol_ft` of any space boundary edge."""
    if not polygons:
        return False
    d = _min_distance_point_to_polygons(p_host.X, p_host.Y, polygons)
    return d <= tol_ft


def scan_link_fixtures_in_space(link_inst, space, host_levels=None, boundary_tolerance_mm=BOUNDARY_TOLERANCE_MM):
    """Find all electrical-family-category fixtures in the linked document
    that belong to `space`. Matching is done by LEVEL NAME (link level name
    == host level name), NOT by absolute Z — because link and host docs often
    use different Z origins.

    Returns (fixtures_list, diagnostic_dict).
    """
    diag = {
        'link_doc_loaded': False,
        'link_doc_title': None,
        'space_id': space.Id.IntegerValue if space else None,
        'space_level_name': None,
        'space_level_elev_mm': None,
        'host_level_names': [],
        'link_level_names': [],
        'per_category_total': {},      # bic_name -> count in link doc
        'with_location_point': 0,
        'no_level_id': 0,              # fixtures with no LevelId — used Z-fallback
        'name_match_failed': 0,        # link level name not in host doc
        'wrong_level': 0,              # right doc, wrong level for this space
        'passed_level_filter': 0,
        'passed_xy_filter': 0,
        'xy_via_fallback': 0,
        'xy_via_tolerance': 0,
        'final': 0,
        'sample_name_match_failed': [],  # (cat, fam, type, link_lvl_name)
        'sample_wrong_level': [],        # (cat, fam, type, matched_host_lvl, z_mm)
        'sample_rejected_xy': [],        # (cat, fam, type, x_mm, y_mm)
        'errors': [],
        'boundary_tolerance_mm': boundary_tolerance_mm,
    }
    out = []

    if link_inst is None or space is None:
        diag['errors'].append("link_inst or space is None")
        return out, diag

    link_doc = link_inst.GetLinkDocument()
    if link_doc is None:
        diag['errors'].append("Link document is not loaded.")
        return out, diag
    diag['link_doc_loaded'] = True
    try:
        diag['link_doc_title'] = link_doc.Title
    except Exception:
        pass

    space_lvl = doc.GetElement(space.LevelId) if space.LevelId else None
    if not space_lvl:
        diag['errors'].append("Space has no Level.")
        return out, diag
    diag['space_level_name'] = space_lvl.Name
    diag['space_level_elev_mm'] = space_lvl.Elevation * 304.8
    space_lvl_id_int = space_lvl.Id.IntegerValue

    # Build host-level name lookups (case-sensitive then case-insensitive).
    if host_levels is None:
        host_levels = get_all_levels_sorted()
    if not host_levels:
        diag['errors'].append("Host doc has no Levels.")
        return out, diag
    diag['host_level_names'] = [l.Name for l in host_levels]
    host_lvl_by_name = {l.Name: l for l in host_levels}
    host_lvl_by_name_lower = {l.Name.lower(): l for l in host_levels}

    def find_host_level(name):
        if not name:
            return None
        if name in host_lvl_by_name:
            return host_lvl_by_name[name]
        return host_lvl_by_name_lower.get(name.lower())

    # Build link-doc level lookups (by Id for direct lookup, sorted list for
    # the Z-nearest fallback when a fixture has no LevelId).
    try:
        link_levels = list(FilteredElementCollector(link_doc).OfClass(Level)
                           .WhereElementIsNotElementType())
    except Exception as ex:
        diag['errors'].append("Could not enumerate link levels: {}".format(ex))
        link_levels = []
    link_levels_sorted = sorted(link_levels, key=lambda l: l.Elevation)
    link_lvl_by_id = {l.Id.IntegerValue: l for l in link_levels}
    diag['link_level_names'] = [
        u"{} (Z={:.0f} mm)".format(l.Name, l.Elevation * 304.8)
        for l in link_levels_sorted
    ]

    tf = link_inst.GetTotalTransform()
    polygons = _space_boundary_polygons_xy(space)
    test_z = space_lvl.Elevation + mm_to_ft(500.0)  # safe interior Z for IsPointInSpace

    seen_ids = set()
    for cat in LINK_FIXTURE_BICS:
        cat_name = _bic_short_name(cat)
        try:
            elems = list(FilteredElementCollector(link_doc)
                         .OfCategory(cat)
                         .WhereElementIsNotElementType()
                         .ToElements())
        except Exception as ex:
            diag['errors'].append(
                "Collector failed for {}: {}".format(cat_name, ex))
            continue
        diag['per_category_total'][cat_name] = len(elems)

        for e in elems:
            try:
                eid_int = e.Id.IntegerValue
                if eid_int in seen_ids:
                    continue
                seen_ids.add(eid_int)

                loc = e.Location
                if not isinstance(loc, LocationPoint):
                    continue
                diag['with_location_point'] += 1

                p_link = loc.Point
                p_host = tf.OfPoint(p_link)

                sym = getattr(e, 'Symbol', None)
                if sym is None:
                    continue
                fname = getattr(sym.Family, 'Name', '<Family?>')
                tname = (param_str(sym, BuiltInParameter.SYMBOL_NAME_PARAM)
                         or '<Type?>')

                # ---- Determine the fixture's link-doc level ----
                link_lvl = None
                link_lvl_id = getattr(e, 'LevelId', None)
                if link_lvl_id and link_lvl_id != ElementId.InvalidElementId:
                    link_lvl = link_lvl_by_id.get(link_lvl_id.IntegerValue)
                if link_lvl is None:
                    # Fallback: nearest link-doc level by Z
                    diag['no_level_id'] += 1
                    if link_levels_sorted:
                        link_lvl = min(link_levels_sorted,
                                       key=lambda l: abs(l.Elevation - p_link.Z))

                if link_lvl is None:
                    # No way to determine a level — skip
                    continue

                # ---- Match link level → host level by NAME ----
                host_lvl = find_host_level(link_lvl.Name)
                if host_lvl is None:
                    diag['name_match_failed'] += 1
                    if len(diag['sample_name_match_failed']) < 6:
                        diag['sample_name_match_failed'].append(
                            (cat_name, fname, tname, link_lvl.Name))
                    continue

                # ---- Does it belong to THIS space's level? ----
                if host_lvl.Id.IntegerValue != space_lvl_id_int:
                    diag['wrong_level'] += 1
                    if len(diag['sample_wrong_level']) < 6:
                        diag['sample_wrong_level'].append(
                            (cat_name, fname, tname, host_lvl.Name,
                             round(p_host.Z * 304.8, 0)))
                    continue
                diag['passed_level_filter'] += 1

                # ---- Mounting offset: height above floor in LINK coords ----
                mounting_offset_ft = p_link.Z - link_lvl.Elevation

                # Read fixture's elevation param for user suggestion
                suggested_elev_mm = 0.0
                try:
                    for bip_e in (BuiltInParameter.INSTANCE_ELEVATION_PARAM,
                                  BuiltInParameter.INSTANCE_FREE_HOST_OFFSET_PARAM):
                        ep = e.get_Parameter(bip_e)
                        if ep and ep.HasValue:
                            val_ft = ep.AsDouble()
                            if abs(val_ft) > 1e-6:
                                suggested_elev_mm = abs(val_ft * 304.8)
                                break
                except Exception:
                    pass
                if abs(suggested_elev_mm) < 1.0:
                    raw_mm = abs(mounting_offset_ft * 304.8)
                    if raw_mm < 5000:
                        suggested_elev_mm = raw_mm

                # ---- XY containment ----
                # Use space's level Z for IsPointInSpace, since the fixture's
                # absolute Z is in a different coord system.
                # Three layers, in order of preference:
                #   1. Strict: IsPointInSpace (Revit's official API)
                #   2. Strict: 2D polygon containment (fallback if #1 misbehaves)
                #   3. Tolerant: within BOUNDARY_TOLERANCE_MM of any boundary
                #      edge — catches wall-hosted families whose LocationPoint
                #      is at the wall centerline or far face.
                xy_ok = False
                via_fallback = False
                near_boundary = False

                test_pt = XYZ(p_host.X, p_host.Y, test_z)
                try:
                    if space.IsPointInSpace(test_pt):
                        xy_ok = True
                except Exception:
                    pass
                if not xy_ok and polygons:
                    if _point_in_space_xy_fallback(p_host, polygons):
                        xy_ok = True
                        via_fallback = True
                if not xy_ok and polygons:
                    if _point_near_space_xy(p_host, polygons,
                                            mm_to_ft(BOUNDARY_TOLERANCE_MM)):
                        xy_ok = True
                        near_boundary = True

                if not xy_ok:
                    if len(diag['sample_rejected_xy']) < 6:
                        diag['sample_rejected_xy'].append(
                            (cat_name, fname, tname,
                             round(p_host.X * 304.8, 0),
                             round(p_host.Y * 304.8, 0)))
                    continue
                diag['passed_xy_filter'] += 1
                if via_fallback:
                    diag['xy_via_fallback'] += 1
                if near_boundary:
                    diag['xy_via_tolerance'] += 1

                # Facing vector — OfVector preserves rotation/mirror, drops translation
                facing_link = getattr(e, 'FacingOrientation', None)
                if facing_link is None:
                    facing_link = XYZ.BasisY
                facing_host = tf.OfVector(facing_link)

                out.append(LinkFixtureData(
                    link_inst.Id.IntegerValue,
                    eid_int,
                    fname, tname,
                    p_host, facing_host,
                    host_lvl.Id.IntegerValue,
                    mounting_offset_ft,
                    near_boundary,
                    suggested_elev_mm=suggested_elev_mm,
                ))
                diag['final'] += 1
            except Exception as ex:
                diag['errors'].append("Per-elem error: {}".format(ex))
                continue
    return out, diag


def _format_scan_diagnostic(diag):
    """Build a multi-line markdown-style diagnostic report for pyRevit's
    output window."""
    lines = []
    lines.append("# Link-Fixture Scan Diagnostic")
    lines.append("")
    lines.append("**Link doc loaded:** {}".format(diag['link_doc_loaded']))
    if diag['link_doc_title']:
        lines.append("**Link doc title:** {}".format(diag['link_doc_title']))
    lines.append("**Space id:** {}".format(diag['space_id']))
    lines.append("**Space level (host):** {}  (elev {} mm)".format(
        diag['space_level_name'], diag['space_level_elev_mm']))
    lines.append("")
    lines.append("## Counts")
    lines.append("- With LocationPoint: **{}**".format(diag['with_location_point']))
    lines.append("- No LevelId (used link-Z fallback): {}".format(diag['no_level_id']))
    lines.append("- Name match failed (link level not found in host): {}".format(
        diag['name_match_failed']))
    lines.append("- Wrong level for this space: {}".format(diag['wrong_level']))
    lines.append("- Passed level filter: **{}**".format(diag['passed_level_filter']))
    lines.append("- Passed XY filter: **{}**  (2D fallback: {}, boundary-tolerance: {})".format(
        diag['passed_xy_filter'], diag['xy_via_fallback'], diag['xy_via_tolerance']))
    lines.append("- **Final fixtures returned: {}**".format(diag['final']))
    lines.append("")
    lines.append("## Levels")
    lines.append("**Host levels:** {}".format(", ".join(diag['host_level_names'])))
    lines.append("")
    lines.append("**Link levels:**")
    for n in diag['link_level_names']:
        lines.append("- {}".format(n))
    lines.append("")
    lines.append("## Per-category totals (entire link doc)")
    if not diag['per_category_total']:
        lines.append("- *(none — collectors returned no elements)*")
    else:
        for cat, n in sorted(diag['per_category_total'].items()):
            lines.append("- `{}`: {}".format(cat, n))
    if diag['sample_name_match_failed']:
        lines.append("")
        lines.append("## Name-match failures (sample)")
        lines.append("*(the link's level name isn't present in your host doc — "
                     "rename a level in one of the models to match, then re-scan)*")
        for row in diag['sample_name_match_failed']:
            lines.append("- {}: {} :: {}  (link level: `{}`)".format(*row))
    if diag['sample_wrong_level']:
        lines.append("")
        lines.append("## On a different level (sample)")
        lines.append("*(these are in the link, but on a level that maps to a "
                     "different host level than this space)*")
        for row in diag['sample_wrong_level']:
            lines.append("- {}: {} :: {}  belongs to host level `{}`  (link Z={} mm)".format(*row))
    if diag['sample_rejected_xy']:
        lines.append("")
        lines.append("## Rejected by XY filter (sample)")
        lines.append("*(right level, but their XY wasn't inside the space)*")
        for row in diag['sample_rejected_xy']:
            lines.append("- {}: {} :: {}  @ ({}, {}) mm".format(*row))
    if diag['errors']:
        lines.append("")
        lines.append("## Errors")
        for e in diag['errors']:
            lines.append("- {}".format(e))
    return "\n".join(lines)



def get_all_levels_sorted():
    """All Levels in the host document, sorted by elevation."""
    lvls = list(FilteredElementCollector(doc).OfClass(Level)
                .WhereElementIsNotElementType())
    lvls.sort(key=lambda l: l.Elevation)
    return lvls


def place_from_link_fixture(lf, target_sym, elevation_mm=300.0):
    """Place a fixture using the manual wall-face method, driven by linked XY.

    Linked Z is ignored. The linked fixture XY is projected to the nearest
    bounding wall of the selected host Space, then placed as a face-hosted
    local family at the user-entered height above the host/reference level.
    """
    lf._last_place_error = None

    host_lvl = doc.GetElement(ElementId(lf.host_level_id))
    if host_lvl is None:
        lf._last_place_error = "No host level found from scanned fixture."
        return None

    link_inst = doc.GetElement(ElementId(lf.link_inst_id))
    if link_inst is None:
        lf._last_place_error = "Link instance not found in host model."
        return None

    link_doc = link_inst.GetLinkDocument()
    if link_doc is None:
        lf._last_place_error = "Link document is not loaded."
        return None

    space = getattr(lf, '_host_space', None)
    if space is None:
        lf._last_place_error = "Internal error: selected Space was not attached before placement."
        return None

    ensure_symbol_active(target_sym)

    try:
        segments_list = space.GetBoundarySegments(SpatialElementBoundaryOptions())
    except Exception as ex:
        lf._last_place_error = "Could not read Space boundary segments: {}".format(ex)
        return None

    if not segments_list:
        lf._last_place_error = "Selected Space has no boundary segments."
        return None

    best = None
    best_dist = float('inf')

    for b_list in segments_list:
        for seg in b_list:
            try:
                link_elem_id = seg.LinkElementId
            except Exception:
                link_elem_id = None
            if link_elem_id is None or link_elem_id == ElementId.InvalidElementId:
                continue
            linked_wall = link_doc.GetElement(link_elem_id)
            if not linked_wall:
                continue
            try:
                if linked_wall.GetType().Name != "Wall":
                    continue
            except Exception:
                continue
            try:
                c = seg.GetCurve()
            except Exception:
                c = None
            if c is None:
                continue
            try:
                z_curve = (c.GetEndPoint(0).Z + c.GetEndPoint(1).Z) * 0.5
            except Exception:
                z_curve = host_lvl.Elevation
            p_project = XYZ(lf.point.X, lf.point.Y, z_curve)
            try:
                res = c.Project(p_project)
            except Exception:
                res = None
            if not res:
                continue
            try:
                p_on_curve = res.XYZPoint
            except Exception:
                try:
                    p_on_curve = res.Point
                except Exception:
                    p_on_curve = None
            if p_on_curve is None:
                continue
            dx = p_on_curve.X - lf.point.X
            dy = p_on_curve.Y - lf.point.Y
            d_xy = math.sqrt(dx * dx + dy * dy)
            if d_xy < best_dist:
                best_dist = d_xy
                best = (c, res, p_on_curve, linked_wall)

    if best is None:
        lf._last_place_error = "No linked wall boundary could be projected from fixture XY."
        return None

    c, res, p_on_curve, linked_wall = best
    try:
        t_norm = c.ComputeNormalizedParameter(res.Parameter)
    except Exception:
        try:
            t_norm = res.Parameter
        except Exception:
            t_norm = 0.5
    try:
        deriv = c.ComputeDerivatives(t_norm, True)
        tangent_host = deriv.BasisX.Normalize()
    except Exception:
        tangent_host = XYZ.BasisX
    try:
        room_inward_normal = XYZ.BasisZ.CrossProduct(tangent_host).Normalize()
    except Exception:
        room_inward_normal = XYZ.BasisY
    try:
        tf = link_inst.GetTotalTransform()
        ext_n_host = tf.OfVector(linked_wall.Orientation).Normalize()
        side = ShellLayerType.Exterior if room_inward_normal.DotProduct(ext_n_host) > 0 else ShellLayerType.Interior
    except Exception:
        side = ShellLayerType.Interior
    try:
        faces = HostObjectUtils.GetSideFaces(linked_wall, side)
    except Exception:
        faces = None
    if not faces or faces.Count == 0:
        other_side = ShellLayerType.Interior if side == ShellLayerType.Exterior else ShellLayerType.Exterior
        try:
            faces = HostObjectUtils.GetSideFaces(linked_wall, other_side)
        except Exception:
            faces = None
    if not faces or faces.Count == 0:
        lf._last_place_error = "Could not get linked wall side face for nearest boundary wall."
        return None
    try:
        host_face_ref = faces[0].CreateLinkReference(link_inst)
    except Exception as ex:
        host_face_ref = None
        lf._last_place_error = "Could not create link face reference: {}".format(ex)
    if not host_face_ref:
        if not getattr(lf, '_last_place_error', None):
            lf._last_place_error = "Could not create host face reference from linked wall."
        return None
    z_ft = host_lvl.Elevation + mm_to_ft(elevation_mm)
    loc = XYZ(p_on_curve.X, p_on_curve.Y, z_ft)
    ref_dir = tangent_host.Negate()
    try:
        inst = doc.Create.NewFamilyInstance(host_face_ref, loc, ref_dir, target_sym)
    except Exception as ex:
        lf._last_place_error = "NewFamilyInstance on linked wall face failed: {}".format(ex)
        return None
    if not inst:
        lf._last_place_error = "NewFamilyInstance returned None."
        return None
    try:
        lvl_id = space.LevelId
        if lvl_id:
            p_sched = inst.get_Parameter(BuiltInParameter.INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM)
            if p_sched and not p_sched.IsReadOnly:
                p_sched.Set(lvl_id)
            p_fam = inst.get_Parameter(BuiltInParameter.FAMILY_LEVEL_PARAM)
            if p_fam and not p_fam.IsReadOnly:
                p_fam.Set(lvl_id)
    except Exception:
        pass
    try:
        p_elev = inst.get_Parameter(BuiltInParameter.INSTANCE_ELEVATION_PARAM)
        if p_elev and not p_elev.IsReadOnly:
            p_elev.Set(mm_to_ft(elevation_mm))
    except Exception:
        pass
    try:
        p_offset = inst.get_Parameter(BuiltInParameter.INSTANCE_FREE_HOST_OFFSET_PARAM)
        if p_offset and not p_offset.IsReadOnly:
            p_offset.Set(0.0)
    except Exception:
        pass
    return inst.Id

# -----------------------------
# External Event Setup
# -----------------------------
class UiRequest(object):
    def __init__(self):
        self.action = None
        self.payload = None

REQ = UiRequest()

class Handler(IExternalEventHandler):
    def Execute(self, uiapp):
        try:
            if REQ.action: REQ.action(REQ.payload)
        finally:
            REQ.action = None
            REQ.payload = None
    def GetName(self): return "PlaceFixtures"

EXTEVENT = ExternalEvent.Create(Handler())

# -----------------------------
# Placement Logic 
# -----------------------------
def place_on_linked_wall_face(link_inst, linked_wall, space, type_data_list, sym_map, level_elev_ft, end_offset_ft, min_spacing_ft):
    placed_ids = List[ElementId]() 
    link_doc = link_inst.GetLinkDocument()
    
    options = SpatialElementBoundaryOptions()
    segments_list = space.GetBoundarySegments(options)
    
    matching_curves = []
    if segments_list:
        for b_list in segments_list:
            for seg in b_list:
                if seg.LinkElementId == linked_wall.Id:
                    matching_curves.append(seg.GetCurve())
                    
    if not matching_curves:
        return placed_ids

    matching_curves.sort(key=lambda x: x.Length, reverse=True)
    c = matching_curves[0] 
    L = c.Length

    intervals = [[0, L]]
    door_ids = linked_wall.FindInserts(True, False, False, False)
    tf = link_inst.GetTotalTransform()
    
    for did in door_ids:
        door = link_doc.GetElement(did)
        if not door or not door.Category: continue
        if door.Category.Id.IntegerValue != int(BuiltInCategory.OST_Doors): continue
        
        width = mm_to_ft(900) 
        for param in [BuiltInParameter.DOOR_WIDTH, BuiltInParameter.GENERIC_WIDTH, BuiltInParameter.FAMILY_WIDTH_PARAM]:
            p = door.get_Parameter(param)
            if p and p.HasValue: 
                width = p.AsDouble()
                break
            if getattr(door, 'Symbol', None):
                p_sym = door.Symbol.get_Parameter(param)
                if p_sym and p_sym.HasValue:
                    width = p_sym.AsDouble()
                    break
        
        loc = door.Location
        if not isinstance(loc, LocationPoint): continue
        pt_host = tf.OfPoint(loc.Point)
        
        res = c.Project(pt_host)
        if not res: continue
        if res.Distance > width: continue 
        
        frac = c.ComputeNormalizedParameter(res.Parameter)
        d = frac * L
        
        buffer = mm_to_ft(50)
        door_start = d - (width / 2.0) - buffer
        door_end = d + (width / 2.0) + buffer
        
        new_intervals = []
        for inv in intervals:
            if door_end <= inv[0] or door_start >= inv[1]:
                new_intervals.append(inv) 
            else:
                if door_start > inv[0]: new_intervals.append([inv[0], door_start])
                if door_end < inv[1]: new_intervals.append([door_end, inv[1]])
        intervals = new_intervals

    min_space_req = mm_to_ft(600)
    valid_intervals = [inv for inv in intervals if (inv[1] - inv[0]) >= min_space_req]
    
    if not valid_intervals:
        return placed_ids 
        
    valid_intervals.sort(key=lambda x: x[1] - x[0], reverse=True)
    best_inv = valid_intervals[0]
    
    seg_start = best_inv[0]
    seg_end = best_inv[1]
    
    transform_mid = c.ComputeDerivatives(0.5, True)
    tangent_mid = transform_mid.BasisX.Normalize()
    room_inward_normal = XYZ.BasisZ.CrossProduct(tangent_mid).Normalize()
    
    ext_n_host = tf.OfVector(linked_wall.Orientation).Normalize()
    side = ShellLayerType.Exterior if room_inward_normal.DotProduct(ext_n_host) > 0 else ShellLayerType.Interior
    faces = HostObjectUtils.GetSideFaces(linked_wall, side)
    
    if not faces or faces.Count == 0:
        other_side = ShellLayerType.Interior if side == ShellLayerType.Exterior else ShellLayerType.Exterior
        faces = HostObjectUtils.GetSideFaces(linked_wall, other_side)
        if not faces or faces.Count == 0: return placed_ids

    host_face_ref = faces[0].CreateLinkReference(link_inst)
    if not host_face_ref: return placed_ids

    usable = (seg_end - seg_start) - (2.0 * end_offset_ft)
    if usable <= 0: return placed_ids

    total_qty = sum([td.qty for td in type_data_list])
    if total_qty <= 0: return placed_ids

    while total_qty > 1 and (usable / float(total_qty + 1)) < min_spacing_ft:
        total_qty -= 1

    seq = []
    remaining = {td: td.qty for td in type_data_list}
    added_count = 0
    while added_count < total_qty:
        added = False
        for td in type_data_list:
            if remaining[td] > 0 and added_count < total_qty:
                seq.append(td)
                remaining[td] -= 1
                added_count += 1
                added = True
        if not added: break

    for i in range(1, total_qty + 1):
        td = seq[i - 1]
        sym = sym_map.get(td.symbol_id)
        if not sym: continue
        
        ensure_symbol_active(sym)
        
        dist_along_usable = end_offset_ft + usable * (float(i) / float(total_qty + 1))
        total_dist = seg_start + dist_along_usable
        
        t_norm = total_dist / L 
        
        transform = c.ComputeDerivatives(t_norm, True)
        p_host = transform.Origin
        tangent_host = transform.BasisX.Normalize()
        
        ref_dir = tangent_host.Negate() 

        z_ft = level_elev_ft + mm_to_ft(td.elev)
        loc = XYZ(p_host.X, p_host.Y, z_ft)

        try:
            inst = doc.Create.NewFamilyInstance(host_face_ref, loc, ref_dir, sym)
            if inst:
                pcom = inst.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if pcom and not pcom.IsReadOnly:
                    pcom.Set(td.comments)
                
                lvl_id = space.LevelId
                if lvl_id:
                    p_sched = inst.get_Parameter(BuiltInParameter.INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM)
                    if p_sched and not p_sched.IsReadOnly:
                        p_sched.Set(lvl_id)
                    
                    p_fam = inst.get_Parameter(BuiltInParameter.FAMILY_LEVEL_PARAM)
                    if p_fam and not p_fam.IsReadOnly:
                        p_fam.Set(lvl_id)

                p_elev = inst.get_Parameter(BuiltInParameter.INSTANCE_ELEVATION_PARAM)
                if p_elev and not p_elev.IsReadOnly:
                    p_elev.Set(mm_to_ft(td.elev))
                    
                p_offset = inst.get_Parameter(BuiltInParameter.INSTANCE_FREE_HOST_OFFSET_PARAM)
                if p_offset and not p_offset.IsReadOnly:
                    p_offset.Set(mm_to_ft(td.offset))
                
                placed_ids.Add(inst.Id)
        except Exception:
            pass 

    return placed_ids

# -----------------------------
# UI Class
# -----------------------------
class PlaceFixturesWindow(forms.WPFWindow):
    def __init__(self):
        xaml = XAML_PATH or script.get_bundle_file("ui.xaml")
        forms.WPFWindow.__init__(self, xaml)

        self._spaces = []
        self._space_map = {}
        self._current_space = None
        self._last_placed_ids = List[ElementId]()

        self._symbols = collect_workplane_symbols()
        self._sym_map = {s.Id.IntegerValue: s for s in self._symbols}
        self._disp_to_symid = {symbol_display(s): s.Id.IntegerValue for s in self._symbols}
        self._family_controls = []

        self._link_instances = []
        self._link_inst_by_id = {}
        self._type_mappings = {}
        self._fixture_row_controls = []
        self._last_placed_link_ids = List[ElementId]()
        self._families_by_name = {}
        for s in self._symbols:
            fname = getattr(s.Family, 'Name', '<Family?>')
            self._families_by_name.setdefault(fname, []).append(s)
        for fname in self._families_by_name:
            self._families_by_name[fname].sort(
                key=lambda x: param_str(x, BuiltInParameter.SYMBOL_NAME_PARAM) or "")
        self._sorted_family_names = sorted(self._families_by_name.keys())

        self._wire_events()

        if hasattr(self, 'spFamilies'):
            self._load_types()
        self._load_spaces()
        if hasattr(self, 'cmbLinkInstance'):
            self._load_link_instances()
        if hasattr(self, 'tvConfig'):
            self._refresh_tree()
        if hasattr(self, 'spLinkFixtures'):
            self._refresh_link_panel()
        self.txtStatus.Text = "Ready. Select a space."

    def _wire_events(self):
        self.btnClose.Click += lambda s,a: self.Close()
        self.btnReloadSpaces.Click += self._on_reload_spaces

        if hasattr(self, 'btnPickWalls'):
            self.btnPickWalls.Click += self._on_pick_walls
        if hasattr(self, 'btnClearWalls'):
            self.btnClearWalls.Click += self._on_clear_walls
        if hasattr(self, 'btnAddTypes'):
            self.btnAddTypes.Click += self._on_add_types
        if hasattr(self, 'btnPlace'):
            self.btnPlace.Click += self._on_place
        if hasattr(self, 'btnUndoPlace'):
            self.btnUndoPlace.Click += self._on_undo_place

        self.cbActiveView.Checked += lambda s,a: self._rebuild_space_list()
        self.cbActiveView.Unchecked += lambda s,a: self._rebuild_space_list()
        self.tbSpaceSearch.TextChanged += lambda s,a: self._rebuild_space_list()
        self.lbSpaces.SelectionChanged += self._on_space_changed

        if hasattr(self, 'tvConfig'):
            self.tvConfig.SelectedItemChanged += self._on_tree_selection_changed
        if hasattr(self, 'canvasWallMap'):
            self.canvasWallMap.SizeChanged += lambda s,a: self._redraw_wall_map()

        if hasattr(self, 'btnScanLink'):
            self.btnScanLink.Click += self._on_scan_link
        if hasattr(self, 'btnRescanLink'):
            self.btnRescanLink.Click += self._on_rescan_link
        if hasattr(self, 'btnLinkCheckAll'):
            self.btnLinkCheckAll.Click += lambda s,a: self._set_all_link_fixtures_checked(True)
        if hasattr(self, 'btnLinkUncheckAll'):
            self.btnLinkUncheckAll.Click += lambda s,a: self._set_all_link_fixtures_checked(False)
        if hasattr(self, 'btnPlaceFromLink'):
            self.btnPlaceFromLink.Click += self._on_place_from_link
        if hasattr(self, 'btnUndoPlaceLink'):
            self.btnUndoPlaceLink.Click += self._on_undo_place_link

        self.Closed += self._on_window_closed

    # -----------------------------
    # Wall mini-map
    # -----------------------------
    def _curve_to_canvas_polyline(self, curve, to_canvas_fn, samples=24):
        """Sample a Revit Curve at `samples`+1 t-values and return a WPF
        Polyline whose Points are the canvas-space (X, flipped Y) of those
        samples. Works uniformly for Line, Arc, etc."""
        pl = Polyline()
        pts = PointCollection()
        for i in range(samples + 1):
            t = i / float(samples)
            try:
                p = curve.Evaluate(t, True)
            except Exception:
                continue
            cx, cy = to_canvas_fn(p.X, p.Y)
            pts.Add(Point(cx, cy))
        pl.Points = pts
        return pl

    def _redraw_wall_map(self):
        """Draw a top-down vector diagram of the current space's picked walls
        on canvasWallMap, with numbered labels (1..N) matching the order of
        walls shown in the tree view."""
        cnv = self.canvasWallMap
        cnv.Children.Clear()

        if not self._current_space:
            return
        walls = self._current_space.walls

        # Show a placeholder if nothing to draw yet
        if not walls:
            tb = TextBlock()
            tb.Text = "(No walls picked for this space yet)"
            tb.Foreground = Brushes.Gray
            tb.FontStyle = System.Windows.FontStyles.Italic
            Canvas.SetLeft(tb, 10)
            Canvas.SetTop(tb, 10)
            cnv.Children.Add(tb)
            return

        space = self._current_space.space

        # ---- collect curves ----
        # outline_curves : every boundary segment of the space (light grey context)
        # wall_to_curves : link-wall-id -> [curves]   (for picked walls)
        outline_curves = []
        wall_to_curves = {}
        try:
            seg_lists = space.GetBoundarySegments(SpatialElementBoundaryOptions())
        except Exception:
            seg_lists = None

        if seg_lists:
            for loop in seg_lists:
                for seg in loop:
                    try:
                        c = seg.GetCurve()
                        outline_curves.append(c)
                        if seg.LinkElementId != ElementId.InvalidElementId:
                            wid = seg.LinkElementId.IntegerValue
                            wall_to_curves.setdefault(wid, []).append(c)
                    except Exception:
                        pass

        # For each picked wall, pick the longest matching boundary segment.
        # If no boundary segment matches (e.g. the wall doesn't actually bound
        # the space), fall back to the wall's location curve transformed to
        # host coords so the wall still appears on the map.
        items = []
        for idx, wd in enumerate(walls, start=1):
            curves = wall_to_curves.get(wd.link_wall_id, [])
            chosen = None
            if curves:
                chosen = sorted(curves, key=lambda c: c.Length, reverse=True)[0]
            else:
                try:
                    link_inst = doc.GetElement(ElementId(wd.link_inst_id))
                    if link_inst:
                        link_doc = link_inst.GetLinkDocument()
                        if link_doc:
                            lw = link_doc.GetElement(ElementId(wd.link_wall_id))
                            if lw and isinstance(lw.Location, LocationCurve):
                                chosen = lw.Location.Curve.CreateTransformed(
                                    link_inst.GetTotalTransform())
                except Exception:
                    chosen = None
            if chosen is not None:
                items.append((idx, wd, chosen))

        if not items and not outline_curves:
            return

        # ---- compute world bounds ----
        all_pts = []
        for c in outline_curves:
            for t in (0.0, 0.25, 0.5, 0.75, 1.0):
                try:
                    p = c.Evaluate(t, True)
                    all_pts.append((p.X, p.Y))
                except Exception:
                    pass
        for _, _, c in items:
            for t in (0.0, 0.5, 1.0):
                try:
                    p = c.Evaluate(t, True)
                    all_pts.append((p.X, p.Y))
                except Exception:
                    pass
        if not all_pts:
            return

        xs = [p[0] for p in all_pts]
        ys = [p[1] for p in all_pts]
        minx, maxx = min(xs), max(xs)
        miny, maxy = min(ys), max(ys)
        w = maxx - minx
        h = maxy - miny
        if w < 1e-6: w = 1.0
        if h < 1e-6: h = 1.0

        # ---- map world -> canvas ----
        cw = cnv.ActualWidth if cnv.ActualWidth > 0 else 320
        ch_ = cnv.ActualHeight if cnv.ActualHeight > 0 else 200
        padding = 28
        avail_w = max(1.0, cw - 2 * padding)
        avail_h = max(1.0, ch_ - 2 * padding)
        scale = min(avail_w / w, avail_h / h)
        draw_w = w * scale
        draw_h = h * scale
        off_x = (cw - draw_w) / 2.0
        off_y = (ch_ - draw_h) / 2.0

        def to_canvas(wx, wy):
            cx = (wx - minx) * scale + off_x
            cy = ch_ - ((wy - miny) * scale + off_y)
            return cx, cy

        # ---- draw space outline (light grey) ----
        for c in outline_curves:
            try:
                pl = self._curve_to_canvas_polyline(c, to_canvas, samples=12)
                pl.Stroke = Brushes.LightGray
                pl.StrokeThickness = 1.0
                cnv.Children.Add(pl)
            except Exception:
                pass

        # ---- centre of space (used to push labels outward) ----
        try:
            sp_pt = get_space_point(space)
            sp_x, sp_y = sp_pt.X, sp_pt.Y
        except Exception:
            sp_x, sp_y = (minx + maxx) * 0.5, (miny + maxy) * 0.5

        # ---- draw each picked wall + numbered label ----
        for idx, wd, curve in items:
            try:
                pl = self._curve_to_canvas_polyline(curve, to_canvas, samples=24)
                if wd.checked:
                    pl.Stroke = Brushes.OrangeRed
                    pl.StrokeThickness = 4.0
                else:
                    pl.Stroke = Brushes.Gray
                    pl.StrokeThickness = 2.0
                cnv.Children.Add(pl)

                # midpoint of the wall in world coords
                try:
                    mid = curve.Evaluate(0.5, True)
                except Exception:
                    mid = curve.GetEndPoint(0)
                mx_canvas, my_canvas = to_canvas(mid.X, mid.Y)

                # outward direction = from space centre toward wall midpoint
                ox = mid.X - sp_x
                oy = mid.Y - sp_y
                olen = math.sqrt(ox * ox + oy * oy)
                if olen > 1e-6:
                    ox /= olen
                    oy /= olen
                else:
                    ox, oy = 1.0, 0.0

                offset_px = 16
                # canvas Y is flipped: a +Y world offset becomes a -Y canvas offset
                lbl_x = mx_canvas + ox * offset_px
                lbl_y = my_canvas - oy * offset_px

                r = 11
                circ = Ellipse()
                circ.Width = r * 2
                circ.Height = r * 2
                circ.Fill = Brushes.White
                circ.Stroke = Brushes.OrangeRed if wd.checked else Brushes.Gray
                circ.StrokeThickness = 2.0
                Canvas.SetLeft(circ, lbl_x - r)
                Canvas.SetTop(circ, lbl_y - r)
                cnv.Children.Add(circ)

                num = TextBlock()
                num.Text = str(idx)
                num.FontWeight = System.Windows.FontWeights.Bold
                num.FontSize = 11
                num.Foreground = Brushes.OrangeRed if wd.checked else Brushes.Gray
                num.TextAlignment = System.Windows.TextAlignment.Center
                num.Width = r * 2
                Canvas.SetLeft(num, lbl_x - r)
                Canvas.SetTop(num, lbl_y - r + 1)
                cnv.Children.Add(num)
            except Exception:
                pass

    def _make_wall_check_handler(self, wd, value):
        """Closure-safe handler for wall CheckBox toggles. Also refreshes the
        mini-map so the orange/grey state matches."""
        def _h(s, a):
            wd.checked = value
            self._redraw_wall_map()
        return _h

    def _on_window_closed(self, s, a):
        """Clear any highlight we left in the active view when the tool exits."""
        def act(ui):
            try:
                uidoc.Selection.SetElementIds(List[ElementId]())
            except Exception:
                pass
        REQ.action = act
        REQ.payload = self
        EXTEVENT.Raise()

    def _highlight_current_space_walls(self):
        """Set Revit's selection to all walls of the current space, giving each
        of them Revit's native selection highlight (cyan/blue) in the active
        view. Persists until the user clicks elsewhere in the model.

        Note: Revit's View.SetElementOverrides does not accept linked element
        IDs, so a per-element coloured override is not available for linked
        walls — selection is the supported way to visually distinguish them.
        """
        def act(ui):
            try:
                refs = List[Reference]()
                if ui._current_space:
                    for wd in ui._current_space.walls:
                        if wd.ref is not None:
                            try:
                                refs.Add(wd.ref)
                            except Exception:
                                pass
                if refs.Count > 0:
                    uidoc.Selection.SetReferences(refs)
                else:
                    uidoc.Selection.SetElementIds(List[ElementId]())
            except Exception:
                pass
        REQ.action = act
        REQ.payload = self
        EXTEVENT.Raise()

    def _set_status(self, text):
        self.Dispatcher.Invoke(System.Action(lambda: setattr(self.txtStatus, "Text", text)))

    def _load_types(self):
        self.spFamilies.Children.Clear()
        self._family_controls = []
        
        families = {}
        for s in self._symbols:
            fname = getattr(s.Family, 'Name', '<Family?>')
            if fname not in families:
                families[fname] = []
            families[fname].append(s)
            
        for fname in sorted(families.keys()):
            panel = StackPanel()
            panel.Orientation = Orientation.Horizontal
            panel.Margin = Thickness(0, 2, 0, 2)
            
            cb = CheckBox()
            cb.VerticalAlignment = System.Windows.VerticalAlignment.Center
            cb.Margin = Thickness(0, 0, 8, 0)
            
            lbl = TextBlock()
            lbl.Text = fname
            lbl.Width = 140
            lbl.TextWrapping = TextWrapping.Wrap
            lbl.Margin = Thickness(0, 0, 8, 0)
            lbl.VerticalAlignment = System.Windows.VerticalAlignment.Center
            
            cmb = ComboBox()
            cmb.Width = 140
            cmb.VerticalAlignment = System.Windows.VerticalAlignment.Center
            
            types = sorted(families[fname], key=lambda x: param_str(x, BuiltInParameter.SYMBOL_NAME_PARAM) or "")
            for t in types:
                tname = param_str(t, BuiltInParameter.SYMBOL_NAME_PARAM) or "<Type?>"
                cbi = ComboBoxItem()
                cbi.Content = tname
                cbi.Tag = t.Id.IntegerValue
                cmb.Items.Add(cbi)
                
            if cmb.Items.Count > 0:
                cmb.SelectedIndex = 0
                
            panel.Children.Add(cb)
            panel.Children.Add(lbl)
            panel.Children.Add(cmb)
            
            self.spFamilies.Children.Add(panel)
            self._family_controls.append((cb, cmb))

    def _load_spaces(self):
        self._spaces = []
        for sp in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_MEPSpaces).WhereElementIsNotElementType():
            self._spaces.append(SpaceData(sp))
        self._spaces.sort(key=lambda x: (x.number, x.name))
        self._rebuild_space_list()

    def _rebuild_space_list(self):
        f = (self.tbSpaceSearch.Text or "").strip().lower()
        filter_view = bool(self.cbActiveView.IsChecked)

        view_space_ids = set()
        if filter_view and doc.ActiveView:
            try:
                view_space_ids = {e.Id.IntegerValue for e in FilteredElementCollector(doc, doc.ActiveView.Id).OfCategory(BuiltInCategory.OST_MEPSpaces)}
            except:
                pass

        self.lbSpaces.Items.Clear()
        self._space_map = {}
        for sd in self._spaces:
            if filter_view and sd.idint not in view_space_ids: 
                continue
            
            disp = sd.display()
            if f and (f not in disp.lower()): continue
            
            self.lbSpaces.Items.Add(disp)
            self._space_map[disp] = sd

    def _on_reload_spaces(self, s, a):
        self._load_spaces()
        if hasattr(self, 'tvConfig'):
            self._refresh_tree()
        if hasattr(self, 'spLinkFixtures'):
            self._refresh_link_panel()
        self.txtStatus.Text = "Spaces reloaded."

    def _on_space_changed(self, s, a):
        disp = self.lbSpaces.SelectedItem
        if disp and disp in self._space_map:
            self._current_space = self._space_map[disp]
            if hasattr(self, 'tvConfig'):
                self._refresh_tree()
            if hasattr(self, 'spLinkFixtures'):
                self._refresh_link_panel()
            if hasattr(self, 'canvasWallMap'):
                self._highlight_current_space_walls()

            if hasattr(self._current_space, 'walls') and len(self._current_space.walls):
                self.txtStatus.Text = "Selected space: {} ({} wall(s) highlighted)".format(
                    disp, len(self._current_space.walls))
            else:
                self.txtStatus.Text = "Selected space: {}".format(disp)

    def _on_tree_selection_changed(self, s, a):
        item = self.tvConfig.SelectedItem
        if not item: return
        tag = item.Tag

        if isinstance(tag, WallData) or isinstance(tag, SpaceData):
            def highlight(ui):
                try:
                    if isinstance(tag, SpaceData):
                        ids = List[ElementId]()
                        ids.Add(ElementId(tag.idint))
                        uidoc.Selection.SetElementIds(ids)
                    elif isinstance(tag, WallData):
                        refs = List[Reference]()
                        refs.Add(tag.ref)
                        uidoc.Selection.SetReferences(refs)
                except Exception:
                    pass

            REQ.action = highlight
            REQ.payload = self
            EXTEVENT.Raise()

    def _on_pick_walls(self, s, a):
        if not self._current_space: return
        self.txtStatus.Text = "Pick linked walls, then Finish."
        self.Hide()

        def act(ui):
            try:
                refs = uidoc.Selection.PickObjects(ObjectType.LinkedElement, LinkedWallReferenceFilter(), "Pick walls (Finish)")
                ui.Dispatcher.Invoke(System.Action(lambda: ui._finish_pick_walls(refs, "Picked {} wall(s).".format(len(refs)))))
            except:
                ui.Dispatcher.Invoke(System.Action(lambda: ui._finish_pick_walls(None, "Pick Walls cancelled.")))
        
        REQ.action = act
        REQ.payload = self
        EXTEVENT.Raise()
        
    def _finish_pick_walls(self, refs, msg):
        self.Show()
        self.Activate()
        self.txtStatus.Text = msg
        if refs and self._current_space:
            self._current_space.walls = []
            for r in refs: 
                try:
                    link_inst = doc.GetElement(r.ElementId)
                    linked_wall = link_inst.GetLinkDocument().GetElement(r.LinkedElementId)
                    fname = compute_friendly_wall_name(link_inst, linked_wall, self._current_space.space)
                except Exception:
                    fname = u"Unknown Wall ({})".format(r.LinkedElementId.IntegerValue)
                self._current_space.walls.append(WallData(r, r.ElementId.IntegerValue, r.LinkedElementId.IntegerValue, fname))
            self._refresh_tree()
            self._highlight_current_space_walls()

    def _on_clear_walls(self, s, a):
        if self._current_space:
            self._current_space.walls = []
            self._refresh_tree()
            self._highlight_current_space_walls()
            self.txtStatus.Text = "Cleared walls."

    def _on_add_types(self, s, a):
        if not self._current_space or not self._current_space.walls: return
        
        sel_ids = []
        for cb, cmb in self._family_controls:
            if cb.IsChecked:
                sel_item = cmb.SelectedItem
                if sel_item:
                    sel_ids.append(sel_item.Tag)
                    
        if not sel_ids:
            self.txtStatus.Text = "Check at least one family to add."
            return

        try: qty = max(0, int(self.tbDefaultQty.Text))
        except: qty = 1
        
        try: elev = float(self.tbElevation.Text)
        except: elev = 300.0

        try: offset = float(self.tbHostOffset.Text)
        except: offset = 0.0

        cmt = self.tbComments.Text or ""

        affected = 0
        for w in self._current_space.walls:
            if w.checked:
                for sid in sel_ids:
                    w.types.append(TypeData(sid, symbol_display(self._sym_map[sid]), qty, elev, offset, cmt))
                affected += 1

        self._refresh_tree()
        self.txtStatus.Text = "Added type(s) to {} wall(s).".format(affected)

    def _on_place(self, s, a):
        def act(ui):
            try: end_ft = mm_to_ft(float(ui.tbEndOffset.Text))
            except: end_ft = mm_to_ft(300.0)
            try: min_ft = mm_to_ft(float(ui.tbMinSpacing.Text))
            except: min_ft = mm_to_ft(250.0)

            all_placed_ids = List[ElementId]() 
            
            tx = Transaction(doc, "Place Fixtures Individually")
            tx.Start()
            try:
                for spd in ui._spaces:
                    if not spd.walls: continue
                    lvl = doc.GetElement(spd.space.LevelId) if spd.space.LevelId else None
                    level_elev = lvl.Elevation if lvl else 0.0

                    for wd in spd.walls:
                        if not wd.checked: continue
                        valid_types = [td for td in wd.types if td.qty > 0]
                        if not valid_types: continue

                        link_inst = doc.GetElement(ElementId(wd.link_inst_id))
                        linked_wall = link_inst.GetLinkDocument().GetElement(ElementId(wd.link_wall_id)) if link_inst else None
                        
                        if link_inst and linked_wall:
                            new_ids = place_on_linked_wall_face(link_inst, linked_wall, spd.space, valid_types, ui._sym_map, level_elev, end_ft, min_ft)
                            if new_ids.Count > 0:
                                all_placed_ids.AddRange(new_ids)
            finally:
                tx.Commit()
            
            ui._last_placed_ids = all_placed_ids
            ui.Dispatcher.Invoke(System.Action(lambda: setattr(ui.btnUndoPlace, "IsEnabled", all_placed_ids.Count > 0)))

            if all_placed_ids.Count > 0:
                uidoc.Selection.SetElementIds(all_placed_ids)

            ui._set_status("Placed {} fixtures.".format(all_placed_ids.Count))

        REQ.action = act
        REQ.payload = self
        EXTEVENT.Raise()
        
    def _on_undo_place(self, s, a):
        if not self._last_placed_ids or self._last_placed_ids.Count == 0:
            return
            
        def act(ui):
            tx = Transaction(doc, "Undo Last Fixture Placement")
            tx.Start()
            deleted = 0
            try:
                for eid in ui._last_placed_ids:
                    if doc.GetElement(eid):
                        doc.Delete(eid)
                        deleted += 1
            finally:
                tx.Commit()
            
            ui._last_placed_ids.Clear()
            ui.Dispatcher.Invoke(System.Action(lambda: setattr(ui.btnUndoPlace, "IsEnabled", False)))
            ui._set_status("Removed {} previously placed fixtures.".format(deleted))
            
        REQ.action = act
        REQ.payload = self
        EXTEVENT.Raise()

    def _refresh_tree(self):
        self.tvConfig.Items.Clear()
        if not self._current_space: return

        root = TreeViewItem()
        root.Header = u"SPACE: {} {}".format(self._current_space.number, self._current_space.name)
        root.IsExpanded = True
        root.Tag = self._current_space 

        for idx, wd in enumerate(self._current_space.walls, start=1):
            w_item = TreeViewItem()
            w_item.IsExpanded = True
            w_item.Tag = wd 
            
            cb = CheckBox()
            cb.Content = u"{}. {}".format(idx, wd.display())
            cb.IsChecked = wd.checked
            cb.Checked += self._make_wall_check_handler(wd, True)
            cb.Unchecked += self._make_wall_check_handler(wd, False)
            w_item.Header = cb

            for td in wd.types:
                t_item = TreeViewItem()
                t_item.Tag = td
                
                panel = StackPanel()
                panel.Orientation = Orientation.Horizontal
                panel.Margin = Thickness(0,2,0,2)

                cmb_type = ComboBox()
                cmb_type.Width = 160
                cmb_type.Margin = Thickness(0,0,10,0)
                cmb_type.VerticalAlignment = System.Windows.VerticalAlignment.Center

                for sym in sorted(self._symbols, key=lambda x: symbol_display(x)):
                    cbi = ComboBoxItem()
                    cbi.Content = symbol_display(sym)
                    cbi.Tag = sym.Id.IntegerValue
                    cmb_type.Items.Add(cbi)
                    if sym.Id.IntegerValue == td.symbol_id:
                        cmb_type.SelectedItem = cbi

                def make_type_changed_handler(tdata):
                    def _h(sender, args):
                        sel = sender.SelectedItem
                        if sel:
                            tdata.symbol_id = sel.Tag
                            tdata.label = sel.Content
                    return _h

                cmb_type.SelectionChanged += make_type_changed_handler(td)

                txt_qty = TextBox()
                txt_qty.Text = str(td.qty)
                txt_qty.Width = 30
                txt_qty.Margin = Thickness(0,0,10,0)
                txt_qty.LostFocus += lambda s,a,t=td: setattr(t, 'qty', max(0, int(s.Text)) if s.Text.isdigit() else 0)

                lbl_elev = TextBlock()
                lbl_elev.Text = "Z:"
                lbl_elev.VerticalAlignment = System.Windows.VerticalAlignment.Center
                txt_elev = TextBox()
                txt_elev.Text = str(td.elev)
                txt_elev.Width = 40
                txt_elev.Margin = Thickness(0,0,10,0)
                txt_elev.LostFocus += lambda s,a,t=td: setattr(t, 'elev', float(s.Text) if s.Text.replace('.','',1).isdigit() else t.elev)

                lbl_off = TextBlock()
                lbl_off.Text = "Off:"
                lbl_off.VerticalAlignment = System.Windows.VerticalAlignment.Center
                txt_off = TextBox()
                txt_off.Text = str(td.offset)
                txt_off.Width = 35
                txt_off.Margin = Thickness(0,0,10,0)
                txt_off.LostFocus += lambda s,a,t=td: setattr(t, 'offset', float(s.Text) if s.Text.replace('.','',1).replace('-','',1).isdigit() else t.offset)

                lbl_cmt = TextBlock()
                lbl_cmt.Text = "Cmt:"
                lbl_cmt.VerticalAlignment = System.Windows.VerticalAlignment.Center
                txt_cmt = TextBox()
                txt_cmt.Text = td.comments
                txt_cmt.Width = 100
                txt_cmt.LostFocus += lambda s,a,t=td: setattr(t, 'comments', s.Text)
                
                btn_remove = Button()
                btn_remove.Content = "X"
                btn_remove.Width = 20
                btn_remove.Margin = Thickness(10,0,0,0)
                btn_remove.Foreground = Brushes.Red
                btn_remove.FontWeight = System.Windows.FontWeights.Bold
                
                def make_remove_handler(wall_data, type_data):
                    def _h(sender, args):
                        if type_data in wall_data.types:
                            wall_data.types.remove(type_data)
                            self._refresh_tree()
                    return _h
                    
                btn_remove.Click += make_remove_handler(wd, td)

                panel.Children.Add(cmb_type) 
                panel.Children.Add(TextBlock(Text="Qty:", VerticalAlignment=System.Windows.VerticalAlignment.Center))
                panel.Children.Add(txt_qty)
                panel.Children.Add(lbl_elev)
                panel.Children.Add(txt_elev)
                panel.Children.Add(lbl_off)
                panel.Children.Add(txt_off)
                panel.Children.Add(lbl_cmt)
                panel.Children.Add(txt_cmt)
                panel.Children.Add(btn_remove) 

                t_item.Header = panel
                w_item.Items.Add(t_item)

            root.Items.Add(w_item)
        self.tvConfig.Items.Add(root)
        self._redraw_wall_map()

    # =================================================================
    # Link-mode methods (Tab 2: From Linked Model)
    # =================================================================
    def _load_link_instances(self):
        """Populate the link-instance combobox with all loaded RevitLinkInstances."""
        self.cmbLinkInstance.Items.Clear()
        self._link_instances = get_loaded_link_instances()
        self._link_inst_by_id = {li.Id.IntegerValue: li for li in self._link_instances}
        for li in self._link_instances:
            cbi = ComboBoxItem()
            cbi.Content = link_instance_display(li)
            cbi.Tag = li.Id.IntegerValue
            self.cmbLinkInstance.Items.Add(cbi)
        if self.cmbLinkInstance.Items.Count > 0:
            self.cmbLinkInstance.SelectedIndex = 0
        else:
            # no loaded links — disable scan controls
            self.btnScanLink.IsEnabled = False
            self.btnRescanLink.IsEnabled = False
            self.txtLinkStatus.Text = "No loaded Revit links found in this model."

    def _selected_link_instance(self):
        sel = self.cmbLinkInstance.SelectedItem
        if not sel:
            return None
        return self._link_inst_by_id.get(sel.Tag)

    def _refresh_link_panel(self):
        """Repaint both the mappings list and the per-space fixtures list,
        based on the current space's cached scan results."""
        self._render_link_mappings()
        self._render_link_fixtures()

    # -----------------------------
    # Scan handlers
    # -----------------------------
    def _on_scan_link(self, s, a):
        self._scan_current_space(force=False)

    def _on_rescan_link(self, s, a):
        self._scan_current_space(force=True)

    def _scan_current_space(self, force):
        if not self._current_space:
            self.txtLinkStatus.Text = "Pick a space from the list first."
            return
        link_inst = self._selected_link_instance()
        if not link_inst:
            self.txtLinkStatus.Text = "No linked model selected."
            return

        tol_mm = 300.0
        try:
            tol_mm = float(self.tbLinkScanTolerance.Text)
        except Exception:
            tol_mm = 300.0
        if tol_mm < 0:
            tol_mm = 0.0

        if (not force
                and self._current_space.link_scanned_id == link_inst.Id.IntegerValue
                and getattr(self._current_space, 'link_scan_tolerance_mm', None) == tol_mm
                and self._current_space.link_fixtures):
            self.txtLinkStatus.Text = "Showing cached scan ({} fixtures). Re-scan to refresh.".format(len(self._current_space.link_fixtures))
            self._refresh_link_panel()
            return

        self.txtLinkStatus.Text = "Scanning..."
        space_data = self._current_space

        def act(ui):
            try:
                results, diag = scan_link_fixtures_in_space(link_inst, space_data.space, boundary_tolerance_mm=tol_mm)
            except Exception as ex:
                ui.Dispatcher.Invoke(System.Action(lambda: setattr(ui.txtLinkStatus, "Text", "Scan failed: {}".format(ex))))
                return

            if not results:
                try:
                    outw = script.get_output()
                    outw.print_md(_format_scan_diagnostic(diag))
                except Exception:
                    pass

            def update_ui():
                space_data.link_scanned_id = link_inst.Id.IntegerValue
                space_data.link_scan_tolerance_mm = tol_mm
                space_data.link_fixtures = results
                ui._update_type_mappings_from_scan(results)
                ui._refresh_link_panel()
                ui.txtLinkStatus.Text = "Scan found {} fixture(s) within selected space (tolerance: {:.0f} mm).".format(len(results), tol_mm)

            ui.Dispatcher.Invoke(System.Action(update_ui))

        REQ.action = act
        REQ.payload = self
        EXTEVENT.Raise()

    def _render_link_mappings(self):
        sp = self.spLinkMappings
        sp.Children.Clear()
        if not self._current_space or not self._current_space.link_fixtures:
            tb = TextBlock()
            tb.Text = "(Scan a space to populate this list)"
            tb.Foreground = Brushes.Gray
            tb.FontStyle = System.Windows.FontStyles.Italic
            tb.Margin = Thickness(4, 4, 4, 4)
            sp.Children.Add(tb)
            return

        # Build a count-per-type breakdown for the CURRENT space's fixtures.
        counts = {}
        order = []
        for lf in self._current_space.link_fixtures:
            k = lf.type_key()
            if k not in counts:
                counts[k] = 0
                order.append(k)
            counts[k] += 1

        # Header row — uses the same StackPanel/width layout as the rows below
        # so columns line up visually.
        hdr = StackPanel()
        hdr.Orientation = Orientation.Horizontal
        hdr.Margin = Thickness(2, 2, 2, 6)

        def hdr_text(txt, width):
            t = TextBlock()
            t.Text = txt
            t.FontWeight = System.Windows.FontWeights.Bold
            t.Foreground = Brushes.DimGray
            if width is not None:
                t.Width = width
            return t

        hdr.Children.Add(hdr_text("Link Family :: Type", 260))
        hdr.Children.Add(hdr_text("Count", 50))
        hdr.Children.Add(hdr_text(u" \u2192 ", None))
        hdr.Children.Add(hdr_text("Your Family", 200))
        hdr.Children.Add(hdr_text("Your Type", 180))
        sp.Children.Add(hdr)

        for (fam, typ) in order:
            row_panel = StackPanel()
            row_panel.Orientation = Orientation.Horizontal
            row_panel.Margin = Thickness(2, 2, 2, 2)

            lbl_link = TextBlock()
            lbl_link.Text = u"{} :: {}".format(fam, typ)
            lbl_link.Width = 260
            lbl_link.TextTrimming = System.Windows.TextTrimming.CharacterEllipsis
            lbl_link.VerticalAlignment = System.Windows.VerticalAlignment.Center
            lbl_link.ToolTip = u"{} :: {}".format(fam, typ)
            row_panel.Children.Add(lbl_link)

            lbl_cnt = TextBlock()
            lbl_cnt.Text = u"x{}".format(counts[(fam, typ)])
            lbl_cnt.Width = 50
            lbl_cnt.VerticalAlignment = System.Windows.VerticalAlignment.Center
            row_panel.Children.Add(lbl_cnt)

            arrow = TextBlock()
            arrow.Text = u" \u2192 "
            arrow.VerticalAlignment = System.Windows.VerticalAlignment.Center
            row_panel.Children.Add(arrow)

            cmb_fam = ComboBox()
            cmb_fam.Width = 200
            cmb_fam.Margin = Thickness(0, 0, 6, 0)

            none_item = ComboBoxItem()
            none_item.Content = u"-- not mapped --"
            none_item.Tag = None
            cmb_fam.Items.Add(none_item)
            for fname in self._sorted_family_names:
                cbi = ComboBoxItem()
                cbi.Content = fname
                cbi.Tag = fname
                cmb_fam.Items.Add(cbi)

            cmb_typ = ComboBox()
            cmb_typ.Width = 180
            cmb_typ.IsEnabled = False

            # Restore existing mapping for this link type, if any
            current_sym_id = self._type_mappings.get((fam, typ))
            current_sym = self._sym_map.get(current_sym_id) if current_sym_id else None
            if current_sym is not None:
                cur_fam_name = getattr(current_sym.Family, 'Name', None)
                for i in range(cmb_fam.Items.Count):
                    if cmb_fam.Items[i].Tag == cur_fam_name:
                        cmb_fam.SelectedIndex = i
                        break
                self._populate_type_combo(cmb_typ, cur_fam_name, current_sym_id)
                cmb_typ.IsEnabled = True
            else:
                cmb_fam.SelectedIndex = 0

            def make_fam_handler(link_key, type_cmb):
                def _h(sender, args):
                    sel = sender.SelectedItem
                    fam_name = sel.Tag if sel else None
                    if fam_name is None:
                        type_cmb.Items.Clear()
                        type_cmb.IsEnabled = False
                        self._type_mappings.pop(link_key, None)
                        self._render_link_fixtures()
                        return
                    self._populate_type_combo(type_cmb, fam_name, None)
                    type_cmb.IsEnabled = True
                    # Auto-pick first type as the mapping; user can change.
                    if type_cmb.Items.Count > 0:
                        type_cmb.SelectedIndex = 0
                return _h

            def make_type_handler(link_key):
                def _h(sender, args):
                    sel = sender.SelectedItem
                    if sel and sel.Tag is not None:
                        self._type_mappings[link_key] = sel.Tag
                    else:
                        self._type_mappings.pop(link_key, None)
                    self._render_link_fixtures()
                return _h

            cmb_fam.SelectionChanged += make_fam_handler((fam, typ), cmb_typ)
            cmb_typ.SelectionChanged += make_type_handler((fam, typ))

            row_panel.Children.Add(cmb_fam)
            row_panel.Children.Add(cmb_typ)
            sp.Children.Add(row_panel)

    def _populate_type_combo(self, cmb_typ, family_name, preselect_sym_id):
        cmb_typ.Items.Clear()
        for sym in self._families_by_name.get(family_name, []):
            tname = param_str(sym, BuiltInParameter.SYMBOL_NAME_PARAM) or "<Type?>"
            cbi = ComboBoxItem()
            cbi.Content = tname
            cbi.Tag = sym.Id.IntegerValue
            cmb_typ.Items.Add(cbi)
            if preselect_sym_id is not None and sym.Id.IntegerValue == preselect_sym_id:
                cmb_typ.SelectedItem = cbi

    # -----------------------------
    # Fixtures list UI
    # -----------------------------
    def _render_link_fixtures(self):
        sp = self.spLinkFixtures
        sp.Children.Clear()
        self._fixture_row_controls = []

        if not self._current_space or not self._current_space.link_fixtures:
            tb = TextBlock()
            tb.Text = "(Scan a space to see fixtures from the linked model here)"
            tb.Foreground = Brushes.Gray
            tb.FontStyle = System.Windows.FontStyles.Italic
            tb.Margin = Thickness(4, 4, 4, 4)
            sp.Children.Add(tb)
            self.txtLinkFixtureCount.Text = ""
            return

        fixtures = self._current_space.link_fixtures
        mapped_count = 0

        hdr = StackPanel()
        hdr.Orientation = Orientation.Horizontal
        hdr.Margin = Thickness(2, 0, 2, 4)
        for txt, width in [("Use", 35), ("Link Fixture", 245), ("Family", 220), ("Type", 220), ("Height", 70), ("Info", 170)]:
            h = TextBlock()
            h.Text = txt
            h.Width = width
            h.FontWeight = System.Windows.FontWeights.Bold
            hdr.Children.Add(h)
        sp.Children.Add(hdr)

        def _populate_type_combo(type_combo, family_name, selected_sym_id=None, fixture_data=None):
            type_combo.Items.Clear()
            if not family_name or family_name not in self._families_by_name:
                type_combo.IsEnabled = False
                if fixture_data is not None:
                    fixture_data.override_symbol_id = None
                return
            type_combo.IsEnabled = True
            idx = -1
            for sym in self._families_by_name.get(family_name, []):
                cbi = ComboBoxItem()
                cbi.Content = param_str(sym, BuiltInParameter.SYMBOL_NAME_PARAM) or "<Type?>"
                cbi.Tag = sym.Id.IntegerValue
                type_combo.Items.Add(cbi)
                if selected_sym_id == sym.Id.IntegerValue:
                    idx = type_combo.Items.Count - 1
            if idx >= 0:
                type_combo.SelectedIndex = idx
            elif type_combo.Items.Count > 0:
                type_combo.SelectedIndex = 0
            try:
                sel = type_combo.SelectedItem
                if fixture_data is not None and sel is not None:
                    fixture_data.override_symbol_id = int(sel.Tag)
            except Exception:
                pass

        for lf in fixtures:
            row = StackPanel()
            row.Orientation = Orientation.Horizontal
            row.Margin = Thickness(2, 2, 2, 2)

            cb = CheckBox()
            cb.IsChecked = lf.checked
            cb.Width = 35
            cb.VerticalAlignment = System.Windows.VerticalAlignment.Center
            def make_check_handler(fixture_data, value):
                def _h(s, a):
                    fixture_data.checked = value
                    self._update_fixture_count_label()
                return _h
            cb.Checked += make_check_handler(lf, True)
            cb.Unchecked += make_check_handler(lf, False)
            row.Children.Add(cb)

            txt_link = TextBlock()
            txt_link.Text = lf.short()
            txt_link.Width = 245
            txt_link.TextTrimming = System.Windows.TextTrimming.CharacterEllipsis
            txt_link.VerticalAlignment = System.Windows.VerticalAlignment.Center
            txt_link.ToolTip = lf.display()
            if lf.near_boundary:
                txt_link.Foreground = Brushes.DarkOrange
            row.Children.Add(txt_link)

            default_sym_id = getattr(lf, 'override_symbol_id', None)
            if not default_sym_id:
                default_sym_id = self._type_mappings.get(lf.type_key())
            default_sym = self._sym_map.get(default_sym_id) if default_sym_id else None
            default_family = getattr(default_sym.Family, 'Name', None) if default_sym else None

            fam_combo = ComboBox()
            fam_combo.Width = 220
            fam_combo.Margin = Thickness(0, 0, 4, 0)
            none_item = ComboBoxItem()
            none_item.Content = "-- not mapped --"
            none_item.Tag = None
            fam_combo.Items.Add(none_item)
            selected_family_idx = 0
            for fname in self._sorted_family_names:
                cbi = ComboBoxItem()
                cbi.Content = fname
                cbi.Tag = fname
                fam_combo.Items.Add(cbi)
                if default_family == fname:
                    selected_family_idx = fam_combo.Items.Count - 1
            fam_combo.SelectedIndex = selected_family_idx
            row.Children.Add(fam_combo)

            type_combo = ComboBox()
            type_combo.Width = 220
            type_combo.Margin = Thickness(0, 0, 4, 0)
            row.Children.Add(type_combo)

            height_tb = TextBox()
            height_tb.Width = 70
            height_tb.Margin = Thickness(0, 0, 4, 0)
            try:
                default_height = float(getattr(lf, 'override_elev_mm', None) or self.tbLinkElevation.Text or 300.0)
            except Exception:
                default_height = 300.0
            lf.override_elev_mm = default_height
            height_tb.Text = "{:.0f}".format(default_height)
            row.Children.Add(height_tb)

            info = TextBlock()
            info.Text = "nearest wall"
            info.Width = 170
            info.Foreground = Brushes.DimGray
            info.VerticalAlignment = System.Windows.VerticalAlignment.Center
            row.Children.Add(info)

            def make_family_changed(fixture_data, family_cb, type_cb):
                def _h(s, a):
                    try:
                        sel = family_cb.SelectedItem
                        fname = sel.Tag if sel is not None else None
                    except Exception:
                        fname = None
                    _populate_type_combo(type_cb, fname, None, fixture_data)
                    self._update_fixture_count_label()
                return _h
            def make_type_changed(fixture_data, type_cb):
                def _h(s, a):
                    try:
                        sel = type_cb.SelectedItem
                        fixture_data.override_symbol_id = int(sel.Tag) if sel is not None and sel.Tag is not None else None
                    except Exception:
                        fixture_data.override_symbol_id = None
                    self._update_fixture_count_label()
                return _h
            fam_combo.SelectionChanged += make_family_changed(lf, fam_combo, type_combo)
            type_combo.SelectionChanged += make_type_changed(lf, type_combo)
            _populate_type_combo(type_combo, default_family, default_sym_id, lf)
            if lf.override_symbol_id:
                mapped_count += 1
            sp.Children.Add(row)
            self._fixture_row_controls.append((lf, cb, fam_combo, type_combo, height_tb))

        self._update_fixture_count_label(mapped_count_override=mapped_count)

    def _update_fixture_count_label(self, mapped_count_override=None):
        if not self._current_space:
            self.txtLinkFixtureCount.Text = ""
            return
        fixtures = self._current_space.link_fixtures
        total = len(fixtures)
        checked = sum(1 for lf in fixtures if lf.checked)
        if mapped_count_override is None:
            mapped = sum(1 for lf in fixtures if (getattr(lf, 'override_symbol_id', None) or self._type_mappings.get(lf.type_key())) is not None)
        else:
            mapped = mapped_count_override
        self.txtLinkFixtureCount.Text = "({} total, {} checked, {} individually mapped)".format(total, checked, mapped)

    def _set_all_link_fixtures_checked(self, value):
        if not self._current_space:
            return
        for lf in self._current_space.link_fixtures:
            lf.checked = value
        for row in self._fixture_row_controls:
            try:
                row[1].IsChecked = value
            except Exception:
                pass
        self._update_fixture_count_label()

    # -----------------------------
    # Placement
    # -----------------------------
    def _on_place_from_link(self, s, a):
        if not self._current_space:
            self.txtLinkStatus.Text = "Pick a space first."
            return

        plan = []
        rows = self._fixture_row_controls if self._fixture_row_controls else []
        if rows:
            for row in rows:
                lf = row[0]
                cb = row[1]
                type_cb = row[3] if len(row) > 3 else None
                height_tb = row[4] if len(row) > 4 else None
                try:
                    lf.checked = bool(cb.IsChecked)
                except Exception:
                    pass
                if not lf.checked:
                    continue
                sym_id = getattr(lf, 'override_symbol_id', None)
                try:
                    if type_cb is not None and type_cb.SelectedItem is not None:
                        sym_id = int(type_cb.SelectedItem.Tag)
                        lf.override_symbol_id = sym_id
                except Exception:
                    pass
                if not sym_id:
                    continue
                sym = self._sym_map.get(sym_id)
                if not sym:
                    continue
                try:
                    elev_mm = float(height_tb.Text) if height_tb is not None else float(self.tbLinkElevation.Text)
                except Exception:
                    elev_mm = 300.0
                lf.override_elev_mm = elev_mm
                plan.append((lf, sym, elev_mm))
        else:
            for lf in self._current_space.link_fixtures:
                if not lf.checked:
                    continue
                sym_id = getattr(lf, 'override_symbol_id', None) or self._type_mappings.get(lf.type_key())
                if not sym_id:
                    continue
                sym = self._sym_map.get(sym_id)
                if not sym:
                    continue
                try:
                    elev_mm = float(getattr(lf, 'override_elev_mm', None) or self.tbLinkElevation.Text)
                except Exception:
                    elev_mm = 300.0
                plan.append((lf, sym, elev_mm))

        if not plan:
            self.txtLinkStatus.Text = "Nothing to place: no checked fixtures have an individual family/type mapping."
            return

        def act(ui):
            placed_ids = List[ElementId]()
            placed = 0
            failed = 0
            fail_details = []
            tx = Transaction(doc, "Place Fixtures from Linked Model")
            tx.Start()
            try:
                for (lf, sym, elev_mm) in plan:
                    try:
                        lf._host_space = ui._current_space.space
                        new_id = place_from_link_fixture(lf, sym, elevation_mm=elev_mm)
                        if new_id is not None:
                            lf.placed_id = new_id
                            placed_ids.Add(new_id)
                            placed += 1
                        else:
                            failed += 1
                            try:
                                fail_details.append(lf.short() + ": " + str(getattr(lf, '_last_place_error', 'unknown failure')))
                            except Exception:
                                pass
                    except Exception as ex:
                        failed += 1
                        try:
                            fail_details.append(lf.short() + ": " + str(ex))
                        except Exception:
                            pass
            finally:
                tx.Commit()
            ui._last_placed_link_ids = placed_ids
            ui.Dispatcher.Invoke(System.Action(lambda: setattr(ui.btnUndoPlaceLink, "IsEnabled", placed_ids.Count > 0)))
            if placed_ids.Count > 0:
                try:
                    uidoc.Selection.SetElementIds(placed_ids)
                except Exception:
                    pass
            msg = "Placed {} fixture(s) from link.".format(placed)
            if failed:
                msg += " ({} failed.)".format(failed)
                try:
                    out = script.get_output()
                    out.print_md("## Link placement failures")
                    for d in fail_details[:20]:
                        out.print_md("- {}".format(d))
                except Exception:
                    pass
            ui.Dispatcher.Invoke(System.Action(lambda: setattr(ui.txtLinkStatus, "Text", msg)))
            ui._set_status(msg)
        REQ.action = act
        REQ.payload = self
        EXTEVENT.Raise()

    def _on_undo_place_link(self, s, a):
        if not self._last_placed_link_ids or self._last_placed_link_ids.Count == 0:
            return

        def act(ui):
            tx = Transaction(doc, "Undo Last Link-Based Placement")
            tx.Start()
            deleted = 0
            try:
                for eid in ui._last_placed_link_ids:
                    if doc.GetElement(eid):
                        doc.Delete(eid)
                        deleted += 1
            finally:
                tx.Commit()

            ui._last_placed_link_ids.Clear()
            ui.Dispatcher.Invoke(System.Action(lambda: setattr(
                ui.btnUndoPlaceLink, "IsEnabled", False)))
            ui._set_status(
                "Removed {} previously placed link-based fixtures.".format(deleted))
            ui.Dispatcher.Invoke(System.Action(lambda: setattr(
                ui.txtLinkStatus, "Text",
                "Undone: {} fixtures removed.".format(deleted))))

        REQ.action = act
        REQ.payload = self
        EXTEVENT.Raise()

def run(xaml_path=None):
    global XAML_PATH
    XAML_PATH = xaml_path
    ui = PlaceFixturesWindow()
    ui.Show()
