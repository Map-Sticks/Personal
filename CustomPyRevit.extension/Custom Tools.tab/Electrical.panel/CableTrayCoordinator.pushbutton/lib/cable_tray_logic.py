# -*- coding: utf-8 -*-
"""Pure planning logic for the Cable Tray Coordinator (no Revit or WPF imports).

All lengths are Revit internal units (decimal feet) unless a name ends in ``_mm``.
Points are plain ``(x, y, z)`` tuples so everything here can be unit tested outside Revit.

Local frame used for planning (right-handed):
  s = distance along the tray (horizontal), v = to the left of the tray direction, w = up (world Z).
"""
from __future__ import division

import math

MM_PER_FT = 304.8
EPS = 1e-6

MODE_DETOUR = "detour"
MODE_BRIDGE = "bridge"

AUTO, OVER, UNDER, LEFT, RIGHT, STRAIGHT = "Auto", "Over", "Under", "Left", "Right", "Straight"
DIRECTIONS = [AUTO, OVER, UNDER, LEFT, RIGHT, STRAIGHT]
DETOUR_DIRECTIONS = [OVER, UNDER, LEFT, RIGHT]
ANGLES = [90, 60, 45, 30]
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
SIZE_TOL_MM = 1.0

DEFAULT_PARAMS = {
    "direction": AUTO,
    "angle": 45,
    "offset_mm": 300.0,
    "clearance_mm": 50.0,
    "auto_offset": True,
    "round_mm": 25.0,
    "min_straight_mm": 100.0,
    "keep_if_fitting_fails": False,
}

PARAM_LIMITS_MM = {
    "offset_mm": (0.0, 5000.0, "Offset"),
    "clearance_mm": (0.0, 2000.0, "Clearance"),
    "round_mm": (0.0, 500.0, "Round up to"),
    "min_straight_mm": (0.0, 2000.0, "Min. straight"),
}

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
    if value < lo or value > hi:
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
        if int(raw.get("angle")) in ANGLES:
            params["angle"] = int(raw.get("angle"))
    except (TypeError, ValueError):
        pass
    for key in PARAM_LIMITS_MM:
        if key in raw:
            try:
                params[key] = parse_mm(raw[key], key)
            except PlanError:
                pass
    for key in ("auto_offset", "keep_if_fitting_fails"):
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
        "min_straight": mm_to_ft(p["min_straight_mm"]),
        "auto_offset": p["auto_offset"],
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


def _finish(plan, frame, s0, s1, span, direction, offset, params, piece_limits):
    """Build route points/segments, validate segment lengths and write the summary."""
    angle = params["angle"]
    min_straight = params["min_straight"]
    plan.update({"direction": direction, "offset": offset, "angle": angle, "frame": frame})
    if direction == STRAIGHT or offset <= EPS:
        points = route_points(s0, s1, s0, s1, STRAIGHT, 0.0, angle)
        plan["direction"] = STRAIGHT
    else:
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
            first, last = piece_limits
            if start < first + min_straight - EPS or end > last - min_straight + EPS:
                raise PlanError(u"The detour ({} long) runs past the end of this tray segment. Use Bridge "
                                u"mode across the joint, a smaller offset or a steeper angle."
                                .format(fmt_mm(end - start)))
            s0, s1 = start, end
            plan["cut"] = (start, end)
        points = route_points(s0, s1, lo, hi, direction, offset, angle)

    segments = []
    for a, b in zip(points[:-1], points[1:]):
        seg_len = distance(a, b)
        if seg_len < min_straight - EPS:
            raise PlanError(u"A {} segment is shorter than the minimum straight ({}). Increase the offset "
                            u"or choose a different angle.".format(fmt_mm(seg_len), fmt_mm(min_straight)))
        n_local = segment_normal(normalize(sub(b, a)))
        segments.append({"start": frame.to_world(a), "end": frame.to_world(b),
                         "normal": frame.vector_to_world(n_local) if n_local else None,
                         "length": seg_len})
    plan["local_points"] = points
    plan["segments"] = segments
    plan["ok"] = True
    plan["summary"] = summary_lines(plan)
    return plan


def plan_detour(tray, obstacles, cut_points, params):
    """Plan a detour that cuts a single tray and re-joins it at the original level.

    tray: {"start", "end", "width", "height"}; obstacles: list of lists of world segments/polygons;
    cut_points: [] or two world points picked on the tray; params: see params_in_feet().
    """
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
            if direction == STRAIGHT:
                raise PlanError("Straight join only applies in Bridge mode.")
            offset = params["offset"]
            if params["auto_offset"] and ext is not None and direction in plan["required"]:
                offset = round_up(plan["required"][direction], params["round"])
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
        span = obstacle_span(ext, direction, params["clearance"])
        return _finish(plan, frame, None, None, span, direction, offset, params, (0.0, run_length))
    except PlanError as ex:
        plan["ok"] = False
        plan["error"] = to_text(ex)
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


def plan_bridge(tray_a, tray_b, obstacles, params):
    """Plan a new run that joins the facing free ends of two in-line trays around an obstacle."""
    plan = _new_plan(MODE_BRIDGE)
    try:
        if tray_a.get("id_value") is not None and tray_a.get("id_value") == tray_b.get("id_value"):
            raise PlanError("Pick two different tray segments.")
        tol = mm_to_ft(SIZE_TOL_MM)
        if abs(tray_a["width"] - tray_b["width"]) > tol or abs(tray_a["height"] - tray_b["height"]) > tol:
            raise PlanError(u"The trays are different sizes ({} x {} and {} x {}); only equal sizes can be joined."
                            .format(fmt_mm(tray_a["width"]), fmt_mm(tray_a["height"]),
                                    fmt_mm(tray_b["width"]), fmt_mm(tray_b["height"])))
        end_a, end_b = nearest_free_ends(tray_a, tray_b)
        frame, gap = horizontal_frame(end_a["point"], end_b["point"], "gap between the tray ends")
        lateral = mm_to_ft(MAX_BRIDGE_LATERAL_MM)
        for tray in (tray_a, tray_b):
            axis = normalize(sub(tray["end"], tray["start"]))
            sin_mis = abs(axis[0] * frame.u[1] - axis[1] * frame.u[0])
            off_line = [frame.to_local(tray[k]) for k in ("start", "end")]
            if abs(axis[2]) > 0.01 or sin_mis > math.sin(math.radians(MAX_BRIDGE_MISALIGN_DEG)) or \
                    any(abs(q[1]) > lateral or abs(q[2]) > lateral for q in off_line):
                raise PlanError(u"The two trays are not in line with each other (max {} sideways or in "
                                u"level). Bridge mode needs both trays on the same axis and level."
                                .format(fmt_mm(lateral)))
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
        if direction == STRAIGHT:
            return _finish(plan, frame, 0.0, gap, None, STRAIGHT, 0.0, params, None)
        run = transition_run(offset, params["angle"])
        if ext is not None:
            span = obstacle_span(ext, direction, params["clearance"])
        else:
            span = (run, gap - run)
        return _finish(plan, frame, 0.0, gap, span, direction, offset, params, None)
    except PlanError as ex:
        plan["ok"] = False
        plan["error"] = to_text(ex)
        return plan


def summary_lines(plan):
    lines = []
    direction = plan.get("direction")
    if direction == STRAIGHT:
        lines.append(u"Method: straight join")
    else:
        lines.append(u"Method: {}, {}{} bends".format(DIRECTION_LABELS.get(direction, direction),
                                                       plan.get("angle"), DEG))
        needed = plan.get("required", {}).get(direction)
        text = u"Offset: {}".format(fmt_mm(plan.get("offset", 0.0)))
        if needed is not None:
            text += u" (clash needs {})".format(fmt_mm(needed))
        lines.append(text)
    if plan.get("cut"):
        lines.append(u"Cuts at {} and {} from the tray start".format(fmt_mm(plan["cut"][0]), fmt_mm(plan["cut"][1])))
    segs = plan.get("segments", [])
    route_len = sum(s["length"] for s in segs)
    straight = distance(plan["local_points"][0], plan["local_points"][-1]) if plan.get("local_points") else 0.0
    lines.append(u"New tray segments: {}   Fittings: {}".format(
        len(segs) + (1 if plan.get("mode") == MODE_DETOUR else 0), len(segs) + 1))
    lines.append(u"Detour length: {} (+{} over straight)".format(fmt_mm(route_len), fmt_mm(route_len - straight)))
    return lines


# --- preview ------------------------------------------------------------------------------------
def preview_view(plan):
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
    if len(points) > 2 and plan.get("direction") in DIRECTION_VECTORS:
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
