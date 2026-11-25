# -*- coding: utf-8 -*-
"""Structured search/filter parser, natural sorting, grouping & takeoff totals.

IronPython 2.7 and CPython 3 compatible.
"""

import re
import browser_state as state

_NUM_RE = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))")
_NAT_RE = re.compile(r"(\d+)")
_TOKEN_RE = re.compile(
    r'(-?)'                                           # optional negation prefix
    r'(?:([A-Za-z0-9_.\s\[\]]+?)(!=|>=|<=|>|<|=|~|:))?'  # optional column + operator
    r'(?:"([^"]*)"|\'([^\']*)\'|(\S+))'               # quoted or bare value
)


def extract_number(value):
    """Parse leading numeric value from a display string like '24.5 m2' or '1200 mm'."""
    if isinstance(value, (int, float)):
        return float(value)
    txt = state.to_text(value).replace(u",", u"").strip()
    if not txt:
        return None
    m = _NUM_RE.match(txt)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def natural_sort_key(value):
    """Natural sort key: numbers sort numerically, text case-insensitively."""
    if isinstance(value, (int, float)):
        return [(0, float(value))]
    txt = state.to_text(value).strip()
    num = extract_number(txt)
    if num is not None and _NUM_RE.match(txt).end() == len(txt):
        return [(0, num)]
    parts = _NAT_RE.split(txt.lower())
    key = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, float(int(part))))
        else:
            key.append((1, part))
    return key or [(1, u"")]


def parse_query(query_text):
    r"""Parse search text into OR-groups of AND-clauses.

    Supports:
      - Plain words: `led panel` (every word matches any visible column)
      - Negation: `-emergency`
      - Column operators: `Category:Walls`, `Level="Level 01"`, `Area>20`, `Mark!=""`,
        `Comments~^DB-\d+`, `Mark:EMPTY`, `Mark:NONEMPTY`, `Mark:DUPLICATE`
      - `OR` separator between groups
    """
    raw = state.to_text(query_text).strip()
    if not raw:
        return []

    # Split top-level OR (uppercase OR bounded by whitespace).
    or_parts = re.split(r"\s+OR\s+", raw)
    groups = []
    for part in or_parts:
        part = part.strip()
        if not part:
            continue
        clauses = []
        # Tokenize while respecting quoted values attached to col=value.
        for match in _TOKEN_RE.finditer(part):
            neg = bool(match.group(1))
            col = (match.group(2) or u"").strip()
            op = match.group(3) or u""
            val = match.group(4)
            if val is None:
                val = match.group(5)
            if val is None:
                val = match.group(6) or u""
            # Guard against accidental multi-word col capture when there is no operator.
            if op and u" " in col and col.lower() not in _KNOWN_MULTIWORD_LOWER:
                # Keep only the final word as column name if preceding words were bare tokens.
                pieces = col.split()
                for bare in pieces[:-1]:
                    if bare.startswith(u"-") and len(bare) > 1:
                        clauses.append({"neg": True, "col": None, "op": "contains", "val": bare[1:]})
                    else:
                        clauses.append({"neg": False, "col": None, "op": "contains", "val": bare})
                col = pieces[-1]
            if not op:
                clauses.append({"neg": neg, "col": None, "op": "contains", "val": val})
            else:
                clauses.append({"neg": neg, "col": col, "op": op, "val": val})
        if clauses:
            groups.append(clauses)
    return groups


_KNOWN_MULTIWORD_LOWER = set([
    u"element id", u"phase created", u"phase demolished", u"design option",
    u"link source", u"edit status", u"type mark", u"type name", u"family name",
])


def _resolve_col_key(col_query, col_keys):
    target = state.to_text(col_query).strip().lower()
    for key in col_keys:
        if key.lower() == target:
            return key
        if state.column_display_label(key).lower() == target:
            return key
        _, name = state.parse_param_key(key)
        if name.lower() == target:
            return key
    # Prefix fallback
    for key in col_keys:
        if state.column_display_label(key).lower().startswith(target):
            return key
    return None


def _compute_duplicates(records, col_key, value_getter):
    counts = {}
    for rec in records:
        val = state.to_text(value_getter(rec, col_key)).strip().lower()
        if val:
            counts[val] = counts.get(val, 0) + 1
    return set(k for k, v in counts.items() if v > 1)


def _match_clause(rec, clause, col_keys, value_getter, dup_cache, all_records):
    col = clause["col"]
    op = clause["op"]
    val = clause["val"]
    val_lower = val.lower()

    if col is None:
        haystack = u" | ".join(
            state.to_text(value_getter(rec, ck)).lower() for ck in col_keys
        )
        matched = val_lower in haystack
    else:
        col_key = _resolve_col_key(col, col_keys)
        if col_key is None:
            matched = False
        else:
            cell_txt = state.to_text(value_getter(rec, col_key))
            cell_lower = cell_txt.strip().lower()
            if op == ":" and val_lower == u"empty":
                matched = (cell_lower == u"")
            elif op == ":" and val_lower == u"nonempty":
                matched = (cell_lower != u"")
            elif op == ":" and val_lower == u"duplicate":
                if col_key not in dup_cache:
                    dup_cache[col_key] = _compute_duplicates(all_records, col_key, value_getter)
                matched = (cell_lower in dup_cache[col_key])
            elif op == ":":
                matched = val_lower in cell_lower
            elif op == "=":
                matched = (cell_lower == val_lower)
            elif op == "!=":
                matched = (cell_lower != val_lower)
            elif op == "~":
                try:
                    matched = bool(re.search(val, cell_txt, re.IGNORECASE))
                except Exception:
                    matched = False
            elif op in (">", ">=", "<", "<="):
                lhs = extract_number(cell_txt)
                rhs = extract_number(val)
                if lhs is None or rhs is None:
                    matched = False
                elif op == ">":
                    matched = lhs > rhs
                elif op == ">=":
                    matched = lhs >= rhs
                elif op == "<":
                    matched = lhs < rhs
                else:
                    matched = lhs <= rhs
            else:
                matched = val_lower in cell_lower

    return (not matched) if clause["neg"] else matched


def filter_records_by_scope(records, scope, level=None, workset=None,
                            selected_space_ids=None, selection_ids=None):
    """Apply scope filtering to cached records."""
    if scope == u"Active View":
        return [r for r in records if r.get("visible")]
    if scope == u"Level":
        if not level:
            return list(records)
        levels = set(level) if isinstance(level, (list, set, tuple)) else set([level])
        return [r for r in records if r.get("level") in levels]
    if scope == u"Workset":
        if not workset:
            return list(records)
        return [r for r in records if r.get("workset") == workset]
    if scope == u"Current Selection":
        if not selection_ids:
            return []
        sel = set(selection_ids)
        return [r for r in records if r.get("id") in sel and not r.get("is_linked")]
    if scope == u"Selected Spaces / Rooms":
        if not selected_space_ids:
            return []
        chosen = set(selected_space_ids)
        return [
            r for r in records
            if r.get("id") in chosen
            or r.get("space_id") in chosen
            or r.get("room_id") in chosen
        ]
    return list(records)


def filter_records_by_query(records, query_text, col_keys, value_getter=None):
    """Filter records using the parsed query expression."""
    groups = parse_query(query_text)
    if not groups:
        return list(records)
    getter = value_getter or state.record_value
    dup_cache = {}
    out = []
    for rec in records:
        for clauses in groups:
            if all(_match_clause(rec, cl, col_keys, getter, dup_cache, records) for cl in clauses):
                out.append(rec)
                break
    return out


def sort_records(records, sort_col_key, ascending=True, value_getter=None):
    if not sort_col_key:
        return list(records)
    getter = value_getter or state.record_value
    return sorted(
        records,
        key=lambda r: natural_sort_key(getter(r, sort_col_key)),
        reverse=(not ascending),
    )


def build_grouped_takeoff(records, col_keys, group_by_keys, value_getter=None):
    """Group records by one or more column keys and compute Count + numeric Sum totals.

    Returns list of summary row dicts:
      {
        "group_key": tuple,
        "row_keys": [...],
        "element_ids": [...],
        "count": int,
        "values": {col_key: display_str},
      }
    """
    getter = value_getter or state.record_value
    if not group_by_keys:
        group_by_keys = [u"Category", u"Family", u"Type"]

    buckets = {}
    order = []
    for rec in records:
        gkey = tuple(state.to_text(getter(rec, k)) for k in group_by_keys)
        if gkey not in buckets:
            buckets[gkey] = []
            order.append(gkey)
        buckets[gkey].append(rec)

    summary_rows = []
    for gkey in order:
        members = buckets[gkey]
        row_vals = {}
        for ck in col_keys:
            if ck in group_by_keys:
                row_vals[ck] = gkey[group_by_keys.index(ck)]
            else:
                raw_vals = [getter(m, ck) for m in members]
                nums = [extract_number(v) for v in raw_vals]
                if nums and all(n is not None for n in nums):
                    total = sum(nums)
                    if total == int(total):
                        row_vals[ck] = state.to_text(int(total))
                    else:
                        row_vals[ck] = u"{:.2f}".format(total)
                else:
                    distinct = sorted(set(state.to_text(v) for v in raw_vals if state.to_text(v)))
                    if len(distinct) == 1:
                        row_vals[ck] = distinct[0]
                    elif len(distinct) == 0:
                        row_vals[ck] = u""
                    else:
                        row_vals[ck] = u"<varies ({})>".format(len(distinct))
        summary_rows.append({
            "group_key": gkey,
            "row_keys": [m["row_key"] for m in members],
            "element_ids": [m["id"] for m in members if not m.get("is_linked")],
            "count": len(members),
            "values": row_vals,
        })
    return summary_rows


def compute_grand_totals(records, col_keys, value_getter=None):
    """Compute grand total row (Count + numeric sums for summable columns)."""
    getter = value_getter or state.record_value
    totals = {"count": len(records), "sums": {}}
    for ck in col_keys:
        if ck in (u"Element Id", u"UniqueId", u"Pinned"):
            continue
        nums = [extract_number(getter(r, ck)) for r in records]
        valid = [n for n in nums if n is not None]
        if valid and len(valid) >= max(1, int(len(records) * 0.5)):
            s = sum(valid)
            totals["sums"][ck] = int(s) if s == int(s) else round(s, 2)
    return totals
