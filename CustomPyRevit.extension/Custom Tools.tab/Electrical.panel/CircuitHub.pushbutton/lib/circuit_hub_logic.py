"""Revit-independent CircuitHub validation (IronPython 2.7 compatible)."""

try:
    text_type = unicode
except NameError:
    text_type = str


def element_id_value(element_id):
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


def panel_rename_plan(existing_names, proposed_names):
    """Return (id, old name, new name) changes, or reject the entire request."""
    changes = []
    seen_ids = set()
    destinations = set()
    occupied = {}
    for panel_id, name in existing_names.items():
        occupied.setdefault((name or "").strip().lower(), set()).add(panel_id)
    for panel_id, proposed in proposed_names:
        if panel_id in seen_ids:
            raise ValueError("A panel was included more than once.")
        seen_ids.add(panel_id)
        if panel_id not in existing_names:
            raise ValueError("A selected panel no longer exists. Refresh and retry.")
        name = (proposed or "").strip()
        if not name or any(ord(char) < 32 for char in name):
            raise ValueError("Panel names must be nonempty and contain no control characters.")
        key = name.lower()
        if key in destinations:
            raise ValueError("Multiple selected panels would be named '{0}'.".format(name))
        destinations.add(key)
        if occupied.get(key, set()) - set([panel_id]):
            raise ValueError(
                "Panel name '{0}' is already used by another panel. "
                "Use an unused intermediate name for swaps.".format(name))
        old_name = existing_names[panel_id] or ""
        if old_name != name:
            changes.append((panel_id, old_name, name))
    return changes


def bulk_panel_name(name, find="", replace="", prefix="", suffix=""):
    if find:
        name = name.replace(find, replace)
    return prefix + name + suffix
