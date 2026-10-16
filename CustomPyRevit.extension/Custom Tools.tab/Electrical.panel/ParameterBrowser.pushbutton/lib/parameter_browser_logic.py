# -*- coding: utf-8 -*-
"""Revit-free logic for the Parameter Browser (IronPython 2.7 and CPython 3 compatible)."""

SOURCES = ["Spaces", "Rooms"]
SCOPES = ["Whole project", "Current view", "Level"]
DEFAULT_FIELDS = ["Number", "Name", "Level", "Department"]
PSEUDO_FIELDS = ["Element Id", "Category", "Level"]
ID_COLUMN = "__id"
SEARCH_COLUMN = "__search"

try:
    text_type = unicode  # noqa: F821  (IronPython 2.7)
except NameError:
    text_type = str


def to_text(value):
    if value is None:
        return u""
    try:
        return text_type(value)
    except Exception:
        return u""


def element_id_value(element_id):
    """ElementId.Value on Revit 2024+, IntegerValue on older versions."""
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


def normalize_settings(data):
    """Return (source, scope, fields) from a possibly invalid settings dict."""
    if not isinstance(data, dict):
        data = {}
    source = data.get("source")
    scope = data.get("scope")
    fields = data.get("fields")
    if source not in SOURCES:
        source = SOURCES[0]
    if scope not in SCOPES:
        scope = SCOPES[0]
    if not isinstance(fields, list):
        fields = list(DEFAULT_FIELDS)
    fields = [to_text(f) for f in fields if to_text(f).strip()]
    return source, scope, fields or list(DEFAULT_FIELDS)


def resolve_fields(selected, available):
    """Keep selected fields that exist (preserving order); fall back to defaults, then Element Id."""
    available = set(available)
    fields = []
    for name in selected:
        if name in available and name not in fields:
            fields.append(name)
    if not fields:
        fields = [f for f in DEFAULT_FIELDS if f in available]
    return fields or ["Element Id"]


def column_choices(available, selected):
    """Selected fields first (in display order), then the rest alphabetically."""
    chosen = [f for f in selected if f in available]
    rest = sorted([f for f in available if f not in chosen], key=lambda x: x.lower())
    return chosen + rest


def level_options(records):
    return sorted(set([r["level"] for r in records if r.get("level")]), key=lambda x: x.lower())


def filter_records(records, scope, level=None):
    if scope == "Current view":
        return [r for r in records if r.get("visible")]
    if scope == "Level":
        if not level:
            return []
        return [r for r in records if r.get("level") == level]
    return list(records)


def missing_fields(records, fields):
    """Fields whose values have not been read from the model yet for these records."""
    missing = []
    for field in fields:
        if field in PSEUDO_FIELDS:
            continue
        for rec in records:
            if field not in rec["values"]:
                missing.append(field)
                break
    return missing


def record_value(rec, field):
    if field == "Element Id":
        return to_text(rec["id"])
    if field == "Category":
        return rec.get("category", u"")
    if field == "Level":
        return rec.get("level", u"")
    return rec["values"].get(field, u"")


def search_text(values):
    return u" | ".join([to_text(v) for v in values]).lower()


def escape_like(token):
    """Escape a literal for a System.Data DataView RowFilter LIKE pattern."""
    out = []
    for ch in token:
        if ch in "[]*%":
            out.append(u"[" + ch + u"]")
        elif ch == "'":
            out.append(u"''")
        else:
            out.append(ch)
    return u"".join(out)


def build_row_filter(text, column=SEARCH_COLUMN):
    """Every whitespace-separated token must appear in the search column (case-insensitive)."""
    tokens = [t for t in to_text(text).lower().split() if t]
    return u" AND ".join([u"[{0}] LIKE '%{1}%'".format(column, escape_like(t)) for t in tokens])


def format_parameter_value(param, element_name_lookup=None):
    """Display text for a Revit Parameter; element_name_lookup(element_id) resolves ElementId values."""
    if param is None:
        return u""
    try:
        text = param.AsValueString()
        if text is not None:
            return to_text(text)
    except Exception:
        pass
    try:
        storage = to_text(param.StorageType)
        if storage.endswith("String"):
            return to_text(param.AsString())
        if not param.HasValue:
            return u""
        if storage.endswith("Integer"):
            return to_text(param.AsInteger())
        if storage.endswith("Double"):
            return to_text(round(param.AsDouble(), 6))
        if storage.endswith("ElementId"):
            eid = param.AsElementId()
            value = element_id_value(eid)
            if value is None or value < 0:
                return u""
            if element_name_lookup is not None:
                name = element_name_lookup(eid)
                if name:
                    return to_text(name)
            return to_text(value)
    except Exception:
        pass
    return u""


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
