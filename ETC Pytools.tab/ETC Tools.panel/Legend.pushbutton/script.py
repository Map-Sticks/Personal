# -*- coding: utf-8 -*-
"""Build a legend-style layout from the SELECTED annotation / detail item
family instances.

Revit's API cannot place components into a true Legend view, so this tool
collects the unique family types from your current selection and lays them out
(graphic + text label) into a Drafting view (a new one, or the active one).
"""

__title__  = "Legend\nBuilder"
__author__ = "Jacques-Michael Oosthuizen"

# Enable the ribbon button only when something is selected.
# (This mirrors the `context: selection` entry in bundle.yaml.)
__context__ = 'selection'

from pyrevit import revit, DB, forms, script

doc    = revit.doc
uidoc  = revit.uidoc
output = script.get_output()

DETAIL_ITEM = int(DB.BuiltInCategory.OST_DetailComponents)


# ---------------------------------------------------------------------------
# 1. Read the current selection and collect UNIQUE family symbols
# ---------------------------------------------------------------------------
sel_ids = uidoc.Selection.GetElementIds()
if not sel_ids or sel_ids.Count == 0:
    forms.alert("Nothing selected.\n\n"
                "Select one or more annotation or detail item family "
                "instances, then run the tool again.",
                exitscript=True)

symbol_map = {}          # "Family : Type" -> FamilySymbol
skipped_categories = set()

for eid in sel_ids:
    el = doc.GetElement(eid)

    # We only care about placed family instances that carry a FamilySymbol.
    if not isinstance(el, DB.FamilyInstance):
        continue

    sym = el.Symbol
    if sym is None:
        continue

    fam = sym.Family
    cat = fam.FamilyCategory
    if cat is None:
        continue

    is_annotation = (cat.CategoryType == DB.CategoryType.Annotation)
    is_detail     = (cat.Id.IntegerValue == DETAIL_ITEM)
    if not (is_annotation or is_detail):
        skipped_categories.add(cat.Name)
        continue

    tname = sym.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM).AsString()
    key = "{} : {}".format(fam.Name, tname)
    symbol_map[key] = sym            # dedupes automatically by key

if not symbol_map:
    forms.alert("No annotation or detail item families found in the "
                "selection.\n\nSelected categories: {}"
                .format(", ".join(sorted(skipped_categories)) or "n/a"),
                exitscript=True)


# ---------------------------------------------------------------------------
# 2. Choose the target Drafting view
# ---------------------------------------------------------------------------
active = uidoc.ActiveView
use_active = False
if active.ViewType == DB.ViewType.DraftingView:
    use_active = forms.alert(
        "Active view is a Drafting view ('{}').\n\n"
        "Place the legend into this view?".format(active.Name),
        yes=True, no=True)

# Grab a Drafting view family type in case we need to create a new view.
drafting_vft = None
for vft in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
    if vft.ViewFamily == DB.ViewFamily.Drafting:
        drafting_vft = vft
        break


# ---------------------------------------------------------------------------
# 3. Layout config (internal units = feet)
# ---------------------------------------------------------------------------
ROW_SPACING  = 0.15
LABEL_OFFSET = 0.10
text_type_id = doc.GetDefaultElementTypeId(DB.ElementTypeGroup.TextNoteType)


# ---------------------------------------------------------------------------
# 4. Build the legend
# ---------------------------------------------------------------------------
placed, failed = 0, []
target_view = None

with revit.Transaction("Build Legend From Selection"):
    if use_active:
        target_view = active
    else:
        if drafting_vft is None:
            forms.alert("No Drafting view type found in the model, so a new "
                        "Drafting view cannot be created.", exitscript=True)
        target_view = DB.ViewDrafting.Create(doc, drafting_vft.Id)
        try:
            target_view.Name = "Legend - {}".format(revit.query.get_name(active))
        except Exception:
            pass

    y = 0.0
    for key in sorted(symbol_map.keys()):
        sym = symbol_map[key]
        if not sym.IsActive:
            sym.Activate()
            doc.Regenerate()

        pt = DB.XYZ(0, y, 0)
        try:
            doc.Create.NewFamilyInstance(pt, sym, target_view)
            placed += 1
        except Exception as ex:
            failed.append((key, str(ex)))

        DB.TextNote.Create(doc, target_view.Id,
                           DB.XYZ(LABEL_OFFSET, y, 0), key, text_type_id)
        y -= ROW_SPACING


# ---------------------------------------------------------------------------
# 5. Report
# ---------------------------------------------------------------------------
output.print_md("**Legend Builder** - placed **{}** item(s) into **{}**."
                .format(placed, target_view.Name))
for key, err in failed:
    output.print_md("- Skipped **{}** - {}".format(key, err))
if skipped_categories:
    output.print_md("_Ignored non-annotation/detail selections: {}_"
                    .format(", ".join(sorted(skipped_categories))))
