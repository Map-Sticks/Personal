"""Revit-independent, three-dimensional geometry for joined detail-line corners."""

import math


JOIN_TOLERANCE = 1e-7


class FilletInputError(ValueError):
    """A rejected pick or impossible fillet, not a Revit API failure."""


def add(a, b):
    return tuple(a[i] + b[i] for i in range(3))


def subtract(a, b):
    return tuple(a[i] - b[i] for i in range(3))


def scale(vector, factor):
    return tuple(value * factor for value in vector)


def dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def length(vector):
    return math.sqrt(dot(vector, vector))


def distance(a, b):
    return length(subtract(a, b))


def unit(vector):
    size = length(vector)
    if size <= JOIN_TOLERANCE:
        raise FilletInputError("A source line is too short.")
    return scale(vector, 1.0 / size)


def positive_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise FilletInputError("Enter a positive radius in mm, using a decimal point.")
    if math.isnan(number) or math.isinf(number) or number <= 0:
        raise FilletInputError("The radius must be finite and greater than zero.")
    return number


def joined_ends(points_a, points_b, vertex_tolerance=JOIN_TOLERANCE):
    matches = [(a, b) for a in range(2) for b in range(2)
               if distance(points_a[a], points_b[b]) <= vertex_tolerance]
    if len(matches) != 1:
        raise FilletInputError("Select two different straight detail lines with one "
                         "shared endpoint. Crossings, gaps and overlapping "
                         "lines are not supported.")
    return matches[0]


def corner_tolerance(view_scale, line_length, paper_mm=4.0, max_fraction=0.25):
    """Corner capture radius in feet: a fixed printed size, scaled by the view.

    Capped at a fraction of the line so a mid-line click always starts
    two-line picking.
    """
    try:
        view_scale = max(1.0, float(view_scale))
    except (TypeError, ValueError):
        view_scale = 1.0
    return min(paper_mm / 304.8 * view_scale, line_length * max_fraction)


def picked_endpoint(points, picked_point, pick_tolerance):
    """Return the nearby endpoint, or None to start explicit two-line picking."""
    nearest = min(range(2), key=lambda index: distance(points[index], picked_point))
    if distance(points[nearest], picked_point) <= pick_tolerance:
        return nearest
    return None


def corner_lines(selected_key, selected_points, picked_point, candidates,
                 vertex_tolerance, pick_tolerance):
    """Find exactly one other detail line ending at the snapped picked corner.

    Candidate entries are ``(unique_id, endpoints, is_straight_line)``.
    """
    nearest = picked_endpoint(selected_points, picked_point, pick_tolerance)
    if nearest is None:
        raise FilletInputError("Click near a corner endpoint, not along the line.")
    corner = selected_points[nearest]
    matches = []
    seen = set()
    for key, points, is_line in candidates:
        if key in seen:
            continue
        endpoints = [index for index in range(2)
                     if distance(points[index], corner) <= vertex_tolerance]
        if endpoints:
            if len(endpoints) != 1:
                raise FilletInputError("A detail line has duplicate endpoints at this corner.")
            matches.append((key, endpoints[0], is_line))
            seen.add(key)
    if selected_key not in seen:
        raise FilletInputError("The picked detail line is not visible at the selected corner.")
    if len(matches) != 2:
        if len(matches) > 2:
            raise FilletInputError("Ambiguous corner: {} detail lines meet here. "
                             "Exactly two are required.".format(len(matches)))
        raise FilletInputError("No adjacent detail line endpoint was found. "
                         "Select a corner shared by two detail lines.")
    if any(not match[2] for match in matches):
        raise FilletInputError("Only corners between two straight detail lines can be filleted.")
    other = next(match for match in matches if match[0] != selected_key)
    return other[0], corner


def make_fillet(points_a, end_a, points_b, end_b, radius, minimum,
                vertex_tolerance=JOIN_TOLERANCE):
    radius = positive_number(radius)
    a, b = points_a[end_a], points_b[end_b]
    if distance(a, b) > vertex_tolerance:
        raise FilletInputError("Pick near the joined ends of two lines that meet. "
                         "Crossings, gaps and extensions are not supported.")
    corner = scale(add(a, b), 0.5)
    u = unit(subtract(points_a[1 - end_a], corner))
    v = unit(subtract(points_b[1 - end_b], corner))
    angle = math.acos(max(-1.0, min(1.0, dot(u, v))))
    if angle < 1e-6 or math.pi - angle < 1e-6:
        raise FilletInputError("Parallel or collinear lines cannot form a fillet.")
    sweep = math.pi - angle
    if radius * sweep <= minimum:
        raise FilletInputError("The fillet arc is below Revit's short-curve tolerance.")
    trim = radius / math.tan(angle / 2.0)
    if (trim + minimum >= distance(corner, points_a[1 - end_a])
            or trim + minimum >= distance(corner, points_b[1 - end_b])):
        raise FilletInputError("The radius is too large: a source line would be consumed.")
    bisector = unit(add(u, v))
    center = add(corner, scale(bisector, radius / math.sin(angle / 2.0)))
    start = add(corner, scale(u, trim))
    end = add(corner, scale(v, trim))
    middle = subtract(center, scale(bisector, radius))
    return dict(corner=corner, start=start, end=end, middle=middle,
                center=center, radius=radius, sweep=sweep, trim=trim)


def plan_fillet(points_a, points_b, radius, minimum,
                vertex_tolerance=JOIN_TOLERANCE):
    """Fillet one corner using the CURRENT endpoints, including previous trims."""
    ea, eb = joined_ends(points_a, points_b, vertex_tolerance)
    geometry = make_fillet(points_a, ea, points_b, eb, radius, minimum,
                           vertex_tolerance)
    a, b = list(points_a), list(points_b)
    a[ea], b[eb] = geometry["start"], geometry["end"]
    return tuple(a), tuple(b), geometry
