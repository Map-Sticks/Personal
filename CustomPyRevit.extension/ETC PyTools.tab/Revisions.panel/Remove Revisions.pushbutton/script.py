# -*- coding: utf-8 -*-
import System
import json
import textwrap
from pyrevit import revit, DB, forms

doc = revit.doc

# ==========================================================
# 1. EXTENSIBLE STORAGE SETUP
# ==========================================================
SCHEMA_GUID = System.Guid("8f3e5b4a-1c9d-4e2f-b7a6-9c8d7e6f5a4b")
SETTINGS_GUID = System.Guid("D9F3C6A2-1B8E-4E7A-A3F5-8C9D2E4F1A0B")

def get_or_create_schema():
    schema = DB.ExtensibleStorage.Schema.Lookup(SCHEMA_GUID)
    if not schema:
        builder = DB.ExtensibleStorage.SchemaBuilder(SCHEMA_GUID)
        builder.SetSchemaName("CustomRevisionHistory")
        builder.SetReadAccessLevel(DB.ExtensibleStorage.AccessLevel.Public)
        builder.SetWriteAccessLevel(DB.ExtensibleStorage.AccessLevel.Public)
        builder.AddSimpleField("HistoryJSON", System.String)
        schema = builder.Finish()
    return schema

def get_sheet_history(sheet):
    schema = get_or_create_schema()
    entity = sheet.GetEntity(schema)
    if entity.IsValid():
        json_str = entity.Get[System.String]("HistoryJSON")
        if json_str:
            try: return json.loads(json_str)
            except: pass
    return []

def save_sheet_history(sheet, history_list):
    schema = get_or_create_schema()
    entity = DB.ExtensibleStorage.Entity(schema)
    entity.Set[System.String]("HistoryJSON", json.dumps(history_list))
    sheet.SetEntity(entity)

def get_project_settings():
    schema = DB.ExtensibleStorage.Schema.Lookup(SETTINGS_GUID)
    if not schema: return {"max_rows": 0, "direction": "Top-Down"}
    entity = doc.ProjectInformation.GetEntity(schema)
    if entity.IsValid():
        json_str = entity.Get[System.String]("SettingsJSON")
        if json_str:
            try: return json.loads(json_str)
            except: pass
    return {"max_rows": 0, "direction": "Top-Down"}

def get_tb_param(element, prefix, index):
    if not element: return None
    return element.LookupParameter("{}_{:02d}".format(prefix, index)) or \
           element.LookupParameter("{}_{}".format(prefix, index))

def update_titleblock_parameters(sheet, full_history_list, settings):
    """ Builds the visual stack, enforces clean cutoffs, and dynamically merges table rows with conditional read checks """
    current_param = sheet.LookupParameter("Current_Custom_Revision")
    if current_param:
        latest = full_history_list[-1].get("FullRevisionNumber", "") if full_history_list else ""
        # Performance check for current rev parameter
        if (current_param.AsString() or "") != latest:
            current_param.Set(latest)

    # 1. Count physical rows available on the titleblock
    physical_rows = 0
    while get_tb_param(sheet, "Hist_Rev", physical_rows + 1):
        physical_rows += 1
    if physical_rows == 0: return

    # Grab the actual physical Title Block instance on this sheet
    titleblocks = DB.FilteredElementCollector(doc, sheet.Id).OfCategory(DB.BuiltInCategory.OST_TitleBlocks).ToElements()
    tb_instance = titleblocks[0] if titleblocks else None

    # 2. Determine the actual limit
    user_max = settings.get("max_rows", 0)
    actual_limit = min(physical_rows, user_max) if user_max > 0 else physical_rows

    MAX_CHARS_PER_LINE = 27 
    valid_stack = []
    
    # 3. Build the stack Newest -> Oldest, enforcing Clean Cutoff
    for rev in full_history_list[::-1]:
        desc = rev.get("Description", "")
        rev_chunks = []
        if desc and len(desc) > MAX_CHARS_PER_LINE:
            chunks = textwrap.wrap(desc, MAX_CHARS_PER_LINE)
            rev_chunks.append({"FullRevisionNumber": rev.get("FullRevisionNumber", ""), "Description": chunks[0], "IssuedBy": rev.get("IssuedBy", ""), "Date": rev.get("Date", "")})
            for chunk in chunks[1:]:
                rev_chunks.append({"FullRevisionNumber": "", "Description": chunk, "IssuedBy": "", "Date": ""})
        else:
            rev_chunks = [rev]

        if len(valid_stack) + len(rev_chunks) > actual_limit:
            break
            
        valid_stack.extend(rev_chunks)

    # 4. Orient the stack based on Titleblock layout
    direction = settings.get("direction", "Top-Down")
    if direction == "Top-Down":
        display_list = valid_stack
    else:
        display_list = valid_stack[::-1]

    # ==========================================================
    # [ THE DYNAMIC TABLE ROW ENGINE ]
    # ==========================================================
    line_states = [1] * (physical_rows + 1)
    
    if display_list:
        if direction == "Top-Down":
            for i in range(1, len(display_list)):
                next_row_data = display_list[i] 
                if next_row_data.get("FullRevisionNumber") == "":
                    line_states[i] = 0
        else:
            for i in range(1, len(display_list)):
                current_row_data = display_list[i - 1] 
                if current_row_data.get("FullRevisionNumber") == "":
                    if i + 1 <= physical_rows:
                        line_states[i + 1] = 0
    # ==========================================================

    # 5. Inject text and line states ONLY if a change is physically required
    for i in range(1, physical_rows + 1):
        p_rev  = get_tb_param(sheet, "Hist_Rev", i)
        p_desc = get_tb_param(sheet, "Hist_Desc", i)
        p_by   = get_tb_param(sheet, "Hist_By", i)
        p_date = get_tb_param(sheet, "Hist_Date", i)
        p_line = get_tb_param(tb_instance, "Hist_Line", i)

        if i <= len(display_list):
            rev_data = display_list[i - 1]
            t_rev  = rev_data.get("FullRevisionNumber", "")
            t_desc = rev_data.get("Description", "")
            t_by   = rev_data.get("IssuedBy", "")
            t_date = rev_data.get("Date", "")
            
            # [ PERFORMANCE FIX ]: Compare strings before calling .Set()
            if p_rev  and (p_rev.AsString() or "") != t_rev:   p_rev.Set(t_rev)
            if p_desc and (p_desc.AsString() or "") != t_desc: p_desc.Set(t_desc)
            if p_by   and (p_by.AsString() or "") != t_by:     p_by.Set(t_by)
            if p_date and (p_date.AsString() or "") != t_date: p_date.Set(t_date)
        else:
            # Clean empty slots only if they aren't already empty
            if p_rev  and (p_rev.AsString() or "") != "":  p_rev.Set("")
            if p_desc and (p_desc.AsString() or "") != "": p_desc.Set("")
            if p_by   and (p_by.AsString() or "") != "":  p_by.Set("")
            if p_date and (p_date.AsString() or "") != "": p_date.Set("")
            
        # [ PERFORMANCE FIX ]: Compare checkbox integer states before calling .Set()
        if p_line and p_line.AsInteger() != line_states[i]: 
            p_line.Set(line_states[i])

def surgical_revision_removal():
    settings = get_project_settings()
    if not settings.get("direction"):
        direction = forms.CommandSwitchWindow.show(["Top-Down", "Bottom-Up"], message="Select your Titleblock Arrangement:")
        if not direction: return
        settings["direction"] = direction

    selected_sheets = forms.select_sheets(title="1. Select Sheets to clean")
    if not selected_sheets: return
        
    history_guids = set()
    for sheet in selected_sheets:
        for h in get_sheet_history(sheet): history_guids.add(h.get("NativeRevisionGuid"))
            
    if not history_guids:
        forms.alert("No custom revisions found on the selected sheets.", title="Nothing to remove")
        return
        
    revisions_to_show = [rev for rev in DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Revisions).ToElements() if rev.UniqueId in history_guids]
    
    selected_to_remove = forms.SelectFromList.show(
        revisions_to_show, name_attr='Name', title="2. Select Revisions to Remove", button_name="Remove Selected", multiselect=True
    )
    
    if not selected_to_remove: return
        
    guids_to_remove = [rev.UniqueId for rev in selected_to_remove]
    ids_to_remove_ints = [rev.Id.IntegerValue for rev in selected_to_remove]
    
    if not forms.alert("You are about to remove {} revision(s) from {} sheet(s).\n\nProceed?".format(len(selected_to_remove), len(selected_sheets)), yes=True, no=True, title="Confirm Removal"):
        return
    
    with DB.Transaction(doc, "Surgical Revision Removal") as tx:
        tx.Start()
        # Note: If direction was null, we don't save the setting here to avoid creating the schema backwards, 
        # but the main tool will handle generating it. 
        
        for sheet in selected_sheets:
            new_ids = System.Collections.Generic.List[DB.ElementId]([eid for eid in sheet.GetAdditionalRevisionIds() if eid.IntegerValue not in ids_to_remove_ints])
            sheet.SetAdditionalRevisionIds(new_ids)
            
            history = get_sheet_history(sheet)
            if history:
                new_history = [h for h in history if h.get("NativeRevisionGuid") not in guids_to_remove]
                if not new_history:
                    schema = get_or_create_schema()
                    entity = sheet.GetEntity(schema)
                    if entity.IsValid(): sheet.DeleteEntity(schema)
                else:
                    save_sheet_history(sheet, new_history)
                    
                update_titleblock_parameters(sheet, new_history, settings)
                
        tx.Commit()
        
    forms.alert("Successfully removed revisions and updated title blocks on {} sheets.".format(len(selected_sheets)), title="Success")

surgical_revision_removal()