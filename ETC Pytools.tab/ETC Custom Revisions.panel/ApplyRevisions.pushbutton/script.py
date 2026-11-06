# -*- coding: utf-8 -*-
import System
import re
import json
import textwrap
from pyrevit import revit, DB, forms, UI

doc = revit.doc

# ==========================================================
# 1. EXTENSIBLE STORAGE SETUP (SHEETS & PROJECT SETTINGS)
# ==========================================================
SCHEMA_GUID = System.Guid("8f3e5b4a-1c9d-4e2f-b7a6-9c8d7e6f5a4b") # Updated safe GUID
SETTINGS_GUID = System.Guid("D9F3C6A2-1B8E-4E7A-A3F5-8C9D2E4F1A0B") # Global Settings GUID

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
    if not schema:
        builder = DB.ExtensibleStorage.SchemaBuilder(SETTINGS_GUID)
        builder.SetSchemaName("CustomRevisionSettings")
        builder.SetReadAccessLevel(DB.ExtensibleStorage.AccessLevel.Public)
        builder.SetWriteAccessLevel(DB.ExtensibleStorage.AccessLevel.Public)
        builder.AddSimpleField("SettingsJSON", System.String)
        schema = builder.Finish()
        
    entity = doc.ProjectInformation.GetEntity(schema)
    if entity.IsValid():
        json_str = entity.Get[System.String]("SettingsJSON")
        if json_str:
            try: return json.loads(json_str)
            except: pass
    return {"max_rows": 0, "direction": "Top-Down"}

def save_project_settings(settings_dict):
    schema = DB.ExtensibleStorage.Schema.Lookup(SETTINGS_GUID)
    entity = DB.ExtensibleStorage.Entity(schema)
    entity.Set[System.String]("SettingsJSON", json.dumps(settings_dict))
    doc.ProjectInformation.SetEntity(entity)

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

# ==========================================================
# 2. LOGIC & PREDICTION ENGINE
# ==========================================================
def split_revision_string(full_rev_string):
    if not full_rev_string: return "", ""
    match = re.search(r"(\d+)$", full_rev_string)
    if match:
        num = match.group(1)
        return full_rev_string[:-len(num)], num
    return full_rev_string, ""

class SheetRowModel(object):
    def __init__(self, sheet, target_native_rev):
        self.sheet = sheet
        self.SheetNumber = sheet.SheetNumber
        self.SheetName = sheet.Name
        
        self.History = get_sheet_history(sheet)
        self.target_guid = target_native_rev.UniqueId
        self.IsSelected = False
        self.PredictedFullRevision = ""
        
        self.existing_entry_index = next((i for i, h in enumerate(self.History) if h.get("NativeRevisionGuid") == self.target_guid), -1)
        self.IsUpdate = self.existing_entry_index >= 0
        
        if self.IsUpdate:
            existing_full = self.History[self.existing_entry_index].get("FullRevisionNumber", "")
            pref, num = split_revision_string(existing_full)
            self.OriginalPrefix, self.OriginalNumber = pref, num
            self.CurrentPrefix, self.NextNumber = pref, num
            self.IsNumberEditable = True
            self.Status = "✏️ Editing Existing"
        else:
            # [ THE FIX ]: Split the logic so it only forces "A" on brand new sheets.
            if not self.History:
                self.OriginalPrefix = "A"
                self.OriginalNumber = "00"
                self.LastRevision = ""
            else:
                self.LastRevision = self.History[-1].get("FullRevisionNumber", "")
                pref, num = split_revision_string(self.LastRevision)
                self.OriginalPrefix = pref  # Just accept the prefix, even if it is "" (blank)
                self.OriginalNumber = num if num else "00"
                
            self.CurrentPrefix = self.OriginalPrefix
            self.NextNumber = self.calculate_next(self.CurrentPrefix)
            self.Status = "OK"

        self.IsValid = True
        self.evaluate_state()

    def calculate_next(self, current_ui_prefix):
        if self.IsUpdate: return self.NextNumber if current_ui_prefix == self.OriginalPrefix else ""
        if current_ui_prefix == self.OriginalPrefix:
            self.IsNumberEditable = False
            if self.OriginalNumber.isdigit():
                return "{:0{}d}".format(int(self.OriginalNumber) + 1, len(self.OriginalNumber))
            return "01"
        self.IsNumberEditable = True
        return ""

    def evaluate_state(self):
        self.NextNumber = self.calculate_next(self.CurrentPrefix)
        if self.IsUpdate: self.IsNumberEditable = True
        self.PredictedFullRevision = "{}{}".format(self.CurrentPrefix, self.NextNumber)

    def reset_to_zero(self):
        self.History = []
        self.IsUpdate = False
        self.existing_entry_index = -1
        self.OriginalPrefix, self.OriginalNumber, self.LastRevision = "", "", ""
        self.CurrentPrefix, self.NextNumber = "", ""
        self.IsNumberEditable = True
        self.Status = "⚠️ History Cleared (Enter new start)"
        self.IsValid = False
        self.PredictedFullRevision = ""

# ==========================================================
# 3. WPF USER INTERFACE
# ==========================================================
class CustomRevisionWindow(forms.WPFWindow):
    def __init__(self, xaml_file_name, target_sheets, target_native_rev):
        forms.WPFWindow.__init__(self, xaml_file_name)
        self.target_sheets = target_sheets
        self.target_native_rev = target_native_rev
        self.row_data = [SheetRowModel(s, target_native_rev) for s in target_sheets]
        self.data_grid.ItemsSource = self.row_data
        self.txt_selected_count.Text = str(len(target_sheets))
        
        # Load Global Settings into UI
        proj_settings = get_project_settings()
        self.combo_direction.Text = proj_settings.get("direction", "Top-Down")
        saved_max = proj_settings.get("max_rows", 0)
        self.box_max_rows.Text = str(saved_max) if saved_max > 0 else ""
        
        self.validate_grid()

    def get_targeted_rows(self):
        checked_rows = [r for r in self.row_data if r.IsSelected]
        return checked_rows if checked_rows else [r for r in self.data_grid.SelectedItems]

    def select_all_click(self, sender, args):
        for row in self.row_data: row.IsSelected = True
        self.data_grid.Items.Refresh()

    def select_none_click(self, sender, args):
        for row in self.row_data: row.IsSelected = False
        self.data_grid.Items.Refresh()

    def bulk_apply_prefix_click(self, sender, args):
        # [FIX]: Removed the 'if text exists' check. 
        # It will now happily accept an empty string and wipe the prefixes!
        new_prefix = self.box_bulk_prefix.Text
        for row in self.get_targeted_rows():
            row.CurrentPrefix = new_prefix
            row.evaluate_state()
        self.data_grid.Items.Refresh()
        self.validate_grid()

    def bulk_apply_number_click(self, sender, args):
        if self.box_bulk_number.Text:
            for row in self.get_targeted_rows():
                row.NextNumber = self.box_bulk_number.Text
                row.PredictedFullRevision = "{}{}".format(row.CurrentPrefix, row.NextNumber)
            self.data_grid.Items.Refresh()
            self.validate_grid()

    def grid_cell_edited(self, sender, args):
        args.Row.Item.evaluate_state()
        self.data_grid.Items.Refresh()
        self.validate_grid()

    def grid_selection_changed(self, sender, args): pass

    def alphanumeric_filter(self, sender, args):
        if not re.match(r"^[a-zA-Z0-9]+$", args.Text): args.Handled = True

    def number_input_keyup(self, sender, args): self.validate_grid()
    def number_input_lostfocus(self, sender, args): self.validate_grid()

    def validate_grid(self):
        duplicate_found = empty_found = False
        for row in self.row_data:
            row.PredictedFullRevision = "{}{}".format(row.CurrentPrefix, row.NextNumber)
            if not row.NextNumber:
                row.Status, row.IsValid, empty_found = "❌ Enter a number", False, True
                continue
            past_revisions = [r.get("FullRevisionNumber") for i, r in enumerate(row.History) if not (row.IsUpdate and i == row.existing_entry_index)]
            if row.PredictedFullRevision in past_revisions:
                row.Status, row.IsValid, duplicate_found = "⚠️ Duplicate Rev!", False, True
            else:
                if row.Status != "⚠️ History Cleared (Enter new start)":
                    row.Status = "OK" if not row.IsUpdate else "✏️ Editing Existing"
                row.IsValid = True
        self.data_grid.Items.Refresh()

        if duplicate_found: self.txt_error.Text, self.btn_apply.IsEnabled = "Cannot apply: Duplicate revisions detected.", False
        elif empty_found: self.txt_error.Text, self.btn_apply.IsEnabled = "Cannot apply: Missing revision numbers.", False
        else: self.txt_error.Text, self.btn_apply.IsEnabled = "", True

    def cancel_click(self, sender, args): self.Close()
    def apply_click(self, sender, args):
        try: user_max = int(self.box_max_rows.Text)
        except: user_max = 0
        settings = {"direction": self.combo_direction.Text, "max_rows": user_max}
        self.Close()
        apply_revisions_to_sheets(self.row_data, self.target_native_rev, settings)

# ==========================================================
# 4. FINAL TRANSACTION
# ==========================================================
def apply_revisions_to_sheets(row_models, native_rev, settings):
    with DB.Transaction(doc, "Apply Custom Revisions") as tx:
        tx.Start()
        save_project_settings(settings) # Save preferences globally
        
        native_id = native_rev.Id
        new_entry_template = {
            "NativeRevisionGuid": native_rev.UniqueId,
            "Description": native_rev.Description,
            "IssuedBy": native_rev.IssuedBy,
            "Date": native_rev.RevisionDate
        }
        
        for row in row_models:
            sheet = row.sheet
            existing_revs = sheet.GetAdditionalRevisionIds()
            
            if not row.History: existing_revs.Clear()
            if native_id not in existing_revs:
                existing_revs.Add(native_id)
                sheet.SetAdditionalRevisionIds(existing_revs)
                
            new_entry = dict(new_entry_template)
            new_entry["FullRevisionNumber"] = row.PredictedFullRevision
            
            history = row.History
            if row.IsUpdate: history[row.existing_entry_index] = new_entry
            else: history.append(new_entry)
                
            save_sheet_history(sheet, history)
            update_titleblock_parameters(sheet, history, settings)
            
        tx.Commit()
    forms.alert("Successfully processed revisions on {} sheets.".format(len(row_models)), title="Success")

# ==========================================================
# 5. MAIN SCRIPT EXECUTION FLOW & AUDIT ENGINE
# ==========================================================
def launch_apply_tool():
    all_revisions = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Revisions).ToElements()
    if not all_revisions: forms.alert("No Native Revisions found.", exitscript=True)

    selected_native_rev = forms.SelectFromList.show(
        all_revisions, name_attr='Name', title="1. Select the Native Revision Sequence", button_name="Select Sequence"
    )

    if selected_native_rev:
        selected_sheets = forms.select_sheets(title="2. Select Sheets to Apply Revision To")
        if selected_sheets:
            CustomRevisionWindow('ui.xaml', selected_sheets, selected_native_rev).show_dialog()

def audit_and_sync_manual_changes():
    settings = get_project_settings()
    if not settings.get("direction"):
        direction = forms.CommandSwitchWindow.show(["Top-Down", "Bottom-Up"], message="Select your Titleblock Arrangement:")
        if not direction: return
        settings["direction"] = direction

    all_sheets = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Sheets).WhereElementIsNotElementType().ToElements()
    sheets_fixed = removed_count = added_count = updated_text_count = 0
    
    with DB.Transaction(doc, "Audit & Sync Custom Revisions") as tx:
        tx.Start()
        save_project_settings(settings) # Lock in settings if they just answered the prompt
        
        for sheet in all_sheets:
            native_ids = sheet.GetAdditionalRevisionIds()
            native_dict = {doc.GetElement(i).UniqueId: doc.GetElement(i) for i in native_ids if doc.GetElement(i)}
            
            history = get_sheet_history(sheet)
            history_guids = {h.get("NativeRevisionGuid") for h in history}
            modified, new_history = False, []
            
            for h in history:
                guid = h.get("NativeRevisionGuid")
                if guid in native_dict:
                    nat_rev = native_dict[guid]
                    if h.get("Description") != nat_rev.Description or h.get("IssuedBy") != nat_rev.IssuedBy or h.get("Date") != nat_rev.RevisionDate:
                        h.update({"Description": nat_rev.Description, "IssuedBy": nat_rev.IssuedBy, "Date": nat_rev.RevisionDate})
                        modified, updated_text_count = True, updated_text_count + 1
                    new_history.append(h)
                else:
                    modified, removed_count = True, removed_count + 1
                    
            for guid, e in native_dict.items():
                if guid not in history_guids:
                    # [ THE CRITICAL FIX ]
                    # Safely ask the Sheet for the revision number instead of the global Project
                    safe_rev_num = sheet.GetRevisionNumberOnSheet(e.Id)
                    
                    new_history.append({
                        "NativeRevisionGuid": e.UniqueId, 
                        "FullRevisionNumber": "SYNC.{}".format(safe_rev_num), 
                        "Description": e.Description, 
                        "IssuedBy": e.IssuedBy, 
                        "Date": e.RevisionDate
                    })
                    modified, added_count = True, added_count + 1
                    
            if modified:
                save_sheet_history(sheet, new_history)
                sheets_fixed += 1
                
            update_titleblock_parameters(sheet, new_history if modified else history, settings)
        tx.Commit()
        
    if sheets_fixed > 0:
        forms.alert("Audit Complete!\n\nJSON Database Fixes: {} Sheets affected\n  - Text Data Updates Synced: {}\n  - Manual Deletions Synced: {}\n  - Manual Additions Synced: {}\n\nAdditionally, all Title Block parameters were forcefully refreshed to fix any accidental typing overrides.".format(sheets_fixed, updated_text_count, removed_count, added_count), title="Sync Successful")
    else:
        forms.alert("Audit Complete!\n\nThe JSON Database is perfectly in sync with native revisions.\nAll Title Block parameters across the project have been forcefully refreshed to overwrite any accidental manual typing.", title="All Good")

options = {"🚀 Apply / Edit Custom Revisions": "apply", "🔄 Audit & Sync Manual Changes": "audit"}
res = forms.CommandSwitchWindow.show(options.keys(), message="Select an action:")

if res:
    if options[res] == "apply": launch_apply_tool()
    elif options[res] == "audit": audit_and_sync_manual_changes()