# -*- coding: utf-8 -*-
"""Randomise Color Scheme Colours + Purge Unused Entries

Pick a single Color Scheme, then optionally:
  - Remove all entries where 'In Use' = No
  - Reassign each remaining entry a random bright colour
Other schemes are untouched.
"""

import random
import colorsys

from pyrevit import revit, DB, forms, script

doc = revit.doc
output = script.get_output()

# -------------------------------------------------------------
# 1. Collect all Color Fill Schemes
# -------------------------------------------------------------
schemes = list(DB.FilteredElementCollector(doc)
                 .OfClass(DB.ColorFillScheme)
                 .ToElements())

if not schemes:
    forms.alert("No Color Schemes found in this project.", exitscript=True)

scheme_map = {}
for s in schemes:
    try:
        cat_name = DB.Category.GetCategory(doc, s.CategoryId).Name
    except Exception:
        cat_name = "Unknown"
    label = "{0} : {1}".format(cat_name, s.Name)
    scheme_map[label] = s

pick = forms.SelectFromList.show(
    sorted(scheme_map.keys()),
    title="Select ONE Color Scheme",
    multiselect=False
)
if not pick:
    script.exit()

scheme = scheme_map[pick]

# -------------------------------------------------------------
# 2. Action
# -------------------------------------------------------------
action_opts = ["Purge unused only",
               "Recolour only",
               "Purge unused AND recolour"]
action = forms.CommandSwitchWindow.show(action_opts,
                                        message="What do you want to do?")
if not action:
    script.exit()

do_purge    = action in ("Purge unused only", "Purge unused AND recolour")
do_recolour = action in ("Recolour only",     "Purge unused AND recolour")

# -------------------------------------------------------------
# 3. Colour style
# -------------------------------------------------------------
s_range = v_range = None
if do_recolour:
    style_opts = ["Bright", "Pastel", "Vivid"]
    mode = forms.CommandSwitchWindow.show(style_opts,
                                          message="Choose colour style:")
    if not mode:
        script.exit()
    if mode == "Bright":
        s_range, v_range = (0.55, 0.85), (0.80, 1.00)
    elif mode == "Pastel":
        s_range, v_range = (0.30, 0.55), (0.90, 1.00)
    else:
        s_range, v_range = (0.75, 1.00), (0.85, 1.00)

# -------------------------------------------------------------
# 4. Analyse entries
# -------------------------------------------------------------
all_entries    = list(scheme.GetEntries())
if not all_entries:
    forms.alert("The selected scheme has no entries.", exitscript=True)

unused_entries = [e for e in all_entries if not e.IsInUse]
used_entries   = [e for e in all_entries if e.IsInUse]

msg = ["Scheme: {0}".format(pick),
       "Total entries: {0}".format(len(all_entries)),
       "In use: {0}".format(len(used_entries)),
       "Not in use: {0}".format(len(unused_entries)), ""]
if do_purge:
    msg.append("- Will remove {0} unused entries".format(len(unused_entries)))
if do_recolour:
    target = len(used_entries) if do_purge else len(all_entries)
    msg.append("- Will recolour {0} entries".format(target))
msg.append("\nContinue?")

if not forms.alert("\n".join(msg), yes=True, no=True):
    script.exit()

# -------------------------------------------------------------
# 5. Colour generator
# -------------------------------------------------------------
def random_bright_color(used_hues):
    h = random.random()
    for _ in range(20):
        h = random.random()
        ok = True
        for u in used_hues:
            d = abs(h - u)
            if d < 0.05 or d > 0.95:
                ok = False
                break
        if ok:
            break
    s_val = random.uniform(s_range[0], s_range[1])
    v_val = random.uniform(v_range[0], v_range[1])
    r, g, b = colorsys.hsv_to_rgb(h, s_val, v_val)
    used_hues.append(h)
    return DB.Color(int(r * 255), int(g * 255), int(b * 255))

# -------------------------------------------------------------
# 6. Apply
# -------------------------------------------------------------
removed = 0
recoloured = 0
skipped = []

t = DB.Transaction(doc, "Colour Scheme: Purge + Randomise")
t.Start()
try:
    if do_purge:
        for entry in unused_entries:
            try:
                if scheme.CanRemoveEntry(entry):
                    scheme.RemoveEntry(entry)
                    removed += 1
                else:
                    skipped.append(entry)
            except Exception:
                skipped.append(entry)

    if do_recolour:
        remaining = list(scheme.GetEntries())
        used_hues = []
        for entry in remaining:
            entry.Color = random_bright_color(used_hues)   # <-- fixed
            scheme.UpdateEntry(entry)
            recoloured += 1

    t.Commit()

    output.print_md("**Done on `{0}`**".format(pick))
    output.print_md("- Removed unused entries: **{0}**".format(removed))
    if skipped:
        output.print_md("- Skipped (could not remove): **{0}**".format(len(skipped)))
    output.print_md("- Recoloured entries: **{0}**".format(recoloured))

except Exception as ex:
    t.RollBack()
    forms.alert("Failed, changes rolled back:\n{0}".format(ex), exitscript=True)