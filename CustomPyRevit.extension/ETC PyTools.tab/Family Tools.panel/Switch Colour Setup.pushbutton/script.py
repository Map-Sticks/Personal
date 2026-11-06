# -*- coding: utf-8 -*-
"""Switch Colour Setup - pyRevit Script (v3 - Fixed)
========================================
Run this script inside an open INNERMOST switch annotation family in the
Revit Family Editor. It will:

1. Create 3 subcategories: "Switch Colour 1", "Switch Colour 2", "Switch Colour 3"
2. Create an integer instance parameter called "Colour"
3. Create 3 Yes/No parameters ("Colour_Is_1", "Colour_Is_2", "Colour_Is_3")
   driven by formulas: Colour = 1, Colour = 2, Colour = 3
4. Duplicate all existing 2D line geometry twice (Copy to Same Place)
5. Assign each set of lines to one of the 3 subcategories
6. Link each set's visibility to the corresponding Yes/No parameter

After running:
  - Set colours for each subcategory in Object Styles or VG Overrides
  - Daisy-chain the "Colour" parameter up through your nesting levels
"""

__title__ = "Switch\nColour Setup"
__author__ = "pyRevit Script"

# ============================================================================
# IMPORTS
# ============================================================================
import clr
import System
from System.Collections.Generic import List

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")

from Autodesk.Revit.DB import (
    Transaction,
    ElementId,
    CurveElement,
    FilteredElementCollector,
    BuiltInParameter,
    GraphicsStyleType,
    ElementTransformUtils,
    Transform,
    XYZ,
)

from Autodesk.Revit.UI import TaskDialog
from pyrevit import revit, DB, script

# ============================================================================
# PRE-FLIGHT CHECKS
# ============================================================================
doc = revit.doc
uidoc = revit.uidoc

# Must be in a family document
if not doc.IsFamilyDocument:
    TaskDialog.Show(
        "Error",
        "This script must be run inside a Family Editor.\n"
        "Open one of your innermost switch annotation families first."
    )
    script.exit()

output = script.get_output()
fm = doc.FamilyManager

# ============================================================================
# CONFIGURATION - Edit these if you need different names/count
# ============================================================================
SUBCATEGORY_NAMES = ["Switch Colour 1", "Switch Colour 2", "Switch Colour 3"]
COLOUR_PARAM_NAME = "Colour"
YESNO_PARAM_NAMES = ["Colour_Is_1", "Colour_Is_2", "Colour_Is_3"]
YESNO_FORMULAS = ["Colour = 1", "Colour = 2", "Colour = 3"]

# ============================================================================
# HELPER: Get or create a subcategory
# ============================================================================
def get_or_create_subcategory(doc, parent_cat, name):
    """Return the subcategory with the given name, creating it if needed.
    
    FIX: The Revit API method is NewSubcategory (lowercase 'c').
    IronPython sometimes can't resolve it on the Categories object because
    it inherits from CategoryNameMap. We use multiple fallback strategies.
    """
    # Check existing subcategories first
    if parent_cat.SubCategories:
        for subcat in parent_cat.SubCategories:
            if subcat.Name == name:
                return subcat

    cats = doc.Settings.Categories
    
    # ----- ATTEMPT 1: Direct call with correct casing (lowercase 'c') -----
    try:
        new_subcat = cats.NewSubcategory(parent_cat, name)
        return new_subcat
    except Exception:
        pass

    # ----- ATTEMPT 2: .NET reflection on the actual Categories class -----
    # doc.Settings.Categories returns a Categories object, but IronPython
    # sometimes resolves GetType() to the parent class CategoryNameMap.
    # We import the Categories class directly and get its CLR type.
    try:
        from Autodesk.Revit.DB import Categories as RevitCategories
        categories_clr_type = clr.GetClrType(RevitCategories)
        
        # Look for the method with correct name (lowercase 'c')
        method = None
        for m in categories_clr_type.GetMethods():
            if m.Name == "NewSubcategory":
                method = m
                break
        
        if method is not None:
            args = System.Array[System.Object]([parent_cat, name])
            new_subcat = method.Invoke(cats, args)
            return new_subcat
    except Exception:
        pass

    # ----- ATTEMPT 3: Try alternate casing just in case -----
    try:
        from Autodesk.Revit.DB import Categories as RevitCategories
        categories_clr_type = clr.GetClrType(RevitCategories)
        method = categories_clr_type.GetMethod("NewSubCategory")
        if method is not None:
            args = System.Array[System.Object]([parent_cat, name])
            new_subcat = method.Invoke(cats, args)
            return new_subcat
    except Exception:
        pass

    # ----- ATTEMPT 4: Use python getattr with various casings -----
    for method_name in ["NewSubcategory", "NewSubCategory", "newSubcategory"]:
        try:
            func = getattr(cats, method_name)
            new_subcat = func(parent_cat, name)
            return new_subcat
        except Exception:
            continue

    # All attempts failed
    return None


# ============================================================================
# HELPER: Get the GraphicsStyle (LineStyle) for a subcategory
# ============================================================================
def get_line_style_for_subcategory(doc, subcat):
    """Return the GraphicsStyle associated with a subcategory."""
    return subcat.GetGraphicsStyle(GraphicsStyleType.Projection)


# ============================================================================
# HELPER: Create family parameter (version-safe)
# ============================================================================
def add_family_parameter(fm, name, is_integer=False, is_yesno=False, is_instance=True):
    """
    Add a family parameter. Handles API differences between
    Revit versions (pre-2024 vs 2024+).
    """
    # Check if parameter already exists
    existing = fm.get_Parameter(name)
    if existing is not None:
        return existing

    try:
        # Revit 2024+ API (ForgeTypeId based)
        from Autodesk.Revit.DB import SpecTypeId, GroupTypeId

        if is_integer:
            param_type = SpecTypeId.Int.Integer
        elif is_yesno:
            param_type = SpecTypeId.Boolean.YesNo
        else:
            param_type = SpecTypeId.Int.Integer

        param = fm.AddParameter(
            name,
            GroupTypeId.Data,
            param_type,
            is_instance
        )

    except Exception:
        # Pre-2024 API (ParameterType based)
        try:
            from Autodesk.Revit.DB import ParameterType, BuiltInParameterGroup

            if is_integer:
                p_type = ParameterType.Integer
            elif is_yesno:
                p_type = ParameterType.YesNo
            else:
                p_type = ParameterType.Integer

            param = fm.AddParameter(
                name,
                BuiltInParameterGroup.PG_DATA,
                p_type,
                is_instance
            )
        except Exception as e:
            TaskDialog.Show(
                "Error",
                "Failed to create parameter '{}': {}".format(name, str(e))
            )
            return None

    return param


# ============================================================================
# MAIN SCRIPT
# ============================================================================
with revit.Transaction("Switch Colour Setup"):

    # Get the parent category of this family
    parent_category = doc.OwnerFamily.FamilyCategory

    # ------------------------------------------------------------------
    # STEP 1: Create subcategories
    # ------------------------------------------------------------------
    subcategories = []
    failed_subcats = []
    for name in SUBCATEGORY_NAMES:
        subcat = get_or_create_subcategory(doc, parent_category, name)
        if subcat is None:
            failed_subcats.append(name)
        subcategories.append(subcat)

    # If any subcategories failed to create, alert user with manual steps
    if failed_subcats:
        TaskDialog.Show(
            "Manual Step Required",
            "Could not create these subcategories automatically:\n\n"
            + "\n".join(["  - " + n for n in failed_subcats]) +
            "\n\nPlease create them manually:\n"
            "1. Go to Object Styles (Manage tab)\n"
            "2. Under '{}', click New\n".format(parent_category.Name) +
            "3. Add each missing subcategory name EXACTLY as shown above\n"
            "4. Click OK, then re-run this script\n\n"
            "The script will detect the existing subcategories and continue."
        )
        script.exit()

    # Get the line styles (GraphicsStyle) for each subcategory
    line_styles = []
    for subcat in subcategories:
        ls = get_line_style_for_subcategory(doc, subcat)
        line_styles.append(ls)

    # ------------------------------------------------------------------
    # STEP 2: Create the "Colour" integer parameter
    # ------------------------------------------------------------------
    colour_param = add_family_parameter(
        fm, COLOUR_PARAM_NAME, is_integer=True, is_instance=True
    )

    if colour_param is None:
        TaskDialog.Show("Error", "Failed to create Colour parameter. Aborting.")
        script.exit()

    # Set default value to 1
    try:
        fm.Set(colour_param, 1)
    except Exception:
        pass  # May fail if types exist, not critical

    # ------------------------------------------------------------------
    # STEP 3: Create Yes/No parameters with formulas
    # ------------------------------------------------------------------
    yesno_params = []
    for i, yesno_name in enumerate(YESNO_PARAM_NAMES):
        yesno_param = add_family_parameter(
            fm, yesno_name, is_yesno=True, is_instance=True
        )
        if yesno_param is None:
            TaskDialog.Show(
                "Error",
                "Failed to create {} parameter. Aborting.".format(yesno_name)
            )
            script.exit()
        yesno_params.append(yesno_param)

        # Set formula
        try:
            fm.SetFormula(yesno_param, YESNO_FORMULAS[i])
        except Exception as e:
            print("Could not set formula for {}: {}".format(yesno_name, str(e)))

    # ------------------------------------------------------------------
    # STEP 4: Collect all existing line-based geometry (CurveElements)
    # ------------------------------------------------------------------
    all_curves = (
        FilteredElementCollector(doc)
        .OfClass(CurveElement)
        .ToElements()
    )

    # Filter out reference lines — keep only visible sketch lines
    original_ids = []
    for curve in all_curves:
        try:
            # Skip reference lines
            if hasattr(curve, "IsReferenceLine") and curve.IsReferenceLine:
                continue
            original_ids.append(curve.Id)
        except Exception:
            continue

    if not original_ids:
        TaskDialog.Show(
            "Warning",
            "No line geometry found in this family.\n"
            "Make sure you have 2D lines drawn before running this script."
        )
        script.exit()

    # Create a .NET List[ElementId] for the copy operation
    id_collection = List[ElementId](original_ids)

    # ------------------------------------------------------------------
    # STEP 5: Copy geometry twice (to same place)
    # ------------------------------------------------------------------
    copied_ids_2 = None
    copied_ids_3 = None

    try:
        # Preferred: CopyElements(Document, ICollection<ElementId>, XYZ)
        copied_ids_2 = ElementTransformUtils.CopyElements(
            doc, id_collection, XYZ.Zero
        )
        copied_ids_3 = ElementTransformUtils.CopyElements(
            doc, id_collection, XYZ.Zero
        )
    except Exception:
        try:
            # Alternative: CopyElements(Document, ICollection<ElementId>, Transform)
            copied_ids_2 = ElementTransformUtils.CopyElements(
                doc, id_collection, Transform.Identity
            )
            copied_ids_3 = ElementTransformUtils.CopyElements(
                doc, id_collection, Transform.Identity
            )
        except Exception as e:
            TaskDialog.Show(
                "Error",
                "Failed to copy elements: {}\n\n"
                "Try manually copying lines in place and re-run.".format(str(e))
            )
            script.exit()

    # ------------------------------------------------------------------
    # STEP 6: Assign subcategories (LineStyles) to each set
    # ------------------------------------------------------------------
    def assign_line_style(element_ids, line_style):
        """Assign a LineStyle (GraphicsStyle) to a set of CurveElements."""
        count = 0
        for eid in element_ids:
            elem = doc.GetElement(eid)
            if elem is not None and isinstance(elem, CurveElement):
                try:
                    elem.LineStyle = line_style
                    count += 1
                except Exception:
                    pass
        return count

    # Original lines → Switch Colour 1
    count_1 = assign_line_style(original_ids, line_styles[0])

    # First copy → Switch Colour 2
    count_2 = assign_line_style(copied_ids_2, line_styles[1])

    # Second copy → Switch Colour 3
    count_3 = assign_line_style(copied_ids_3, line_styles[2])

    # ------------------------------------------------------------------
    # STEP 7: Associate visibility of each set to Yes/No parameters
    # ------------------------------------------------------------------
    def associate_visibility(element_ids, yesno_param):
        """Link the IS_VISIBLE_PARAM of elements to a Yes/No parameter."""
        count = 0
        for eid in element_ids:
            elem = doc.GetElement(eid)
            if elem is None:
                continue
            try:
                vis_param = elem.get_Parameter(
                    BuiltInParameter.IS_VISIBLE_PARAM
                )
                if vis_param is not None:
                    fm.AssociateElementParameterToFamilyParameter(
                        vis_param, yesno_param
                    )
                    count += 1
            except Exception:
                pass
        return count

    vis_1 = associate_visibility(original_ids, yesno_params[0])
    vis_2 = associate_visibility(copied_ids_2, yesno_params[1])
    vis_3 = associate_visibility(copied_ids_3, yesno_params[2])


# ============================================================================
# SUMMARY
# ============================================================================
summary = (
    "Setup complete!\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "SUBCATEGORIES CREATED:\n"
    "  • Switch Colour 1\n"
    "  • Switch Colour 2\n"
    "  • Switch Colour 3\n\n"
    "PARAMETERS CREATED:\n"
    "  • Colour (Integer) — set 1, 2, or 3\n"
    "  • Colour_Is_1 (Yes/No) — formula: Colour = 1\n"
    "  • Colour_Is_2 (Yes/No) — formula: Colour = 2\n"
    "  • Colour_Is_3 (Yes/No) — formula: Colour = 3\n\n"
    "GEOMETRY:\n"
    "  • Set 1 (originals): {} lines → Switch Colour 1\n"
    "  • Set 2 (copy):      {} lines → Switch Colour 2\n"
    "  • Set 3 (copy):      {} lines → Switch Colour 3\n\n"
    "VISIBILITY LINKED:\n"
    "  • Set 1: {} elements\n"
    "  • Set 2: {} elements\n"
    "  • Set 3: {} elements\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "NEXT STEPS:\n"
    "1. Set subcategory colours in Object Styles\n"
    "2. Load this family back into the middle annotation\n"
    "3. In the middle annotation, add Colour_A/B/C params\n"
    "   and associate each nested family's 'Colour' param\n"
    "4. Daisy-chain Colour_A/B/C up to the Lighting Device\n"
    "5. Repeat this script for each switch type family"
).format(count_1, count_2, count_3, vis_1, vis_2, vis_3)

TaskDialog.Show("Switch Colour Setup", summary)