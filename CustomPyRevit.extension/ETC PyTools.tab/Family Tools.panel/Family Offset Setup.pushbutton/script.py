# -*- coding: utf-8 -*-
"""Family Offset Setup
Family Editor only.
  Wall mode    : Left/Right + Wall Offset
  Ceiling mode : Left/Right + Up/Down (in plan)
Creates reference planes, parameters, formulas, dimensions and labels.
You only lock the symbol to the planes.
"""
from pyrevit import revit, DB, forms, script
from System.Collections.Generic import List

doc = revit.doc
uidoc = revit.uidoc
TITLE = "Family Offset Setup"
MM = 1.0 / 304.8          # mm -> internal feet
NOT_A_REFERENCE = 12      # FamilyInstanceReferenceType.NotAReference

# ------------------------------------------------------------------ checks
if not doc.IsFamilyDocument:
    forms.alert("This button only works in the Family Editor.\n\n"
                "Open the family (Edit Family) and run it again.",
                title=TITLE, exitscript=True)

view = doc.ActiveView
if not isinstance(view, DB.ViewPlan) or view.IsTemplate:
    forms.alert("Open a plan view first (e.g. 'Ref. Level').\n\n"
                "Planes and dimensions are created in the active plan view.",
                title=TITLE, exitscript=True)

fm = doc.FamilyManager
OUR_RP_NAMES = ("LR Anchor", "Symbol L-R", "Symbol Wall Offset",
                "UD Anchor", "Symbol U-D")


def is_horizontal(rp):
    d = rp.Direction
    return abs(d.Y) < 1e-6 and abs(d.Z) < 1e-6


all_rps = list(DB.FilteredElementCollector(doc).OfClass(DB.ReferencePlane))
rps_by_name = dict((rp.Name, rp) for rp in all_rps if rp.Name)

# ------------------------------------------------------------------ inputs
HOST_WALL = "Wall-based   (Left/Right + Wall Offset)"
HOST_CEIL = "Ceiling-based   (Left/Right + Up/Down)"
host = forms.CommandSwitchWindow.show([HOST_WALL, HOST_CEIL],
                                      message="Family type:")
if not host:
    script.exit()
ceiling = (host == HOST_CEIL)

MODE_SIGNED = "Signed offset  (-ve = left/down, +ve = right/up)"
MODE_YESNO = "Yes/No switch + offset value"
mode = forms.CommandSwitchWindow.show([MODE_SIGNED, MODE_YESNO],
                                      message="Offset control type:")
if not mode:
    script.exit()
signed = (mode == MODE_SIGNED)

anchor_str = forms.ask_for_string(
    default="3000",
    prompt="Anchor plane distance (mm).\n"
           "Max travel LEFT{} from the symbol's starting position.".format(
               " and DOWN" if ceiling else ""),
    title=TITLE)
if not anchor_str:
    script.exit()
try:
    anchor_mm = float(anchor_str.strip())
    if anchor_mm <= 0:
        raise ValueError
except Exception:
    forms.alert("Enter a positive number in mm.", title=TITLE, exitscript=True)

base_rp, base_name, base_y = None, None, 0.0
if not ceiling:
    base_names = sorted(n for n, rp in rps_by_name.items()
                        if is_horizontal(rp) and n not in OUR_RP_NAMES)
    if not base_names:
        forms.alert("No named horizontal reference plane found to measure the "
                    "wall offset from (e.g. 'Center (Front/Back)').",
                    title=TITLE, exitscript=True)
    if "Center (Front/Back)" in base_names:
        base_names.remove("Center (Front/Back)")
        base_names.insert(0, "Center (Front/Back)")
    base_name = forms.SelectFromList.show(
        base_names, title="Measure Wall_Offset FROM which plane? (wall face)",
        multiselect=False, button_name="Use this plane")
    if not base_name:
        script.exit()
    base_rp = rps_by_name[base_name]
    base_y = base_rp.BubbleEnd.Y

# ------------------------------------------------------------------ symbol position
cx, cy = 0.0, base_y
boxes = []
for eid in uidoc.Selection.GetElementIds():
    el = doc.GetElement(eid)
    bb = el.get_BoundingBox(view) if el else None
    if bb:
        boxes.append(bb)
if boxes:
    cx = (min(b.Min.X for b in boxes) + max(b.Max.X for b in boxes)) / 2.0
    cy = (min(b.Min.Y for b in boxes) + max(b.Max.Y for b in boxes)) / 2.0

if not ceiling and abs(cy - base_y) < 1 * MM:
    cy = base_y + 100 * MM   # avoid zero-length wall dimension

anchor = anchor_mm * MM
half = 1000 * MM
report = []


# ------------------------------------------------------------------ helpers
def set_not_reference(rp):
    """Stops a far-off plane being hover/pickable in the project."""
    try:
        prm = rp.get_Parameter(DB.BuiltInParameter.ELEM_REFERENCE_NAME)
        if prm and not prm.IsReadOnly and prm.AsInteger() != NOT_A_REFERENCE:
            prm.Set(NOT_A_REFERENCE)
            report.append("Set 'Not a Reference': " + rp.Name)
    except Exception as ex:
        report.append("Could not set 'Not a Reference' on {} ({})".format(rp.Name, ex))


def get_or_make_rp(name, p1, p2, pin=False):
    if name in rps_by_name:
        report.append("Plane reused (exists): " + name)
        return rps_by_name[name]
    rp = doc.FamilyCreate.NewReferencePlane(p1, p2, DB.XYZ.BasisZ, view)
    rp.Name = name
    if pin:
        rp.Pinned = True
    rps_by_name[name] = rp
    report.append("Plane created: " + name)
    return rp


existing_params = dict((p.Definition.Name, p) for p in fm.Parameters)


def add_param(name, kind):
    """Instance parameter. kind = 'length' or 'yesno'. Revit 2020-2026."""
    if name in existing_params:
        report.append("Parameter reused (exists): " + name)
        return existing_params[name]

    def a1():
        spec = DB.SpecTypeId.Length if kind == 'length' else DB.SpecTypeId.Boolean.YesNo
        grp = DB.GroupTypeId.Geometry if kind == 'length' else DB.GroupTypeId.Graphics
        return fm.AddParameter(name, grp, spec, True)

    def a2():
        spec = DB.SpecTypeId.Length if kind == 'length' else DB.SpecTypeId.Boolean.YesNo
        grp = (DB.BuiltInParameterGroup.PG_GEOMETRY if kind == 'length'
               else DB.BuiltInParameterGroup.PG_GRAPHICS)
        return fm.AddParameter(name, grp, spec, True)

    def a3():
        ptype = DB.ParameterType.Length if kind == 'length' else DB.ParameterType.YesNo
        grp = (DB.BuiltInParameterGroup.PG_GEOMETRY if kind == 'length'
               else DB.BuiltInParameterGroup.PG_GRAPHICS)
        return fm.AddParameter(name, grp, ptype, True)

    last_err = None
    for fn in (a1, a2, a3):
        try:
            p = fn()
            existing_params[name] = p
            report.append("Parameter created: " + name)
            return p
        except Exception as ex:
            last_err = ex
    raise Exception("Could not create parameter '{}': {}".format(name, last_err))


def labelled_dim_exists(param):
    for d in DB.FilteredElementCollector(doc).OfClass(DB.Dimension):
        try:
            lbl = d.FamilyLabel
            if lbl and lbl.Id == param.Id:
                return True
        except Exception:
            pass
    return False


def make_dim(rp_a, rp_b, line, param, label):
    if labelled_dim_exists(param):
        report.append("Dimension skipped (already labelled): " + label)
        return
    refs = DB.ReferenceArray()
    refs.Append(rp_a.GetReference())
    refs.Append(rp_b.GetReference())
    try:
        dim = doc.FamilyCreate.NewLinearDimension(view, line, refs)
        dim.FamilyLabel = param
        report.append("Dimension created + labelled: " + label)
    except Exception as ex:
        report.append("Dimension FAILED for {}: {}".format(label, ex))


def set_if_free(param, value):
    if not param.IsDeterminedByFormula:
        fm.Set(param, value)


def fmt(v):
    return str(int(v)) if float(v).is_integer() else str(v)


def axis_params(pos_name, signed_name, yn_name, yn_off_name):
    """Creates driving params for one axis, returns (pos_param, formula, setter)."""
    p_pos = add_param(pos_name, 'length')
    if signed:
        p_s = add_param(signed_name, 'length')
        formula = "{} mm + {}".format(fmt(anchor_mm), signed_name)

        def zero():
            set_if_free(p_s, 0.0)
    else:
        p_yn = add_param(yn_name, 'yesno')
        p_off = add_param(yn_off_name, 'length')
        formula = "if({0}, {1} mm - {2}, {1} mm + {2})".format(
            yn_name, fmt(anchor_mm), yn_off_name)

        def zero():
            set_if_free(p_yn, 0)
            set_if_free(p_off, 0.0)
    return p_pos, formula, zero


def apply_formula(p_pos, formula, label):
    try:
        fm.SetFormula(p_pos, formula)
        report.append("Formula on {}: {}".format(label, formula))
    except Exception as ex:
        report.append("Formula FAILED on {} (set manually in FT): {} ({})".format(
            label, formula, ex))


# ------------------------------------------------------------------ build
new_ids = []
span = max(anchor, half)

with revit.Transaction("Family Offset Setup"):
    if fm.CurrentType is None:
        fm.NewType("Type 1")

    # ---------- Left / Right (both modes)
    rp_lr_anchor = get_or_make_rp("LR Anchor",
                                  DB.XYZ(cx - anchor, cy + half, 0),
                                  DB.XYZ(cx - anchor, cy - half, 0), pin=True)
    rp_lr = get_or_make_rp("Symbol L-R",
                           DB.XYZ(cx, cy + half, 0),
                           DB.XYZ(cx, cy - half, 0))
    set_not_reference(rp_lr_anchor)
    new_ids += [rp_lr_anchor.Id, rp_lr.Id]

    p_lr_pos, lr_formula, lr_zero = axis_params(
        "LR_Position", "Left_Right_Offset", "Symbol_Left", "Symbol_Offset")
    set_if_free(p_lr_pos, abs(rp_lr.BubbleEnd.X - rp_lr_anchor.BubbleEnd.X))

    # ---------- Second axis
    if ceiling:
        rp_ud_anchor = get_or_make_rp("UD Anchor",
                                      DB.XYZ(cx - half, cy - anchor, 0),
                                      DB.XYZ(cx + half, cy - anchor, 0), pin=True)
        rp_ud = get_or_make_rp("Symbol U-D",
                               DB.XYZ(cx - half, cy, 0),
                               DB.XYZ(cx + half, cy, 0))
        set_not_reference(rp_ud_anchor)
        new_ids += [rp_ud_anchor.Id, rp_ud.Id]

        p_ud_pos, ud_formula, ud_zero = axis_params(
            "UD_Position", "Up_Down_Offset", "Symbol_Down", "Symbol_UD_Offset")
        set_if_free(p_ud_pos, abs(rp_ud.BubbleEnd.Y - rp_ud_anchor.BubbleEnd.Y))
    else:
        rp_wall = get_or_make_rp("Symbol Wall Offset",
                                 DB.XYZ(cx - span - 500 * MM, cy, 0),
                                 DB.XYZ(cx + half, cy, 0))
        new_ids.append(rp_wall.Id)
        p_wall = add_param("Wall_Offset", 'length')
        set_if_free(p_wall, abs(rp_wall.BubbleEnd.Y - base_y))

    doc.Regenerate()

    # ---------- Dimensions
    x_a, x_b = rp_lr_anchor.BubbleEnd.X, rp_lr.BubbleEnd.X
    y_dim = cy + 600 * MM
    make_dim(rp_lr_anchor, rp_lr,
             DB.Line.CreateBound(DB.XYZ(x_a, y_dim, 0), DB.XYZ(x_b, y_dim, 0)),
             p_lr_pos, "LR_Position")

    if ceiling:
        y_a, y_b = rp_ud_anchor.BubbleEnd.Y, rp_ud.BubbleEnd.Y
        x_dim = cx + 600 * MM
        make_dim(rp_ud_anchor, rp_ud,
                 DB.Line.CreateBound(DB.XYZ(x_dim, y_a, 0), DB.XYZ(x_dim, y_b, 0)),
                 p_ud_pos, "UD_Position")
    else:
        x_dim = rp_lr_anchor.BubbleEnd.X - 300 * MM
        make_dim(base_rp, rp_wall,
                 DB.Line.CreateBound(DB.XYZ(x_dim, base_y, 0),
                                     DB.XYZ(x_dim, rp_wall.BubbleEnd.Y, 0)),
                 p_wall, "Wall_Offset")
    doc.Regenerate()

    # ---------- Formulas last (offsets = 0 -> nothing moves)
    lr_zero()
    apply_formula(p_lr_pos, lr_formula, "LR_Position")
    if ceiling:
        ud_zero()
        apply_formula(p_ud_pos, ud_formula, "UD_Position")

uidoc.Selection.SetElementIds(List[DB.ElementId](new_ids))

if ceiling:
    steps = ("\n\nNEXT STEP (only one):\n"
             "AL: lock the symbol (and 3D object if required) to\n"
             "   'Symbol L-R' and 'Symbol U-D'.\n\n"
             "Then flex in FT.\n"
             "Max travel left / down = {} mm (anchors pinned, Not a Reference)."
             .format(fmt(anchor_mm)))
else:
    steps = ("\n\nNEXT STEP (only one):\n"
             "AL: lock the symbol (and 3D object if required) to\n"
             "   'Symbol L-R' and 'Symbol Wall Offset'.\n\n"
             "Then flex in FT.\n"
             "Wall_Offset measured from: {}\n"
             "Max left travel = {} mm (LR Anchor pinned, Not a Reference)."
             .format(base_name, fmt(anchor_mm)))
forms.alert("\n".join(report) + steps, title=TITLE)
