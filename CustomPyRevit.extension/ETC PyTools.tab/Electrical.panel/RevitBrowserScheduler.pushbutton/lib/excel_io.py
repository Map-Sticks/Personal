# -*- coding: utf-8 -*-
"""Excel (.xlsx) protected exporter and pure-Python .xlsx importer (zero Excel COM / openpyxl needed).

Export uses xlsxwriter (shipped with pyRevit) with a CSV fallback.
Import uses Python's built-in zipfile + xml.etree.ElementTree so it works identically on
IronPython 2.7 (pyRevit 4.8-7.x) and CPython 3.
"""

import csv
import io
import json
import re
import time
import zipfile
from xml.etree import ElementTree as ET

import browser_state as state

META_SHEET_NAME = u"_Meta"
SCHEDULE_SHEET_NAME = u"Schedule"
LEGEND_SHEET_NAME = u"_Legend"
EXPORT_SCHEMA_VERSION = 1

_CELL_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")


def _col_letters_to_index(letters):
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord('A') + 1)
    return idx - 1


def export_schedule_xlsx(filepath, records, col_keys, doc_meta=None,
                         protect_sheet=True, allow_pinned=False):
    """Write a formatted, cell-protected .xlsx workbook with hidden _Meta baseline.

    Returns dict with {path, format, rows, columns, locked_cells, editable_cells}.
    """
    try:
        import xlsxwriter
    except ImportError:
        return export_schedule_csv(filepath, records, col_keys)

    doc_meta = doc_meta or {}
    # Always include UniqueId and Element Id at the front for rock-solid re-import matching.
    export_cols = []
    for required in (u"UniqueId", u"Element Id"):
        if required not in col_keys:
            export_cols.append(required)
    for ck in col_keys:
        if ck not in export_cols:
            export_cols.append(ck)

    headers = [state.column_display_label(ck) for ck in export_cols]
    wb = xlsxwriter.Workbook(filepath)
    try:
        ws = wb.add_worksheet(SCHEDULE_SHEET_NAME)
        ws_meta = wb.add_worksheet(META_SHEET_NAME)
        ws_legend = wb.add_worksheet(LEGEND_SHEET_NAME)

        fmt_header = wb.add_format({
            "bold": True, "bg_color": "#1A528A", "font_color": "#FFFFFF",
            "border": 1, "locked": True, "align": "left", "valign": "vcenter",
        })
        fmt_header_type = wb.add_format({
            "bold": True, "bg_color": "#2B6CB0", "font_color": "#FFFFFF",
            "border": 1, "locked": True, "align": "left", "valign": "vcenter",
        })
        fmt_editable = wb.add_format({
            "bg_color": "#FFFFFF", "font_color": "#111827",
            "border": 1, "locked": False,
        })
        fmt_editable_type = wb.add_format({
            "bg_color": "#EBF5FF", "font_color": "#1E3A8A",
            "border": 1, "locked": False,
        })
        fmt_locked = wb.add_format({
            "bg_color": "#ECEFF3", "font_color": "#6B7280",
            "border": 1, "locked": True,
        })

        widths = [max(12, len(h) + 4) for h in headers]
        for c_idx, ck in enumerate(export_cols):
            h_fmt = fmt_header_type if state.is_type_param_key(ck) else fmt_header
            ws.write(0, c_idx, headers[c_idx], h_fmt)
            if state.is_type_param_key(ck):
                ws.write_comment(0, c_idx, "TYPE parameter: editing affects ALL instances of that type.")

        locked_cells = 0
        editable_cells = 0
        baseline_rows = []

        for r_idx, rec in enumerate(records, start=1):
            row_baseline = {
                "unique_id": state.to_text(rec.get("unique_id")),
                "element_id": rec.get("id"),
                "row_key": rec.get("row_key"),
                "values": {},
            }
            for c_idx, ck in enumerate(export_cols):
                val = state.record_value(rec, ck)
                val_txt = state.to_text(val)
                row_baseline["values"][ck] = val_txt
                editable, code, _reason = state.evaluate_cell_modifiability(
                    rec, ck, allow_pinned=allow_pinned)
                if editable:
                    editable_cells += 1
                    cell_fmt = fmt_editable_type if code == "editable_type" else fmt_editable
                else:
                    locked_cells += 1
                    cell_fmt = fmt_locked

                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    ws.write_number(r_idx, c_idx, val, cell_fmt)
                else:
                    ws.write_string(r_idx, c_idx, val_txt, cell_fmt)

                widths[c_idx] = min(55, max(widths[c_idx], len(val_txt) + 3))
            baseline_rows.append(row_baseline)

        for c_idx, w in enumerate(widths):
            ws.set_column(c_idx, c_idx, w)
        ws.freeze_panes(1, 0)
        if records:
            ws.autofilter(0, 0, len(records), len(export_cols) - 1)
        if protect_sheet:
            ws.protect("", {
                "select_locked_cells": True,
                "select_unlocked_cells": True,
                "sort": True,
                "autofilter": True,
                "format_columns": True,
                "format_rows": True,
            })

        # Write hidden _Meta sheet for 3-way diffing on re-import.
        meta_header = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "doc_title": state.to_text(doc_meta.get("doc_title", u"")),
            "doc_guid": state.to_text(doc_meta.get("doc_guid", u"")),
            "columns": export_cols,
            "headers": headers,
        }
        ws_meta.write_string(0, 0, "META_HEADER")
        ws_meta.write_string(0, 1, json.dumps(meta_header))
        for idx, rb in enumerate(baseline_rows, start=1):
            ws_meta.write_string(idx, 0, rb["unique_id"] or state.to_text(rb["element_id"]))
            ws_meta.write_string(idx, 1, json.dumps(rb))
        ws_meta.hide()

        # Write _Legend sheet.
        legend_rows = [
            (u"Cell Style", u"Meaning"),
            (u"White (Unlocked)", u"Editable Instance parameter. Modify freely and re-import."),
            (u"Light Blue (Unlocked)", u"Editable Type parameter [T]. Changes affect ALL instances of that type."),
            (u"Grey (Locked)", u"Non-modifiable (Read-Only, Computed, Linked Model, Group Member, Pinned, or Owned by another user). Do not modify."),
            (u"Hidden _Meta Sheet", u"Stores original exported values for 3-way conflict detection. Do not delete."),
        ]
        for r_i, (k, desc) in enumerate(legend_rows):
            ws_legend.write_string(r_i, 0, k, fmt_header if r_i == 0 else fmt_editable)
            ws_legend.write_string(r_i, 1, desc, fmt_header if r_i == 0 else fmt_editable)
        ws_legend.set_column(0, 0, 26)
        ws_legend.set_column(1, 1, 90)
    finally:
        wb.close()

    return {
        "path": filepath,
        "format": "xlsx",
        "rows": len(records),
        "columns": len(export_cols),
        "locked_cells": locked_cells,
        "editable_cells": editable_cells,
    }


def export_schedule_csv(filepath, records, col_keys):
    """Fallback CSV exporter if xlsxwriter is unavailable."""
    out_path = filepath
    if out_path.lower().endswith(".xlsx"):
        out_path = out_path[:-5] + ".csv"
    export_cols = []
    for required in (u"UniqueId", u"Element Id"):
        if required not in col_keys:
            export_cols.append(required)
    for ck in col_keys:
        if ck not in export_cols:
            export_cols.append(ck)
    headers = [state.column_display_label(ck) for ck in export_cols]
    if str is bytes:
        # IronPython 2.7 / Python 2: csv.writer emits bytes and requires a binary file.
        with open(out_path, "wb") as fp:
            fp.write(b"\xef\xbb\xbf")
            writer = csv.writer(fp)
            writer.writerow([state.to_text(h).encode("utf-8") for h in headers])
            for rec in records:
                row = [state.to_text(state.record_value(rec, ck)).encode("utf-8") for ck in export_cols]
                writer.writerow(row)
    else:
        with io.open(out_path, "w", encoding="utf-8-sig", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow(headers)
            for rec in records:
                row = [state.to_text(state.record_value(rec, ck)) for ck in export_cols]
                writer.writerow(row)
    return {
        "path": out_path,
        "format": "csv",
        "rows": len(records),
        "columns": len(export_cols),
        "locked_cells": 0,
        "editable_cells": len(records) * len(export_cols),
    }


def _strip_ns(tag):
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _read_shared_strings(zf):
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    strings = []
    for si in root:
        if _strip_ns(si.tag) != "si":
            continue
        parts = []
        for elem in si.iter():
            if _strip_ns(elem.tag) == "t" and elem.text is not None:
                parts.append(state.to_text(elem.text))
        strings.append(u"".join(parts))
    return strings


def _workbook_sheets(zf):
    """Return ordered list of (sheet_name, zip_member_path)."""
    wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
    rel_map = {}
    if "xl/_rels/workbook.xml.rels" in zf.namelist():
        rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rels_root:
            if _strip_ns(rel.tag) == "Relationship":
                rid = rel.attrib.get("Id")
                target = rel.attrib.get("Target", "")
                if target.startswith("/"):
                    target = target.lstrip("/")
                elif not target.startswith("xl/"):
                    target = "xl/" + target
                rel_map[rid] = target

    sheets = []
    for elem in wb_root.iter():
        if _strip_ns(elem.tag) == "sheet":
            name = state.to_text(elem.attrib.get("name", u""))
            rid = None
            for k, v in elem.attrib.items():
                if k.endswith("id"):
                    rid = v
                    break
            target = rel_map.get(rid)
            if target:
                sheets.append((name, target))
    return sheets


def _read_sheet_rows(zf, member_path, shared_strings):
    """Parse an OpenXML worksheet into a list of row lists (strings)."""
    root = ET.fromstring(zf.read(member_path))
    rows = []
    for row_el in root.iter():
        if _strip_ns(row_el.tag) != "row":
            continue
        cells = {}
        max_col = -1
        for c_el in row_el:
            if _strip_ns(c_el.tag) != "c":
                continue
            ref = c_el.attrib.get("r", "")
            m = _CELL_REF_RE.match(ref)
            col_idx = _col_letters_to_index(m.group(1)) if m else (max_col + 1)
            max_col = max(max_col, col_idx)
            ctype = c_el.attrib.get("t", "")
            val = u""
            if ctype == "s":
                v_el = None
                for ch in c_el:
                    if _strip_ns(ch.tag) == "v":
                        v_el = ch
                        break
                if v_el is not None and v_el.text is not None:
                    s_idx = int(v_el.text)
                    val = shared_strings[s_idx] if 0 <= s_idx < len(shared_strings) else u""
            elif ctype == "inlineStr":
                parts = [state.to_text(t.text) for t in c_el.iter()
                         if _strip_ns(t.tag) == "t" and t.text is not None]
                val = u"".join(parts)
            elif ctype == "b":
                for ch in c_el:
                    if _strip_ns(ch.tag) == "v":
                        val = u"Yes" if ch.text == "1" else u"No"
            else:
                for ch in c_el:
                    if _strip_ns(ch.tag) == "v" and ch.text is not None:
                        val = state.to_text(ch.text)
                        break
            cells[col_idx] = val
        if max_col >= 0:
            rows.append([cells.get(i, u"") for i in range(max_col + 1)])
    return rows


def read_schedule_xlsx(filepath):
    """Read an exported .xlsx file (Schedule + optional hidden _Meta sheet).

    Returns:
      {
        "headers": [...],
        "rows": [[...], ...],
        "meta_header": dict or None,
        "baselines": {unique_id_or_eid: {"values": {col_key: orig_val}, ...}},
      }
    """
    with zipfile.ZipFile(filepath, "r") as zf:
        shared = _read_shared_strings(zf)
        sheets = _workbook_sheets(zf)
        sheet_map = dict((name, path) for name, path in sheets)

        sched_path = sheet_map.get(SCHEDULE_SHEET_NAME)
        if sched_path is None and sheets:
            sched_path = sheets[0][1]
        if sched_path is None:
            raise ValueError("Workbook contains no worksheets.")

        all_rows = _read_sheet_rows(zf, sched_path, shared)
        headers = all_rows[0] if all_rows else []
        data_rows = all_rows[1:] if len(all_rows) > 1 else []

        meta_header = None
        baselines = {}
        meta_path = sheet_map.get(META_SHEET_NAME)
        if meta_path and meta_path in zf.namelist():
            meta_rows = _read_sheet_rows(zf, meta_path, shared)
            for mrow in meta_rows:
                if len(mrow) < 2:
                    continue
                if mrow[0] == u"META_HEADER":
                    try:
                        meta_header = json.loads(mrow[1])
                    except Exception:
                        meta_header = None
                else:
                    try:
                        parsed = json.loads(mrow[1])
                        baselines[mrow[0]] = parsed
                    except Exception:
                        continue

    return {
        "headers": headers,
        "rows": data_rows,
        "meta_header": meta_header,
        "baselines": baselines,
    }
