# -*- coding: utf-8 -*-
"""Convert spatial boundaries into validated plan-view FilledRegion loops."""

import math

from System.Collections.Generic import List
from Autodesk.Revit.Exceptions import RegenerationFailedException
from pyrevit import DB

import room_regions_logic as logic


BOUNDARY_LOCATIONS = {
    "Finish": DB.SpatialElementBoundaryLocation.Finish,
    "Center": DB.SpatialElementBoundaryLocation.Center,
    "CoreBoundary": DB.SpatialElementBoundaryLocation.CoreBoundary,
    "CoreCenter": DB.SpatialElementBoundaryLocation.CoreCenter,
}


def _xy(point):
    return (float(point.X), float(point.Y))


def _line(start, end):
    return {"kind": "line", "start": _xy(start), "end": _xy(end)}


def _arc(start, middle, end):
    return {"kind": "arc", "start": _xy(start), "mid": _xy(middle),
            "end": _xy(end)}


def _distance(start, end):
    return math.hypot(start.X - end.X, start.Y - end.Y)


def _arc_segments(curve, short_tolerance):
    if not curve.IsBound:
        points = [curve.Evaluate(angle, False)
                  for angle in (0.0, math.pi / 2.0, math.pi,
                                3.0 * math.pi / 2.0, 2.0 * math.pi)]
        return [_arc(points[0], points[1], points[2]),
                _arc(points[2], points[3], points[4])]
    start, end = curve.GetEndPoint(0), curve.GetEndPoint(1)
    if _distance(start, end) < short_tolerance:
        points = [curve.Evaluate(value, True)
                  for value in (0.0, 0.25, 0.5, 0.75, 1.0)]
        return [_arc(points[0], points[1], points[2]),
                _arc(points[2], points[3], points[4])]
    return [_arc(start, curve.Evaluate(0.5, True), end)]


def _tessellated_segments(curve):
    if not curve.IsBound:
        if not curve.IsCyclic:
            raise logic.GeometryError("An unbounded non-cyclic boundary cannot be tessellated.")
        period = float(curve.Period)
        if period <= 0.0 or math.isnan(period) or math.isinf(period):
            raise logic.GeometryError("An unbounded boundary has an invalid period.")
        segments = []
        for start, end in ((0.0, period / 2.0), (period / 2.0, period)):
            part = curve.Clone()
            try:
                part.MakeBound(start, end)
                segments.extend(_tessellated_segments(part))
            finally:
                part.Dispose()
        return segments
    points = list(curve.Tessellate())
    if len(points) < 2:
        raise logic.GeometryError("A boundary curve could not be tessellated.")
    return [_line(points[index], points[index + 1])
            for index in range(len(points) - 1)]


def _project_segments(curve, transform, short_tolerance):
    if transform is not None:
        curve = curve.CreateTransformed(transform)
    if isinstance(curve, DB.Line):
        return [_line(curve.GetEndPoint(0), curve.GetEndPoint(1))], None
    if isinstance(curve, DB.Arc) and abs(abs(curve.Normal.Z) - 1.0) < 1.0e-9:
        return _arc_segments(curve, short_tolerance), None
    return _tessellated_segments(curve), (
        "A non-horizontal or non-circular boundary curve was tessellated.")


def _xyz(point, elevation):
    return DB.XYZ(point[0], point[1], elevation)


def _make_curve(segment, elevation):
    start = _xyz(segment["start"], elevation)
    end = _xyz(segment["end"], elevation)
    if segment["kind"] == "line":
        return DB.Line.CreateBound(start, end)
    return DB.Arc.Create(start, end, _xyz(segment["mid"], elevation))


def _curve_loops(loops, elevation):
    result = List[DB.CurveLoop]()
    for segments in loops:
        curve_loop = DB.CurveLoop()
        for segment in segments:
            curve_loop.Append(_make_curve(segment, elevation))
        result.Add(curve_loop)
    return result


def _try_validated(doc, view, loops, elevation, label, failures):
    try:
        converted = _curve_loops(loops, elevation)
        if DB.BoundaryValidation.IsValidBoundaryOnView(doc, view.Id, converted):
            return converted
        failures.append("{}: Revit rejected the boundary.".format(label))
    except RegenerationFailedException:
        raise
    except Exception as ex:
        failures.append("{}: {}".format(label, ex))
    return None


def _tessellate_cleaned_loop(loop, elevation, short_tolerance, snap_tolerance):
    segments = []
    for segment in loop:
        if segment["kind"] == "line":
            segments.append(segment)
        else:
            segments.extend(_tessellated_segments(_make_curve(segment, elevation)))
    return logic.clean_loop(segments, short_tolerance, snap_tolerance)


def build_boundaries(doc, view, spatial, transform, location, include_islands,
                     simplify, short_tolerance):
    """Return (IList[CurveLoop], warnings), or raise with an actionable reason."""
    if location not in BOUNDARY_LOCATIONS:
        raise logic.GeometryError("Unsupported boundary location: {}".format(location))
    options = DB.SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = BOUNDARY_LOCATIONS[location]
    warnings = []
    loops = []
    rejected = []
    snap_tolerance = max(short_tolerance * 1.01, 1.0e-8)
    try:
        boundaries = spatial.GetBoundarySegments(options)
        if not boundaries:
            raise logic.GeometryError("The room or Space has no boundary segments.")
        for boundary in boundaries:
            projected = []
            for segment in boundary:
                parts, warning = _project_segments(
                    segment.GetCurve(), transform, short_tolerance)
                projected.extend(parts)
                if warning and warning not in warnings:
                    warnings.append(warning)
            try:
                cleaned, loop_warnings = logic.clean_loop(
                    projected, short_tolerance, snap_tolerance, simplify)
                loops.append(cleaned)
                warnings.extend(loop_warnings)
            except logic.GeometryError as ex:
                try:
                    area = abs(logic.signed_area(projected))
                except logic.GeometryError:
                    area = None
                rejected.append((area, str(ex)))
    finally:
        options.Dispose()

    outer_index = logic.largest_loop_index(loops)
    outer = loops[outer_index]
    outer_area = abs(logic.signed_area(outer))
    for area, reason in rejected:
        if area is None or area >= outer_area:
            raise logic.GeometryError(
                "A boundary loop could not be cleaned and may be the outer loop: {}.".format(reason))
        warnings.append("An invalid smaller boundary loop was omitted: {}.".format(reason))
    chosen = loops if include_islands else [outer]
    elevation = float(view.GenLevel.ProjectElevation)
    failures = []
    result = _try_validated(doc, view, chosen, elevation, "All selected loops", failures)
    if result is not None:
        return result, warnings

    if len(chosen) > 1:
        result = _try_validated(doc, view, [outer], elevation, "Outer loop", failures)
        if result is not None:
            warnings.append("Revit rejected the full boundary; islands and other loops were omitted.")
            warnings.extend(failures)
            return result, warnings

    try:
        tessellated, loop_warnings = _tessellate_cleaned_loop(
            outer, elevation, short_tolerance, snap_tolerance)
        result = _try_validated(doc, view, [tessellated], elevation,
                                "Tessellated outer loop", failures)
        if result is not None:
            warnings.append("Revit rejected the original boundary; a tessellated outer loop was used.")
            if len(chosen) > 1:
                warnings.append("Islands and other loops were omitted.")
            warnings.extend(loop_warnings)
            warnings.extend(failures)
            return result, warnings
    except RegenerationFailedException:
        raise
    except Exception as ex:
        failures.append("Tessellated outer loop: {}".format(ex))
    raise logic.GeometryError("; ".join(failures))
