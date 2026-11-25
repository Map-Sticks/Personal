# -*- coding: utf-8 -*-
"""Revit-free core state, modifiability rules, staged edits, and O(k) delta tracking.

IronPython 2.7 and CPython 3 compatible.
"""

import re

try:
    text_type = unicode  # noqa: F821 (IronPython 2.7)
except NameError:
    text_type = str

SCOPES = [
    u"Whole Project",
    u"Active View",
    u"Level",
    u"Current Selection",
    u"Workset",
    u"Selected Spaces / Rooms",
]

REFRESH_MODES = [
    u"Live (Auto)",
    u"Notify Only",
    u"Manual",
]

PSEUDO_FIELDS = [
    u"Element Id",
    u"UniqueId",
    u"Category",
    u"Family",
    u"Type",
    u"Level",
    u"Phase Created",
    u"Phase Demolished",
    u"Workset",
    u"Design Option",
    u"Group",
    u"Pinned",
    u"Host",
    u"Room",
    u"Space",
    u"Link Source",
    u"Edit Status",
]

PSEUDO_FIELD_SET = set(PSEUDO_FIELDS)
COUNT_PREFIX = u"Count: "
TYPE_SUFFIX = u" [T]"
TX_PREFIX = u"Browser & Scheduler:"
STORM_THRESHOLD = 500
ID_COLUMN = u"__row_key"
SEARCH_COLUMN = u"__search"

# Special built-in sources beyond ordinary BuiltInCategories.
SPECIAL_SOURCES = [
    u"Views",
    u"Sheets",
    u"Schedules",
    u"Levels",
    u"Grids",
    u"Rooms",
    u"Spaces",
    u"Areas",
    u"Model Groups",
    u"MEP Systems",
    u"Materials",
    u"Element Types",
]

# Default content categories for spatial rollups and takeoffs.
CONTENT_CATEGORIES = [
    (u"Lighting Fixtures", "OST_LightingFixtures"),
    (u"Electrical Fixtures", "OST_ElectricalFixtures"),
    (u"Electrical Equipment", "OST_ElectricalEquipment"),
    (u"Lighting Devices", "OST_LightingDevices"),
    (u"Fire Alarm Devices", "OST_FireAlarmDevices"),
    (u"Communication Devices", "OST_CommunicationDevices"),
    (u"Data Devices", "OST_DataDevices"),
    (u"Security Devices", "OST_SecurityDevices"),
    (u"Nurse Call Devices", "OST_NurseCallDevices"),
    (u"Mechanical Equipment", "OST_MechanicalEquipment"),
    (u"Air Terminals", "OST_DuctTerminal"),
    (u"Plumbing Fixtures", "OST_PlumbingFixtures"),
    (u"Sprinklers", "OST_Sprinklers"),
    (u"Furniture", "OST_Furniture"),
    (u"Doors", "OST_Doors"),
    (u"Windows", "OST_Windows"),
]
CONTENT_LABELS = [label for label, _ in CONTENT_CATEGORIES]

CRITICAL_SYSTEM_CATEGORIES = set([
    u"Levels",
    u"Grids",
    u"Views",
    u"Sheets",
    u"Schedules",
    u"Phases",
    u"Project Information",
])


def to_text(value):
    if value is None:
        return u""
    try:
        return text_type(value)
    except Exception:
        return u""


def element_id_value(element_id):
    """ElementId.Value on Revit 2024+, IntegerValue on Revit 2023 and older."""
    if element_id is None:
        return -1
    if isinstance(element_id, int):
        return element_id
    try:
        return int(element_id.Value)
    except AttributeError:
        try:
            return int(element_id.IntegerValue)
        except AttributeError:
            return -1


def make_row_key(eid, is_linked=False, link_instance_id=None):
    if is_linked:
        return u"L:{}:{}".format(link_instance_id if link_instance_id is not None else 0, eid)
    return u"H:{}".format(eid)


def make_column_spec(key, label=None, is_type=False, is_pseudo=False, is_count=False,
                     storage_type=u"String", width=140):
    return {
        "key": to_text(key),
        "label": to_text(label if label is not None else key),
        "is_type": bool(is_type),
        "is_pseudo": bool(is_pseudo),
        "is_count": bool(is_count),
        "storage_type": to_text(storage_type or u"String"),
        "width": int(width or 140),
    }


def make_param_key(name, is_type=False):
    name_txt = to_text(name)
    if name_txt in PSEUDO_FIELD_SET:
        return name_txt
    if name_txt.startswith(COUNT_PREFIX):
        return name_txt
    if is_type:
        return u"T:" + name_txt
    return u"I:" + name_txt


def parse_param_key(key):
    """Return (kind, name) where kind is 'P' (pseudo), 'C' (count), 'T' (type), or 'I' (instance)."""
    txt = to_text(key)
    if txt in PSEUDO_FIELD_SET:
        return ("P", txt)
    if txt.startswith(COUNT_PREFIX):
        return ("C", txt[len(COUNT_PREFIX):])
    if txt.startswith(u"T:"):
        return ("T", txt[2:])
    if txt.startswith(u"I:"):
        return ("I", txt[2:])
    return ("I", txt)


def column_display_label(key):
    kind, name = parse_param_key(key)
    if kind == "P":
        return name
    if kind == "C":
        return COUNT_PREFIX + name
    if kind == "T":
        return name + TYPE_SUFFIX
    return name


def is_pseudo_key(key):
    return parse_param_key(key)[0] == "P"


def is_count_key(key):
    return parse_param_key(key)[0] == "C"


def is_type_param_key(key):
    return parse_param_key(key)[0] == "T"


def count_field(category_label):
    return COUNT_PREFIX + to_text(category_label)


def count_fields():
    return [count_field(label) for label in CONTENT_LABELS]


def record_value(rec, col_key):
    """Return display value for a column key on a record."""
    kind, name = parse_param_key(col_key)
    if kind == "P":
        if name == u"Element Id":
            return to_text(rec.get("id", u""))
        if name == u"UniqueId":
            return to_text(rec.get("unique_id", u""))
        if name == u"Category":
            return to_text(rec.get("category", u""))
        if name == u"Family":
            return to_text(rec.get("family", u""))
        if name == u"Type":
            return to_text(rec.get("type", u""))
        if name == u"Level":
            return to_text(rec.get("level", u""))
        if name == u"Phase Created":
            return to_text(rec.get("phase_created", u""))
        if name == u"Phase Demolished":
            return to_text(rec.get("phase_demolished", u""))
        if name == u"Workset":
            return to_text(rec.get("workset", u""))
        if name == u"Design Option":
            return to_text(rec.get("design_option", u""))
        if name == u"Group":
            return to_text(rec.get("group_name", u""))
        if name == u"Pinned":
            return u"Yes" if rec.get("pinned") else u"No"
        if name == u"Host":
            return to_text(rec.get("host", u""))
        if name == u"Room":
            return to_text(rec.get("room", u""))
        if name == u"Space":
            return to_text(rec.get("space", u""))
        if name == u"Link Source":
            return to_text(rec.get("link_name", u"Host"))
        if name == u"Edit Status":
            return element_edit_summary(rec)
        return u""
    if kind == "C":
        return rec.get("counts", {}).get(name, 0)
    return to_text(rec.get("values", {}).get(col_key, u""))


def element_edit_summary(rec):
    """Human-readable modifiability summary for an element row."""
    reasons = []
    if rec.get("is_linked"):
        reasons.append(u"Linked (Read-Only)")
    if rec.get("checkout_status") == "OwnedByOtherUser":
        owner = to_text(rec.get("checkout_owner")) or u"another user"
        reasons.append(u"Owned by {}".format(owner))
    if rec.get("group_id", -1) not in (None, -1):
        gname = to_text(rec.get("group_name")) or u"Group"
        reasons.append(u"In Group ({})".format(gname))
    if rec.get("pinned"):
        reasons.append(u"Pinned")
    if not reasons:
        return u"Editable"
    return u"; ".join(reasons)


def evaluate_cell_modifiability(rec, col_key, allow_pinned=False):
    """Classify whether a (record, column) cell may be modified.

    Returns (editable: bool, code: str, reason: unicode).
    Codes:
      editable, editable_type, pseudo, count, linked, owned_by_other,
      in_group, pinned, read_only, not_present
    """
    kind, name = parse_param_key(col_key)
    if kind == "P":
        return (False, "pseudo", u"System/computed column '{}' is read-only.".format(name))
    if kind == "C":
        return (False, "count", u"Computed count column '{}' is read-only.".format(name))
    if rec.get("is_linked"):
        link_name = to_text(rec.get("link_name")) or u"Linked Model"
        return (False, "linked", u"Element belongs to linked model '{}' (read-only).".format(link_name))
    if rec.get("checkout_status") == "OwnedByOtherUser":
        owner = to_text(rec.get("checkout_owner")) or u"another user"
        return (False, "owned_by_other", u"Workshared element is owned by {}.".format(owner))
    if rec.get("group_id", -1) not in (None, -1) and kind != "T":
        gname = to_text(rec.get("group_name")) or u"Group"
        return (False, "in_group", u"Element is inside group '{}' (edit group to modify).".format(gname))
    if rec.get("pinned") and not allow_pinned:
        return (False, "pinned", u"Element is pinned (unpin or enable pinned override).")

    pmeta = rec.get("param_meta", {}).get(col_key)
    if pmeta is None:
        return (False, "not_present", u"Parameter '{}' does not exist on this element.".format(name))
    if pmeta.get("is_read_only") or not pmeta.get("user_modifiable", True):
        return (False, "read_only", u"Parameter '{}' is read-only or formula-driven.".format(name))
    if kind == "T":
        return (True, "editable_type", u"Type parameter (affects all instances of this type).")
    return (True, "editable", u"Editable instance parameter.")


def evaluate_delete_safety(rec, allow_pinned=False):
    """Classify whether an element may be deleted. Returns (allowed, level, reason).

    level is 'ok', 'warn', or 'block'.
    """
    if rec.get("is_linked"):
        return (False, "block", u"Linked model element cannot be deleted from host.")
    if rec.get("checkout_status") == "OwnedByOtherUser":
        owner = to_text(rec.get("checkout_owner")) or u"another user"
        return (False, "block", u"Owned by {} in worksharing.".format(owner))
    if rec.get("is_type_element"):
        return (True, "warn", u"Element Type: deleting may fail if instances exist or purge type.")
    if rec.get("group_id", -1) not in (None, -1):
        return (False, "block", u"Member of group '{}' — edit or ungroup first.".format(
            to_text(rec.get("group_name")) or u"Group"))
    if rec.get("pinned") and not allow_pinned:
        return (False, "block", u"Element is pinned.")
    if to_text(rec.get("category")) in CRITICAL_SYSTEM_CATEGORIES:
        return (True, "warn", u"Critical system element ({}) — may cascade-delete hosted items.".format(
            to_text(rec.get("category"))))
    return (True, "ok", u"Safe to delete.")


def type_blast_radius(records, type_id):
    """Count how many host records share a given type_id."""
    if type_id is None or type_id < 0:
        return 0
    return sum(1 for r in records if not r.get("is_linked") and r.get("type_id") == type_id)


class StagedEdits(object):
    """Tracks unsaved inline/bulk parameter edits before committing to Revit."""

    def __init__(self):
        # (row_key, col_key) -> dict(row_key, col_key, element_id, type_id, is_type, old_value, new_value, conflict)
        self.edits = {}

    def count(self):
        return len(self.edits)

    def has_edit(self, row_key, col_key):
        return (row_key, col_key) in self.edits

    def get_value(self, row_key, col_key, default=None):
        entry = self.edits.get((row_key, col_key))
        return entry["new_value"] if entry is not None else default

    def stage(self, rec, col_key, new_value, allow_pinned=False):
        """Stage one edit. Returns (ok, message)."""
        ok, code, reason = evaluate_cell_modifiability(rec, col_key, allow_pinned=allow_pinned)
        if not ok:
            return (False, reason)
        new_txt = to_text(new_value)
        old_txt = record_value(rec, col_key)
        key = (rec["row_key"], col_key)
        if new_txt == old_txt:
            self.edits.pop(key, None)
            return (True, u"Reverted to current value.")
        self.edits[key] = {
            "row_key": rec["row_key"],
            "col_key": col_key,
            "element_id": rec["id"],
            "type_id": rec.get("type_id", -1),
            "is_type": (code == "editable_type"),
            "old_value": old_txt,
            "new_value": new_txt,
            "conflict": False,
        }
        return (True, u"Staged.")

    def clear(self):
        self.edits = {}

    def remove_for_rows(self, row_keys):
        removed = 0
        key_set = set(row_keys)
        for k in list(self.edits.keys()):
            if k[0] in key_set:
                del self.edits[k]
                removed += 1
        return removed

    def mark_external_update(self, rec):
        """If Revit modified a record that has staged edits, flag conflicts instead of wiping them."""
        conflicts = 0
        rk = rec["row_key"]
        for (erk, col_key), entry in list(self.edits.items()):
            if erk != rk:
                continue
            current = record_value(rec, col_key)
            if current == entry["new_value"]:
                del self.edits[(erk, col_key)]
            elif current != entry["old_value"]:
                entry["conflict"] = True
                conflicts += 1
        return conflicts

    def stage_set_value(self, records, col_key, new_value, allow_pinned=False):
        staged = 0
        skipped = []
        for rec in records:
            ok, msg = self.stage(rec, col_key, new_value, allow_pinned=allow_pinned)
            if ok:
                staged += 1
            else:
                skipped.append((rec["row_key"], msg))
        return staged, skipped

    def stage_find_replace(self, records, col_key, find_text, replace_text,
                           use_regex=False, case_sensitive=False, allow_pinned=False):
        staged = 0
        skipped = []
        if not find_text:
            return 0, []
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(find_text if use_regex else re.escape(find_text), flags)
        for rec in records:
            cur = self.get_value(rec["row_key"], col_key, record_value(rec, col_key))
            updated = pattern.sub(replace_text, cur)
            if updated != cur:
                ok, msg = self.stage(rec, col_key, updated, allow_pinned=allow_pinned)
                if ok:
                    staged += 1
                else:
                    skipped.append((rec["row_key"], msg))
        return staged, skipped

    def stage_auto_number(self, records, col_key, prefix=u"", start=1, step=1,
                          padding=2, suffix=u"", allow_pinned=False):
        staged = 0
        skipped = []
        num = int(start)
        fmt = u"{{0}}{{1:0{}d}}{{2}}".format(max(1, int(padding)))
        for rec in records:
            val = fmt.format(to_text(prefix), num, to_text(suffix))
            ok, msg = self.stage(rec, col_key, val, allow_pinned=allow_pinned)
            if ok:
                staged += 1
                num += int(step)
            else:
                skipped.append((rec["row_key"], msg))
        return staged, skipped

    def stage_paste_tsv(self, records, col_keys, tsv_text, allow_pinned=False):
        """Paste tab-separated lines (Excel clipboard) into selected rows/columns."""
        lines = [line for line in to_text(tsv_text).replace(u"\r\n", u"\n").split(u"\n") if line != u""]
        staged = 0
        skipped = []
        for r_idx, line in enumerate(lines):
            if r_idx >= len(records):
                break
            cells = line.split(u"\t")
            rec = records[r_idx]
            for c_idx, cell_val in enumerate(cells):
                if c_idx >= len(col_keys):
                    break
                ok, msg = self.stage(rec, col_keys[c_idx], cell_val, allow_pinned=allow_pinned)
                if ok:
                    staged += 1
                else:
                    skipped.append((rec["row_key"], msg))
        return staged, skipped

    def as_list(self):
        return list(self.edits.values())


class DeltaTracker(object):
    """O(k) DocumentChanged incremental tracker strictly scoped to listed elements."""

    def __init__(self, storm_threshold=STORM_THRESHOLD):
        self.storm_threshold = int(storm_threshold)
        self.tracked_element_ids = set()
        self.tracked_type_ids = set()
        self.tracked_category_ids = set()
        self.watch_adds = True
        self.pending_added = set()
        self.pending_modified = set()
        self.pending_deleted = set()
        self.pending_type_modified = set()
        self.storm_triggered = False

    def rebuild_index(self, records, active_category_ids=None, watch_types=False):
        self.tracked_element_ids = set(
            r["id"] for r in records if not r.get("is_linked") and r.get("id") is not None
        )
        if watch_types:
            self.tracked_type_ids = set(
                r["type_id"] for r in records
                if not r.get("is_linked") and r.get("type_id") not in (None, -1)
            )
        else:
            self.tracked_type_ids = set()
        self.tracked_category_ids = set(active_category_ids or [])
        self.clear_pending()

    def clear_pending(self):
        self.pending_added.clear()
        self.pending_modified.clear()
        self.pending_deleted.clear()
        self.pending_type_modified.clear()
        self.storm_triggered = False

    def pending_total(self):
        return (len(self.pending_added) + len(self.pending_modified) +
                len(self.pending_deleted) + len(self.pending_type_modified))

    def record_document_changed(self, added_ids, modified_ids, deleted_ids, tx_names=None):
        """Filter raw DocumentChanged IDs in O(k) time. Returns True if any tracked item is affected."""
        if tx_names:
            for name in tx_names:
                if to_text(name).startswith(TX_PREFIX):
                    return False

        hit = False
        if deleted_ids:
            del_hits = self.tracked_element_ids.intersection(deleted_ids)
            if del_hits:
                self.pending_deleted.update(del_hits)
                self.pending_modified.difference_update(del_hits)
                self.pending_added.difference_update(del_hits)
                hit = True

        if modified_ids:
            mod_hits = self.tracked_element_ids.intersection(modified_ids)
            if mod_hits:
                self.pending_modified.update(mod_hits)
                hit = True
            if self.tracked_type_ids:
                type_hits = self.tracked_type_ids.intersection(modified_ids)
                if type_hits:
                    self.pending_type_modified.update(type_hits)
                    hit = True

        if added_ids and self.watch_adds and self.tracked_category_ids:
            # Cap raw added intake so giant paste/sync bursts trip the storm guard immediately.
            if len(added_ids) > self.storm_threshold:
                self.storm_triggered = True
                return True
            self.pending_added.update(added_ids)
            hit = True

        if self.pending_total() > self.storm_threshold:
            self.storm_triggered = True
        return hit

    def pop_delta(self):
        delta = (
            set(self.pending_added),
            set(self.pending_modified),
            set(self.pending_deleted),
            set(self.pending_type_modified),
            self.storm_triggered,
        )
        self.clear_pending()
        return delta


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
