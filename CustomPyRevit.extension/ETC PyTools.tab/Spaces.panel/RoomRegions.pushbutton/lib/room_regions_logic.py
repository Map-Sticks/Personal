# -*- coding: utf-8 -*-
"""Revit-independent Room Regions selection and boundary helpers."""

import math


try:
    text_type = unicode
except NameError:
    text_type = str


class GeometryError(ValueError):
    pass


def element_id_value(element_id):
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


class ActionQueue(object):
    """FIFO of (label, callable(uiapp)) drained inside ExternalEvent.Execute."""

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


def _literal(value):
    return text_type(value).replace(u"'", u"''")


def _like_literal(value):
    escapes = {u"'": u"''", u"[": u"[[]", u"]": u"[]]",
               u"%": u"[%]", u"*": u"[*]"}
    return u"".join(escapes.get(char, char) for char in text_type(value))


def row_filter(sources, link_ids, all_levels, follow_phase, search):
    """Build a DataView filter; the selected link ids are Revit unique-id strings."""
    clauses = []
    if "space" in sources:
        clauses.append(u"[__source] = 'space'")
    if "room" in sources:
        clauses.append(u"[__source] = 'room'")
    if "link" in sources and link_ids:
        links = u" OR ".join(u"[__link] = '{}'".format(_literal(item))
                             for item in link_ids)
        clauses.append(u"([__source] = 'link' AND ({}))".format(links))
    result = [u"({})".format(u" OR ".join(clauses)) if clauses else u"1 = 0"]
    if not all_levels:
        result.append(u"[__level_match] = TRUE")
    if follow_phase:
        result.append(u"[__phase_match] = TRUE")
    for word in text_type(search or u"").split():
        result.append(u"[__search] LIKE '%{}%'".format(_like_literal(word)))
    return u" AND ".join(result)


def range_indexes(keys, anchor, current):
    """Return the visible range between checkbox clicks, or just the current row."""
    if current not in keys:
        return []
    end = keys.index(current)
    if anchor not in keys:
        return [end]
    start = keys.index(anchor)
    return list(range(min(start, end), max(start, end) + 1))


def tick_indexes(keys, selected_keys, anchor, current, extend_range):
    """Target highlighted visible rows, a Shift range, or the clicked row alone."""
    if extend_range:
        return range_indexes(keys, anchor, current)
    if current not in keys:
        return []
    selected = set(selected_keys)
    if current in selected:
        return [index for index, key in enumerate(keys) if key in selected]
    return [keys.index(current)]


def _distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _arc_data(start, middle, end):
    """Circle centre, radius, start angle and signed sweep through middle."""
    bx, by = middle[0] - start[0], middle[1] - start[1]
    cx, cy = end[0] - start[0], end[1] - start[1]
    determinant = 2.0 * (bx * cy - by * cx)
    scale = max(bx * bx + by * by, cx * cx + cy * cy, 1.0e-24)
    if abs(determinant) <= scale * 1.0e-12:
        raise GeometryError("An arc became collinear or has coincident points.")
    b2, c2 = bx * bx + by * by, cx * cx + cy * cy
    centre = (start[0] + (cy * b2 - by * c2) / determinant,
              start[1] + (bx * c2 - cx * b2) / determinant)
    radius = _distance(centre, start)
    first = math.atan2(start[1] - centre[1], start[0] - centre[0])
    mid = math.atan2(middle[1] - centre[1], middle[0] - centre[0])
    last = math.atan2(end[1] - centre[1], end[0] - centre[0])
    turn = 2.0 * math.pi
    ccw = (last - first) % turn
    through_mid = (mid - first) % turn
    sweep = ccw if through_mid <= ccw else ccw - turn
    if abs(sweep) <= 1.0e-12:
        raise GeometryError("An arc has no measurable sweep.")
    return centre, radius, first, sweep


def segment_length(segment):
    if segment["kind"] == "line":
        return _distance(segment["start"], segment["end"])
    if segment["kind"] == "arc":
        _, radius, _, sweep = _arc_data(segment["start"], segment["mid"],
                                         segment["end"])
        return radius * abs(sweep)
    raise GeometryError("Unsupported boundary segment: {}".format(segment["kind"]))


def _copy_segment(segment):
    result = {"kind": segment["kind"], "start": tuple(segment["start"]),
              "end": tuple(segment["end"])}
    if segment["kind"] == "arc":
        result["mid"] = tuple(segment["mid"])
    return result


def _reverse(segment):
    result = _copy_segment(segment)
    result["start"], result["end"] = result["end"], result["start"]
    return result


def _chain(segments, tolerance):
    remaining = [_copy_segment(segment) for segment in segments]
    ordered = [remaining.pop(0)]
    while remaining:
        end = ordered[-1]["end"]
        choices = []
        for index, segment in enumerate(remaining):
            choices.append((_distance(end, segment["start"]), index, False))
            choices.append((_distance(end, segment["end"]), index, True))
        distance, index, reverse = min(choices)
        if distance > tolerance:
            raise GeometryError("Boundary gap {:.6f} ft exceeds the snapping tolerance."
                                .format(distance))
        segment = remaining.pop(index)
        ordered.append(_reverse(segment) if reverse else segment)
    if _distance(ordered[-1]["end"], ordered[0]["start"]) > tolerance:
        raise GeometryError("Boundary loop is not closed within the snapping tolerance.")
    return ordered


def _snap(segments):
    largest_gap = 0.0
    for index, segment in enumerate(segments):
        following = segments[(index + 1) % len(segments)]
        end, start = segment["end"], following["start"]
        largest_gap = max(largest_gap, _distance(end, start))
        joint = ((end[0] + start[0]) / 2.0, (end[1] + start[1]) / 2.0)
        segment["end"] = joint
        following["start"] = joint
    return largest_gap


def _can_merge(first, second):
    if first["kind"] != "line" or second["kind"] != "line":
        return False
    a = (first["end"][0] - first["start"][0],
         first["end"][1] - first["start"][1])
    b = (second["end"][0] - second["start"][0],
         second["end"][1] - second["start"][1])
    product = math.hypot(*a) * math.hypot(*b)
    return (product > 0.0 and abs(a[0] * b[1] - a[1] * b[0]) <= product * 1.0e-9
            and a[0] * b[0] + a[1] * b[1] > 0.0)


def _merge_collinear(segments):
    segments = list(segments)
    changed = True
    while changed and len(segments) > 2:
        changed = False
        for index in range(len(segments)):
            following = (index + 1) % len(segments)
            if _can_merge(segments[index], segments[following]):
                merged = {"kind": "line", "start": segments[index]["start"],
                          "end": segments[following]["end"]}
                if following == 0:
                    segments = [merged] + segments[1:index]
                else:
                    segments[index:following + 1] = [merged]
                changed = True
                break
    return segments


def clean_loop(segments, short_tolerance, snap_tolerance, simplify=False):
    """Chain and close one 2D loop, dropping sub-tolerance segments."""
    if short_tolerance <= 0 or snap_tolerance < short_tolerance:
        raise GeometryError("Invalid Revit boundary tolerances.")
    current = [_copy_segment(segment) for segment in segments]
    removed, largest_gap = 0, 0.0
    while True:
        kept = [segment for segment in current
                if segment_length(segment) >= short_tolerance]
        removed += len(current) - len(kept)
        if len(kept) < 2:
            raise GeometryError("Boundary has fewer than two usable curves.")
        current = _chain(kept, snap_tolerance)
        largest_gap = max(largest_gap, _snap(current))
        if all(segment_length(segment) >= short_tolerance for segment in current):
            break
    if simplify:
        current = _merge_collinear(current)
    if abs(signed_area(current)) <= short_tolerance * short_tolerance:
        raise GeometryError("Boundary loop has no usable area.")
    warnings = []
    if removed:
        warnings.append("Removed {} short boundary segment(s).".format(removed))
    if largest_gap > 1.0e-9:
        warnings.append("Snapped boundary gaps up to {:.6f} ft.".format(largest_gap))
    return current, warnings


def signed_area(segments):
    area = 0.0
    for segment in segments:
        start, end = segment["start"], segment["end"]
        if segment["kind"] == "line":
            area += (start[0] * end[1] - end[0] * start[1]) / 2.0
        elif segment["kind"] == "arc":
            centre, radius, first, sweep = _arc_data(start, segment["mid"], end)
            last = first + sweep
            area += (centre[0] * radius * (math.sin(last) - math.sin(first))
                     - centre[1] * radius * (math.cos(last) - math.cos(first))
                     + radius * radius * sweep) / 2.0
        else:
            raise GeometryError("Unsupported boundary segment: {}".format(segment["kind"]))
    return area


def largest_loop_index(loops):
    if not loops:
        raise GeometryError("No boundary loops were returned.")
    return max(range(len(loops)), key=lambda index: abs(signed_area(loops[index])))
