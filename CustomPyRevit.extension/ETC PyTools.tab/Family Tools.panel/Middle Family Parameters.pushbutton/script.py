# -*- coding: utf-8 -*-
"""Middle Family Parameter Setup - pyRevit Script
========================================
Run this script inside a MIDDLE generic annotation family
(the one that hosts the individual switch type families).

It will create all the Switch and Colour parameters for
1 to 6 positions, matching this pattern per position:

  VISIBILITY GROUP:
    Switch_X  (Integer, Instance, default=1)
    Colour_X  (Integer, Instance, default=1)

  GRAPHICS GROUP:
    Switch_X_Is_1  (Yes/No, formula: Switch_X = 1)
    Switch_X_Is_2  (Yes/No, formula: Switch_X = 2)
    Switch_X_Is_3  (Yes/No, formula: Switch_X = 3)
    Switch_X_Is_4  (Yes/No, formula: Switch_X = 4)
    Switch_X_Is_5  (Yes/No, formula: Switch_X = 5)
    Symbol X - Fan   (Yes/No, formula: Switch_X = 6)
    Symbol X - Neon  (Yes/No, formula: Switch_X = 7)

Parameters that already exist will be skipped.
"""

__title__ = "Middle Family\nParam Setup"
__author__ = "pyRevit Script"

# ============================================================================
# IMPORTS
# ============================================================================
import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")

from System.Windows import Window, SizeToContent, WindowStartupLocation
from System.Windows.Controls import (
    StackPanel, Label, ComboBox, ComboBoxItem, Button
)
from System.Windows import Thickness, HorizontalAlignment, FontWeights

from Autodesk.Revit.DB import (
    BuiltInParameter,
)

from Autodesk.Revit.UI import TaskDialog
from pyrevit import revit, script

# ============================================================================
# PRE-FLIGHT CHECKS
# ============================================================================
doc = revit.doc

if not doc.IsFamilyDocument:
    TaskDialog.Show(
        "Error",
        "This script must be run inside a Family Editor.\n"
        "Open one of your middle annotation families first."
    )
    script.exit()

fm = doc.FamilyManager

# ============================================================================
# POSITION SELECTOR DIALOG
# ============================================================================
class PositionDialog(Window):
    def __init__(self):
        self.Title = "Switch Position Setup"
        self.Width = 320
        self.Height = 180
        self.SizeToContent = SizeToContent.Height
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.ResizeMode = 0  # NoResize
        self.selected_count = 0

        # Main panel
        panel = StackPanel()
        panel.Margin = Thickness(15)

        # Label
        lbl = Label()
        lbl.Content = "How many switch positions?"
        lbl.FontWeight = FontWeights.Bold
        lbl.Margin = Thickness(0, 0, 0, 5)
        panel.Children.Add(lbl)

        # Dropdown
        self.combo = ComboBox()
        self.combo.Margin = Thickness(0, 0, 0, 15)
        for i in range(1, 7):
            item = ComboBoxItem()
            positions = ["A", "B", "C", "D", "E", "F"]
            if i == 1:
                item.Content = "1 position  (A)"
            else:
                item.Content = "{} positions  (A-{})".format(i, positions[i-1])
            self.combo.Items.Add(item)
        self.combo.SelectedIndex = 0
        panel.Children.Add(self.combo)

        # OK Button
        btn = Button()
        btn.Content = "Create Parameters"
        btn.Height = 30
        btn.Click += self.on_ok
        panel.Children.Add(btn)

        self.Content = panel

    def on_ok(self, sender, args):
        self.selected_count = self.combo.SelectedIndex + 1
        self.DialogResult = True
        self.Close()


dialog = PositionDialog()
result = dialog.ShowDialog()

if not result or dialog.selected_count == 0:
    script.exit()

num_positions = dialog.selected_count

# ============================================================================
# HELPER: Create family parameter (version-safe)
# ============================================================================
def add_family_parameter(fm, name, is_integer=False, is_yesno=False, is_instance=True):
    """
    Add a family parameter. Handles API differences between
    Revit versions (pre-2024 vs 2024+).
    Returns the parameter, or None if creation fails.
    Skips if parameter already exists.
    """
    # Check if parameter already exists
    existing = fm.get_Parameter(name)
    if existing is not None:
        return existing, False  # (param, was_created)

    param = None

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
        try:
            # Pre-2024 API (ParameterType based)
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
            return None, False

    return param, True  # (param, was_created)


# ============================================================================
# PARAMETER DEFINITIONS PER POSITION
# ============================================================================
POSITION_LABELS = ["A", "B", "C", "D", "E", "F"]

def get_params_for_position(pos):
    """
    Returns a list of parameter definitions for a given position letter.
    Each entry: (name, is_integer, is_yesno, formula_or_none, default_value_or_none)
    
    Matches the user's working setup:
      - Switch_X (Integer, default 1)
      - Colour_X (Integer, default 1)
      - Switch_X_Is_1 through Switch_X_Is_5 (Yes/No with formulas)
      - Symbol X - Fan (Yes/No, formula: Switch_X = 6)
      - Symbol X - Neon (Yes/No, formula: Switch_X = 7)
    """
    params = []

    # Integer control parameters
    params.append({
        "name": "Switch_{}".format(pos),
        "is_integer": True,
        "is_yesno": False,
        "formula": None,
        "default": 1,
    })
    params.append({
        "name": "Colour_{}".format(pos),
        "is_integer": True,
        "is_yesno": False,
        "formula": None,
        "default": 1,
    })

    # Yes/No driver parameters (Switch_X_Is_1 through Switch_X_Is_5)
    for i in range(1, 6):
        params.append({
            "name": "Switch_{}_Is_{}".format(pos, i),
            "is_integer": False,
            "is_yesno": True,
            "formula": "Switch_{} = {}".format(pos, i),
            "default": None,
        })

    # Symbol X - Fan (Switch_X = 6)
    params.append({
        "name": "Symbol {} - Fan".format(pos),
        "is_integer": False,
        "is_yesno": True,
        "formula": "Switch_{} = 6".format(pos),
        "default": None,
    })

    # Symbol X - Neon (Switch_X = 7)
    params.append({
        "name": "Symbol {} - Neon".format(pos),
        "is_integer": False,
        "is_yesno": True,
        "formula": "Switch_{} = 7".format(pos),
        "default": None,
    })

    return params


# ============================================================================
# MAIN: CREATE ALL PARAMETERS
# ============================================================================
created_params = []
skipped_params = []
failed_params = []
formula_errors = []

with revit.Transaction("Middle Family Param Setup"):

    for pos_index in range(num_positions):
        pos_letter = POSITION_LABELS[pos_index]
        param_defs = get_params_for_position(pos_letter)

        for pdef in param_defs:
            name = pdef["name"]

            # Create the parameter
            param, was_created = add_family_parameter(
                fm,
                name,
                is_integer=pdef["is_integer"],
                is_yesno=pdef["is_yesno"],
                is_instance=True
            )

            if param is None:
                failed_params.append(name)
                continue

            if not was_created:
                skipped_params.append(name)
            else:
                created_params.append(name)

                # Set default value for integer params
                if pdef["default"] is not None:
                    try:
                        fm.Set(param, pdef["default"])
                    except Exception:
                        pass

            # Set formula for Yes/No params
            if pdef["formula"] is not None:
                try:
                    fm.SetFormula(param, pdef["formula"])
                except Exception as e:
                    formula_errors.append(
                        "{}: {}".format(name, str(e))
                    )


# ============================================================================
# SUMMARY
# ============================================================================
positions_text = ", ".join(POSITION_LABELS[:num_positions])

summary = "Setup complete for {} position(s): {}\n".format(
    num_positions, positions_text
)
summary += "━" * 40 + "\n\n"

# Created
summary += "CREATED ({}):\n".format(len(created_params))
if created_params:
    for p in created_params:
        summary += "  + {}\n".format(p)
else:
    summary += "  (none — all already existed)\n"

# Skipped
summary += "\nSKIPPED - ALREADY EXISTED ({}):\n".format(len(skipped_params))
if skipped_params:
    for p in skipped_params:
        summary += "  ~ {}\n".format(p)
else:
    summary += "  (none)\n"

# Failed
if failed_params:
    summary += "\nFAILED ({}):\n".format(len(failed_params))
    for p in failed_params:
        summary += "  ! {}\n".format(p)

# Formula errors
if formula_errors:
    summary += "\nFORMULA ERRORS:\n"
    for e in formula_errors:
        summary += "  ! {}\n".format(e)

summary += "\n" + "━" * 40 + "\n"
summary += "NEXT STEPS:\n"
summary += "1. Select each nested switch family instance\n"
summary += "2. Associate its 'Visible' to the matching\n"
summary += "   Switch_X_Is_N / Symbol X - Fan/Neon param\n"
summary += "3. Associate its 'Colour' param to Colour_X\n"
summary += "4. Load into the Lighting Device family\n"
summary += "5. Link Switch_X and Colour_X up to the top level"

TaskDialog.Show("Middle Family Param Setup", summary)