# -*- coding: utf-8 -*-
"""Revit-free payload helpers for Parameter Browser WebView (IronPython 2.7/3.4 and CPython 3).

Functions here only duck-type Revit objects, so they are unit tested with mocks.
"""
import json
import os
import re

import parameter_browser_logic as logic

PAYLOAD_VERSION = 1
DATA_VARIABLE = "PBWV_DATA"
SETTINGS_FILE_NAME = "ParameterBrowser.settings.json"


def settings_path(environ=None):
    """The original Parameter Browser settings file, used to seed the first WebView run."""
    environ = os.environ if environ is None else environ
    appdata = environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(appdata, "pyRevit", SETTINGS_FILE_NAME)


def load_settings(path):
    try:
        with open(path, "r") as fp:
            data = json.load(fp)
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def seed_settings(raw):
    source, scope, fields = logic.normalize_settings(raw)
    categories = logic.normalize_content_categories(
        raw.get("content_categories") if isinstance(raw, dict) else None)
    return {"source": source, "scope": scope, "fields": fields, "content_categories": categories}


def read_parameter_values(element, element_name_lookup=None):
    """{parameter name: display text} reading each parameter once (first one wins on duplicates)."""
    values = {}
    try:
        parameters = list(element.Parameters)
    except Exception:
        return values
    for param in parameters:
        try:
            name = logic.to_text(param.Definition.Name)
        except Exception:
            continue
        if not name or name in values or name in logic.PSEUDO_FIELDS:
            continue
        try:
            values[name] = logic.format_parameter_value(param, element_name_lookup)
        except Exception:
            values[name] = u""
    return values


def source_fields(parameter_names):
    names = set(logic.PSEUDO_FIELDS)
    names.update([logic.to_text(n) for n in parameter_names if n])
    names.update(logic.count_fields())
    return sorted(names, key=lambda x: x.lower())


def _id_text(value):
    return None if value is None else logic.to_text(value)


def _clean_record(rec):
    return {
        "id": _id_text(rec["id"]),
        "label": logic.to_text(rec.get("label")),
        "category": logic.to_text(rec.get("category")),
        "level": logic.to_text(rec.get("level")),
        "visible": bool(rec.get("visible")),
        "values": dict((logic.to_text(k), logic.to_text(v)) for k, v in rec.get("values", {}).items()),
        "counts": dict((logic.to_text(k), int(v)) for k, v in rec.get("counts", {}).items()),
    }


def _clean_item(item):
    return {
        "id": _id_text(item["id"]),
        "category": logic.to_text(item.get("category")),
        "family": logic.to_text(item.get("family")),
        "type": logic.to_text(item.get("type")),
        "mark": logic.to_text(item.get("mark")),
        "level": logic.to_text(item.get("level")),
        "space": _id_text(item.get("space")),
        "room": _id_text(item.get("room")),
    }


def build_payload(document, view, sources, contents, phase_notes, settings, control_id, generated):
    """JSON-ready dict for the browser.

    sources: {"Spaces"/"Rooms": {"records": [...], "fields": [...]}} with numeric ids.
    Element ids become strings so 64-bit ids survive JSON and JavaScript numbers exactly.
    """
    data_sources = {}
    for name in logic.SOURCES:
        src = sources.get(name) or {}
        data_sources[name] = {
            "fields": [logic.to_text(f) for f in src.get("fields", [])] or source_fields([]),
            "records": [_clean_record(r) for r in src.get("records", [])],
        }
    return {
        "version": PAYLOAD_VERSION,
        "document": logic.to_text(document),
        "view": logic.to_text(view),
        "generated": logic.to_text(generated),
        "control_id": logic.to_text(control_id),
        "defaults": {
            "sources": list(logic.SOURCES),
            "scopes": list(logic.SCOPES),
            "fields": list(logic.DEFAULT_FIELDS),
            "pseudo_fields": list(logic.PSEUDO_FIELDS),
            "count_prefix": logic.COUNT_PREFIX,
            "categories": list(logic.CONTENT_LABELS),
        },
        "settings": seed_settings(settings),
        "phase_notes": dict((logic.to_text(k), logic.to_text(v)) for k, v in (phase_notes or {}).items()),
        "sources": data_sources,
        "contents": [_clean_item(i) for i in contents],
    }


def _json_default(value):
    """Last-resort conversion for CLR values IronPython's json cannot encode."""
    try:
        return int(value)
    except Exception:
        return logic.to_text(value)


_NON_ASCII = re.compile(u"[^\\x00-\\x7f]")


def _escape_char(match):
    code = ord(match.group(0))
    if code > 0xFFFF:
        # CPython 3 astral character -> UTF-16 surrogate pair (IronPython strings already are UTF-16).
        code -= 0x10000
        return u"\\u{0:04x}\\u{1:04x}".format(0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF))
    return u"\\u{0:04x}".format(code)


def ascii_json(payload):
    """JSON with every non-ASCII character escaped as \\uXXXX.

    json.dumps(ensure_ascii=True) is not used: in IronPython 2.7 every string is a 'str', so the
    json module calls s.decode('utf-8') on text such as u'12 m\\xb2' and raises UnicodeDecodeError.
    """
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    return _NON_ASCII.sub(_escape_char, logic.to_text(encoded))


def data_script(payload):
    """JavaScript that defines window.PBWV_DATA. ASCII-only JSON is also valid JavaScript."""
    return u"window.{0} = {1};\n".format(DATA_VARIABLE, ascii_json(payload))
