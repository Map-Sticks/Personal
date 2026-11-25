# -*- coding: utf-8 -*-
"""3-way Excel import diff engine with non-modifiable guardrails and Type conflict detection.

IronPython 2.7 and CPython 3 compatible.
"""

import browser_state as state


def _norm_compare(val):
    """Normalize numeric/text string formatting for fair Excel-vs-Revit comparison."""
    txt = state.to_text(val).strip()
    if txt.endswith(u".0"):
        try:
            f = float(txt)
            if f == int(f):
                return state.to_text(int(f))
        except Exception:
            pass
    return txt


def _map_headers_to_col_keys(headers, meta_header, records):
    """Resolve Excel column headers back to internal col_keys."""
    if meta_header and isinstance(meta_header.get("columns"), list):
        meta_cols = meta_header["columns"]
        meta_headers = meta_header.get("headers") or []
        by_header = dict((state.to_text(h).strip().lower(), ck)
                         for h, ck in zip(meta_headers, meta_cols))
    else:
        by_header = {}

    # Also build lookup from loaded records' param_meta + pseudo fields.
    known_by_label = {}
    for pf in state.PSEUDO_FIELDS:
        known_by_label[pf.lower()] = pf
    for rec in records[:50]:
        for ck in rec.get("param_meta", {}):
            known_by_label[state.column_display_label(ck).lower()] = ck
            known_by_label[ck.lower()] = ck

    resolved = []
    for h in headers:
        hl = state.to_text(h).strip().lower()
        if hl in by_header:
            resolved.append(by_header[hl])
        elif hl in known_by_label:
            resolved.append(known_by_label[hl])
        elif hl.endswith(u" [t]"):
            resolved.append(u"T:" + state.to_text(h).strip()[:-4])
        else:
            resolved.append(state.make_param_key(h, is_type=False))
    return resolved


def build_import_plan(imported_wb, records, allow_pinned=False):
    """Compare an imported .xlsx workbook against current records + exported _Meta baseline.

    Returns dict:
      {
        "changes": [ ... ],     # list of classified cell change dicts
        "summary": {
          "ready": int,
          "blocked": int,
          "conflicts": int,
          "type_conflicts": int,
          "missing_elements": int,
          "unchanged": int,
        },
        "missing_rows": [ ... ],
      }
    """
    headers = imported_wb.get("headers") or []
    rows = imported_wb.get("rows") or []
    meta_header = imported_wb.get("meta_header")
    baselines = imported_wb.get("baselines") or {}

    col_keys = _map_headers_to_col_keys(headers, meta_header, records)

    # Index current records by UniqueId and by ElementId (host first).
    by_uid = {}
    by_eid = {}
    for rec in records:
        uid = state.to_text(rec.get("unique_id"))
        if uid:
            by_uid[uid] = rec
        if not rec.get("is_linked") and rec.get("id") is not None:
            by_eid[state.to_text(rec["id"])] = rec

    uid_col_idx = col_keys.index(u"UniqueId") if u"UniqueId" in col_keys else -1
    eid_col_idx = col_keys.index(u"Element Id") if u"Element Id" in col_keys else -1

    changes = []
    missing_rows = []
    unchanged = 0
    # Track Type parameter writes to detect conflicting values for the same (type_id, col_key).
    type_writes = {}

    for r_idx, row in enumerate(rows, start=2):
        row_uid = state.to_text(row[uid_col_idx]).strip() if 0 <= uid_col_idx < len(row) else u""
        row_eid = state.to_text(row[eid_col_idx]).strip() if 0 <= eid_col_idx < len(row) else u""
        rec = by_uid.get(row_uid) if row_uid else None
        if rec is None and row_eid:
            rec = by_eid.get(_norm_compare(row_eid))
        if rec is None:
            missing_rows.append({"excel_row": r_idx, "unique_id": row_uid, "element_id": row_eid})
            continue

        baseline_entry = baselines.get(row_uid) or baselines.get(state.to_text(rec.get("id")))
        baseline_vals = baseline_entry.get("values", {}) if isinstance(baseline_entry, dict) else {}

        for c_idx, ck in enumerate(col_keys):
            excel_val = state.to_text(row[c_idx]) if c_idx < len(row) else u""
            current_val = state.to_text(state.record_value(rec, ck))
            exported_val = baseline_vals.get(ck)

            if _norm_compare(excel_val) == _norm_compare(current_val):
                unchanged += 1
                continue

            editable, code, reason = state.evaluate_cell_modifiability(
                rec, ck, allow_pinned=allow_pinned)

            status = "ready"
            detail = reason
            apply_default = True

            if not editable:
                status = "blocked"
                apply_default = False
            elif exported_val is not None and _norm_compare(exported_val) != _norm_compare(current_val):
                # 3-way conflict: Revit changed since the Excel file was exported!
                status = "conflict"
                detail = u"Model changed since export (Exported: '{}', Revit now: '{}').".format(
                    exported_val, current_val)
                apply_default = False
            elif code == "editable_type":
                blast = state.type_blast_radius(records, rec.get("type_id"))
                detail = u"Type parameter — affects {} loaded instance(s).".format(blast)

            item = {
                "excel_row": r_idx,
                "row_key": rec["row_key"],
                "element_id": rec["id"],
                "unique_id": rec.get("unique_id", u""),
                "category": rec.get("category", u""),
                "family_type": u"{} : {}".format(rec.get("family", u""), rec.get("type", u"")),
                "col_key": ck,
                "column_label": state.column_display_label(ck),
                "is_type": (code == "editable_type"),
                "type_id": rec.get("type_id", -1),
                "exported_value": exported_val if exported_val is not None else u"(no baseline)",
                "current_value": current_val,
                "new_value": excel_val,
                "status": status,
                "guard_code": code,
                "reason": detail,
                "selected": apply_default,
            }
            changes.append(item)

            if status == "ready" and item["is_type"] and item["type_id"] not in (None, -1):
                tkey = (item["type_id"], ck)
                type_writes.setdefault(tkey, []).append(item)

    # Detect conflicting edits to the same Type parameter across multiple rows.
    type_conflicts = 0
    for tkey, items in type_writes.items():
        distinct_vals = set(_norm_compare(i["new_value"]) for i in items)
        if len(distinct_vals) > 1:
            for i in items:
                i["status"] = "type_conflict"
                i["selected"] = False
                i["reason"] = u"Conflicting values for the same Type across {} rows: {}".format(
                    len(items), u", ".join(sorted(distinct_vals)))
                type_conflicts += 1

    summary = {
        "ready": sum(1 for c in changes if c["status"] == "ready"),
        "blocked": sum(1 for c in changes if c["status"] == "blocked"),
        "conflicts": sum(1 for c in changes if c["status"] == "conflict"),
        "type_conflicts": type_conflicts,
        "missing_elements": len(missing_rows),
        "unchanged": unchanged,
    }
    return {
        "changes": changes,
        "summary": summary,
        "missing_rows": missing_rows,
    }
