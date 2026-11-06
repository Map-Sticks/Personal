# -*- coding: utf-8 -*-
"""
Bulk toggle Flip Work Plane for selected family instances.
Select elements first, then click the button.
"""

__title__ = "Flip\nWork Plane"
__context__ = "Selection"

from pyrevit import revit, DB, forms, script

doc = revit.doc
selection = revit.get_selection()
sel_ids = list(selection.element_ids)

if not sel_ids:
    forms.alert("Pre-select one or more elements, then run again.", exitscript=True)

elems = [doc.GetElement(eid) for eid in sel_ids]
fis = [e for e in elems if isinstance(e, DB.FamilyInstance)]

if not fis:
    forms.alert("Selection contains no Family Instances.", exitscript=True)

flippable = []
skipped = []

# Filter what can flip (and stay safe)
for fi in fis:
    try:
        if fi.CanFlipWorkPlane:
            flippable.append(fi)
        else:
            skipped.append(fi)
    except Exception:
        skipped.append(fi)

if not flippable:
    forms.alert("None of the selected instances support Flip Work Plane.", exitscript=True)

flipped_count = 0
failed = []

with revit.Transaction("Bulk Flip Work Plane"):
    for fi in flippable:
        try:
            # Toggle current state
            fi.IsWorkPlaneFlipped = (not fi.IsWorkPlaneFlipped)
            flipped_count += 1
        except Exception:
            failed.append(fi.Id)

out = script.get_output()
out.print_md("## Bulk Flip Work Plane – Results")
out.print_md("- Flipped: **{}**".format(flipped_count))
out.print_md("- Skipped (not supported): **{}**".format(len(skipped)))
out.print_md("- Failed (exception): **{}**".format(len(failed)))
if failed:
    out.print_md("**Failed ElementIds:** " + ", ".join([str(x.IntegerValue) for x in failed]))