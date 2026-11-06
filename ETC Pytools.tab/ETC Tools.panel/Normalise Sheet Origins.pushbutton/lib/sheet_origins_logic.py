"""Revit-independent helpers for the Normalise Sheet Origins button."""

import re

try:
    text_type = unicode
except NameError:
    text_type = str


def natural_sort_key(value):
    """Return a Python 2/3 comparable key for sheet numbers."""
    parts = re.split(r"(\d+)", text_type(value).lower())
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)


def corner_shift(reference_corner, target_corner):
    """Return the (dx, dy) that moves target_corner onto reference_corner."""
    return (
        reference_corner[0] - target_corner[0],
        reference_corner[1] - target_corner[1],
    )


def needs_shift(shift, tolerance):
    return max(abs(shift[0]), abs(shift[1])) > tolerance


def is_movement_root(info):
    """Decide whether a sheet-owned element must be moved itself.

    info keys: is_view, has_category, in_group, is_nested, is_revision_schedule,
    is_viewport_or_schedule, has_bbox.
    Elements that follow something else (group members, components nested in the
    title block, title-block revision schedules) or that are not placed graphics
    (view definitions, internal uncategorised elements) are skipped.
    """
    if info["is_view"] or not info["has_category"]:
        return False
    if info["in_group"] or info["is_nested"] or info["is_revision_schedule"]:
        return False
    return info["is_viewport_or_schedule"] or info["has_bbox"]


def drop_followers(candidate_ids, dependents_by_id):
    """Remove candidates that Revit moves automatically with another candidate.

    dependents_by_id maps a candidate id to the ids Revit reports as dependent on it.
    Mutual dependencies keep both, so nothing is ever dropped by a cycle.
    """
    candidates = set(candidate_ids)
    followers = set()
    for host in candidate_ids:
        for dependent in dependents_by_id.get(host, ()):
            if dependent == host or dependent not in candidates:
                continue
            if host in dependents_by_id.get(dependent, ()):
                continue
            followers.add(dependent)
    return [item for item in candidate_ids if item not in followers]


STATUS_ALIGNED = "Aligned"
STATUS_MISALIGNED = "Misaligned"


def audit_status(offset, tolerance):
    return STATUS_MISALIGNED if needs_shift(offset, tolerance) else STATUS_ALIGNED


def csv_row(values):
    """One CSV line; every field quoted so commas/quotes in sheet names are safe."""
    fields = []
    for value in values:
        text = "" if value is None else text_type(value)
        fields.append('"' + text.replace('"', '""') + '"')
    return ",".join(fields)
