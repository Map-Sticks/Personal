# -*- coding: utf-8 -*-
"""Preset storage, validation, and built-in starter presets.

User presets live in %APPDATA%\\pyRevit\\RevitBrowserScheduler\\presets\\*.json.
Architecture leaves room for an optional team/shared folder in a future update.
IronPython 2.7 and CPython 3 compatible.
"""

import json
import os
import re
import time

import browser_state as state

SCHEMA_VERSION = 1


def default_presets_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(base, "pyRevit", "RevitBrowserScheduler", "presets")


def default_settings_path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(base, "pyRevit", "RevitBrowserScheduler.settings.json")


def safe_filename(name):
    cleaned = re.sub(r'[^A-Za-z0-9._ -]+', '_', state.to_text(name).strip())
    cleaned = cleaned.strip(' .') or u"preset"
    return cleaned + u".json"


def make_preset(name, categories=None, special_sources=None, include_instances=True,
                include_types=False, include_links=False, scope=u"Whole Project",
                level=None, workset=None, selected_space_ids=None,
                columns=None, query=u"", sort_column=None, sort_ascending=True,
                grouped_mode=False, group_by=None, show_contents_panel=False,
                content_categories=None, refresh_mode=u"Live (Auto)",
                protect_excel=True, description=u"", builtin=False):
    now = time.strftime("%Y-%m-%d %H:%M")
    return {
        "schema_version": SCHEMA_VERSION,
        "name": state.to_text(name).strip() or u"Untitled Preset",
        "description": state.to_text(description),
        "builtin": bool(builtin),
        "updated": now,
        "categories": [state.to_text(c) for c in (categories or [])],
        "special_sources": [state.to_text(s) for s in (special_sources or [])],
        "include_instances": bool(include_instances),
        "include_types": bool(include_types),
        "include_links": bool(include_links),
        "scope": state.to_text(scope) if scope in state.SCOPES else state.SCOPES[0],
        "level": state.to_text(level) if level else None,
        "workset": state.to_text(workset) if workset else None,
        "selected_space_ids": [int(x) for x in (selected_space_ids or [])],
        "columns": [state.to_text(c) for c in (columns or [u"Category", u"Family", u"Type", u"Level", u"I:Mark"])],
        "query": state.to_text(query),
        "sort_column": state.to_text(sort_column) if sort_column else None,
        "sort_ascending": bool(sort_ascending),
        "grouped_mode": bool(grouped_mode),
        "group_by": [state.to_text(g) for g in (group_by or [])],
        "show_contents_panel": bool(show_contents_panel),
        "content_categories": [state.to_text(c) for c in (content_categories or list(state.CONTENT_LABELS))],
        "refresh_mode": state.to_text(refresh_mode) if refresh_mode in state.REFRESH_MODES else state.REFRESH_MODES[0],
        "protect_excel": bool(protect_excel),
    }


def normalize_preset(data):
    if not isinstance(data, dict):
        data = {}
    return make_preset(
        name=data.get("name", u"Untitled Preset"),
        categories=data.get("categories"),
        special_sources=data.get("special_sources"),
        include_instances=data.get("include_instances", True),
        include_types=data.get("include_types", False),
        include_links=data.get("include_links", False),
        scope=data.get("scope", u"Whole Project"),
        level=data.get("level"),
        workset=data.get("workset"),
        selected_space_ids=data.get("selected_space_ids"),
        columns=data.get("columns"),
        query=data.get("query", u""),
        sort_column=data.get("sort_column"),
        sort_ascending=data.get("sort_ascending", True),
        grouped_mode=data.get("grouped_mode", False),
        group_by=data.get("group_by"),
        show_contents_panel=data.get("show_contents_panel", False),
        content_categories=data.get("content_categories"),
        refresh_mode=data.get("refresh_mode", u"Live (Auto)"),
        protect_excel=data.get("protect_excel", True),
        description=data.get("description", u""),
        builtin=data.get("builtin", False),
    )


def builtin_presets():
    """Starter presets covering takeoffs, totals, specific spaces, combined selected spaces, views & QA."""
    return [
        make_preset(
            name=u"Electrical & Lighting Takeoff (Totals)",
            description=u"Grouped quantity takeoff with Count totals across Lighting, Electrical, Fire Alarm, and Data.",
            categories=[
                u"Lighting Fixtures", u"Lighting Devices", u"Electrical Fixtures",
                u"Electrical Equipment", u"Fire Alarm Devices", u"Data Devices",
                u"Communication Devices", u"Security Devices",
            ],
            columns=[u"Category", u"Family", u"Type", u"Level", u"T:Type Mark", u"I:Mark", u"Space"],
            grouped_mode=True,
            group_by=[u"Category", u"Family", u"Type"],
            sort_column=u"Category",
            builtin=True,
        ),
        make_preset(
            name=u"Multi-Category Quantity Takeoff",
            description=u"Combined architectural & MEP takeoff grouped by Category, Level, Family and Type with totals.",
            categories=[
                u"Doors", u"Windows", u"Walls", u"Floors",
                u"Lighting Fixtures", u"Electrical Fixtures", u"Mechanical Equipment",
            ],
            columns=[u"Category", u"Level", u"Family", u"Type", u"I:Area", u"I:Length", u"I:Comments"],
            grouped_mode=True,
            group_by=[u"Category", u"Level", u"Family", u"Type"],
            sort_column=u"Category",
            builtin=True,
        ),
        make_preset(
            name=u"Spaces & Rooms Schedule",
            description=u"Browse Spaces and Rooms with electrical count rollups and interactive contents panel.",
            special_sources=[u"Spaces", u"Rooms"],
            columns=[
                u"Category", u"I:Number", u"I:Name", u"Level", u"I:Area",
                u"Count: Lighting Fixtures", u"Count: Electrical Fixtures",
                u"Count: Fire Alarm Devices", u"Edit Status",
            ],
            sort_column=u"I:Number",
            show_contents_panel=True,
            builtin=True,
        ),
        make_preset(
            name=u"Selected Spaces - Combined Takeoff",
            description=u"Pick one or more specific Spaces/Rooms and schedule/total all elements inside that combination.",
            categories=[
                u"Lighting Fixtures", u"Lighting Devices", u"Electrical Fixtures",
                u"Electrical Equipment", u"Fire Alarm Devices", u"Data Devices",
                u"Air Terminals", u"Mechanical Equipment", u"Furniture",
            ],
            scope=u"Selected Spaces / Rooms",
            columns=[u"Space", u"Room", u"Category", u"Family", u"Type", u"I:Mark", u"I:Comments"],
            grouped_mode=False,
            group_by=[u"Space", u"Category", u"Family", u"Type"],
            sort_column=u"Space",
            show_contents_panel=True,
            builtin=True,
        ),
        make_preset(
            name=u"Views & Sheets Index",
            description=u"Universal index of all Views, Sheets, and Schedules for fast navigation and metadata edits.",
            special_sources=[u"Sheets", u"Views", u"Schedules"],
            columns=[u"Category", u"I:Sheet Number", u"I:Sheet Name", u"I:View Name", u"I:View Scale", u"Edit Status"],
            sort_column=u"Category",
            builtin=True,
        ),
        make_preset(
            name=u"Model QA - Missing & Duplicate Marks",
            description=u"Finds model elements with duplicate or blank Mark values across key MEP & architectural categories.",
            categories=[
                u"Doors", u"Windows", u"Electrical Equipment", u"Lighting Fixtures",
                u"Fire Alarm Devices", u"Mechanical Equipment",
            ],
            columns=[u"Category", u"Level", u"Family", u"Type", u"I:Mark", u"Space", u"Edit Status"],
            query=u"Mark:EMPTY OR Mark:DUPLICATE",
            sort_column=u"I:Mark",
            builtin=True,
        ),
    ]


class PresetStore(object):
    """Manages built-in + user JSON presets on disk."""

    def __init__(self, user_dir=None, shared_dir=None):
        self.user_dir = user_dir or default_presets_dir()
        # Reserved for future team presets support.
        self.shared_dir = shared_dir

    def ensure_dir(self):
        if not os.path.exists(self.user_dir):
            os.makedirs(self.user_dir)

    def list_presets(self):
        """Return list of presets: user presets first (sorted), then built-ins."""
        user_presets = []
        if os.path.isdir(self.user_dir):
            for fname in sorted(os.listdir(self.user_dir), key=lambda x: x.lower()):
                if not fname.lower().endswith(".json"):
                    continue
                path = os.path.join(self.user_dir, fname)
                try:
                    with open(path, "r") as fp:
                        data = json.load(fp)
                    preset = normalize_preset(data)
                    preset["builtin"] = False
                    preset["_path"] = path
                    user_presets.append(preset)
                except Exception:
                    continue
        builtins = builtin_presets()
        user_names = set(p["name"].lower() for p in user_presets)
        combined = list(user_presets)
        for bp in builtins:
            if bp["name"].lower() not in user_names:
                combined.append(bp)
        return combined

    def save_preset(self, preset):
        self.ensure_dir()
        clean = normalize_preset(preset)
        clean["builtin"] = False
        clean["updated"] = time.strftime("%Y-%m-%d %H:%M")
        path = os.path.join(self.user_dir, safe_filename(clean["name"]))
        to_write = dict((k, v) for k, v in clean.items() if not k.startswith("_"))
        with open(path, "w") as fp:
            json.dump(to_write, fp, indent=2, sort_keys=True)
        clean["_path"] = path
        return clean

    def delete_preset(self, name):
        path = os.path.join(self.user_dir, safe_filename(name))
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False

    def export_preset(self, preset, filepath):
        clean = normalize_preset(preset)
        to_write = dict((k, v) for k, v in clean.items() if not k.startswith("_"))
        with open(filepath, "w") as fp:
            json.dump(to_write, fp, indent=2, sort_keys=True)
        return filepath

    def import_preset(self, filepath):
        with open(filepath, "r") as fp:
            data = json.load(fp)
        preset = normalize_preset(data)
        preset["builtin"] = False
        return self.save_preset(preset)
