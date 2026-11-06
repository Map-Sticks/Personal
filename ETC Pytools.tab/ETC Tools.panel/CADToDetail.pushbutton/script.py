# -*- coding: utf-8 -*-
"""Legend Builder.

Press the button ->
  1. Filter which categories to include (untick Tags, etc.).
  2. Pick the annotation / detail item family TYPES to place (grouped by
     category, with search).
  3. Set spacing / layout options.
  4. Items are placed into a Drafting view, THEN you can:
        - Keep them,
        - Adjust spacing (re-places without re-selecting), or
        - Cancel (removes what was placed).

Note: Revit's API cannot place components into a true Legend view, so a
Drafting view is used (the active one if it is a drafting view, otherwise a new
one is created).
"""

__title__  = "Legend\nBuilder"
__author__ = "Jacques-Michael Oosthuizen"

# Force the button to ALWAYS be enabled (never greyed out), matching the
# `context: zero-doc` entry in bundle.yaml. The script guards against there
# being no open document below.
__context__ = 'zero-doc'

from pyrevit import revit, DB, forms, script

doc    = revit.doc
uidoc  = revit.uidoc
output = script.get_output()

# zero-doc means the button can fire even with no project open -> guard it.
if doc is None or uidoc is None:
    forms.alert("Open a Revit project first, then run Legend Builder.",
                exitscript=True)

DETAIL_ITEM = int(DB.BuiltInCategory.OST_DetailComponents)
MM_TO_FT    = 1.0 / 304.8   # Revit internal units are feet


# ---------------------------------------------------------------------------
# 1. Collect ALL annotation + detail item family TYPES, tracked by category
# ---------------------------------------------------------------------------
symbol_map   = {}   # "Family : Type" -> FamilySymbol
cat_of_name  = {}   # "Family : Type" -> category name

for fam in DB.FilteredElementCollector(doc).OfClass(DB.Family):
    cat = fam.FamilyCategory
    if cat is None:
        continue

    is_annotation = (cat.CategoryType == DB.CategoryType.Annotation)
    is_detail     = (cat.Id.IntegerValue == DETAIL_ITEM)
    if not (is_annotation or is_detail):
        continue

    for sym_id in fam.GetFamilySymbolIds():
        sym = doc.GetElement(sym_id)
        if sym is None:
            continue
        tname = sym.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM).AsString()
        name = "{} : {}".format(fam.Name, tname)
        symbol_map[name]  = sym
        cat_of_name[name] = cat.Name

if not symbol_map:
    forms.alert("No annotation or detail item families found in this model.",
                exitscript=True)


# ---------------------------------------------------------------------------
# 2. Category filter (untick Tags, etc.)  -- done ONCE
# ---------------------------------------------------------------------------
all_cats = sorted(set(cat_of_name.values()))

keep_cats = forms.SelectFromList.show(
    all_cats,
    title="Legend Builder - categories to INCLUDE (untick Tags, etc.)",
    multiselect=True,
    button_name="Next")

if not keep_cats:
    script.exit()

keep_cats = set(keep_cats)


# ---------------------------------------------------------------------------
# 3. Family/type picker, grouped by category  -- done ONCE
# ---------------------------------------------------------------------------
groups = {}
for name, cname in cat_of_name.items():
    if cname in keep_cats:
        groups.setdefault(cname, []).append(name)
for cname in groups:
    groups[cname] = sorted(groups[cname])

picked = forms.SelectFromList.show(
    groups,                       # dict -> group dropdown + search box
    title="Legend Builder - select families/types to place",
    multiselect=True,
    button_name="Place")

if not picked:
    script.exit()

selected_symbols = [(name, symbol_map[name]) for name in picked]


# ---------------------------------------------------------------------------
# 4. Resolve the target Drafting view ONCE (created lazily on first placement)
# ---------------------------------------------------------------------------
active = uidoc.ActiveView
use_active = False
if active.ViewType == DB.ViewType.DraftingView:
    use_active = forms.alert(
        "Active view is a Drafting view ('{}').\n\n"
        "Place the legend into this view?".format(active.Name),
        yes=True, no=True)

drafting_vft = None
for vft in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
    if vft.ViewFamily == DB.ViewFamily.Drafting:
        drafting_vft = vft
        break

text_type_id  = doc.GetDefaultElementTypeId(DB.ElementTypeGroup.TextNoteType)
target_view   = None   # created on first placement; reused across retries


# ---------------------------------------------------------------------------
# Helper: ask for spacing settings (mm). Returns dict or None if cancelled.
# ---------------------------------------------------------------------------
def ask_spacing(default_str):
    raw = forms.ask_for_string(
        default=default_str,
        prompt=("Enter spacing in mm, comma-separated:\n\n"
                "RowSpacing, LabelOffset, ItemsPerColumn (0 = single column), "
                "ColumnSpacing"),
        title="Legend Builder - spacing")
    if not raw:
        return None

    def _num(parts, idx, fallback):
        try:
            return float(parts[idx].strip())
        except Exception:
            return fallback

    p = raw.split(",")
    return {
        "raw":  raw,
        "row":  _num(p, 0, 50.0)  * MM_TO_FT,
        "lbl":  _num(p, 1, 30.0)  * MM_TO_FT,
        "ipc":  int(_num(p, 2, 0.0)),
        "col":  _num(p, 3, 150.0) * MM_TO_FT,
    }


# ---------------------------------------------------------------------------
# Helper: place all selected symbols using a settings dict.
# Returns (placed_count, failed_list, created_element_ids).
# ---------------------------------------------------------------------------
def place_items(view, s):
    placed, failed, created = 0, [], []
    for i, (name, sym) in enumerate(selected_symbols):
        if not sym.IsActive:
            sym.Activate()
            doc.Regenerate()

        if s["ipc"] > 0:
            row = i % s["ipc"]
            col = i // s["ipc"]
        else:
            row, col = i, 0

        x = col * s["col"]
        y = -row * s["row"]

        try:
            inst = doc.Create.NewFamilyInstance(DB.XYZ(x, y, 0), sym, view)
            created.append(inst.Id)
            placed += 1
        except Exception as ex:
            failed.append((name, str(ex)))

        note = DB.TextNote.Create(doc, view.Id,
                                  DB.XYZ(x + s["lbl"], y, 0), name, text_type_id)
        created.append(note.Id)
    return placed, failed, created


# ---------------------------------------------------------------------------
# 5. Place -> review -> (adjust | keep | cancel) loop
# ---------------------------------------------------------------------------
default_str  = "50, 30, 0, 150"
last_placed  = 0
last_failed  = []

while True:
    settings = ask_spacing(default_str)
    if settings is None:
        # user cancelled the spacing dialog
        if target_view is not None:
            # nothing has been committed as "kept"; leave what exists? -> remove
            with revit.Transaction("Legend Builder - discard"):
                pass
        script.exit()
    default_str = settings["raw"]   # remember for next prompt

    with revit.Transaction("Build Legend (preview)"):
        if target_view is None:
            if use_active:
                target_view = active
            else:
                if drafting_vft is None:
                    forms.alert("No Drafting view type found in the model, so "
                                "a new Drafting view cannot be created.",
                                exitscript=True)
                target_view = DB.ViewDrafting.Create(doc, drafting_vft.Id)
                try:
                    target_view.Name = "Legend Builder"
                except Exception:
                    pass

        last_placed, last_failed, created_ids = place_items(target_view, settings)

    # Show the result so the user can look at the view before deciding.
    choice = forms.alert(
        "Placed {} item(s) into '{}'.\n\n"
        "Look at the view. What next?".format(last_placed, target_view.Name),
        title="Legend Builder - review",
        options=["Keep", "Adjust spacing", "Cancel"])

    if choice == "Keep":
        break

    # For both "Adjust spacing" and "Cancel" we remove what we just placed.
    with revit.Transaction("Legend Builder - clear preview"):
        for eid in created_ids:
            try:
                doc.Delete(eid)
            except Exception:
                pass

    if choice == "Adjust spacing":
        continue   # re-prompt spacing only; selection preserved

    # Cancel: also remove a view we created for this run, then exit.
    if not use_active and target_view is not None:
        with revit.Transaction("Legend Builder - remove view"):
            try:
                doc.Delete(target_view.Id)
            except Exception:
                pass
    script.exit()


# ---------------------------------------------------------------------------
# 6. Report (only reached on "Keep")
# ---------------------------------------------------------------------------
output.print_md("**Legend Builder** - placed **{}** item(s) into **{}**."
                .format(last_placed, target_view.Name))
if last_failed:
    for name, err in last_failed:
        output.print_md("- Skipped **{}** - {}".format(name, err))
