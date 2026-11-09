# -*- coding: utf-8 -*-
"""Pure planning logic for the Cable Tray Coordinator (no Revit or WPF imports).

All lengths are Revit internal units (decimal feet) unless a name ends in ``_mm``.
Points are plain ``(x, y, z)`` tuples so everything here can be unit tested outside Revit.

Local frame used for planning (right-handed):
  s = distance along the tray (horizontal), v = to the left of the tray direction, w = up (world Z).
"""
from __future__ import division

import math
import heapq

MM_PER_FT = 304.8
EPS = 1e-6

MODE_DETOUR = "detour"
MODE_BRIDGE = "bridge"

AUTO, OVER, UNDER, LEFT, RIGHT, STRAIGHT = "Auto", "Over", "Under", "Left", "Right", "Straight"
DIRECTIONS = [AUTO, OVER, UNDER, LEFT, RIGHT, STRAIGHT]
DETOUR_DIRECTIONS = [OVER, UNDER, LEFT, RIGHT]
ANGLES = [90, 60, 45, 30, 22.5, 15]
SLOPE_ANGLES = (15, 22.5, 30, 45, 60)
# How far a spatial bridge may pull a free tray end back to make room for its fittings (mm).
TRIM_STEPS_MM = (100, 200, 300, 500, 750, 1000, 1500)
# (v, w) unit offset of the raised/lowered/shifted section for each direction.
DIRECTION_VECTORS = {OVER: (0.0, 1.0), UNDER: (0.0, -1.0), LEFT: (1.0, 0.0), RIGHT: (-1.0, 0.0)}
DIRECTION_LABELS = {OVER: "Rise over", UNDER: "Drop under", LEFT: "Offset left", RIGHT: "Offset right",
                    STRAIGHT: "Straight join", AUTO: "Auto"}

# Trays steeper than this (end-to-end elevation difference) are treated as not horizontal.
MAX_LEVEL_DIFF_MM = 5.0
# Two tray ends must be within this angle of the gap direction to be bridged.
MAX_BRIDGE_MISALIGN_DEG = 0.25
# ...and their axes within this distance of the gap line (sideways and in elevation).
MAX_BRIDGE_LATERAL_MM = 2.0
# Largest sideways shift a bridge makes with an angled (30/45/60) step; bigger shifts turn 90 degrees.
MAX_JOG_MM = 1500.0
SIZE_TOL_MM = 1.0
# A click on the second bridge tray within this distance of an end (or a quarter of the tray)
# connects to that end; further along it tees into the side of the tray.
TEE_END_PICK_MM = 600.0
# Without clicks, a free end pointing at the side of the other tray at least this far from its ends
# (or a quarter of the tray) and within TEE_AUTO_REACH_MM tees into it.
TEE_AUTO_END_MM = 1000.0
TEE_AUTO_REACH_MM = 20000.0
# How far the planner slides a tee along the main tray to find a shorter or clear branch (mm).
TEE_SLIDE_MM = (250, 500, 1000, 2000, 3000)
# A detour that runs into a connected tray end may re-route this many straights of the connected run.
MAX_RUN_REACH = 7
# Gap kept between a new cut and the obstacle envelope (mm).
CUT_MARGIN_MM = 10.0

DEFAULT_PARAMS = {
    "direction": AUTO,
    "angle": 45,
    "offset_mm": 300.0,
    "clearance_mm": 50.0,
    "auto_offset": True,
    "round_mm": 25.0,
    "min_straight_mm": 100.0,
    "keep_if_fitting_fails": False,
    "individual_obstacles": False,
    "min_bend_radius_mm": 150.0,
    "shorten_fitting_legs": True,
    "shallower_bends": True,
}

PARAM_LIMITS_MM = {
    "offset_mm": (0.0, 5000.0, "Offset"),
    "clearance_mm": (0.0, 2000.0, "Clearance"),
    "round_mm": (0.0, 500.0, "Offset step"),
    "min_straight_mm": (0.0, 2000.0, "Min. straight"),
    "min_bend_radius_mm": (0.0, 3000.0, "Min. bend radius"),
}

# Bend radii the tool may step down to when the standard fittings do not fit (mm); the user's
# Min. bend radius is added to them and nothing smaller is used. 0 = never reduce the radius.
BEND_RADII_MM = (150.0, 300.0, 450.0, 600.0)
# Straight leg given to elbows when legs may be shortened (a 0 mm leg breaks typical families).
MIN_FITTING_LEG_MM = 25.0
# Radius/leg reductions smaller than this are not worth changing the fitting for.
MIN_FITTING_CHANGE_MM = 10.0

DEG = u"\u00b0"


def _preset(name, direction, angle, offset_mm, auto_offset, description):
    return {"name": name, "direction": direction, "angle": angle, "offset_mm": float(offset_mm),
            "clearance_mm": 50.0, "auto_offset": auto_offset, "description": description, "builtin": True}


BUILTIN_PRESETS = [
    _preset(u"Auto - smallest detour (45" + DEG + u")", AUTO, 45, 300, True,
            u"Pick an obstacle: the tool tries over, under, left and right and uses the smallest offset."),
    _preset(u"Auto - smallest detour (90" + DEG + u")", AUTO, 90, 300, True,
            u"As above with 90" + DEG + u" bends (shortest detour length, needs vertical fittings)."),
    _preset(u"Rise over - 45" + DEG, OVER, 45, 300, True, u"Lift the tray over the obstacle with 45" + DEG + u" bends."),
    _preset(u"Rise over - 90" + DEG, OVER, 90, 300, True, u"Lift the tray straight up and over with 90" + DEG + u" bends."),
    _preset(u"Drop under - 45" + DEG, UNDER, 45, 300, True, u"Drop the tray under the obstacle with 45" + DEG + u" bends."),
    _preset(u"Drop under - 90" + DEG, UNDER, 90, 300, True, u"Drop the tray straight down and under with 90" + DEG + u" bends."),
    _preset(u"Offset left - 45" + DEG, LEFT, 45, 300, True, u"Jog the tray sideways (left of its direction), 45" + DEG + u" bends."),
    _preset(u"Offset left - 90" + DEG, LEFT, 90, 300, True, u"Jog the tray sideways (left of its direction), 90" + DEG + u" bends."),
    _preset(u"Offset right - 45" + DEG, RIGHT, 45, 300, True, u"Jog the tray sideways (right of its direction), 45" + DEG + u" bends."),
    _preset(u"Offset right - 90" + DEG, RIGHT, 90, 300, True, u"Jog the tray sideways (right of its direction), 90" + DEG + u" bends."),
    _preset(u"Manual drop 300 mm - 45" + DEG, UNDER, 45, 300, False,
            u"Fixed 300 mm drop between two cut points (or around an obstacle)."),
    _preset(u"Straight join (bridge mode)", STRAIGHT, 90, 0, False,
            u"Join two in-line tray ends with a straight piece."),
]


class PlanError(Exception):
    """A user-facing problem with the inputs or the requested route."""


class RunReachError(PlanError):
    """The detour runs into a connected end of the tray segment (``side`` = "start" or "end")."""

    def __init__(self, side, message):
        PlanError.__init__(self, message)
        self.side = side


def to_text(value):
    try:
        return unicode(value)  # noqa: F821 (IronPython 2.7)
    except NameError:
        return str(value)


def mm_to_ft(mm):
    return float(mm) / MM_PER_FT


def ft_to_mm(ft):
    return float(ft) * MM_PER_FT


def fmt_mm(ft):
    return u"{:,.0f} mm".format(ft_to_mm(ft)).replace(u",", u" ")


# --- vectors ---------------------------------------------------------------------------------
def add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def scale(a, k):
    return (a[0] * k, a[1] * k, a[2] * k)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def length(a):
    return math.sqrt(dot(a, a))


def distance(a, b):
    return length(sub(a, b))


def normalize(a):
    n = length(a)
    if n < EPS:
        raise PlanError("Zero-length direction.")
    return scale(a, 1.0 / n)


def lerp(a, b, t):
    return add(a, scale(sub(b, a), t))


# --- frames ------------------------------------------------------------------------------------
class Frame(object):
    """Local (s, v, w) frame of a horizontal tray run."""

    def __init__(self, origin, u):
        self.origin = tuple(origin)
        self.u = (u[0], u[1], 0.0)
        self.v = (-u[1], u[0], 0.0)  # Z x u: left of the direction of travel
        self.w = (0.0, 0.0, 1.0)

    def to_local(self, p):
        d = sub(p, self.origin)
        return (dot(d, self.u), dot(d, self.v), d[2])

    def to_world(self, q):
        return add(self.origin, add(scale(self.u, q[0]), add(scale(self.v, q[1]), scale(self.w, q[2]))))

    def vector_to_world(self, q):
        return add(scale(self.u, q[0]), add(scale(self.v, q[1]), scale(self.w, q[2])))


def horizontal_frame(origin, toward, what="tray"):
    d = sub(toward, origin)
    run = math.sqrt(d[0] * d[0] + d[1] * d[1])
    if run < EPS:
        raise PlanError(u"The {} is vertical or has no length; only horizontal runs are supported.".format(what))
    if abs(d[2]) > mm_to_ft(MAX_LEVEL_DIFF_MM):
        raise PlanError(u"The {} is sloped ({} level difference); only horizontal runs are supported."
                        .format(what, fmt_mm(abs(d[2]))))
    return Frame(origin, (d[0] / run, d[1] / run, 0.0)), run


# --- parameters and presets --------------------------------------------------------------------
def parse_mm(text, key):
    lo, hi, label = PARAM_LIMITS_MM[key]
    try:
        value = float(to_text(text).strip().replace(u",", u"."))
    except (TypeError, ValueError):
        raise PlanError(u"{} must be a number in mm.".format(label))
    if math.isnan(value) or math.isinf(value) or value < lo or value > hi:
        raise PlanError(u"{} must be between {:.0f} and {:.0f} mm.".format(label, lo, hi))
    return value


def normalize_params(raw):
    """Return a complete, valid parameter dict (mm values) from possibly partial/invalid data."""
    params = dict(DEFAULT_PARAMS)
    if not isinstance(raw, dict):
        return params
    if raw.get("direction") in DIRECTIONS:
        params["direction"] = raw["direction"]
    try:
        value = float(raw.get("angle"))
        for angle in ANGLES:
            if abs(value - angle) < 1e-6:
                params["angle"] = angle
    except (TypeError, ValueError):
        pass
    for key in PARAM_LIMITS_MM:
        if key in raw:
            try:
                params[key] = parse_mm(raw[key], key)
            except PlanError:
                pass
    for key in ("auto_offset", "keep_if_fitting_fails", "individual_obstacles", "shorten_fitting_legs",
                "shallower_bends"):
        if key in raw:
            params[key] = bool(raw[key])
    return params


def params_in_feet(params):
    p = normalize_params(params)
    return {
        "direction": p["direction"],
        "angle": p["angle"],
        "offset": mm_to_ft(p["offset_mm"]),
        "clearance": mm_to_ft(p["clearance_mm"]),
        "round": mm_to_ft(p["round_mm"]),
        "min_straight": mm_to_ft(max(p["min_straight_mm"], 10.0)),
        "fitting_allowance": mm_to_ft(max(p["min_straight_mm"], 10.0)),
        "auto_offset": p["auto_offset"],
        "individual_obstacles": p["individual_obstacles"],
        "min_bend_radius": mm_to_ft(p["min_bend_radius_mm"]),
        "shorten_legs": p["shorten_fitting_legs"],
        "shallower_bends": p["shallower_bends"],
    }


def normalize_user_presets(raw):
    names = set(p["name"] for p in BUILTIN_PRESETS)
    result = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = to_text(item.get("name") or u"").strip()
        if not name or name in names:
            continue
        names.add(name)
        preset = normalize_params(item)
        preset.update({"name": name, "builtin": False,
                       "description": to_text(item.get("description") or u"User preset.")})
        result.append(preset)
    return result


def all_presets(user_presets):
    return list(BUILTIN_PRESETS) + normalize_user_presets(user_presets)


def make_user_preset(name, params, existing_names):
    name = to_text(name or u"").strip()
    if not name:
        raise PlanError("Enter a preset name.")
    if name in set(p["name"] for p in BUILTIN_PRESETS):
        raise PlanError(u"'{}' is a built-in preset name.".format(name))
    preset = normalize_params(params)
    preset.update({"name": name, "builtin": False,
                   "description": u"User preset: {}, {}{}.".format(
                       DIRECTION_LABELS[preset["direction"]], preset["angle"], DEG)})
    return preset, name in existing_names


# --- obstacles ---------------------------------------------------------------------------------
def clip_to_slab(p0, p1, axis, lo, hi):
    """Clip segment p0-p1 to lo <= p[axis] <= hi; return the clipped pair or None."""
    a0, a1 = p0[axis], p1[axis]
    t0, t1 = 0.0, 1.0
    da = a1 - a0
    if abs(da) < EPS:
        if a0 < lo - EPS or a0 > hi + EPS:
            return None
    else:
        ta, tb = (lo - a0) / da, (hi - a0) / da
        t0, t1 = max(t0, min(ta, tb)), min(t1, max(ta, tb))
        if t0 > t1 + EPS:
            return None
    return lerp(p0, p1, t0), lerp(p0, p1, t1)


def clip_polygon_to_slab(points, axis, lo, hi):
    """Clip a polygon (3+ points) to lo <= p[axis] <= hi (Sutherland-Hodgman); [] if outside."""
    for bound, keep_above in ((lo, True), (hi, False)):
        if not points:
            return []
        inside = (lambda q: q[axis] >= bound - EPS) if keep_above else (lambda q: q[axis] <= bound + EPS)
        result = []
        prev = points[-1]
        for cur in points:
            if inside(cur):
                if not inside(prev):
                    result.append(lerp(prev, cur, (bound - prev[axis]) / (cur[axis] - prev[axis])))
                result.append(cur)
            elif inside(prev):
                result.append(lerp(prev, cur, (bound - prev[axis]) / (cur[axis] - prev[axis])))
            prev = cur
        points = result
    return points


def clip_primitive(points, axis, lo, hi):
    """Clip a segment (2 points) or a polygon (3+ points) to a slab; None if nothing is left."""
    if len(points) == 2:
        return clip_to_slab(points[0], points[1], axis, lo, hi)
    clipped = clip_polygon_to_slab(list(points), axis, lo, hi)
    return tuple(clipped) if clipped else None


def _bounds(segments, axes):
    result = []
    for axis in axes:
        values = [p[axis] for seg in segments for p in seg]
        result.append((min(values), max(values)))
    return result


def obstacle_extents(local_segments, half_w, half_h, clearance):
    """Extents of the obstacle inside the tray's corridors, in the local (s, v, w) frame.

    Items are edge segments (2 points) or face polygons (3+ points). Faces matter: a tray passing
    through the middle of a big wall or slab may have no *edge* inside its corridor.

    ``vertical`` = parts within the tray width (+clearance): what an over/under detour must clear.
    ``horizontal`` = parts within the tray height (+clearance): what a left/right detour must clear.
    ``clash`` = True when the obstacle intrudes on the tray envelope plus clearance.
    """
    vlim = half_w + clearance
    wlim = half_h + clearance
    vert, horiz, clash = [], [], []
    for item in local_segments:
        a = clip_primitive(item, 1, -vlim, vlim)
        if a:
            vert.append(a)
            b = clip_primitive(a, 2, -wlim, wlim)
            if b:
                clash.append(b)
        c = clip_primitive(item, 2, -wlim, wlim)
        if c:
            horiz.append(c)
    ext = {"vertical": None, "horizontal": None, "clash": None}
    if vert:
        s, w = _bounds(vert, (0, 2))
        ext["vertical"] = {"s": s, "w": w}
    if horiz:
        s, v = _bounds(horiz, (0, 1))
        ext["horizontal"] = {"s": s, "v": v}
    if clash:
        ext["clash"] = {"s": _bounds(clash, (0,))[0]}
    return ext


def required_offsets(ext, half_w, half_h, clearance):
    """Centre-line offset needed for each direction to clear the obstacle (only where it applies)."""
    result = {}
    vert, horiz = ext.get("vertical"), ext.get("horizontal")
    if vert:
        result[OVER] = max(0.0, vert["w"][1] + clearance + half_h)
        result[UNDER] = max(0.0, half_h - vert["w"][0] + clearance)
    if horiz:
        result[LEFT] = max(0.0, horiz["v"][1] + clearance + half_w)
        result[RIGHT] = max(0.0, half_w - horiz["v"][0] + clearance)
    return result


def obstacle_span(ext, direction, clearance):
    key = "vertical" if direction in (OVER, UNDER) else "horizontal"
    part = ext.get(key)
    if not part:
        return None
    return part["s"][0] - clearance, part["s"][1] + clearance


def round_up(value, step):
    if step <= EPS:
        return value
    return math.ceil(value / step - 1e-9) * step


def choose_direction(offsets):
    """Smallest offset wins; ties prefer over, under, left, right (in that order)."""
    best = None
    for direction in DETOUR_DIRECTIONS:
        if direction in offsets and (best is None or offsets[direction] < offsets[best] - EPS):
            best = direction
    return best


# --- route -------------------------------------------------------------------------------------
def transition_run(offset, angle_deg):
    if angle_deg >= 90 or offset <= EPS:
        return 0.0
    return offset / math.tan(math.radians(angle_deg))


def route_points(s0, s1, span_lo, span_hi, direction, offset, angle_deg):
    """Local polyline from (s0,0,0) to (s1,0,0) that is offset over [span_lo, span_hi]."""
    if direction == STRAIGHT or offset <= EPS:
        return [(s0, 0.0, 0.0), (s1, 0.0, 0.0)]
    run = transition_run(offset, angle_deg)
    dv, dw = DIRECTION_VECTORS[direction]
    raw = [(s0, 0.0, 0.0), (span_lo - run, 0.0, 0.0), (span_lo, dv * offset, dw * offset),
           (span_hi, dv * offset, dw * offset), (span_hi + run, 0.0, 0.0), (s1, 0.0, 0.0)]
    points = [raw[0]]
    for p in raw[1:]:
        if distance(p, points[-1]) > EPS:
            points.append(p)
    return points


def segment_normal(direction):
    """Local 'up' normal for a route segment, or None for horizontal segments.

    A sloped/vertical cable tray created by the API gets an arbitrary rotation about its axis and
    NewElbowFitting then fails ("failed to insert elbow"); setting CurveNormal to the bent 'up'
    vector of the original tray keeps the bend in the vertical plane of the route.
    """
    ds, dv, dw = direction
    if abs(dw) < EPS:
        return None
    return normalize((-dw, 0.0, ds))


def _widen_span(span, min_straight):
    """Return (span_lo, span_hi) widened so the offset section is at least min_straight long."""
    lo, hi = span
    if hi - lo < min_straight:
        mid = (lo + hi) / 2.0
        lo, hi = mid - min_straight / 2.0, mid + min_straight / 2.0
    return lo, hi


def _new_plan(mode):
    return {"ok": False, "error": None, "warnings": [], "mode": mode, "direction": None, "angle": None,
            "offset": 0.0, "required": {}, "frame": None, "length": 0.0, "local_points": [],
            "segments": [], "cut": None, "obstacle_boxes": [], "width": 0.0, "height": 0.0,
            "summary": []}


def _local_obstacles(frame, obstacles):
    return [tuple(frame.to_local(p) for p in item) for items in obstacles for item in items]


def _obstacle_boxes(ext, clearance):
    boxes = []
    if ext.get("vertical"):
        boxes.append({"view": "elevation", "s": ext["vertical"]["s"], "y": ext["vertical"]["w"],
                      "clearance": clearance})
    if ext.get("horizontal"):
        boxes.append({"view": "plan", "s": ext["horizontal"]["s"], "y": ext["horizontal"]["v"],
                      "clearance": clearance})
    return boxes


def _resolve_direction_and_offset(plan, ext, params, half_w, half_h):
    """Pick direction/offset from the obstacle (or the manual values). Returns (direction, offset)."""
    direction = params["direction"]
    clearance = params["clearance"]
    if ext is None:
        if direction == AUTO:
            raise PlanError("Auto needs an obstacle. Add an obstacle or choose a direction.")
        return direction, params["offset"]

    offsets = required_offsets(ext, half_w, half_h, clearance)
    plan["required"] = offsets
    plan["clearance"] = clearance
    if not ext.get("clash"):
        plan["warnings"].append(u"The obstacle does not clash with this tray (clearance {} included)."
                                .format(fmt_mm(clearance)))
        if params["auto_offset"] or direction == AUTO:
            raise PlanError(u"No clash to avoid. Untick 'Calculate from obstacle' and enter an offset "
                            u"to move the tray anyway.")
    if direction == AUTO:
        direction = choose_direction(offsets)
        if direction is None:
            raise PlanError("The obstacle is not near this tray.")
    if direction == STRAIGHT:
        return direction, 0.0
    if direction not in offsets:
        raise PlanError(u"The obstacle is not beside/above/below the tray for '{}'."
                        .format(DIRECTION_LABELS[direction]))
    needed = round_up(offsets[direction], params["round"])
    if params["auto_offset"]:
        return direction, needed
    if params["offset"] + EPS < offsets[direction]:
        plan["warnings"].append(u"Offset {} is less than the {} needed to clear the obstacle: it will still clash."
                                .format(fmt_mm(params["offset"]), fmt_mm(offsets[direction])))
    return direction, params["offset"]


def achieved_clearance(plan, offset=None, direction=None):
    """Gap left between the routed tray and the obstacle (ft), or None without an obstacle."""
    direction = direction or plan.get("direction")
    needed = (plan.get("required") or {}).get(direction)
    if needed is None or plan.get("clearance") is None:
        return None
    return plan["clearance"] + (plan.get("offset", 0.0) if offset is None else offset) - needed


def _offset_increased_warning(plan, direction, offset):
    text = u"Offset increased to {} to avoid short bend segments".format(fmt_mm(offset))
    gap = achieved_clearance(plan, offset, direction)
    if gap is None:
        return text + u"."
    return text + (u": the gap to the obstacle becomes {} (Clearance {}). For a closer fit choose a shallower "
                   u"Angle or reduce Min. straight / Min. bend radius.".format(fmt_mm(gap),
                                                                             fmt_mm(plan["clearance"])))


def _finish(plan, frame, s0, s1, span, direction, offset, params, piece_limits):
    """Build route points/segments, validate segment lengths and write the summary."""
    angle = params["angle"]
    min_straight = params["min_straight"]
    if piece_limits is not None and offset <= EPS:
        raise PlanError("A detour needs a positive offset. Use Straight join in Bridge mode.")
    plan.update({"direction": direction, "offset": offset, "angle": angle, "frame": frame})
    if direction == STRAIGHT or offset <= EPS:
        points = route_points(s0, s1, s0, s1, STRAIGHT, 0.0, angle)
        plan["direction"] = STRAIGHT
    else:
        minimum_offset = min_straight * math.sin(math.radians(angle))
        if offset < minimum_offset - EPS:
            offset = round_up(minimum_offset, params["round"])
            plan["warnings"].append(_offset_increased_warning(plan, direction, offset))
            plan["offset"] = offset
            if offset + EPS >= plan["required"].get(direction, float("inf")):
                plan["warnings"] = [w for w in plan["warnings"] if "will still clash" not in w]
        run = transition_run(offset, angle)
        lo, hi = _widen_span(span, min_straight)
        start, end = lo - run, hi + run
        if piece_limits is None:  # bridge: the detour must fit inside the gap
            if start < s0 - EPS or end > s1 + EPS:
                raise PlanError(u"The gap ({}) is too short for this detour ({} needed). Use a smaller "
                                u"offset, a steeper angle, or cut a longer gap.".format(
                                    fmt_mm(s1 - s0), fmt_mm(max(end, s1) - min(start, s0))))
            # Avoid slivers between the tray end and the bend: extend the offset section instead.
            if EPS < start - s0 < min_straight:
                lo, start = s0 + run, s0
            if EPS < s1 - end < min_straight:
                hi, end = s1 - run, s1
        else:  # detour: the cuts must leave enough tray at both ends
            first, last = piece_limits[:2]
            free = tuple(piece_limits[2:4]) if len(piece_limits) > 2 else (False, False)
            past = [start < first + min_straight - EPS, end > last - min_straight + EPS]
            # A clash that reaches a free tray end moves that end sideways instead (no return bend);
            # the shifted straight then still needs its minimum length.
            if past[0] and free[0] and not past[1]:
                hi = max(hi, first + min_straight)
                end = hi + run
                past[1] = end > last - min_straight + EPS
            if past[1] and free[1] and not past[0]:
                lo = min(lo, last - min_straight)
                start = lo - run
                past[0] = start < first + min_straight - EPS
            for index, side in enumerate(("start", "end")):
                if past[index] and not free[index]:
                    raise RunReachError(side, u"The detour ({} long) runs past the connected {} of this tray "
                                        u"segment. Use Bridge mode across the joint, a smaller offset or a "
                                        u"steeper angle.".format(fmt_mm(end - start), side))
            s0, s1 = (first if past[0] else start), (last if past[1] else end)
            plan["cut"] = (s0, s1)
            plan["open_ends"] = tuple(past)
        points = route_points(s0, s1, lo, hi, direction, offset, angle)
        if piece_limits is not None and any(plan["open_ends"]):
            dv, dw = DIRECTION_VECTORS[direction]
            shift = (dv * offset, dw * offset)
            if all(plan["open_ends"]):
                raw = [(s0,) + shift, (s1,) + shift]
            elif plan["open_ends"][0]:
                # Everything before the return bend is shifted, right from the free start.
                raw = [(s0,) + shift, (hi,) + shift, (hi + run, 0.0, 0.0), (s1, 0.0, 0.0)]
            else:
                raw = [(s0, 0.0, 0.0), (lo - run, 0.0, 0.0), (lo,) + shift, (s1,) + shift]
            points = [raw[0]]
            for p in raw[1:]:
                if distance(p, points[-1]) > EPS:
                    points.append(p)

    return _set_segments(plan, frame, points, params)


def _set_segments(plan, frame, points, params):
    segments = []
    min_straight = max(params["min_straight"], mm_to_ft(2))
    for index, (a, b) in enumerate(zip(points[:-1], points[1:])):
        seg_len = distance(a, b)
        if seg_len < min_straight - EPS:
            raise PlanError(u"A {} segment is shorter than the minimum straight ({}). Increase the offset "
                            u"or choose a different angle.".format(fmt_mm(seg_len), fmt_mm(min_straight)))
        d = normalize(sub(b, a))
        horizontal = math.sqrt(d[0] ** 2 + d[1] ** 2)
        n_local = None
        if abs(d[2]) > EPS:
            n_local = ((-d[2] * d[0] / horizontal, -d[2] * d[1] / horizontal, horizontal)
                       if horizontal > EPS else (-d[2], 0.0, 0.0))
            if horizontal <= EPS and index > 0:
                previous = normalize(sub(a, points[index - 1]))
                n_local = (-d[2] * previous[0], -d[2] * previous[1], 0.0)
        segments.append({"start": frame.to_world(a), "end": frame.to_world(b),
                         "normal": frame.vector_to_world(n_local) if n_local else None,
                         "length": seg_len})
    plan["local_points"] = points
    plan["segments"] = segments
    plan["minimum_straight"] = params.get("user_min_straight", params["min_straight"])
    plan["fitting_allowance"] = params["min_straight"] - plan["minimum_straight"]
    plan["takeup_measured"] = bool(params.get("takeup_measured"))
    plan["ok"] = True
    plan["summary"] = summary_lines(plan)
    return plan


def fitting_allowance(params, angle):
    """Length reserved per planned straight for the fittings at its two ends (ft).

    Uses the measured per-fitting take-up for this bend angle when known; otherwise falls back to
    one extra Min. straight (the original estimate).
    """
    takeup = (params.get("takeup") or {}).get(angle)
    if takeup is not None:
        return 2.0 * takeup, True
    return params.get("fitting_allowance", 0.0), False


def joint_takeup(params, angle):
    """Length one elbow of this angle takes off each straight it joins (ft); estimate if unmeasured."""
    return fitting_allowance(params, angle)[0] / 2.0


def usable_angles(params, angles):
    """The angles whose elbow take-up is known; all of them if nothing was measured (estimates only).

    Mixing a measured angle with an unmeasured (estimated) one would make the unmeasured one look free.
    """
    takeup = params.get("takeup") or {}
    if not any(a in takeup for a in ANGLES):
        return tuple(angles)
    return tuple(a for a in angles if a in takeup)


def fitting_options(takeup, params):
    """Smaller elbow settings to try when the standard ones do not fit, least change first.

    ``takeup`` carries the standard "bend_radius"/"bend_leg" (ft) measured from the tray's own
    elbows (absent when the family has no adjustable size). Shorter legs come before any radius
    reduction; radii step down through BEND_RADII_MM and the user's minimum, never below it.
    Returns [{"radius", "leg", "standard_radius", "standard_leg"}] (ft; None = unchanged).
    """
    takeup = takeup or {}
    radius, leg = takeup.get("bend_radius"), takeup.get("bend_leg")
    change = mm_to_ft(MIN_FITTING_CHANGE_MM)
    short_leg = None
    if params.get("shorten_legs") and leg and leg - mm_to_ft(MIN_FITTING_LEG_MM) > change:
        short_leg = mm_to_ft(MIN_FITTING_LEG_MM)
    minimum = params.get("min_bend_radius") or 0.0
    radii = []
    if radius and minimum > EPS:
        values = sorted(set(round(r, 1) for r in BEND_RADII_MM + (round(ft_to_mm(minimum), 1),)), reverse=True)
        radii = [mm_to_ft(r) for r in values if mm_to_ft(r) >= minimum - EPS and mm_to_ft(r) < radius - change]
    options = []
    if short_leg is not None:
        options.append({"radius": None, "leg": short_leg})
    for r in radii:
        options.append({"radius": r, "leg": short_leg})
    for option in options:
        option.update(standard_radius=radius, standard_leg=leg)
    return options


def adjusted_takeup(takeup, option):
    """Per-angle elbow take-up with a smaller radius/leg.

    An elbow takes (centre-line radius) x tan(angle/2) + leg off each straight, so a radius change
    dR and leg change dL reduce it by dR x tan(angle/2) + dL (matches Revit's channel bends).
    """
    result = dict(takeup)
    d_radius = option["standard_radius"] - option["radius"] if option.get("radius") else 0.0
    d_leg = option["standard_leg"] - option["leg"] if option.get("leg") else 0.0
    for angle in ANGLES:
        for key in (angle, ("v", angle)):
            if key in takeup:
                result[key] = max(takeup[key] - d_radius * math.tan(math.radians(angle) / 2.0) - d_leg, 0.0)
    return result


FITTING_PROBLEM_HINTS = ("need more room", "Offset increased", "merged to avoid short")


def _fitting_problems(plan):
    """Number of compromises the fittings forced on an ok plan (None if it failed)."""
    if not plan or not plan.get("ok"):
        return None
    count = sum(1 for w in plan.get("warnings") or () if any(h in w for h in FITTING_PROBLEM_HINTS))
    if any(t > EPS for t in plan.get("trims") or ()):
        count += 1
    return count


def _plan_rank(plan):
    """(compromises, route length incl. trims) of an ok plan, lower is better; None if it failed."""
    problems = _fitting_problems(plan)
    if problems is None:
        return None
    length = sum(s["length"] for s in plan.get("segments") or ()) + sum(plan.get("trims") or ())
    return problems, round(length, 4)


def _offset_increased(plan):
    return bool(plan) and any("Offset increased" in w for w in plan.get("warnings") or ())


def plan_with_fittings(planner, params):
    """planner(params) with the standard elbows; if they force a failure, a loop, a pulled-back end, a
    bigger offset or merged detours, retry with smaller elbows (fitting_options) and keep the first
    option without such problems, else the one with the fewest (then the most compact route).

    If the offset still had to grow past the clearance and "shallower_bends" is on, shallower bend
    angles are tried too (a shallow offset needs less height/width for the same straights), so the
    route stays at the clearance set. The chosen option is recorded in plan["fitting_option"] so the
    build sets it on every elbow.
    """
    base = planner(params)
    rank = _plan_rank(base)
    if rank is not None and rank[0] == 0:
        return base
    options = [None] + fitting_options(params.get("takeup"), params)
    best, best_rank, best_option = base, rank, None

    def attempt(angle, option):
        q = dict(params, angle=angle)
        if option is not None:
            q["takeup"] = adjusted_takeup(params["takeup"], option)
        plan = planner(q)
        found = _plan_rank(plan)
        if found is not None and (best_rank is None or found < best_rank):
            return plan, found
        return None, found

    for option in options[1:]:
        plan, found = attempt(params["angle"], option)
        if plan is not None:
            best, best_rank, best_option = plan, found, option
        if found is not None and found[0] == 0:
            break
    if params.get("shallower_bends") and (best_rank is None or best_rank[0] > 0) and (
            _offset_increased(base) or _offset_increased(best)):
        done = False
        for angle in [a for a in usable_angles(params, ANGLES) if a < params["angle"]]:
            for option in options:
                plan, found = attempt(angle, option)
                if plan is not None:
                    best, best_rank, best_option = plan, found, option
                if found is not None and found[0] == 0:
                    done = True
                    break
            if done:
                break
    if best is base:
        return base
    if best.get("angle") is not None and best["angle"] != params["angle"]:
        best["summary"] = list(best.get("summary") or [])
        best["summary"].insert(min(1, len(best["summary"])), u"Bend angle {}{} instead of {}{} so the route keeps "
                               u"the clearance ('Allow shallower bends').".format(best["angle"], DEG,
                                                                                 params["angle"], DEG))
    if best_option is None:
        return best
    best["fitting_option"] = best_option
    parts = []
    if best_option.get("radius"):
        parts.append(u"bend radius {} (standard {})".format(fmt_mm(best_option["radius"]),
                                                            fmt_mm(best_option["standard_radius"])))
        best["warnings"].append(u"Bend radius reduced to {}: check it suits the cables' minimum bending "
                                u"radius (Min. bend radius setting).".format(fmt_mm(best_option["radius"])))
    if best_option.get("leg"):
        parts.append(u"fitting legs {} (standard {})".format(fmt_mm(best_option["leg"]),
                                                            fmt_mm(best_option["standard_leg"])))
    best["summary"] = list(best.get("summary") or [])
    best["summary"].insert(min(1, len(best["summary"])),
                           u"Smaller elbows so the route fits: " + u", ".join(parts))
    return best


def _planning_params(params, angle=None):
    """Params whose min_straight includes the fittings' take-up (applied once: planned params pass through)."""
    if "user_min_straight" in params:
        return params
    allowance, measured = fitting_allowance(params, params["angle"] if angle is None else angle)
    if allowance <= EPS:
        return params
    return dict(params, min_straight=params["min_straight"] + allowance,
                user_min_straight=params["min_straight"], fitting_allowance=0.0, takeup_measured=measured)


def _vertical_params(params, direction):
    """Over/Under detours bend about the tray's height: use the (smaller) measured vertical take-up."""
    vertical = (params.get("takeup") or {}).get(("v", params["angle"]))
    if direction not in (OVER, UNDER) or vertical is None or "user_min_straight" not in params:
        return params
    return dict(params, min_straight=params["user_min_straight"] + 2.0 * vertical)


def plan_detour(tray, obstacles, cut_points, params):
    raw = params
    params = _planning_params(params)
    plan = _plan_detour(tray, obstacles, cut_points, params)
    if plan["ok"]:
        return plan
    candidates = []
    reach = plan.get("run_side")
    if params["direction"] == AUTO and obstacles and not cut_points:
        for direction in DETOUR_DIRECTIONS:
            alternate = dict(params, direction=direction)
            candidate = _plan_detour(tray, obstacles, cut_points, alternate)
            if candidate["ok"]:
                candidates.append(candidate)
    if reach and tray.get("runs") and not params.get("individual_obstacles"):
        rerouted = _plan_run_reroute(tray, obstacles, raw, reach)
        if rerouted["ok"]:
            candidates.append(rerouted)
        elif not candidates:
            return rerouted
    if candidates:
        return min(candidates, key=lambda p: sum(s["length"] for s in p["segments"]))
    return plan


def _plan_detour(tray, obstacles, cut_points, params):
    """Plan a detour that cuts a single tray and re-joins it at the original level.

    tray: {"start", "end", "width", "height"}; obstacles: list of lists of world segments/polygons;
    cut_points: [] or two world points picked on the tray; params: see params_in_feet().
    """
    if params.get("individual_obstacles") and obstacles and not cut_points:
        return _plan_individual_detour(tray, obstacles, params)
    plan = _new_plan(MODE_DETOUR)
    try:
        frame, run_length = horizontal_frame(tray["start"], tray["end"])
        plan.update({"frame": frame, "length": run_length, "width": tray["width"], "height": tray["height"]})
        half_w, half_h = tray["width"] / 2.0, tray["height"] / 2.0
        ext = None
        if obstacles:
            ext = obstacle_extents(_local_obstacles(frame, obstacles), half_w, half_h, params["clearance"])
            plan["obstacle_boxes"] = _obstacle_boxes(ext, params["clearance"])

        if cut_points and len(cut_points) == 2:
            a, b = sorted(frame.to_local(p)[0] for p in cut_points)
            direction = params["direction"]
            if direction == AUTO:
                if ext is None:
                    raise PlanError("With cut points, choose a direction (Over/Under/Left/Right).")
                direction, _ = _resolve_direction_and_offset(plan, ext, params, half_w, half_h)
            params = _vertical_params(params, direction)
            if direction == STRAIGHT:
                raise PlanError("Straight join only applies in Bridge mode.")
            offset = params["offset"]
            if params["auto_offset"] and ext is not None and direction in plan["required"]:
                offset = round_up(plan["required"][direction], params["round"])
            minimum_offset = params["min_straight"] * math.sin(math.radians(params["angle"]))
            if offset < minimum_offset - EPS:
                offset = round_up(minimum_offset, params["round"])
                plan["warnings"].append(_offset_increased_warning(plan, direction, offset))
            run = transition_run(offset, params["angle"])
            span = (a + run, b - run)
            if span[1] - span[0] < params["min_straight"] - EPS:
                raise PlanError(u"The cut points are {} apart; this offset/angle needs at least {}."
                                .format(fmt_mm(b - a), fmt_mm(2 * run + params["min_straight"])))
            if ext is not None and direction in (OVER, UNDER, LEFT, RIGHT):
                obs = obstacle_span(ext, direction, params["clearance"])
                if obs and (obs[0] < span[0] - EPS or obs[1] > span[1] + EPS):
                    plan["warnings"].append("The obstacle extends beyond the cut points: the bends may clash.")
            return _finish(plan, frame, a, b, span, direction, offset, params, (0.0, run_length))

        if ext is None:
            raise PlanError("Add the obstacle to avoid, or pick two cut points on the tray.")
        direction, offset = _resolve_direction_and_offset(plan, ext, params, half_w, half_h)
        if direction == STRAIGHT:
            raise PlanError("Straight join only applies in Bridge mode.")
        params = _vertical_params(params, direction)
        span = obstacle_span(ext, direction, params["clearance"])
        return _finish(plan, frame, None, None, span, direction, offset, params,
                       (0.0, run_length) + free_ends(tray))
    except RunReachError as ex:
        plan.update(ok=False, error=to_text(ex), run_side=ex.side)
        return plan
    except PlanError as ex:
        plan["ok"] = False
        plan["error"] = to_text(ex)
        return plan


def free_ends(tray):
    """(start is free, end is free) for a tray dict; ends without connection data count as connected."""
    flags = []
    for key in ("start", "end"):
        match = [e for e in tray.get("ends") or () if distance(e["point"], tray[key]) < mm_to_ft(1)]
        flags.append(bool(match) and not match[0]["connected"])
    return tuple(flags)


def _blocked_ranges(frame, obstacles, tray, params, run_length):
    """Ranges of s along a tray frame where a selected obstacle (grown by the bridge envelope) blocks it."""
    radius = max(tray["width"], tray["height"]) / 2
    local = [[tuple(frame.to_local(p) for p in primitive) for primitive in obstacle] for obstacle in obstacles]
    ranges = []
    for b in obstacle_bounds(local, (radius, radius, tray["height"] / 2), params["clearance"]):
        if b[1][0] <= 0 <= b[1][1] and b[2][0] <= 0 <= b[2][1] and b[0][1] >= 0 and b[0][0] <= run_length:
            ranges.append(b[0])
    return ranges


def _plan_run_reroute(tray, obstacles, params, side):
    """Detour whose clash reaches a connected tray end: cut the connected run on the far side of the clash.

    ``tray["runs"][side]`` lists the run beyond that end, nearest first: fittings ({"kind": "fitting"})
    and straights (tray dicts with "kind": "tray" and "near" = their end facing this tray). The tray is
    cut short of the obstacle, the run is cut on the first clear horizontal straight, everything in
    between is removed, and a spatial bridge re-joins the two cut ends around the obstacle.
    """
    plan = _new_plan(MODE_BRIDGE)
    try:
        run = (tray.get("runs") or {}).get(side) or {}
        items = run.get("items") or []
        user_min = max(params["min_straight"], mm_to_ft(2))
        keep = user_min + 2 * max(joint_takeup(params, a) for a in ANGLES)
        margin = mm_to_ft(CUT_MARGIN_MM)
        frame, total = horizontal_frame(tray["start"], tray["end"])
        blocked = _blocked_ranges(frame, obstacles, tray, params, total)
        if not blocked:
            raise PlanError("No selected obstacle clashes with this tray.")
        s_cut = min(r[0] for r in blocked) - margin if side == "end" else max(r[1] for r in blocked) + margin
        if (s_cut if side == "end" else total - s_cut) < keep:
            raise PlanError(u"The clash covers this tray segment almost to its other end, so no straight is "
                            u"left to re-route from. Pick the neighbouring segment, or use Bridge mode.")
        end_point = tray[side]
        cut_end = {"point": lerp(tray["start"], tray["end"], s_cut / total), "connected": False}
        other = "start" if side == "end" else "end"
        source = dict(tray, ends=[cut_end] + [e for e in tray["ends"]
                                              if distance(e["point"], tray[other]) < mm_to_ft(1)])
        source[side] = cut_end["point"]
        tol = mm_to_ft(SIZE_TOL_MM)
        removed, straights, last_error = [], 0, None
        for item in items:
            if item.get("kind") != "tray":
                removed.append(item)
                continue
            straights += 1
            if straights > MAX_RUN_REACH:
                break
            if abs(item["width"] - tray["width"]) > tol or abs(item["height"] - tray["height"]) > tol:
                last_error = PlanError(u"The connected run changes size at {}.".format(item.get("label", u"a tray")))
                break
            near = item["near"]
            far = item["end"] if distance(item["start"], near) < distance(item["end"], near) else item["start"]
            if abs(far[2] - near[2]) <= mm_to_ft(MAX_LEVEL_DIFF_MM):
                f, run_length = horizontal_frame(near, far)
                ranges = _blocked_ranges(f, obstacles, item, params, run_length)
                c = max([0.0] + [r[1] + margin for r in ranges])
                if run_length - c >= keep:
                    cut = {"point": lerp(near, far, c / run_length), "connected": False}
                    target = dict(item, start=cut["point"], end=far,
                                  ends=[cut, {"point": far, "connected": True}])
                    try:
                        result = _spatial_bridge(source, target, cut_end, cut, obstacles, params)
                    except PlanError as ex:
                        last_error = ex
                    else:
                        result["run_reroute"] = {"side": side, "tray_end": end_point, "cut_tray": item,
                                                 "cut_near": near, "cut_at": c, "removed": list(removed)}
                        result["warnings"].append(
                            u"The clash reaches the connected {} of this tray: {} element(s) of the connected run "
                            u"are removed and re-routed (cut {} into {}).".format(
                                side, len(removed), fmt_mm(c), item.get("label", u"the next straight")))
                        result["summary"] = summary_lines(result)
                        return result
            removed.append(item)
        stop = run.get("stop")
        raise PlanError(u"The detour runs past the connected {} of this tray, and no clear horizontal straight "
                        u"that can be re-routed was found within {} segments of the connected run{}{}. Use "
                        u"Bridge mode between clear segments.".format(
                            side, MAX_RUN_REACH, u" (the run ends at {})".format(stop) if stop else u"",
                            u": " + to_text(last_error) if last_error else u""))
    except PlanError as ex:
        plan.update(ok=False, error=to_text(ex))
        return plan


def _plan_individual_detour(tray, obstacles, params):
    plan = _new_plan(MODE_DETOUR)
    try:
        frame, run = horizontal_frame(tray["start"], tray["end"])
        plan.update(frame=frame, length=run, width=tray["width"], height=tray["height"])
        single = dict(params, individual_obstacles=False)
        groups = []
        for obstacle in obstacles:
            ext = obstacle_extents(_local_obstacles(frame, [obstacle]), tray["width"] / 2,
                                   tray["height"] / 2, params["clearance"])
            if ext["clash"] and ext["clash"]["s"][1] >= 0 and ext["clash"]["s"][0] <= run:
                groups.append([obstacle])
        if not groups:
            raise PlanError("No selected obstacle clashes with this tray.")
        merged = False
        while True:
            routes = [(plan_detour(tray, group, [], single), group) for group in groups]
            for route, _ in routes:
                if not route["ok"]:
                    raise PlanError(route["error"])
            routes.sort(key=lambda item: item[0]["cut"][0])
            used = dict(params, min_straight=min(r["minimum_straight"] + r["fitting_allowance"] for r, _ in routes))
            for i in range(len(routes) - 1):
                if routes[i + 1][0]["cut"][0] - routes[i][0]["cut"][1] < used["min_straight"] + EPS:
                    groups = [group for _, group in routes]
                    groups[i:i + 2] = [groups[i] + groups[i + 1]]
                    merged = True
                    break
            else:
                break
        points = []
        for route, _ in routes:
            points.extend(route["local_points"])
            plan["obstacle_boxes"].extend(route["obstacle_boxes"])
            plan["warnings"].extend(route["warnings"])
        plan.update(cut=(points[0][0], points[-1][0]), direction=routes[0][0]["direction"],
                    angle=params["angle"], offset=max(p["offset"] for p, _ in routes),
                    open_ends=(routes[0][0]["open_ends"][0], routes[-1][0]["open_ends"][1]))
        plan["individual_routes"] = [{"direction": p["direction"], "offset": p["offset"], "cut": p["cut"]}
                                     for p, _ in routes]
        directions = set(p["direction"] in (LEFT, RIGHT) for p, _ in routes)
        plan["mixed_routes"] = len(directions) > 1
        if merged:
            plan["warnings"].append("Nearby obstacles merged to avoid short connecting sections.")
        plan["warnings"].append("{} individual avoidance group(s).".format(len(routes)))
        # Each group's own obstacles are cleared by its planner; check it against everything else.
        for route, group in routes:
            rest = [o for o in obstacles if not any(o is g for g in group)]
            if rest and not route_is_clear(route["local_points"], local_obstacle_bounds(
                    frame, rest, tray["width"] / 2, tray["height"] / 2, params["clearance"])):
                raise PlanError("Individual detours clash with another selected obstacle. Try grouped mode "
                                "or another direction/clearance.")
        return _set_segments(plan, frame, points, used)
    except PlanError as ex:
        plan.update(ok=False, error=to_text(ex))
        return plan


def obstacle_bounds(obstacles, radius, clearance):
    """Axis-aligned obstacle boxes grown by radius (a number or per-axis tuple) plus clearance."""
    radii = tuple(radius) if isinstance(radius, (tuple, list)) else (radius,) * 3
    padding = [r + clearance for r in radii]
    boxes = []
    for obstacle in obstacles:
        points = [p for primitive in obstacle for p in primitive]
        if points:
            boxes.append(tuple((min(p[i] for p in points) - padding[i],
                                max(p[i] for p in points) + padding[i]) for i in range(3)))
    return boxes


def local_obstacle_bounds(frame, obstacles, half_w, half_h, clearance):
    """Obstacle boxes in a tray frame, grown like the planner's envelope (bends sit at span +/- clearance)."""
    local = [[tuple(frame.to_local(p) for p in primitive) for primitive in obstacle] for obstacle in obstacles]
    return obstacle_bounds(local, (0.0, half_w, half_h), clearance)


def route_is_clear(points, boxes):
    for a, b in zip(points[:-1], points[1:]):
        for bounds in boxes:
            t0, t1 = 0.0, 1.0
            for axis, (lo, hi) in enumerate(bounds):
                lo, hi = lo + EPS, hi - EPS
                delta = b[axis] - a[axis]
                if abs(delta) < EPS:
                    if a[axis] < lo or a[axis] > hi:
                        break
                    continue
                enter, leave = (lo - a[axis]) / delta, (hi - a[axis]) / delta
                t0, t1 = max(t0, min(enter, leave)), min(t1, max(enter, leave))
                if t0 > t1:
                    break
            else:
                return False
    return True


def segment_is_clear(a, b, boxes):
    """route_is_clear for one straight, unrolled for speed (it runs for every search edge)."""
    ax, ay, az = a
    dx, dy, dz = b[0] - ax, b[1] - ay, b[2] - az
    for (xlo, xhi), (ylo, yhi), (zlo, zhi) in boxes:
        t0, t1 = 0.0, 1.0
        blocked = True
        for start, delta, lo, hi in ((ax, dx, xlo, xhi), (ay, dy, ylo, yhi), (az, dz, zlo, zhi)):
            lo, hi = lo + EPS, hi - EPS
            if -EPS < delta < EPS:
                if start < lo or start > hi:
                    blocked = False
                    break
                continue
            enter, leave = (lo - start) / delta, (hi - start) / delta
            if enter > leave:
                enter, leave = leave, enter
            if enter > t0:
                t0 = enter
            if leave < t1:
                t1 = leave
            if t0 > t1:
                blocked = False
                break
        if blocked:
            return False
    return True


def _orthogonal_bridge(plan, tray_a, tray_b, end_a, end_b, obstacles, params, max_cost=None):
    """Bounded visibility search: 90-degree turns, 30/45/60-degree vertical slopes, never arbitrary twists.

    ``max_cost`` (ft, optional) gives up once every remaining route would be longer than that.
    """
    if params["direction"] == STRAIGHT:
        raise PlanError("Straight join needs aligned ends at the same elevation. Choose Auto for a spatial bridge.")
    frame, _ = horizontal_frame(tray_a["start"], tray_a["end"])
    frame = Frame(end_a["point"], frame.u)
    start, target = frame.to_local(end_a["point"]), frame.to_local(end_b["point"])
    outward = []
    for tray, end in ((tray_a, end_a), (tray_b, end_b)):
        vector = frame.vector_to_world((1, 0, 0))
        if end.get("outward") is not None:  # a tee: the side of the main tray facing the branch
            d = end["outward"]
        else:
            horizontal_frame(tray["start"], tray["end"])
            other = tray["end"] if distance(end["point"], tray["start"]) < EPS else tray["start"]
            d = normalize(sub(end["point"], other))
        local = (dot(d, vector), dot(d, frame.v), d[2])
        axis = max(range(2), key=lambda i: abs(local[i]))
        if abs(abs(local[axis]) - 1) > 1e-5:
            raise PlanError("Bridge supports parallel or perpendicular horizontal tray axes only.")
        outward.append((axis, 1 if local[axis] > 0 else -1))
    tee = bool(end_b.get("tee"))
    user_min = max(params["min_straight"], mm_to_ft(2))
    takeup = dict((angle, joint_takeup(params, angle)) for angle in ANGLES)
    slope_angles = usable_angles(params, SLOPE_ANGLES)
    bend_angles = (90,) + tuple(a for a in slope_angles if a != 90)
    takeup[0] = (params.get("takeup") or {}).get("union", 0.0)
    takeup["tee"] = end_b.get("tee_takeup", takeup[90]) if tee else 0.0
    arrival_joint = "tee" if tee else 0
    # Largest straight any move may need (two of the largest elbows); sizes the search grid.
    minimum = user_min + 2 * max(takeup.values())
    # Horizontally a square envelope covers both tray orientations and 90-degree turns; vertically
    # only half the height is needed (horizontal runs keep their 'up', risers turn about it).
    radius = max(tray_a["width"], tray_a["height"]) / 2
    local_obstacles = [[tuple(frame.to_local(p) for p in primitive) for primitive in obstacle]
                       for obstacle in obstacles]
    boxes = obstacle_bounds(local_obstacles, (radius, radius, tray_a["height"] / 2), params["clearance"])
    # A slope swept along its run: exact slab is the centre line +/- (h/2)/cos(angle) vertically,
    # and half the tray width across it (square radius, as for horizontal runs).
    slope_boxes = {}
    for axis in range(2):
        for angle in slope_angles:
            radii = [radius, radius, tray_a["height"] / 2 / math.cos(math.radians(angle))]
            radii[axis] = 0.0
            slope_boxes[axis, angle] = obstacle_bounds(local_obstacles, tuple(radii), params["clearance"])
    source_boxes = []
    source_lines = []
    for tray in (tray_a, tray_b):
        line = [frame.to_local(tray["start"]), frame.to_local(tray["end"])]
        source_lines.append(line)
        axis = max(range(2), key=lambda i: abs(line[1][i] - line[0][i]))
        source_boxes.append(tuple((min(p[i] for p in line) - (0 if i == axis else radius),
                                   max(p[i] for p in line) + (0 if i == axis else radius))
                                  for i in range(3)))
    coords = []
    margin = minimum + radius + params["clearance"] + params["offset"]
    for axis in range(3):
        values = [start[axis], target[axis], start[axis] - minimum, start[axis] + minimum,
                  target[axis] - minimum, target[axis] + minimum]
        for bounds in boxes:
            values.extend((bounds[axis][0], bounds[axis][1]))
        values.extend((min(values) - margin, max(values) + margin))
        coords.append(sorted(set(values)))
    direction = params["direction"]
    cruise = None
    if direction in (OVER, UNDER):
        lo, hi = min(start[2], target[2]), max(start[2], target[2])
        coords[2] = [v for v in coords[2] if v >= lo - EPS] if direction == OVER else [
            v for v in coords[2] if v <= hi + EPS]
        if boxes:
            cruise = max([hi] + [b[2][1] for b in boxes]) if direction == OVER else min(
                [lo] + [b[2][0] for b in boxes])
    elif direction in (LEFT, RIGHT):
        coords[2] = sorted(set((start[2], target[2])))
        lo, hi = min(start[1], target[1]), max(start[1], target[1])
        coords[1] = [v for v in coords[1] if v >= lo - EPS] if direction == LEFT else [
            v for v in coords[1] if v <= hi + EPS]
    if len(coords[0]) * len(coords[1]) * len(coords[2]) > 100000:
        raise PlanError("Too many obstacle routing coordinates. Select fewer obstacles for this bridge.")
    first_axis, first_sign = outward[0]
    last_axis, last_sign = outward[1][0], -outward[1][1]
    # State: (point, move axis (-1 start, 0/1 horizontal, 2 riser, 3 slope), sign (for a slope: of its
    # horizontal run), last horizontal axis, has cruised, elbow angles the straight has room for at its
    # far end, slope angle (0 if not a slope)).
    initial = (start, -1, first_sign, first_axis, cruise is None, bend_angles, 0)

    def heuristic(point):
        return distance(point, target)

    tx, ty, tz = target

    def snapped(q):
        if abs(q[0] - tx) < 1e-7 and abs(q[1] - ty) < 1e-7 and abs(q[2] - tz) < 1e-7:
            return target
        return tuple(q)

    run_per_rise = dict((a, 1 / math.tan(math.radians(a))) for a in slope_angles)
    length_per_rise = dict((a, 1 / math.sin(math.radians(a))) for a in slope_angles)
    max_jog = max(user_min + 2 * takeup[90], mm_to_ft(MAX_JOG_MM))

    def moves(state):
        """(axis, sign, end point, start-joint angle, slope angle) of every allowed next straight.

        Moves too short for their own joints, or starting with a joint the previous straight has no
        room for, are skipped here (the search would reject them anyway) to keep IronPython fast.
        Returns a list: an IronPython 2.7 generator of this shape fails with "Sequence contains no elements".
        """
        result = []
        p, prev_axis, prev_sign, horizontal_axis = state[:4]
        allowed = state[5]
        prev_slope = state[6]
        joint = 0 if prev_axis == -1 else prev_slope if prev_axis in (3, 4) else 90
        if prev_axis == -1 or joint in allowed:
            # Every straight's far end is at least a union (no take-up).
            shortest = user_min + takeup[joint] - EPS
            for axis in range(3):
                if axis == prev_axis:
                    continue
                if prev_axis in (-1, 2, 3, 4) and axis != horizontal_axis:
                    continue
                for value in coords[axis]:
                    delta = value - p[axis]
                    if abs(delta) < shortest:
                        continue
                    sign = 1 if delta > 0 else -1
                    if prev_axis in (-1, 3, 4) and sign != prev_sign:
                        continue
                    q = list(p)
                    q[axis] = value
                    result.append((axis, sign, snapped(q), joint, 0, abs(delta)))
        if prev_axis in (-1, 0, 1):
            for angle in slope_angles:
                if prev_axis != -1 and angle not in allowed:
                    continue
                # A slope has its own elbow at both ends.
                shortest = user_min + 2 * takeup[angle] - EPS
                for value in coords[2]:
                    rise = abs(value - p[2])
                    if rise < EPS or rise * length_per_rise[angle] < shortest:
                        continue
                    q = list(p)
                    q[horizontal_axis] += prev_sign * rise * run_per_rise[angle]
                    q[2] = value
                    result.append((3, prev_sign, snapped(q), angle, angle, rise * length_per_rise[angle]))
            # Sideways step (jog): a level diagonal that shifts across and continues the same way,
            # e.g. a 45-degree offset round a wall corner where two 90-degree turns do not fit.
            # Capped to short offsets: larger shifts use 90-degree turns (no long diagonal runs).
            lateral = 1 - horizontal_axis
            for angle in slope_angles:
                if prev_axis != -1 and angle not in allowed:
                    continue
                shortest = user_min + 2 * takeup[angle] - EPS
                for value in coords[lateral]:
                    side = abs(value - p[lateral])
                    if side < EPS or side > max_jog or side * length_per_rise[angle] < shortest:
                        continue
                    q = list(p)
                    q[horizontal_axis] += prev_sign * side * run_per_rise[angle]
                    q[lateral] = value
                    result.append((4, prev_sign, snapped(q), angle, angle, side * length_per_rise[angle]))
        return result

    queue, costs, previous = [(heuristic(start), 0.0, initial)], {initial: 0.0}, {}
    penalty = minimum * 0.1
    infinity = float("inf")
    clear_boxes = boxes + source_boxes
    # The branch of a tee has to end inside the main tray's envelope.
    arrival_clear = boxes + source_boxes[:1] if tee else clear_boxes
    slope_clear = dict((key, value + source_boxes) for key, value in slope_boxes.items())
    final = None
    visited = 0
    edges = 0
    while queue:
        estimate, cost, state = heapq.heappop(queue)
        if max_cost is not None and estimate > max_cost:
            break
        if cost > costs[state] + EPS:
            continue
        p, prev_axis, prev_sign, horizontal_axis, has_cruised, allowed = state[:6]
        if p == target and state != initial:
            final = state
            break
        visited += 1
        if visited > 10000:
            raise PlanError("Bridge search limit reached. Reduce obstacles or increase available space.")
        for axis, sign, q, joint, slope, seg_len in moves(state):
            if q == start:
                continue
            arriving = q == target
            if arriving and (axis if axis < 3 else horizontal_axis, sign) != (last_axis, last_sign):
                continue
            if arriving and (axis == 2 or (tee and axis >= 3)):
                continue
            # Far-end joint: a union (or the tee) into the target, the slope's/jog's own elbow, a
            # riser's 90-degree elbow, or for a horizontal run whichever elbow the next move needs.
            far_options = ((arrival_joint,) if arriving and axis < 2 else (slope,) if axis >= 3 else
                           (90,) if axis == 2 else bend_angles)
            base = user_min + takeup[joint] - EPS
            next_allowed = tuple(a for a in far_options if seg_len >= base + takeup[a])
            if not next_allowed:
                continue
            vertical = axis in (2, 3)
            next_cruised = has_cruised or (not vertical and cruise is not None and (
                (direction == OVER and p[2] >= cruise - EPS) or
                (direction == UNDER and p[2] <= cruise + EPS)))
            if arriving and not next_cruised:
                continue
            if not vertical and state != initial and not arriving and cruise is not None:
                if direction == OVER and p[2] < cruise - EPS:
                    continue
                if direction == UNDER and p[2] > cruise + EPS:
                    continue
            edges += 1
            if edges > 150000:
                raise PlanError("Bridge search limit reached. Reduce obstacles or increase available space.")
            next_state = (q, axis, sign, axis if axis < 2 else horizontal_axis, next_cruised, next_allowed, slope)
            next_cost = cost + seg_len + penalty
            if next_cost + EPS >= costs.get(next_state, infinity):
                continue
            if not segment_is_clear(p, q, slope_clear[horizontal_axis, slope] if axis == 3 else
                                    arrival_clear if arriving else clear_boxes):
                continue
            costs[next_state] = next_cost
            previous[next_state] = state
            heapq.heappush(queue, (next_cost + heuristic(q), next_cost, next_state))
    if final is None:
        raise PlanError("No clear bridge with the minimum straight length fits the selected ends and "
                        "obstacles. Move the ends, reduce clearance/minimum, or select a different gap.")
    plan["search_cost"] = costs[final]
    points = []
    axes = []
    slopes = []
    while final != initial:
        points.append(final[0])
        axes.append(final[1])
        slopes.append(final[6])
        final = previous[final]
    points.append(start)
    points.reverse()
    axes.reverse()
    slopes.reverse()
    joints = set((s0 or s1 or 90) for s0, s1 in zip(slopes[:-1], slopes[1:]))
    joints.update(s for s in (slopes[0], slopes[-1]) if s)
    slope_angles = sorted(set(s for s, ax in zip(slopes, axes) if s and ax == 3))
    jog_angles = sorted(set(s for s, ax in zip(slopes, axes) if s and ax == 4))
    plan.update(frame=frame, length=distance(start, target), width=tray_a["width"],
                height=tray_a["height"], ends=(end_a, end_b), direction=STRAIGHT, angle=90,
                spatial_bridge=True, sloped=bool(slope_angles), slope_angles=slope_angles,
                jog_angles=jog_angles, bend_angles=sorted(joints, reverse=True) or [90])
    plan["source_lines"] = source_lines
    for bounds in boxes:
        plan["obstacle_boxes"].extend([
            {"view": "plan", "s": bounds[0], "y": bounds[1], "clearance": 0},
            {"view": "elevation", "s": bounds[0], "y": bounds[2], "clearance": 0}])
    plan["warnings"].append("Spatial bridge uses 90-degree turns, 30/45/60-degree slopes for level changes, "
                            "30/45/60-degree sideways steps and conservative obstacle envelopes. Only selected "
                            "obstacles are checked.")
    if not params["auto_offset"]:
        plan["warnings"].append("For spatial bridges, manual offset extends the search margin; "
                                "it is not a fixed rise/drop distance.")
    _set_segments(plan, frame, points, params)
    route_len = sum(s["length"] for s in plan["segments"])
    if route_len - plan["length"] > max(plan["length"], mm_to_ft(2000)):
        plan["warnings"].append(u"This bridge is {} long for a {} gap: the fittings need more room than the "
                                u"ends allow. Consider moving or trimming an end instead.".format(
                                    fmt_mm(route_len), fmt_mm(plan["length"])))
    # Straights are sized per joint: none for a union, measured take-up for each elbow.
    plan["minimum_straight"] = user_min
    plan["fitting_allowance"] = 2 * max(takeup[a] for a in plan["bend_angles"])
    plan["takeup_measured"] = all(a in (params.get("takeup") or {}) for a in plan["bend_angles"])
    plan["summary"] = summary_lines(plan)
    return plan


def _trimmed(tray, end, trim):
    """Copy of tray with its free ``end`` pulled back along the tray by ``trim`` (ft)."""
    if trim <= EPS:
        return tray, end
    at_start = distance(end["point"], tray["start"]) < EPS
    other = tray["end"] if at_start else tray["start"]
    point = add(end["point"], scale(normalize(sub(other, end["point"])), trim))
    new_end = dict(end, point=point)
    result = dict(tray, ends=[new_end if e is end else e for e in tray["ends"]])
    result["start" if at_start else "end"] = point
    return result, new_end


def _spatial_bridge(tray_a, tray_b, end_a, end_b, obstacles, params, tee_targets=None):
    """Spatial bridge from the ends as picked, or with free ends pulled back to fit the fittings.

    Tries growing trims (cheapest first) and keeps the route with the least new tray plus trimmed
    length; a candidate whose straight-line lower bound cannot beat the best so far is skipped.
    Searches are first capped at a reasonable length so a long loop at the picked ends does not
    hide (or slow down) a short route after a small trim; the uncapped loop is only the fallback.
    ``tee_targets`` (tee end dicts on tray_b, nearest first) replaces end_b: the tee may go at any of
    them and only tray_a is pulled back.
    """
    user_min = max(params["min_straight"], mm_to_ft(2))
    keep = user_min + 2 * max(joint_takeup(params, a) for a in ANGLES)
    targets = list(tee_targets) if tee_targets else [end_b]
    limits = []
    for tray in (tray_a, tray_b):
        total = distance(tray["start"], tray["end"])
        limits.append(min(total / 2, total - keep))
    if tee_targets:
        limits[1] = 0.0
    # Single-end pull-backs first: they usually suffice, and the route they find tightens the cap
    # for the (rarer, more expensive) pull-back of both ends.
    trims = [(0.0, 0.0)]
    for both in (False, True):
        for step in TRIM_STEPS_MM:
            t = mm_to_ft(step)
            trims.extend(c for c in (((t, t),) if both else ((t, 0.0), (0.0, t)))
                         if c[0] <= limits[0] + EPS and c[1] <= limits[1] + EPS)
    candidates = [(trim_a, trim_b, target) for trim_a, trim_b in trims for target in targets]
    best, best_score = None, None
    for trim_a, trim_b, target in candidates:
        a, ea = _trimmed(tray_a, end_a, trim_a)
        b, eb = _trimmed(tray_b, target, trim_b)
        if best is not None and distance(ea["point"], eb["point"]) + trim_a + trim_b >= best_score - EPS:
            continue
        # Same threshold as the long-loop warning, plus room for the per-move search penalty.
        cap = 2 * distance(end_a["point"], target["point"]) + mm_to_ft(2000) + keep
        # Scored on the search cost (length plus a small per-segment penalty) so later candidates
        # can stop as soon as they cannot beat the best route found so far.
        limit = cap if best is None else min(cap, best_score - trim_a - trim_b)
        try:
            plan = _orthogonal_bridge(_new_plan(MODE_BRIDGE), a, b, ea, eb, obstacles, params, limit)
        except PlanError:
            continue
        score = plan["search_cost"] + trim_a + trim_b
        if best is None or score < best_score - EPS:
            best, best_score = plan, score
            plan["trims"] = (trim_a, trim_b)
            plan["original_ends"] = (end_a["point"], target["point"])
            plan["tee_target"] = target if tee_targets else None
    if best is None:
        error = None
        for target in targets:
            try:
                best = _orthogonal_bridge(_new_plan(MODE_BRIDGE), tray_a, tray_b, end_a, target, obstacles, params)
            except PlanError as ex:
                error = error or ex
                continue
            best["trims"] = (0.0, 0.0)
            best["original_ends"] = (end_a["point"], target["point"])
            best["tee_target"] = target if tee_targets else None
            break
        if best is None:
            raise error
    best["summary"] = summary_lines(best)
    return best


def _plan_axis(tray):
    """(start point, horizontal unit direction, horizontal length) of a tray."""
    start, end = tray["start"], tray["end"]
    d = (end[0] - start[0], end[1] - start[1], 0.0)
    run = length(d)
    if run < EPS:
        raise PlanError("Vertical trays cannot take a tee.")
    return start, scale(d, 1.0 / run), run


def _side_hit(branch, main):
    """(free end of ``branch``, distance along ``main``) when that end points squarely at main's side."""
    start, u, run = _plan_axis(main)
    zone = min(mm_to_ft(TEE_AUTO_END_MM), run / 4)
    best = None
    for end in branch["ends"]:
        if end["connected"]:
            continue
        other = branch["end"] if distance(end["point"], branch["start"]) < EPS else branch["start"]
        d = sub(end["point"], other)
        d = (d[0], d[1], 0.0)
        if length(d) < EPS or abs(end["point"][2] - other[2]) > mm_to_ft(MAX_LEVEL_DIFF_MM):
            continue
        d = normalize(d)
        if abs(dot(d, u)) > 0.02:
            continue
        rel = sub(end["point"], start)
        s = dot(rel, u)
        reach = -dot((rel[0], rel[1], 0.0), d)
        if zone <= s <= run - zone and 0 < reach <= mm_to_ft(TEE_AUTO_REACH_MM) and (
                best is None or reach < best[2]):
            best = (end, s, reach)
    return best[:2] if best else None


def _in_line(end_point, main):
    """True when a point lies on the main tray's axis line (in plan): a side tee makes no sense then."""
    start, u, _ = _plan_axis(main)
    rel = sub(end_point, start)
    return abs(rel[0] * u[1] - rel[1] * u[0]) < main["width"] / 2


def tee_request(tray_a, tray_b):
    """How two bridge trays connect: None (end to end) or {"branch", "main", "end", "s", "auto"}.

    A click (``pick_point``) on the second tray decides: near one of its ends connects to that end,
    further along tees into its side. Without clicks, a free end of one tray that points squarely at
    the side of the other (away from its ends) tees into it.
    """
    pick = tray_b.get("pick_point")
    if pick is not None:
        start, u, run = _plan_axis(tray_b)
        s = dot(sub(pick, start), u)
        zone = min(mm_to_ft(TEE_END_PICK_MM), run / 4)
        if not zone <= s <= run - zone:
            return None
        ends = [e for e in tray_a["ends"] if not e["connected"]]
        if not ends:
            raise PlanError("The first tray has no free end to connect to the side of the second tray.")
        anchor = tray_a.get("pick_point") or add(start, scale(u, s))
        end = min(ends, key=lambda e: distance(e["point"], anchor))
        if _in_line(end["point"], tray_b):
            return None
        return {"branch": tray_a, "main": tray_b, "end": end, "s": s, "auto": False}
    if tray_a.get("pick_point") is not None:
        return None
    for branch, main in ((tray_a, tray_b), (tray_b, tray_a)):
        try:
            hit = _side_hit(branch, main)
        except PlanError:
            continue
        if hit:
            return {"branch": branch, "main": main, "end": hit[0], "s": hit[1], "auto": True}
    return None


def tee_takeups(params, branch, main):
    """(along the main, along the branch) length a tee takes from each tray (measured or estimated)."""
    measured = params.get("takeup") or {}
    elbow = joint_takeup(params, 90)
    along_main = measured.get("tee_main", max(elbow, branch["width"] / 2))
    along_branch = measured.get("tee_branch", max(elbow, main["width"] / 2))
    return along_main, along_branch


def _tee_bridge(request, obstacles, params):
    """Bridge whose second end tees into the side of the main tray, slid along it for the best route."""
    branch, main, end_a = request["branch"], request["main"], request["end"]
    if abs(main["end"][2] - main["start"][2]) > mm_to_ft(MAX_LEVEL_DIFF_MM):
        raise PlanError("A tee needs a horizontal main tray.")
    start, u, run = _plan_axis(main)
    user_min = max(params["min_straight"], mm_to_ft(2))
    along_main, along_branch = tee_takeups(params, branch, main)
    lo, hi = along_main + user_min, run - along_main - user_min
    if lo > hi:
        raise PlanError(u"The second tray ({}) is too short to take a tee.".format(fmt_mm(run)))
    n = (-u[1], u[0], 0.0)
    if dot(sub(end_a["point"], start), n) < 0:
        n = scale(n, -1.0)
    projection = dot(sub(end_a["point"], start), u)
    bases = [request["s"], projection]
    positions = []
    for s in bases + [b + sign * mm_to_ft(d) for b in bases[:1] for d in TEE_SLIDE_MM for sign in (-1, 1)]:
        s = min(max(s, lo), hi)
        if all(abs(s - p) > mm_to_ft(5) for p in positions):
            positions.append(s)
    z0, z1 = main["start"][2], main["end"][2]
    targets = []
    for s in positions:
        point = add(start, scale(u, s))
        point = (point[0], point[1], z0 + (z1 - z0) * s / run)
        targets.append({"point": point, "connected": False, "tee": True, "outward": n,
                        "tee_takeup": along_branch, "s": s})
    targets.sort(key=lambda t: distance(t["point"], end_a["point"]))
    plan = _spatial_bridge(branch, main, end_a, None, obstacles, params, targets)
    target = plan["tee_target"]
    plan["tee"] = {"point": target["point"], "s": target["s"], "outward": n, "auto": request["auto"],
                   "main_label": main.get("label"), "takeup_main": along_main, "takeup_branch": along_branch}
    plan["warnings"].append(u"Tees into the side of {} {} from its start{}.".format(
        main.get("label", u"the main tray"), fmt_mm(target["s"]),
        u" (found automatically: the free end points at its side)" if request["auto"] else u""))
    plan["summary"] = summary_lines(plan)
    return plan


def nearest_free_ends(tray_a, tray_b):
    """Return (end_a, end_b): the closest pair of unconnected ends of two trays."""
    best = None
    for ea in tray_a["ends"]:
        for eb in tray_b["ends"]:
            d = distance(ea["point"], eb["point"])
            if best is None or d < best[0]:
                best = (d, ea, eb)
    if best is None:
        raise PlanError("Could not read the tray ends.")
    _, ea, eb = best
    if ea["connected"] or eb["connected"]:
        raise PlanError("The facing tray ends are already connected. Cut a gap first (or use Detour mode).")
    return ea, eb


def bridge_ends(tray_a, tray_b):
    """(end_a, end_b) for an end-to-end bridge: the clicked end of the second tray, if clicked."""
    pick = tray_b.get("pick_point")
    if pick is None:
        return nearest_free_ends(tray_a, tray_b)
    end_b = min(tray_b["ends"], key=lambda e: distance(e["point"], pick))
    if end_b["connected"]:
        raise PlanError("The clicked end of the second tray is already connected. Click further along the "
                        "tray to tee into its side, or cut a gap first.")
    free = [e for e in tray_a["ends"] if not e["connected"]]
    if not free:
        raise PlanError("The first tray has no free end. Cut a gap first (or use Detour mode).")
    anchor = tray_a.get("pick_point") or end_b["point"]
    return min(free, key=lambda e: distance(e["point"], anchor)), end_b


def _angle_free(plan, spatial):
    """Run a spatial bridge whose route does not depend on the Angle setting; flag it (also on failure)."""
    plan["angle_free"] = True
    result = spatial()
    result["angle_free"] = True
    return result


def plan_bridge(tray_a, tray_b, obstacles, params):
    """Plan a new run that joins the facing free ends of two in-line trays around an obstacle."""
    raw = params
    params = _planning_params(params)
    plan = _new_plan(MODE_BRIDGE)
    try:
        if tray_a.get("id_value") is not None and tray_a.get("id_value") == tray_b.get("id_value"):
            raise PlanError("Pick two different tray segments.")
        tol = mm_to_ft(SIZE_TOL_MM)
        request = tee_request(tray_a, tray_b)
        if request is not None:
            plan["angle_free"] = True
            tee_plan = _tee_bridge(request, obstacles, raw)
            tee_plan["angle_free"] = True
            tee_plan["tee"]["swapped"] = request["branch"] is tray_b
            if abs(tray_a["width"] - tray_b["width"]) > tol or abs(tray_a["height"] - tray_b["height"]) > tol:
                tee_plan["warnings"].append(u"The trays are different sizes: Revit places the tee from the main "
                                            u"tray's routing preferences; check that it supports the branch size.")
            return tee_plan
        if abs(tray_a["width"] - tray_b["width"]) > tol or abs(tray_a["height"] - tray_b["height"]) > tol:
            raise PlanError(u"The trays are different sizes ({} x {} and {} x {}); only equal sizes can be joined."
                            .format(fmt_mm(tray_a["width"]), fmt_mm(tray_a["height"]),
                                    fmt_mm(tray_b["width"]), fmt_mm(tray_b["height"])))
        end_a, end_b = bridge_ends(tray_a, tray_b)
        a, b = end_a["point"], end_b["point"]
        delta = sub(b, a)
        horizontal_gap = math.sqrt(delta[0] ** 2 + delta[1] ** 2)
        if horizontal_gap < EPS or abs(delta[2]) > mm_to_ft(MAX_BRIDGE_LATERAL_MM):
            return _angle_free(plan, lambda: _spatial_bridge(tray_a, tray_b, end_a, end_b, obstacles, raw))
        frame, gap = horizontal_frame(a, b, "gap between the tray ends")
        lateral = mm_to_ft(MAX_BRIDGE_LATERAL_MM)
        for tray in (tray_a, tray_b):
            axis = normalize(sub(tray["end"], tray["start"]))
            sin_mis = abs(axis[0] * frame.u[1] - axis[1] * frame.u[0])
            off_line = [frame.to_local(tray[k]) for k in ("start", "end")]
            if abs(axis[2]) > 0.01 or sin_mis > math.sin(math.radians(MAX_BRIDGE_MISALIGN_DEG)) or \
                    any(abs(q[1]) > lateral or abs(q[2]) > lateral for q in off_line):
                return _angle_free(plan, lambda: _spatial_bridge(tray_a, tray_b, end_a, end_b, obstacles, raw))
        plan.update({"frame": frame, "length": gap, "width": tray_a["width"], "height": tray_a["height"],
                     "ends": (end_a, end_b)})
        if gap < params["min_straight"] - EPS:
            raise PlanError(u"The gap ({}) is shorter than the minimum straight.".format(fmt_mm(gap)))
        half_w, half_h = tray_a["width"] / 2.0, tray_a["height"] / 2.0

        ext = None
        if obstacles:
            ext = obstacle_extents(_local_obstacles(frame, obstacles), half_w, half_h, params["clearance"])
            plan["obstacle_boxes"] = _obstacle_boxes(ext, params["clearance"])
        if ext is None and params["direction"] == AUTO:
            direction, offset = STRAIGHT, 0.0
        else:
            direction, offset = _resolve_direction_and_offset(plan, ext, params, half_w, half_h)
        params = _vertical_params(params, direction)
        if direction == STRAIGHT:
            result = _finish(plan, frame, 0.0, gap, None, STRAIGHT, 0.0, params, None)
            if not route_is_clear(result["local_points"], local_obstacle_bounds(
                    frame, obstacles, half_w, half_h, params["clearance"])):
                raise PlanError("Straight join clashes with a selected obstacle. Choose Auto or an avoidance direction.")
            return result
        run = transition_run(offset, params["angle"])
        if ext is not None:
            span = obstacle_span(ext, direction, params["clearance"])
        else:
            span = (run, gap - run)
        result = _finish(plan, frame, 0.0, gap, span, direction, offset, params, None)
        accepted_clash = any("will still clash" in w for w in result["warnings"])
        if not accepted_clash and not route_is_clear(result["local_points"], local_obstacle_bounds(
                frame, obstacles, half_w, half_h, params["clearance"])):
            return _spatial_bridge(tray_a, tray_b, end_a, end_b, obstacles, raw)
        return result
    except PlanError as ex:
        plan["ok"] = False
        plan["error"] = to_text(ex)
        return plan


def route_counts(plan):
    """(new tray segments, fittings, route length, straight-line length) of a planned route."""
    open_ends = plan.get("open_ends") or (False, False)
    segs = plan.get("segments", [])
    route_len = sum(s["length"] for s in segs)
    straight = distance(plan["local_points"][0], plan["local_points"][-1]) if plan.get("local_points") else 0.0
    if plan.get("mode") == MODE_DETOUR:
        # The tray keeps one piece; a far piece is added unless the detour ends at a free end.
        new_trays = len(segs) + (0 if any(open_ends) else 1) - (1 if all(open_ends) else 0)
        fittings = len(segs) + 1 - sum(1 for flag in open_ends if flag)
    else:
        new_trays = len(segs) + (1 if plan.get("tee") else 0)
        fittings = len(segs) + 1
    return new_trays, fittings, route_len, straight


def summary_lines(plan):
    lines = []
    direction = plan.get("direction")
    if plan.get("individual_routes"):
        lines.append("Method: individual obstacle avoidance, {}{} bends".format(plan["angle"], DEG))
        for i, route in enumerate(plan["individual_routes"]):
            lines.append("Group {}: {}, offset {}, cuts {} / {}".format(
                i + 1, DIRECTION_LABELS[route["direction"]], fmt_mm(route["offset"]),
                fmt_mm(route["cut"][0]), fmt_mm(route["cut"][1])))
    elif plan.get("spatial_bridge"):
        lines.append("Method: obstacle-checked spatial bridge, 90{0} turns{1}{2}".format(
            DEG, u", {} slopes".format(u"/".join(u"{}{}".format(a, DEG) for a in plan["slope_angles"]))
            if plan.get("slope_angles") else u"",
            u", {} sideways steps".format(u"/".join(u"{}{}".format(a, DEG) for a in plan["jog_angles"]))
            if plan.get("jog_angles") else u""))
        trims = plan.get("trims") or ()
        if any(t > EPS for t in trims):
            lines.append(u"Tray ends pulled back to make room for the fittings: {}".format(u", ".join(
                u"{} tray {}".format(name, fmt_mm(t)) for name, t in zip((u"first", u"second"), trims) if t > EPS)))
        if plan.get("tee"):
            lines.append(u"Tee into the side of {} at {} from its start".format(
                plan["tee"].get("main_label") or u"the main tray", fmt_mm(plan["tee"]["s"])))
        reroute = plan.get("run_reroute")
        if reroute:
            lines.append(u"Connected run re-routed: {} element(s) removed, cut {} into {}".format(
                len(reroute["removed"]), fmt_mm(reroute["cut_at"]),
                reroute["cut_tray"].get("label", u"the next straight")))
    elif direction == STRAIGHT:
        lines.append(u"Method: straight join")
    else:
        lines.append(u"Method: {}, {}{} bends".format(DIRECTION_LABELS.get(direction, direction),
                                                       plan.get("angle"), DEG))
        needed = plan.get("required", {}).get(direction)
        text = u"Offset: {}".format(fmt_mm(plan.get("offset", 0.0)))
        if needed is not None:
            text += u" (clash needs {})".format(fmt_mm(needed))
        lines.append(text)
        gap = achieved_clearance(plan)
        if gap is not None:
            lines.append(u"Gap to obstacle: {} (Clearance set {})".format(fmt_mm(gap), fmt_mm(plan["clearance"])))
    if plan.get("cut"):
        lines.append(u"Cuts at {} and {} from the tray start".format(fmt_mm(plan["cut"][0]), fmt_mm(plan["cut"][1])))
    open_ends = plan.get("open_ends") or (False, False)
    if any(open_ends):
        lines.append(u"The clash reaches the free {} of the tray: {} moved sideways (no return bend)".format(
            u"start and end" if all(open_ends) else u"start" if open_ends[0] else u"end",
            u"the whole tray is" if all(open_ends) else u"that end is"))
    new_trays, fittings, route_len, straight = route_counts(plan)
    lines.append(u"New tray segments: {}   Fittings: {}".format(new_trays, fittings))
    lines.append(u"Detour length: {} (+{} over straight)".format(fmt_mm(route_len), fmt_mm(route_len - straight)))
    if plan.get("minimum_straight") is not None:
        lines.append(u"Min. straight: {}; allowed for fittings per straight: {} ({})".format(
            fmt_mm(plan["minimum_straight"]), fmt_mm(plan.get("fitting_allowance", 0.0)),
            u"measured from your fittings" if plan.get("takeup_measured") else u"estimate"))
    return lines


# --- regenerate: ranked alternative routes ------------------------------------------------------
OPTION_COUNT = 5
# Test builds tried beyond OPTION_COUNT before giving up on filling the list.
OPTION_EXTRA_TRIALS = 4
OPTION_BUDGET_S = 25.0
# Ranking weights, in mm of extra tray: every fitting and every compromise "costs" this much tray.
OPTION_COSTS_MM = {"fitting": 400.0, "problem": 300.0, "radius": 300.0, "leg": 100.0, "reroute": 1500.0,
                   "removed": 300.0, "odd_angle": 100.0, "moved_per_mm_m": 0.25}
STANDARD_ANGLES = (45, 90)
RELAXED_RADIUS_MM = 150.0
RELAXED_MIN_STRAIGHT_MM = 50.0


def option_variants(mode, params, obstacle_count, cut_points=False):
    """Parameter sets (mm) Regenerate plans: every direction x bend angle (x grouped/individual obstacles).

    Clearance, Offset step, Min. straight, Min. bend radius and the elbow options stay as the user set
    them: Regenerate never trades away a clearance or a fitting limit.
    """
    base = normalize_params(params)
    if mode == MODE_DETOUR:
        directions = list(DETOUR_DIRECTIONS)
    elif obstacle_count:
        directions = [AUTO, OVER, UNDER, LEFT, RIGHT]
    else:
        directions = [AUTO]
    individual = [False, True] if mode == MODE_DETOUR and obstacle_count > 1 and not cut_points else [False]
    variants = []
    for separate in individual:
        for direction in directions:
            for angle in ANGLES:
                variants.append(dict(base, direction=direction, angle=angle, auto_offset=bool(obstacle_count),
                                     individual_obstacles=separate))
    return variants


def relaxed_variants(variants, params):
    """(variants, description) with smaller elbows and a shorter Min. straight allowed; ([], u"") if
    nothing can be relaxed. Only offered when no route fits the user's own limits; clearance never changes.
    """
    base = normalize_params(params)
    changes, parts = {}, []
    if not base["shorten_fitting_legs"]:
        changes["shorten_fitting_legs"] = True
        parts.append(u"{:g} mm elbow legs".format(MIN_FITTING_LEG_MM))
    if base["min_bend_radius_mm"] <= EPS or base["min_bend_radius_mm"] > RELAXED_RADIUS_MM + EPS:
        changes["min_bend_radius_mm"] = RELAXED_RADIUS_MM
        parts.append(u"bend radius down to {:g} mm".format(RELAXED_RADIUS_MM))
    if base["min_straight_mm"] > RELAXED_MIN_STRAIGHT_MM + EPS:
        changes["min_straight_mm"] = max(RELAXED_MIN_STRAIGHT_MM, base["min_straight_mm"] / 2.0)
        parts.append(u"Min. straight {:g} mm".format(changes["min_straight_mm"]))
    if not changes:
        return [], u""
    return [dict(v, **changes) for v in variants], u", ".join(parts)


RELAXABLE_KEYS = ("shorten_fitting_legs", "min_bend_radius_mm", "min_straight_mm")


def own_limits(params):
    """The user's own values of the settings a RELAXED option may change."""
    base = normalize_params(params)
    return dict((key, base[key]) for key in RELAXABLE_KEYS)


def relaxed_in_use(params, option):
    """True if `option` is RELAXED and `params` still hold its relaxed values (not edited since)."""
    if not option or not option.get("relaxed"):
        return False
    try:
        mine, theirs = own_limits(params), own_limits(option["params"])
    except PlanError:
        return False
    return all(abs(float(mine[k]) - float(theirs[k])) <= EPS for k in RELAXABLE_KEYS)


LIMIT_NAMES = {"shorten_fitting_legs": u"{:g} mm elbow legs".format(MIN_FITTING_LEG_MM),
               "min_bend_radius_mm": u"Min. bend radius", "min_straight_mm": u"Min. straight"}


def _limit_value(key, value):
    if key == "shorten_fitting_legs":
        return u"on" if value else u"off"
    if key == "min_bend_radius_mm" and value <= EPS:
        return u"0 (never reduce)"
    return u"{:g} mm".format(value)


def relaxed_notice(option, own):
    """Says which of the user's limits a RELAXED option overrides and what they were."""
    theirs = own_limits(option["params"])
    parts = [u"{} {} (yours: {})".format(LIMIT_NAMES[k], _limit_value(k, theirs[k]), _limit_value(k, own[k]))
             for k in RELAXABLE_KEYS if own and k in own and abs(float(theirs[k]) - float(own[k])) > EPS]
    return (u"Option {} is RELAXED - it goes below your limits: {}. Clearance is unchanged. Check the cables' "
            u"minimum bending radius.".format(option["number"], u"; ".join(parts) or option["relaxed"]))


def keep_own_limits(params, own):
    """`params` with the user's own limits restored, so a RELAXED option is never remembered as a setting."""
    result = dict(params)
    result.update(own or {})
    return result


def plan_angles(plan):
    """Bend angles a planned route uses."""
    if plan.get("spatial_bridge"):
        return [90] + list(plan.get("slope_angles") or ()) + list(plan.get("jog_angles") or ())
    if plan.get("direction") == STRAIGHT or plan.get("angle") is None:
        return []
    return [plan["angle"]]


def _moved_area_mm_m(plan):
    """How far a detour moves tray off its original line, summed along it (mm x m)."""
    points = plan.get("local_points") or ()
    area = 0.0
    for a, b in zip(points, points[1:]):
        da, db = math.hypot(a[1], a[2]), math.hypot(b[1], b[2])
        area += ft_to_mm((da + db) / 2.0) * ft_to_mm(abs(b[0] - a[0])) / 1000.0
    return area


def option_score(plan):
    """Cost of an ok plan in mm of extra tray (lower is better): the extra route length, each fitting,
    any gap beyond the clearance, tray moved off its line, pulled-back ends, smaller elbows, odd angles
    and re-routed runs."""
    costs = OPTION_COSTS_MM
    _, fittings, route_len, straight = route_counts(plan)
    cost = max(ft_to_mm(route_len - straight), 0.0) + fittings * costs["fitting"]
    cost += _moved_area_mm_m(plan) * costs["moved_per_mm_m"]
    gap = achieved_clearance(plan)
    if gap is not None:
        cost += max(ft_to_mm(gap - plan["clearance"]), 0.0)
    cost += (_fitting_problems(plan) or 0) * costs["problem"]
    cost += ft_to_mm(sum(plan.get("trims") or ()))
    option = plan.get("fitting_option") or {}
    if option.get("radius"):
        cost += costs["radius"]
    if option.get("leg"):
        cost += costs["leg"]
    reroute = plan.get("run_reroute")
    if reroute:
        cost += costs["reroute"] + costs["removed"] * len(reroute.get("removed") or ())
    if any(a not in STANDARD_ANGLES for a in plan_angles(plan)):
        cost += costs["odd_angle"]
    return cost


def option_signature(plan):
    """Geometry of a route (~3 mm grid) plus its elbow size: equal signatures build the same thing."""
    points = []
    for seg in plan.get("segments") or ():
        for p in (seg["start"], seg["end"]):
            points.append(tuple(int(round(c * 100)) for c in p))
    option = plan.get("fitting_option") or {}
    tee = plan.get("tee") or {}
    return (tuple(points), round(option.get("radius") or 0.0, 3), round(option.get("leg") or 0.0, 3),
            tuple(plan.get("open_ends") or ()), round(tee.get("s") or 0.0, 2))


def option_method(plan, params):
    """Kind of route, used to offer different approaches before variants of the same one."""
    if plan.get("individual_routes"):
        return ("individual",) + tuple(r["direction"] for r in plan["individual_routes"])
    if plan.get("run_reroute"):
        return ("reroute", params.get("direction"))
    if plan.get("spatial_bridge"):
        return ("tee" if plan.get("tee") else "spatial", params.get("direction"))
    return ("inline", plan.get("direction"))


def option_label(plan, relaxed=u""):
    """One line describing an option for the list."""
    angles = plan_angles(plan)
    odd = [a for a in sorted(set(angles)) if a != 90]
    _, fittings, route_len, straight = route_counts(plan)
    if plan.get("individual_routes"):
        routes = plan["individual_routes"]
        names = sorted(set(DIRECTION_LABELS[r["direction"]] for r in routes))
        head = u"{} individual detours ({}), {}{} bends".format(len(routes), u"/".join(names), plan["angle"], DEG)
    elif plan.get("run_reroute"):
        head = u"Re-route the connected run ({} element(s) replaced)".format(
            len(plan["run_reroute"].get("removed") or ()))
    elif plan.get("spatial_bridge"):
        head = u"Tee into the side" if plan.get("tee") else u"3D route"
        head += u", 90{}".format(DEG) + (u" + " + u"/".join(u"{}{}".format(a, DEG) for a in odd) if odd else u"")
    elif plan.get("direction") == STRAIGHT:
        head = u"Straight join"
    elif not fittings and plan.get("mode") == MODE_DETOUR and plan.get("offset"):
        # Both ends free: the whole tray moves, so there are no bends.
        head = u"Move the whole tray {} {}".format(plan["direction"].lower(), fmt_mm(plan["offset"]))
    else:
        head = u"{}, {}{} bends".format(DIRECTION_LABELS.get(plan["direction"], plan["direction"]), plan["angle"], DEG)
    parts = [head]
    gap = achieved_clearance(plan)
    if gap is not None:
        parts.append(u"gap {}".format(fmt_mm(gap)))
    parts.append(u"{} fittings".format(fittings))
    parts.append(u"+{} tray".format(fmt_mm(max(route_len - straight, 0.0))))
    trims = [t for t in plan.get("trims") or () if t > EPS]
    if trims:
        parts.append(u"end pulled back {}".format(fmt_mm(max(trims))))
    option = plan.get("fitting_option") or {}
    if option.get("radius"):
        parts.append(u"R{} elbows".format(fmt_mm(option["radius"]).replace(u" mm", u"")))
    elif option.get("leg"):
        parts.append(u"short-leg elbows")
    if relaxed:
        parts.append(u"RELAXED: " + relaxed)
    return u" | ".join(parts)


def evaluate_options(plan_fn, variants, clock=None, budget_s=OPTION_BUDGET_S, relaxed=u""):
    """Plan every variant; return (candidates best first, stats).

    plan_fn(params_mm) -> plan or None. Failed plans and plans that knowingly clash are dropped, as
    are routes that build the same geometry as a better one. A route that ignores the Angle setting
    (spatial bridge or tee) is planned once per direction. ``clock`` (e.g. time.time) with
    ``budget_s`` stops a long search early; stats["stopped"] then says so.
    """
    start = clock() if clock else 0.0
    best = {}
    skip = set()
    stats = {"planned": 0, "failed": 0, "stopped": False, "error": None}
    for index, params in enumerate(variants):
        group = (params["direction"], params["individual_obstacles"])
        if group in skip:
            continue
        if clock and clock() - start > budget_s:
            stats["stopped"] = True
            break
        try:
            plan = plan_fn(params)
        except PlanError as ex:
            plan = {"ok": False, "error": to_text(ex)}
        stats["planned"] += 1
        if plan and plan.get("angle_free"):
            skip.add(group)
        if not plan or not plan.get("ok") or any("will still clash" in w for w in plan.get("warnings") or ()):
            stats["failed"] += 1
            if plan and plan.get("error") and stats["error"] is None:
                stats["error"] = plan["error"]
            continue
        candidate = {"params": params, "plan": plan, "score": option_score(plan), "index": index,
                     "method": option_method(plan, params), "relaxed": relaxed,
                     "label": option_label(plan, relaxed)}
        # The same route from several settings: keep the one whose Angle matches what it builds.
        honest = plan.get("angle") in (None, params["angle"]) or bool(plan.get("angle_free"))
        candidate["honest"] = honest
        key = option_signature(plan)
        current = best.get(key)
        if current is None or candidate["score"] < current["score"] - 1e-6 or (
                abs(candidate["score"] - current["score"]) <= 1e-6 and honest and not current["honest"]):
            best[key] = candidate
    candidates = sorted(best.values(), key=lambda c: (c["score"], c["index"]))
    return candidates, stats


def diverse_order(candidates):
    """Best route of each approach first (best first), then the remaining variants by score."""
    first, rest, methods = [], [], set()
    for candidate in candidates:
        if candidate["method"] in methods:
            rest.append(candidate)
        else:
            methods.add(candidate["method"])
            first.append(candidate)
    return first + rest


def choose_options(candidates, verify=None, count=OPTION_COUNT, extra_trials=OPTION_EXTRA_TRIALS):
    """Up to ``count`` options, best first, preferring different approaches.

    ``verify(candidate)`` (optional) returns None if the route really builds, else the reason it does
    not; at most count + extra_trials candidates are tried. Returns (options, rejected candidates).
    """
    chosen, rejected = [], []
    for candidate in diverse_order(candidates):
        if len(chosen) >= count or len(chosen) + len(rejected) >= count + extra_trials:
            break
        reason = verify(candidate) if verify else None
        if reason:
            candidate["rejected"] = reason
            rejected.append(candidate)
        else:
            chosen.append(candidate)
    chosen.sort(key=lambda c: (c["score"], c["index"]))
    for number, candidate in enumerate(chosen, 1):
        candidate["number"] = number
    return chosen, rejected


def option_list_text(option):
    return u"{}. {}{}".format(option["number"], u"[Best] " if option["number"] == 1 else u"", option["label"])


# --- preview ------------------------------------------------------------------------------------
def preview_view(plan):
    if plan.get("spatial_bridge") or plan.get("mixed_routes"):
        return plan.get("preview_view", "plan")
    return "plan" if plan.get("direction") in (LEFT, RIGHT) else "elevation"


def preview_primitives(plan, width_px, height_px, pad_px=28.0):
    """Turn a plan into simple 2D drawing primitives (pixel coordinates, y down) for a WPF Canvas.

    Elevation = side view (s across, w up) for over/under/straight; plan = top view (s across, v up)
    for left/right. The scale is uniform so angles look true.
    """
    view = preview_view(plan)
    axis = 2 if view == "elevation" else 1
    thickness = plan.get("height") if view == "elevation" else plan.get("width")
    thickness = thickness or mm_to_ft(100)
    points = [(p[0], p[axis]) for p in plan.get("local_points") or []]
    boxes = [b for b in plan.get("obstacle_boxes") or [] if b["view"] == view]

    xs = [p[0] for p in points] + [x for b in boxes for x in b["s"]]
    ys = [p[1] for p in points] + [y for b in boxes for y in b["y"]] + [-thickness, thickness]
    source_lines = []
    if plan.get("spatial_bridge") and points:
        for index, (line, end) in enumerate(zip(plan["source_lines"],
                                                (plan["local_points"][0], plan["local_points"][-1]))):
            if index == 1 and plan.get("tee"):
                # The main tray passes through the tee point.
                along = normalize(sub(line[1], line[0]))
                source_lines.append((add(end, scale(along, -min(distance(line[0], end), thickness * 3))),
                                     add(end, scale(along, min(distance(line[1], end), thickness * 3)))))
                continue
            other = line[1] if distance(line[0], end) < EPS else line[0]
            stub = add(end, scale(normalize(sub(other, end)), min(distance(other, end), thickness * 3)))
            source_lines.append((end, stub))
        xs.extend(p[0] for line in source_lines for p in line)
        ys.extend(p[axis] for line in source_lines for p in line)
    if plan.get("mode") == MODE_DETOUR and not points and plan.get("length"):
        xs += [0.0, plan["length"]]
    if not xs:
        return {"view": view, "empty": True}
    x_lo, x_hi = min(xs), max(xs)
    margin = max(mm_to_ft(600), 0.35 * (x_hi - x_lo))
    x_lo, x_hi = x_lo - margin, x_hi + margin
    if plan.get("mode") == MODE_DETOUR and plan.get("length"):
        x_lo, x_hi = max(x_lo, -margin * 0.25), min(x_hi, plan["length"] + margin * 0.25)
    y_lo, y_hi = min(ys) - thickness, max(ys) + thickness
    k = min((width_px - 2 * pad_px) / max(x_hi - x_lo, EPS), (height_px - 2 * pad_px) / max(y_hi - y_lo, EPS))
    x_off = pad_px + ((width_px - 2 * pad_px) - k * (x_hi - x_lo)) / 2.0
    y_off = pad_px + ((height_px - 2 * pad_px) - k * (y_hi - y_lo)) / 2.0

    def px(x, y):
        return (x_off + (x - x_lo) * k, y_off + (y_hi - y) * k)

    prim = {"view": view, "empty": False, "thickness_px": max(2.0, thickness * k), "stubs": [], "ghost": [],
            "route": [px(x, y) for x, y in points], "obstacles": [], "clearance": [], "cuts": [], "labels": []}
    if plan.get("mode") == MODE_DETOUR:
        tray_end = plan.get("length") or 0.0
        if plan.get("cut"):
            a, b = plan["cut"]
            prim["stubs"] = [(px(max(0.0, x_lo), 0.0), px(a, 0.0)), (px(b, 0.0), px(min(tray_end, x_hi), 0.0))]
            prim["ghost"] = [(px(a, 0.0), px(b, 0.0))]
            for name, s in (("Cut A", a), ("Cut B", b)):
                top, bottom = px(s, thickness * 1.2), px(s, -thickness * 1.2)
                prim["cuts"].append((top[0], top[1], bottom[1]))
                prim["labels"].append((top[0] - 14, bottom[1] + 2, name))
        else:
            prim["stubs"] = [(px(max(0.0, x_lo), 0.0), px(min(tray_end, x_hi), 0.0))]
    elif plan.get("spatial_bridge"):
        prim["stubs"] = [(px(line[0][0], line[0][axis]), px(line[1][0], line[1][axis]))
                         for line in source_lines]
        for name, point in (("End A", points[0]), ("Tee" if plan.get("tee") else "End B", points[-1])):
            x, y = px(point[0], point[1])
            prim["labels"].append((x, y + 4, name))
    else:
        gap = plan.get("length") or 0.0
        prim["stubs"] = [(px(x_lo, 0.0), px(0.0, 0.0)), (px(gap, 0.0), px(x_hi, 0.0))]
    for b in boxes:
        c = b["clearance"]
        tl = px(b["s"][0], b["y"][1])
        br = px(b["s"][1], b["y"][0])
        prim["obstacles"].append((tl[0], tl[1], max(1.0, br[0] - tl[0]), max(1.0, br[1] - tl[1])))
        tl = px(b["s"][0] - c, b["y"][1] + c)
        br = px(b["s"][1] + c, b["y"][0] - c)
        prim["clearance"].append((tl[0], tl[1], br[0] - tl[0], br[1] - tl[1]))
    if len(points) > 2 and plan.get("direction") in DIRECTION_VECTORS and not plan.get("individual_routes"):
        mid = points[len(points) // 2 - 1]
        sign = u"+" if plan["direction"] in (OVER, LEFT) else u"-"
        lx, ly = px(mid[0], mid[1])
        prim["labels"].append((lx, ly - 22, u"{}{}".format(sign, fmt_mm(plan["offset"]))))
    return prim


# --- ExternalEvent queue -----------------------------------------------------------------------
class ActionQueue(object):
    """FIFO of (label, callable(uiapp)) drained inside IExternalEventHandler.Execute."""

    def __init__(self):
        self.items = []

    def add(self, label, action):
        self.items.append((label, action))

    def clear(self):
        self.items = []

    def __len__(self):
        return len(self.items)

    def drain(self, uiapp, on_error):
        items, self.items = self.items, []
        for label, action in items:
            try:
                action(uiapp)
            except Exception as ex:
                on_error(label, ex)
        return len(items)
