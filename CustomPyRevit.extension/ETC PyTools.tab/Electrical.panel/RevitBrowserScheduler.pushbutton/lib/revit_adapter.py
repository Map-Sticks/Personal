# -*- coding: utf-8 -*-
"""Thin Revit API adapter for Universal Revit Browser & Scheduler.

Must only be called from Revit's valid API context (command entry or ExternalEvent.Execute).
Compatible with IronPython 2.7 and Revit 2023-2025.
"""

from System import Int32, Int64
from System.Collections.Generic import List

from pyrevit import DB, HOST_APP, revit

import browser_state as state

USE_64_BIT_IDS = int(HOST_APP.version) >= 2024


def as_eid(value):
    return DB.ElementId(Int64(value) if USE_64_BIT_IDS else Int32(value))


def safe_name(element):
    """IronPython-safe element/type name reader (avoids ElementType.Name getter bug)."""
    if element is None:
        return u""
    try:
        name = revit.query.get_name(element)
        if name:
            return state.to_text(name)
    except Exception:
        pass
    try:
        p = element.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
        if p is not None and p.HasValue:
            return state.to_text(p.AsString())
    except Exception:
        pass
    try:
        return state.to_text(element.Name)
    except Exception:
        return u""


def element_name_by_id(doc, element_id):
    if element_id is None or state.element_id_value(element_id) < 0:
        return u""
    try:
        return safe_name(doc.GetElement(element_id))
    except Exception:
        return u""


def read_doc_metadata(doc):
    """Return lightweight metadata (levels, worksets, categories, spaces/rooms, doc guid)."""
    levels = []
    for lv in DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements():
        name = safe_name(lv)
        if name:
            levels.append(name)
    levels = sorted(set(levels), key=lambda x: x.lower())

    worksets = []
    if doc.IsWorkshared:
        try:
            for ws in DB.FilteredWorksetCollector(doc).OfKind(DB.WorksetKind.UserWorkset):
                worksets.append(state.to_text(ws.Name))
        except Exception:
            pass
    worksets = sorted(set(worksets), key=lambda x: x.lower())

    categories = []
    cat_id_by_name = {}
    for cat in doc.Settings.Categories:
        try:
            if cat is None or not cat.Name:
                continue
            cname = state.to_text(cat.Name)
            cid = state.element_id_value(cat.Id)
            if cname not in cat_id_by_name:
                cat_id_by_name[cname] = cid
                categories.append((cname, cid, state.to_text(cat.CategoryType)))
        except Exception:
            continue
    categories.sort(key=lambda item: item[0].lower())

    # Collect Space and Room choices for the "Selected Spaces / Rooms" scope picker.
    spatial_choices = []
    for bic, kind in ((DB.BuiltInCategory.OST_MEPSpaces, u"Space"),
                      (DB.BuiltInCategory.OST_Rooms, u"Room")):
        try:
            for el in (DB.FilteredElementCollector(doc).OfCategory(bic)
                       .WhereElementIsNotElementType().ToElements()):
                eid = state.element_id_value(el.Id)
                num = u""
                nm = u""
                try:
                    num = state.to_text(el.Number)
                except Exception:
                    pass
                try:
                    p = el.get_Parameter(DB.BuiltInParameter.ROOM_NAME)
                    if p is not None:
                        nm = state.to_text(p.AsString())
                except Exception:
                    pass
                lv_name = element_name_by_id(doc, getattr(el, "LevelId", None))
                label = u"[{}] {} {} ({})".format(kind, num, nm, lv_name or u"-").strip()
                spatial_choices.append({"id": eid, "kind": kind, "label": label, "level": lv_name})
        except Exception:
            continue
    spatial_choices.sort(key=lambda x: x["label"].lower())

    doc_guid = u""
    try:
        if doc.ProjectInformation is not None:
            doc_guid = state.to_text(doc.ProjectInformation.UniqueId)
    except Exception:
        pass

    return {
        "doc_title": state.to_text(doc.Title),
        "doc_guid": doc_guid,
        "levels": levels,
        "worksets": worksets,
        "categories": categories,
        "cat_id_by_name": cat_id_by_name,
        "spatial_choices": spatial_choices,
    }


def format_parameter_value(doc, param):
    if param is None:
        return u""
    try:
        vs = param.AsValueString()
        if vs is not None:
            return state.to_text(vs)
    except Exception:
        pass
    try:
        storage = state.to_text(param.StorageType)
        if storage.endswith("String"):
            return state.to_text(param.AsString())
        if not param.HasValue:
            return u""
        if storage.endswith("Integer"):
            return state.to_text(param.AsInteger())
        if storage.endswith("Double"):
            return state.to_text(round(param.AsDouble(), 6))
        if storage.endswith("ElementId"):
            eid = param.AsElementId()
            val = state.element_id_value(eid)
            if val < 0:
                return u""
            return element_name_by_id(doc, eid) or state.to_text(val)
    except Exception:
        pass
    return u""


def _all_phases_latest_first(doc):
    try:
        phases = list(doc.Phases)
        phases.reverse()
        return phases
    except Exception:
        return []


def _lookup_spatial(element, method_name, phases):
    if not isinstance(element, DB.FamilyInstance):
        return (None, u"")
    prop_name = method_name[len("get_"):]
    for phase in phases:
        target = None
        try:
            target = getattr(element, method_name)(phase)
        except Exception:
            try:
                target = getattr(element, prop_name)[phase]
            except Exception:
                continue
        if target is not None:
            tid = state.element_id_value(target.Id)
            num = u""
            nm = u""
            try:
                num = state.to_text(target.Number)
            except Exception:
                pass
            try:
                p = target.get_Parameter(DB.BuiltInParameter.ROOM_NAME)
                if p is not None:
                    nm = state.to_text(p.AsString())
            except Exception:
                pass
            label = u" ".join([x for x in (num, nm) if x]) or u"Id {}".format(tid)
            return (tid, label)
    return (None, u"")


def _checkout_info(doc, eid):
    if not doc.IsWorkshared:
        return ("NotOwned", u"")
    try:
        status = DB.WorksharingUtils.GetCheckoutStatus(doc, eid)
        status_txt = state.to_text(status)
        if "Other" in status_txt:
            owner = u""
            try:
                info = DB.WorksharingUtils.GetWorksharingTooltipInfo(doc, eid)
                owner = state.to_text(info.Owner)
            except Exception:
                pass
            return ("OwnedByOtherUser", owner)
        if "Current" in status_txt:
            return ("OwnedByCurrentUser", u"")
    except Exception:
        pass
    return ("NotOwned", u"")


def _extract_param_meta(param, is_type):
    try:
        name = state.to_text(param.Definition.Name)
    except Exception:
        name = u""
    return {
        "name": name,
        "is_type": bool(is_type),
        "is_read_only": bool(getattr(param, "IsReadOnly", True)),
        "user_modifiable": bool(getattr(param, "UserModifiable", False)),
        "storage_type": state.to_text(getattr(param, "StorageType", u"String")),
    }


def _read_element_params(doc, element, el_type, wanted_col_keys):
    """Read requested [I] and [T] parameter values and metadata for one element."""
    values = {}
    pmeta = {}
    for ck in wanted_col_keys:
        kind, pname = state.parse_param_key(ck)
        if kind in ("P", "C"):
            continue
        target = el_type if kind == "T" else element
        if target is None:
            continue
        try:
            param = target.LookupParameter(pname)
        except Exception:
            param = None
        if param is not None:
            values[ck] = format_parameter_value(doc, param)
            pmeta[ck] = _extract_param_meta(param, is_type=(kind == "T"))
    return values, pmeta


def build_element_record(doc, element, wanted_col_keys, visible_ids=None,
                         phases=None, is_linked=False, link_name=u"Host",
                         link_instance_id=None, workset_table=None):
    """Convert one Revit Element into a plain-Python record dict."""
    eid = state.element_id_value(element.Id)
    is_type_el = isinstance(element, DB.ElementType)
    type_id_obj = None
    el_type = None
    if not is_type_el:
        try:
            type_id_obj = element.GetTypeId()
            if type_id_obj is not None and state.element_id_value(type_id_obj) >= 0:
                el_type = doc.GetElement(type_id_obj)
        except Exception:
            el_type = None
    else:
        el_type = element

    type_id_val = state.element_id_value(el_type.Id) if el_type is not None else -1

    cat_name = u""
    cat_id = -1
    try:
        if element.Category is not None:
            cat_name = state.to_text(element.Category.Name)
            cat_id = state.element_id_value(element.Category.Id)
    except Exception:
        pass
    if not cat_name:
        cat_name = state.to_text(element.GetType().Name)

    family_name = u""
    type_name = u""
    if isinstance(element, DB.FamilyInstance):
        try:
            family_name = state.to_text(element.Symbol.FamilyName)
        except Exception:
            pass
        type_name = safe_name(element.Symbol) if getattr(element, "Symbol", None) is not None else safe_name(element)
    elif el_type is not None:
        try:
            family_name = state.to_text(el_type.FamilyName)
        except Exception:
            pass
        type_name = safe_name(el_type)
    else:
        type_name = safe_name(element)

    level_name = u""
    try:
        lv_id = getattr(element, "LevelId", None)
        if lv_id is not None and state.element_id_value(lv_id) >= 0:
            level_name = element_name_by_id(doc, lv_id)
    except Exception:
        pass

    phase_created = u""
    phase_demolished = u""
    try:
        pc = getattr(element, "CreatedPhaseId", None)
        if pc is not None and state.element_id_value(pc) >= 0:
            phase_created = element_name_by_id(doc, pc)
        pd = getattr(element, "DemolishedPhaseId", None)
        if pd is not None and state.element_id_value(pd) >= 0:
            phase_demolished = element_name_by_id(doc, pd)
    except Exception:
        pass

    ws_name = u""
    if workset_table is not None:
        try:
            ws_id = element.WorksetId
            if ws_id is not None:
                ws = workset_table.GetWorkset(ws_id)
                if ws is not None:
                    ws_name = state.to_text(ws.Name)
        except Exception:
            pass

    design_option = u"Main Model"
    try:
        opt = element.DesignOption
        if opt is not None:
            design_option = safe_name(opt)
    except Exception:
        pass

    group_id = -1
    group_name = u""
    try:
        gid = element.GroupId
        group_id = state.element_id_value(gid)
        if group_id >= 0:
            group_name = element_name_by_id(doc, gid)
    except Exception:
        pass

    pinned = False
    try:
        pinned = bool(element.Pinned)
    except Exception:
        pass

    host_name = u""
    try:
        host = getattr(element, "Host", None)
        if host is not None:
            host_name = safe_name(host)
    except Exception:
        pass

    phases = phases if phases is not None else []
    space_id, space_label = _lookup_spatial(element, "get_Space", phases)
    room_id, room_label = _lookup_spatial(element, "get_Room", phases)

    if is_linked:
        checkout_status, checkout_owner = ("NotOwned", u"")
    else:
        checkout_status, checkout_owner = _checkout_info(doc, element.Id)

    values, pmeta = _read_element_params(doc, element, el_type, wanted_col_keys)

    return {
        "row_key": state.make_row_key(eid, is_linked=is_linked, link_instance_id=link_instance_id),
        "id": eid,
        "unique_id": state.to_text(getattr(element, "UniqueId", u"")),
        "is_linked": bool(is_linked),
        "link_name": state.to_text(link_name),
        "link_instance_id": link_instance_id,
        "is_type_element": is_type_el,
        "type_id": type_id_val,
        "category": cat_name,
        "category_id": cat_id,
        "family": family_name,
        "type": type_name,
        "level": level_name,
        "phase_created": phase_created,
        "phase_demolished": phase_demolished,
        "workset": ws_name,
        "design_option": design_option,
        "group_id": group_id,
        "group_name": group_name,
        "pinned": pinned,
        "host": host_name,
        "room": room_label,
        "room_id": room_id,
        "space": space_label,
        "space_id": space_id,
        "visible": (eid in visible_ids) if (visible_ids is not None and not is_linked) else True,
        "checkout_status": checkout_status,
        "checkout_owner": checkout_owner,
        "values": values,
        "param_meta": pmeta,
        "counts": {},
    }


def _collect_special_source(doc, source_name, view_id=None):
    def _base():
        if view_id is not None:
            try:
                return DB.FilteredElementCollector(doc, view_id)
            except Exception:
                pass
        return DB.FilteredElementCollector(doc)

    if source_name == u"Views":
        return [v for v in DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements()
                if not isinstance(v, (DB.ViewSheet, DB.ViewSchedule)) and not v.IsTemplate]
    if source_name == u"Sheets":
        return list(DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet).ToElements())
    if source_name == u"Schedules":
        return [v for v in DB.FilteredElementCollector(doc).OfClass(DB.ViewSchedule).ToElements()
                if not v.IsTemplate]
    if source_name == u"Levels":
        return list(DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements())
    if source_name == u"Grids":
        return list(_base().OfClass(DB.Grid).ToElements())
    if source_name == u"Rooms":
        return list(_base().OfCategory(DB.BuiltInCategory.OST_Rooms).WhereElementIsNotElementType().ToElements())
    if source_name == u"Spaces":
        return list(_base().OfCategory(DB.BuiltInCategory.OST_MEPSpaces).WhereElementIsNotElementType().ToElements())
    if source_name == u"Areas":
        return list(_base().OfCategory(DB.BuiltInCategory.OST_Areas).WhereElementIsNotElementType().ToElements())
    if source_name == u"Model Groups":
        return list(_base().OfClass(DB.Group).ToElements())
    if source_name == u"MEP Systems":
        return list(DB.FilteredElementCollector(doc).OfClass(DB.MEPSystem).ToElements())
    if source_name == u"Materials":
        return list(DB.FilteredElementCollector(doc).OfClass(DB.Material).ToElements())
    if source_name == u"Element Types":
        return list(DB.FilteredElementCollector(doc).WhereElementIsElementType().ToElements())
    return []


def _collect_doc_elements(doc, category_ids, special_sources, include_instances=True,
                          include_types=False, view_id=None):
    """Collect deduplicated elements for any combination of categories & special sources."""
    seen = {}
    if category_ids:
        id_list = List[DB.ElementId]()
        for cid in category_ids:
            id_list.Add(as_eid(cid))
        cat_filter = DB.ElementMulticategoryFilter(id_list)
        if include_instances:
            col = DB.FilteredElementCollector(doc, view_id) if view_id is not None else DB.FilteredElementCollector(doc)
            try:
                for el in col.WherePasses(cat_filter).WhereElementIsNotElementType().ToElements():
                    seen[state.element_id_value(el.Id)] = el
            except Exception:
                pass
        if include_types:
            try:
                for el in (DB.FilteredElementCollector(doc).WherePasses(cat_filter)
                           .WhereElementIsElementType().ToElements()):
                    seen[state.element_id_value(el.Id)] = el
            except Exception:
                pass

    for src in (special_sources or []):
        for el in _collect_special_source(doc, src, view_id=view_id):
            seen[state.element_id_value(el.Id)] = el
    return list(seen.values())


def discover_parameters(doc, elements, sample_per_category=10):
    """Stratified parameter discovery across all selected categories & types.

    Returns sorted list of column keys (pseudo + [I] instance + [T] type + count fields).
    """
    keys = set(state.PSEUDO_FIELDS)
    by_cat = {}
    for el in elements:
        cid = -1
        try:
            if el.Category is not None:
                cid = state.element_id_value(el.Category.Id)
        except Exception:
            pass
        bucket = by_cat.setdefault(cid, [])
        if len(bucket) < sample_per_category:
            bucket.append(el)

    seen_types = set()
    for bucket in by_cat.values():
        for el in bucket:
            try:
                for p in el.Parameters:
                    if p.Definition is not None and p.Definition.Name:
                        keys.add(state.make_param_key(p.Definition.Name, is_type=False))
            except Exception:
                pass
            if not isinstance(el, DB.ElementType):
                try:
                    tid = el.GetTypeId()
                    tid_val = state.element_id_value(tid)
                    if tid_val >= 0 and tid_val not in seen_types:
                        seen_types.add(tid_val)
                        el_type = doc.GetElement(tid)
                        if el_type is not None:
                            for p in el_type.Parameters:
                                if p.Definition is not None and p.Definition.Name:
                                    keys.add(state.make_param_key(p.Definition.Name, is_type=True))
                except Exception:
                    pass

    for cf in state.count_fields():
        keys.add(cf)
    return sorted(keys, key=lambda k: state.column_display_label(k).lower())


def collect_schedule_records(doc, uidoc, category_ids, special_sources, wanted_col_keys,
                             scope=u"Whole Project", include_instances=True,
                             include_types=False, include_links=False,
                             compute_spatial_counts=False):
    """Main read entrypoint: collects elements, discovers params, builds records & spatial counts."""
    view = uidoc.ActiveView if uidoc is not None else None
    view_id = view.Id if (view is not None and scope == u"Active View") else None

    visible_ids = None
    if view is not None and scope != u"Active View":
        try:
            visible_ids = set(
                state.element_id_value(eid)
                for eid in DB.FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType().ToElementIds()
            )
        except Exception:
            visible_ids = None

    elements = _collect_doc_elements(
        doc, category_ids, special_sources,
        include_instances=include_instances,
        include_types=include_types,
        view_id=view_id,
    )
    available_keys = discover_parameters(doc, elements)
    phases = _all_phases_latest_first(doc)
    ws_table = doc.GetWorksetTable() if doc.IsWorkshared else None

    records = []
    for el in elements:
        records.append(build_element_record(
            doc, el, wanted_col_keys, visible_ids=visible_ids,
            phases=phases, is_linked=False, link_name=u"Host",
            workset_table=ws_table,
        ))

    # Optional Read-Only Linked Model Elements
    if include_links and category_ids:
        for link_inst in DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance).ToElements():
            link_doc = None
            try:
                link_doc = link_inst.GetLinkDocument()
            except Exception:
                continue
            if link_doc is None:
                continue
            lname = safe_name(link_inst) or state.to_text(link_doc.Title)
            lid = state.element_id_value(link_inst.Id)
            link_phases = _all_phases_latest_first(link_doc)
            link_elements = _collect_doc_elements(
                link_doc, category_ids, special_sources,
                include_instances=include_instances,
                include_types=include_types,
                view_id=None,
            )
            for lel in link_elements:
                records.append(build_element_record(
                    link_doc, lel, wanted_col_keys, visible_ids=None,
                    phases=link_phases, is_linked=True, link_name=lname,
                    link_instance_id=lid, workset_table=None,
                ))

    # Optional spatial contents index when Rooms/Spaces are present or Count: columns requested.
    contents_by_spatial = {}
    need_counts = compute_spatial_counts or any(state.is_count_key(k) for k in wanted_col_keys)
    if need_counts:
        spatial_ids = set(
            r["id"] for r in records
            if not r.get("is_linked") and r.get("category") in (u"Spaces", u"Rooms")
        )
        if spatial_ids:
            contents_by_spatial = _collect_spatial_contents(doc, phases)
            for rec in records:
                if rec["id"] in spatial_ids:
                    items = contents_by_spatial.get(rec["id"], [])
                    counts = {}
                    for it in items:
                        counts[it["category"]] = counts.get(it["category"], 0) + 1
                    rec["counts"] = counts

    return records, available_keys, contents_by_spatial


def _collect_spatial_contents(doc, phases):
    """Map Space/Room id -> list of contained FamilyInstance summary dicts."""
    bic_list = List[DB.BuiltInCategory]()
    bic_labels = {}
    for label, bic_name in state.CONTENT_CATEGORIES:
        bic = getattr(DB.BuiltInCategory, bic_name, None)
        if bic is not None:
            bic_list.Add(bic)
            bic_labels[bic] = label
    if not bic_list.Count:
        return {}
    by_spatial = {}
    elements = (DB.FilteredElementCollector(doc)
                .WherePasses(DB.ElementMulticategoryFilter(bic_list))
                .WhereElementIsNotElementType().ToElements())
    for el in elements:
        if not isinstance(el, DB.FamilyInstance):
            continue
        try:
            label = bic_labels[el.Category.BuiltInCategory]
        except Exception:
            continue
        sp_id, _ = _lookup_spatial(el, "get_Space", phases)
        rm_id, _ = _lookup_spatial(el, "get_Room", phases)
        if sp_id is None and rm_id is None:
            continue
        item = {
            "id": state.element_id_value(el.Id),
            "category": label,
            "family": state.to_text(el.Symbol.FamilyName) if getattr(el, "Symbol", None) else u"",
            "type": safe_name(el.Symbol) if getattr(el, "Symbol", None) else safe_name(el),
            "mark": format_parameter_value(doc, el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK)),
        }
        if sp_id is not None:
            by_spatial.setdefault(sp_id, []).append(item)
        if rm_id is not None and rm_id != sp_id:
            by_spatial.setdefault(rm_id, []).append(item)
    return by_spatial


def set_parameter_from_string(doc, param, new_text):
    """Write a display string to a Revit Parameter respecting StorageType and project units.

    Returns (ok: bool, error_message: unicode).
    """
    if param is None:
        return (False, u"Parameter not found.")
    if param.IsReadOnly or not param.UserModifiable:
        return (False, u"Parameter is read-only.")

    txt = state.to_text(new_text)
    storage = state.to_text(param.StorageType)

    if storage.endswith("String"):
        param.Set(txt)
        return (True, u"")

    if storage.endswith("Integer"):
        low = txt.strip().lower()
        if low in (u"yes", u"true", u"1"):
            param.Set(1)
            return (True, u"")
        if low in (u"no", u"false", u"0", u""):
            param.Set(0)
            return (True, u"")
        try:
            param.Set(int(round(float(low))))
            return (True, u"")
        except Exception:
            try:
                if param.SetValueString(txt):
                    return (True, u"")
            except Exception:
                pass
            return (False, u"Cannot parse '{}' as integer/Yes-No.".format(txt))

    if storage.endswith("Double"):
        try:
            if param.SetValueString(txt):
                return (True, u"")
        except Exception:
            pass
        try:
            param.Set(float(txt.replace(u",", u"").strip()))
            return (True, u"")
        except Exception:
            return (False, u"Cannot parse '{}' in project units.".format(txt))

    if storage.endswith("ElementId"):
        if not txt.strip() or txt.strip().lower() in (u"none", u"<none>"):
            param.Set(DB.ElementId.InvalidElementId)
            return (True, u"")
        try:
            if param.SetValueString(txt):
                return (True, u"")
        except Exception:
            pass
        # Fallback: numeric ElementId string.
        try:
            target_id = as_eid(int(txt.strip()))
            if doc.GetElement(target_id) is not None:
                param.Set(target_id)
                return (True, u"")
        except Exception:
            pass
        return (False, u"Cannot resolve '{}' to an ElementId.".format(txt))

    return (False, u"Unsupported parameter StorageType: {}.".format(storage))


def apply_parameter_changes(doc, changes, tx_name=None, allow_pinned=False):
    """Apply a list of {element_id, type_id, col_key, is_type, new_value} in one undoable transaction.

    Deduplicates Type parameter writes by (type_id, col_key).
    Returns {applied: int, failed: [(element_id, col_key, error)], affected_ids: set}.
    """
    applied = 0
    failed = []
    affected_ids = set()
    seen_type_writes = set()

    tx_title = tx_name or (state.TX_PREFIX + u" Apply parameter edits")
    with revit.Transaction(tx_title, doc=doc):
        for ch in changes:
            eid_val = ch["element_id"]
            col_key = ch["col_key"]
            kind, pname = state.parse_param_key(col_key)
            if kind in ("P", "C"):
                failed.append((eid_val, col_key, u"Pseudo/computed column cannot be edited."))
                continue
            if kind == "T":
                tid_val = ch.get("type_id", -1)
                if tid_val in (None, -1):
                    failed.append((eid_val, col_key, u"Element has no TypeId."))
                    continue
                if (tid_val, col_key) in seen_type_writes:
                    continue
                seen_type_writes.add((tid_val, col_key))
                target = doc.GetElement(as_eid(tid_val))
            else:
                target = doc.GetElement(as_eid(eid_val))

            if target is None:
                failed.append((eid_val, col_key, u"Element no longer exists."))
                continue
            if not allow_pinned and getattr(target, "Pinned", False) and kind != "T":
                failed.append((eid_val, col_key, u"Element is pinned."))
                continue
            try:
                param = target.LookupParameter(pname)
                ok, err = set_parameter_from_string(doc, param, ch["new_value"])
                if ok:
                    applied += 1
                    affected_ids.add(eid_val)
                    if kind == "T":
                        affected_ids.add(ch.get("type_id"))
                else:
                    failed.append((eid_val, col_key, err))
            except Exception as ex:
                failed.append((eid_val, col_key, state.to_text(ex)))
    return {"applied": applied, "failed": failed, "affected_ids": affected_ids}


def preview_delete_cascade(doc, element_ids):
    """Run a rolled-back transaction calling doc.Delete to measure exact cascade impact.

    Returns {selected_count, total_deleted, dependent_count, dependent_categories: {cat: count}, error}.
    """
    id_list = List[DB.ElementId]()
    valid_selected = set()
    for val in element_ids:
        eid = as_eid(val)
        if doc.GetElement(eid) is not None:
            id_list.Add(eid)
            valid_selected.add(val)
    if id_list.Count == 0:
        return {"selected_count": 0, "total_deleted": 0, "dependent_count": 0,
                "dependent_categories": {}, "error": u"No valid elements to delete."}

    tx = DB.Transaction(doc, state.TX_PREFIX + u" Preview delete impact")
    dep_cats = {}
    total_deleted = 0
    error_msg = None
    try:
        tx.Start()
        deleted = doc.Delete(id_list)
        total_deleted = deleted.Count
        # Note: elements are deleted inside this tx, so count dependents directly.
        dep_count = max(0, total_deleted - len(valid_selected))
    except Exception as ex:
        dep_count = 0
        error_msg = state.to_text(ex)
    finally:
        try:
            if tx.HasStarted() and not tx.HasEnded():
                tx.RollBack()
        except Exception:
            pass
        try:
            tx.Dispose()
        except Exception:
            pass

    return {
        "selected_count": len(valid_selected),
        "total_deleted": total_deleted,
        "dependent_count": dep_count,
        "dependent_categories": dep_cats,
        "error": error_msg,
    }


def delete_elements(doc, element_ids, unpin_first=False):
    """Delete host elements in one undoable transaction. Returns (deleted_count, error)."""
    id_list = List[DB.ElementId]()
    for val in element_ids:
        eid = as_eid(val)
        el = doc.GetElement(eid)
        if el is not None:
            id_list.Add(eid)
    if id_list.Count == 0:
        return (0, u"No valid elements found to delete.")
    with revit.Transaction(state.TX_PREFIX + u" Delete elements", doc=doc):
        if unpin_first:
            for eid in id_list:
                el = doc.GetElement(eid)
                if el is not None and getattr(el, "Pinned", False):
                    el.Pinned = False
        deleted = doc.Delete(id_list)
        return (deleted.Count, None)
