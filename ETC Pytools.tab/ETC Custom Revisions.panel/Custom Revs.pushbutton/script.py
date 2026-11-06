# -*- coding: utf-8 -*-
import System
import clr
import json
from pyrevit import revit, DB, forms

doc = revit.doc
app = doc.Application

# ==========================================================
# 1. EXTENSIBLE STORAGE SETUP (For syncing Max Rows)
# ==========================================================
SETTINGS_GUID = System.Guid("D9F3C6A2-1B8E-4E7A-A3F5-8C9D2E4F1A0B")

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

# ==========================================================
# 2. REVIT API VERSION HANDLING HELPERS
# ==========================================================
def get_spec_type(type_str):
    if hasattr(DB, "SpecTypeId"):
        return DB.SpecTypeId.String.Text if type_str == "text" else DB.SpecTypeId.Boolean.YesNo
    else:
        return DB.ParameterType.Text if type_str == "text" else DB.ParameterType.YesNo

def get_pg_data():
    if hasattr(DB, "GroupTypeId"): return DB.GroupTypeId.Data
    else: return DB.BuiltInParameterGroup.PG_DATA

def find_sp_definition(sp_file, param_name):
    for group in sp_file.Groups:
        my_def = group.Definitions.get_Item(param_name)
        if my_def: return my_def
    return None

# ==========================================================
# 3. MAIN SCRIPT EXECUTION
# ==========================================================
def setup_project_parameters():
    # 1. Ask user for target rows
    res = forms.ask_for_string(prompt="How many Revision Rows do you want to set up in this project?", title="1. Parameter Setup", default="10")
    if not res or not res.isdigit(): return
    target_rows = int(res)
    if target_rows < 1: return

    # Ask User for Numbering Format
    format_choice = forms.CommandSwitchWindow.show(
        ["Single Digits (1, 2, 3...)", "Double Digits (01, 02, 03...)"],
        message="How do you want your parameters numbered?"
    )
    if not format_choice: return
    use_padding = format_choice.startswith("Double")

    confirm = forms.alert(
        "You requested {} rows.\n\n"
        "If there are currently more than {} rows of parameters in this project, this tool will PERMANENTLY DELETE the excess parameters to keep the model clean.\n\n"
        "Proceed?".format(target_rows, target_rows),
        yes=True, no=True, title="Confirm Setup & Cleanup"
    )
    if not confirm: return

    # 2. Check and Load Shared Parameter File
    sp_file = app.OpenSharedParameterFile()
    if not sp_file:
        forms.alert("No Shared Parameter file is currently loaded in Revit.\nPlease select your firm's Shared Parameter (.txt) file.", title="Select SP File")
        filepath = forms.pick_file(file_ext='txt', title="Select Firm Shared Parameter File")
        if not filepath: return
        app.SharedParametersFilename = filepath
        sp_file = app.OpenSharedParameterFile()

    if not sp_file:
        forms.alert("Failed to access the Shared Parameter file. Exiting.", exitscript=True)

    # 3. Ask User to Select or Create a Group
    existing_groups = sorted([g.Name for g in sp_file.Groups])
    options = ["➕ Create New Group..."] + existing_groups
    
    selected_option = forms.SelectFromList.show(options, title="2. Select Parameter Group", button_name="Select Group")
    if not selected_option: return

    group_name = selected_option
    if group_name == "➕ Create New Group...":
        group_name = forms.ask_for_string(prompt="Enter new group name:", title="New Group", default="ETC_Custom_Revisions")
        if not group_name: return

    # 4. Prepare the Binding Map (Only for Sheets)
    binding_map = doc.ParameterBindings
    sheet_cat = doc.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Sheets)
    sheet_cat_set = app.Create.NewCategorySet()
    sheet_cat_set.Insert(sheet_cat)
    sheet_binding = app.Create.NewInstanceBinding(sheet_cat_set)

    param_types = {
        "Hist_Rev": "text",
        "Hist_Desc": "text",
        "Hist_By": "text",
        "Hist_Date": "text",
        "Hist_Line": "yesno"
    }

    sp_created_count = 0
    added_count = 0
    deleted_count = 0

    with DB.Transaction(doc, "Setup Revision Parameters") as tx:
        tx.Start()

        # A. Find or Create the Target Group
        group = sp_file.Groups.get_Item(group_name)
        if not group:
            try: group = sp_file.Groups.Create(group_name)
            except Exception as e: forms.alert("Could not create group. Is your Shared Parameter file read-only?", exitscript=True)

        # B. Generate Parameter Names
        parameters_to_bind = ["Current_Custom_Revision"] 
        for i in range(1, target_rows + 1):
            for prefix in param_types.keys():
                if use_padding:
                    parameters_to_bind.append("{}_{:02d}".format(prefix, i))
                else:
                    parameters_to_bind.append("{}_{}".format(prefix, i))

        # C. CREATE & BIND
        for p_name in parameters_to_bind:
            p_type_str = "text"
            for prefix, t in param_types.items():
                if p_name.startswith(prefix):
                    p_type_str = t
                    break

            my_def = find_sp_definition(sp_file, p_name)
            
            if not my_def:
                opt = DB.ExternalDefinitionCreationOptions(p_name, get_spec_type(p_type_str))
                opt.Visible = True
                try:
                    my_def = group.Definitions.Create(opt)
                    sp_created_count += 1
                except Exception as e:
                    forms.alert("Failed to write '{}' to the SP file. Is the .txt file locked or read-only?".format(p_name), exitscript=True)

            # [ THE FIX ]: Only bind to the project if it is NOT a visibility line
            if p_type_str != "yesno":
                target_pg = get_pg_data()
                if not binding_map.Contains(my_def):
                    binding_map.Insert(my_def, sheet_binding, target_pg)
                    added_count += 1
                else:
                    binding_map.ReInsert(my_def, sheet_binding, target_pg)

        # D. CLEANUP EXCESS PARAMETERS
        all_sps = DB.FilteredElementCollector(doc).OfClass(DB.SharedParameterElement).ToElements()
        params_to_delete = []

        for sp in all_sps:
            name = sp.Name
            
            # Skip Hist_Line during global project cleanup because they belong to the Title Block Family instances!
            if name.startswith("Hist_Line"):
                continue

            for prefix in param_types.keys():
                if prefix == "Hist_Line": continue # Double safety
                
                prefix_with_underscore = prefix + "_"
                if name.startswith(prefix_with_underscore):
                    num_str = name.replace(prefix_with_underscore, "")
                    if num_str.isdigit():
                        num = int(num_str)
                        if num > target_rows:
                            params_to_delete.append(sp.Id)
                    break

        for p_id in params_to_delete:
            try:
                doc.Delete(p_id)
                deleted_count += 1
            except: pass

        # E. Update Global Project Settings
        settings = get_project_settings()
        settings["max_rows"] = target_rows
        save_project_settings(settings)

        tx.Commit()

    forms.alert(
        "Project Setup Complete!\n\n"
        "📝 Missing Parameters written to SP File: {}\n"
        "🟢 Text Parameters Bound to Sheets: {}\n"
        "🔴 Excess Parameters Deleted: {}\n\n"
        "Note: The 'Hist_Line' parameters were created in your SP file so you can add them manually to your Title Block Family geometry, but they were safely kept out of your Project Parameters list.".format(sp_created_count, added_count, deleted_count),
        title="Success"
    )

setup_project_parameters()