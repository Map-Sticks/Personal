# -*- coding: utf-8 -*-
"""Circuit Hub v11.3 - compact circuit manager with category filter and pick-panel-in-view."""
__title__ = "Circuit\nHub"
__author__ = "Jacques-Michael"
__persistentengine__ = True

import os
import re
import json
import traceback

import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System import Action
from System.Collections.Generic import List
from System.Windows import MessageBox, MessageBoxButton, MessageBoxImage, Thickness, FontWeights, VerticalAlignment, DragDrop, DragDropEffects, DataObject
from System.Windows.Controls import ListBoxItem, StackPanel, Button, TextBlock, ItemsControl
from System.Windows import Controls
from System.Windows.Input import Keyboard, Key, ModifierKeys, MouseButtonState
from System.Windows.Media import Brushes, VisualTreeHelper

from pyrevit import revit, forms, script
from Autodesk.Revit.DB import ElementId, ElementSet, Transaction, FilteredElementCollector, BuiltInCategory, BuiltInParameter, Level
from Autodesk.Revit.DB.Electrical import ElectricalSystem, ElectricalSystemType, PanelScheduleView, PanelScheduleTemplate
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent, RevitCommandId, PostableCommand
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

doc = revit.doc
uidoc = revit.uidoc
APP = None
DOC_CHANGED_HANDLER = None
ELEMENT_LEVEL_CACHE = {}
PROJECT_LEVELS_CACHE = []

DEFAULT_ALLOWED_CATEGORIES = [
    "Electrical Fixtures",
    "Lighting Fixtures",
    "Electrical Equipment",
    "Mechanical Equipment",
    "Specialty Equipment"
]


def settings_file_path():
    try:
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
        folder = os.path.join(root, "CircuitHub")
        if not os.path.exists(folder):
            os.makedirs(folder)
        return os.path.join(folder, "circuit_hub_settings.json")
    except Exception:
        return os.path.join(os.path.dirname(__file__), "circuit_hub_settings.json")

SETTINGS_FILE = settings_file_path()


def default_settings():
    return {
        "level_filter": "<All Levels>",
        "category_filter_enabled": True,
        "allowed_categories": list(DEFAULT_ALLOWED_CATEGORIES),
        "show_empty_panels": False,
        "auto_refresh_enabled": False
    }


def load_settings():
    data = default_settings()
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data.update(loaded)
    except Exception:
        pass
    return data


def save_settings(data):
    try:
        current = load_settings()
        current.update(data)
        folder = os.path.dirname(SETTINGS_FILE)
        if folder and not os.path.exists(folder):
            os.makedirs(folder)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(current, f, indent=2, sort_keys=True)
        if os.path.exists(SETTINGS_FILE):
            try:
                os.remove(SETTINGS_FILE)
            except Exception:
                pass
        os.rename(tmp, SETTINGS_FILE)
        return True
    except Exception:
        print("CircuitHub settings save failed")
        print(traceback.format_exc())
        return False


def show_error(title, msg):
    try:
        MessageBox.Show(str(msg), title, MessageBoxButton.OK, MessageBoxImage.Error)
    except Exception:
        print(title)
        print(msg)


def show_warning(title, msg):
    try:
        MessageBox.Show(str(msg), title, MessageBoxButton.OK, MessageBoxImage.Warning)
    except Exception:
        print(title)
        print(msg)


class UserFacingError(Exception):
    pass


def clean_revit_validation_message(msg):
    text = str(msg or "")
    lower = text.lower()
    if "circular connection" in lower:
        return "Revit blocked this panel assignment because it would create a circular electrical connection.\n\nNo circuits were moved.\n\nChoose a different target panel."
    if "there should be at least one component that can create the specified circuit type" in lower:
        return "Revit could not create a power circuit from the selected element(s).\n\nThe selection may include a tag, annotation, linked item, non-electrical family, or a family without a valid power connector."
    if "cannot create the specified circuit type" in lower:
        return "Revit could not create the requested circuit type from the selected element(s)."
    return None


def as_eid(x):
    return x if isinstance(x, ElementId) else ElementId(int(x))


def id_list(items):
    result = List[ElementId]()
    for x in items:
        result.Add(as_eid(x))
    return result


def element_set_from_ids(items):
    result = ElementSet()
    count = 0
    for x in items:
        el = doc.GetElement(as_eid(x))
        if el is not None:
            result.Insert(el)
            count += 1
    return result, count


def safe_name(el, fallback=""):
    if el is None:
        return fallback
    try:
        n = el.Name
        if n:
            return n
    except Exception:
        pass
    try:
        return str(el.Id.IntegerValue)
    except Exception:
        return fallback


def category_name(el):
    try:
        if el is not None and el.Category:
            return el.Category.Name
    except Exception:
        pass
    return "<No Category>"


def element_label(el):
    return "{0}: {1} ({2})".format(category_name(el), safe_name(el), el.Id.IntegerValue)


def get_load_name(circuit):
    try:
        p = circuit.get_Parameter(BuiltInParameter.RBS_ELEC_CIRCUIT_NAME)
        return (p.AsString() or "") if p else ""
    except Exception:
        return ""


def set_load_name(circuit, value):
    p = circuit.get_Parameter(BuiltInParameter.RBS_ELEC_CIRCUIT_NAME)
    if p and not p.IsReadOnly:
        p.Set(value or "")
        return True
    return False


def circuit_number(circuit):
    try:
        return circuit.CircuitNumber or "?"
    except Exception:
        return "?"


def panel_id_of(circuit):
    try:
        panel = circuit.BaseEquipment
        return panel.Id.IntegerValue if panel else None
    except Exception:
        return None


def circuit_is_on_panel(circuit, panel_id_value):
    try:
        return panel_id_of(circuit) == panel_id_value
    except Exception:
        return False


def circuit_label(circuit):
    ln = get_load_name(circuit)
    return "[{0}] {1}".format(circuit_number(circuit), ln) if ln else "[{0}]".format(circuit_number(circuit))


def circuit_sort_key(circuit):
    """Natural circuit-number sort: 1, 2, 10 instead of 1, 10, 2."""
    try:
        num = circuit_number(circuit) or ""
    except Exception:
        num = ""
    parts = re.findall(r"\d+|\D+", str(num))
    key = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return (key, circuit_label(circuit).lower())


def nearest_level_name_from_bbox(el):
    try:
        bb = el.get_BoundingBox(None)
        if bb is None:
            return None
        z = (bb.Min.Z + bb.Max.Z) / 2.0
        levels = PROJECT_LEVELS_CACHE
        if not levels:
            return None
        nearest = None
        best = None
        for lvl in levels:
            try:
                d = abs(float(lvl.Elevation) - float(z))
                if best is None or d < best:
                    best = d
                    nearest = lvl
            except Exception:
                pass
        if nearest is not None:
            return safe_name(nearest, "<No Level>")
    except Exception:
        pass
    return None


def element_level_name(el):
    """Best effort level for fixtures on unassigned circuits.
    Uses direct LevelId, common level parameters, host level, then nearest project level by bounding box.
    Cached per Cache.build() to avoid repeated parameter/bounding-box scans.
    """
    if el is None:
        return remember_level_name("<No Level>")
    try:
        eid_int = el.Id.IntegerValue
        if eid_int in ELEMENT_LEVEL_CACHE:
            return ELEMENT_LEVEL_CACHE[eid_int]
    except Exception:
        eid_int = None
    def remember_level_name(value):
        try:
            if eid_int is not None:
                ELEMENT_LEVEL_CACHE[eid_int] = value
        except Exception:
            pass
        return value
    try:
        level_id = el.LevelId
        if level_id and level_id.IntegerValue > 0:
            level = doc.GetElement(level_id)
            if level:
                return remember_level_name(safe_name(level, "<No Level>"))
    except Exception:
        pass
    param_names = [
        "Level", "Schedule Level", "Reference Level", "Associated Level", "Host Level",
        "FAMILY_LEVEL_PARAM", "SCHEDULE_LEVEL_PARAM", "INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM",
        "RBS_START_LEVEL_PARAM", "INSTANCE_REFERENCE_LEVEL_PARAM"
    ]
    for name in param_names:
        try:
            if name.isupper():
                p = el.get_Parameter(getattr(BuiltInParameter, name))
            else:
                p = el.LookupParameter(name)
            if p and p.HasValue:
                try:
                    eid = p.AsElementId()
                    if eid and eid.IntegerValue > 0:
                        level = doc.GetElement(eid)
                        if level:
                            return remember_level_name(safe_name(level, "<No Level>"))
                except Exception:
                    pass
                try:
                    txt = p.AsValueString() or p.AsString()
                    if txt:
                        return remember_level_name(txt)
                except Exception:
                    pass
        except Exception:
            pass
    try:
        host = el.Host
        lvl = element_level_name(host)
        if lvl and lvl != "<No Level>":
            return remember_level_name(lvl)
    except Exception:
        pass
    bbox_level = nearest_level_name_from_bbox(el)
    if bbox_level:
        return remember_level_name(bbox_level)
    return remember_level_name("<No Level>")


def circuit_level_name(circuit):
    levels = []
    try:
        for el in circuit.Elements:
            lvl = element_level_name(el)
            if lvl:
                levels.append(lvl)
    except Exception:
        pass
    # Prefer a real level over <No Level>. If multiple levels exist, use the first sorted value.
    real = sorted(list(set([x for x in levels if x and x != "<No Level>"])))
    if real:
        return real[0]
    return "<No Level>"


def unassigned_key_for_circuit(circuit):
    return ("unassigned", circuit_level_name(circuit))


def is_unassigned_panel_key(key):
    try:
        return isinstance(key, tuple) and len(key) == 2 and key[0] == "unassigned"
    except Exception:
        return False


def unassigned_panel_label(key):
    try:
        return "UNASSIGNED / NO PANEL - {0}".format(key[1])
    except Exception:
        return "UNASSIGNED / NO PANEL - <No Level>"


def panel_level_name(panel):
    if panel is None:
        return "<No Level>"
    try:
        level_id = panel.LevelId
        if level_id and level_id.IntegerValue > 0:
            level = doc.GetElement(level_id)
            if level:
                return safe_name(level, "<No Level>")
    except Exception:
        pass
    for bip_name in ["FAMILY_LEVEL_PARAM", "SCHEDULE_LEVEL_PARAM", "INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM", "RBS_START_LEVEL_PARAM"]:
        try:
            bip = getattr(BuiltInParameter, bip_name)
            p = panel.get_Parameter(bip)
            if p and p.HasValue:
                try:
                    eid = p.AsElementId()
                    if eid and eid.IntegerValue > 0:
                        level = doc.GetElement(eid)
                        if level:
                            return safe_name(level, "<No Level>")
                except Exception:
                    pass
                try:
                    txt = p.AsValueString() or p.AsString()
                    if txt:
                        return txt
                except Exception:
                    pass
        except Exception:
            pass
    return "<No Level>"


def run_transaction(name, work):
    t = Transaction(doc, "CircuitHub: " + name)
    t.Start()
    try:
        msg = work()
        t.Commit()
        return msg
    except Exception:
        t.RollBack()
        raise


# ---------------- Selection filters ----------------
class ElectricalEquipmentSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        try:
            return element is not None and element.Category is not None and element.Category.Id.IntegerValue == int(BuiltInCategory.OST_ElectricalEquipment)
        except Exception:
            return False

    def AllowReference(self, reference, point):
        return False


# ---------------- Phase helpers ----------------
def lookup_param(el, names):
    if el is None:
        return None
    for name in names:
        try:
            p = el.LookupParameter(name)
            if p and p.HasValue:
                return p
        except Exception:
            pass
    return None


def param_number(el, names):
    p = lookup_param(el, names)
    if p is None:
        return None
    try:
        v = p.AsInteger()
        if v not in (None, 0):
            return float(v)
    except Exception:
        pass
    try:
        txt = p.AsValueString() or p.AsString() or ""
        m = re.search(r"[-+]?\d+(?:\.\d+)?", txt)
        if m:
            return float(m.group(0))
    except Exception:
        pass
    try:
        v = p.AsDouble()
        if v not in (None, 0):
            return float(v)
    except Exception:
        pass
    return None


def element_type(el):
    try:
        return doc.GetElement(el.GetTypeId())
    except Exception:
        return None


def poles_of(el):
    names = ["Number of Poles", "No. of Poles", "Poles", "Pole", "Number Of Poles"]
    value = param_number(el, names)
    if value is None:
        value = param_number(element_type(el), names)
    return int(round(value)) if value is not None else None


def explicit_phase_value(el):
    names = ["Number of Phases", "No. of Phases", "Phases", "Phase", "Phase Count", "Number Of Phases"]
    p = lookup_param(el, names)
    if p is None:
        p = lookup_param(element_type(el), names)
    if p is None:
        return None
    try:
        v = p.AsInteger()
        if v in (1, 2, 3):
            return int(v)
    except Exception:
        pass
    try:
        txt = (p.AsValueString() or p.AsString() or "").lower()
        if "single" in txt or "1ph" in txt or "1 phase" in txt:
            return 1
        if "two" in txt or "2ph" in txt or "2 phase" in txt:
            return 2
        if "three" in txt or "3ph" in txt or "3 phase" in txt:
            return 3
        m = re.search(r"\d+", txt)
        if m:
            v = int(m.group(0))
            if v in (1, 2, 3):
                return v
    except Exception:
        pass
    return None


def phase_class_direct(el):
    explicit = explicit_phase_value(el)
    if explicit is not None:
        return explicit
    poles = poles_of(el)
    if poles is not None:
        if poles <= 1:
            return 1
        if poles == 2:
            return 2
        if poles >= 3:
            return 3
    return None


def phase_class(el):
    direct = phase_class_direct(el)
    if direct is not None:
        return direct
    try:
        phases = []
        for sub in el.Elements:
            sub_phase = phase_class_direct(sub)
            if sub_phase is not None:
                phases.append(sub_phase)
        unique = sorted(list(set(phases)))
        if len(unique) == 1:
            return unique[0]
    except Exception:
        pass
    return None


def phase_text(value):
    if value == 1:
        return "single-phase"
    if value == 2:
        return "two-phase"
    if value == 3:
        return "three-phase"
    return "unknown phase"


class Cache(object):
    def __init__(self):
        self.panels = {}
        self.circuits = []
        self.templates = {}
        self.level_names = []
        self.category_names = []

    def build(self):
        global ELEMENT_LEVEL_CACHE
        global PROJECT_LEVELS_CACHE
        ELEMENT_LEVEL_CACHE = {}
        try:
            PROJECT_LEVELS_CACHE = list(FilteredElementCollector(doc).OfClass(Level))
        except Exception:
            PROJECT_LEVELS_CACHE = []
        self.panels = {}
        self.circuits = []
        self.templates = {}
        cat_set = set()
        for panel in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_ElectricalEquipment).WhereElementIsNotElementType():
            self.panels[panel.Id.IntegerValue] = panel
            cat_set.add(category_name(panel))
        level_set = set([panel_level_name(p) for p in self.panels.values()])
        for es in FilteredElementCollector(doc).OfClass(ElectricalSystem):
            try:
                if es.SystemType == ElectricalSystemType.PowerCircuit:
                    self.circuits.append(es)
            except Exception:
                self.circuits.append(es)
            try:
                for el in es.Elements:
                    cat_set.add(category_name(el))
            except Exception:
                pass
        # Include levels from unassigned circuit fixtures so level filter can find dummy groups.
        # Only inspect unassigned circuits; scanning every assigned circuit is expensive in large models.
        for c in self.circuits:
            try:
                if panel_id_of(c) is None:
                    lvl = circuit_level_name(c)
                    if lvl:
                        level_set.add(lvl)
            except Exception:
                pass
        self.level_names = sorted(list(level_set))
        for name in DEFAULT_ALLOWED_CATEGORIES:
            cat_set.add(name)
        self.category_names = sorted([x for x in cat_set if x])
        for tpl in FilteredElementCollector(doc).OfClass(PanelScheduleTemplate):
            self.templates[safe_name(tpl)] = tpl

    def grouped(self):
        groups = {}
        # Add all real panels so empty panels can be shown/hidden by the checkbox.
        for pid in self.panels.keys():
            groups.setdefault(pid, [])
        for c in self.circuits:
            pid = panel_id_of(c)
            if pid is None:
                key = unassigned_key_for_circuit(c)
            else:
                key = pid
            groups.setdefault(key, []).append(c)
        return groups

CACHE = Cache()


def find_circuits_containing(eid_int):
    result = []
    for circuit in CACHE.circuits:
        try:
            for el in circuit.Elements:
                if el.Id.IntegerValue == eid_int:
                    result.append(circuit)
                    break
        except Exception:
            pass
    return result


def elements_with_existing_circuits(element_ids):
    result = []
    for eid in element_ids:
        circuits = find_circuits_containing(eid)
        if circuits:
            result.append((eid, circuits))
    return result


def describe_existing_memberships(existing):
    lines = []
    for eid, circuits in existing:
        el = doc.GetElement(ElementId(eid))
        cn = ", ".join([circuit_number(c) for c in circuits])
        lines.append("- {0} is already on circuit(s): {1}".format(element_label(el) if el else eid, cn))
    return "\n".join(lines)


def phase_class_from_existing_membership(eid):
    phases = []
    for c in find_circuits_containing(eid):
        p = phase_class(c)
        if p is not None:
            phases.append(p)
    unique = sorted(list(set(phases)))
    return unique[0] if len(unique) == 1 else None


def validate_phase_compatibility(target_circuit, element_ids):
    target_phase = phase_class(target_circuit)
    conflicts = []
    for eid in element_ids:
        el = doc.GetElement(ElementId(eid))
        if el is None:
            continue
        el_phase = phase_class(el)
        if el_phase is None:
            el_phase = phase_class_from_existing_membership(eid)
        if target_phase is None or el_phase is None:
            continue
        if target_phase != el_phase:
            conflicts.append((element_label(el), el_phase, target_phase))
    if conflicts:
        lines = ["Phase conflict detected before transaction:"]
        for label, el_phase, target_phase in conflicts:
            lines.append("- {0}: fixture is {1}, target circuit is {2}".format(label, phase_text(el_phase), phase_text(target_phase)))
        raise UserFacingError("\n".join(lines))


class CircuitHubExternalEventHandler(IExternalEventHandler):
    def Execute(self, app):
        try:
            if APP is not None:
                APP.execute_pending_external_event()
        except Exception:
            print(traceback.format_exc())

    def GetName(self):
        return "CircuitHub External Event"


class CircuitHub(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, script.get_bundle_file("ui.xaml"))
        self.settings = load_settings()
        self.target_circuit_id = None
        self.expanded_panel_ids = set()
        self.expanded_circuit_ids = set()
        self.pending_select_tags = []
        self.doc_refresh_pending = False
        self.drag_tags = []
        self.drag_hover_item = None
        self.alt_drag_preserved_tags = []
        self.pending_external_action = None
        self.external_event_handler = CircuitHubExternalEventHandler()
        self.external_event = ExternalEvent.Create(self.external_event_handler)
        self.level_filter = self.settings.get("level_filter", "<All Levels>")
        self.category_filter_enabled = bool(self.settings.get("category_filter_enabled", True))
        self.allowed_categories = list(self.settings.get("allowed_categories", DEFAULT_ALLOWED_CATEGORIES))
        self.show_empty_panels = bool(self.settings.get("show_empty_panels", False))
        self.auto_refresh_enabled = bool(self.settings.get("auto_refresh_enabled", False))
        self.building = False
        self.wire_events()
        self.Closed += self.on_closed
        self.PreviewKeyDown += self.on_preview_key_down
        CACHE.build()
        self.populate_level_filter()
        self.sync_category_filter_ui()
        self.build_list()
        self.log("Circuit Hub ready. Category filter is {0}. Empty panels are {1}. Auto-refresh is {2}. Loaded categories: {3}. Settings file: {4}".format(
            "ON" if self.category_filter_enabled else "OFF",
            "shown" if self.show_empty_panels else "hidden",
            "ON" if self.auto_refresh_enabled else "OFF",
            ", ".join(self.allowed_categories) if self.allowed_categories else "<none>", SETTINGS_FILE))

    def wire_events(self):
        self.btnRefresh.Click += self.on_refresh
        self.btnUndoLast.Click += self.on_undo_last
        self.btnLocateRevitSelection.Click += self.on_locate_revit_selection
        self.btnShowAllPanels.Click += self.on_show_all_panels
        self.btnShowTargetOnly.Click += self.on_show_target_only
        self.btnExpandSelected.Click += self.on_expand_selected
        self.btnCollapseSelected.Click += self.on_collapse_selected
        self.btnClose.Click += self.on_close
        self.tbSearch.TextChanged += self.on_search_changed
        self.cmbLevelFilter.SelectionChanged += self.on_level_filter_changed
        self.chkShowEmptyPanels.Checked += self.on_show_empty_panels_changed
        self.chkShowEmptyPanels.Unchecked += self.on_show_empty_panels_changed
        self.chkAutoRefresh.Checked += self.on_auto_refresh_changed
        self.chkAutoRefresh.Unchecked += self.on_auto_refresh_changed
        self.chkCategoryFilter.Checked += self.on_category_filter_checked_changed
        self.chkCategoryFilter.Unchecked += self.on_category_filter_checked_changed
        self.btnCategoryFilter.Click += self.on_category_filter_settings
        self.btnApplyCategoryFilter.Click += self.on_apply_category_filter_to_selection
        self.lbItems.SelectionChanged += self.on_list_selection_changed
        self.lbItems.PreviewMouseLeftButtonDown += self.on_preview_mouse_left_button_down
        self.lbItems.PreviewMouseMove += self.on_preview_mouse_move
        self.lbItems.Drop += self.on_drop
        self.lbItems.DragOver += self.on_drag_over
        self.lbItems.DragLeave += self.on_drag_leave
        self.lbItems.MouseDoubleClick += self.on_list_double_click
        self.btnSelectRowsInView.Click += self.on_select_rows_in_view
        self.btnMoveSelectedCircuits.Click += self.on_move_selected_circuits
        self.btnRemoveSelectedFixtureRows.Click += self.on_remove_selected_fixture_rows
        self.btnRenameLoad.Click += self.on_rename_load
        self.btnMoveTargetPanel.Click += self.on_move_target_panel
        self.btnPickPanelInView.Click += self.on_pick_panel_in_view_for_selected_fixture_circuits
        self.btnAddRevitSelection.Click += self.on_add_revit_selection
        self.btnReassignRevitSelection.Click += self.on_reassign_revit_selection
        self.btnMoveCircuitsToPanel.Click += self.on_move_existing_circuits_to_panel
        self.btnRemoveRevitSelection.Click += self.on_remove_revit_selection
        self.btnRemoveSelectionFromAllCircuits.Click += self.on_remove_revit_selection_from_all_circuits
        self.btnCreateCircuit.Click += self.on_create_circuit
        self.btnCreateSchedule.Click += self.on_create_schedule

    # ---------------- settings / filter ----------------
    def save_current_settings(self):
        return save_settings({
            "level_filter": self.level_filter or "<All Levels>",
            "category_filter_enabled": bool(self.category_filter_enabled),
            "allowed_categories": list(self.allowed_categories or []),
            "show_empty_panels": bool(self.show_empty_panels),
            "auto_refresh_enabled": bool(self.auto_refresh_enabled)
        })

    def sync_category_filter_ui(self):
        try:
            self.chkCategoryFilter.IsChecked = bool(self.category_filter_enabled)
            self.chkShowEmptyPanels.IsChecked = bool(self.show_empty_panels)
            self.chkAutoRefresh.IsChecked = bool(self.auto_refresh_enabled)
            self.txtCategoryFilterSummary.Text = self.category_filter_summary()
        except Exception:
            pass

    def category_filter_summary(self):
        if not self.category_filter_enabled:
            return "Category filter: OFF"
        if not self.allowed_categories:
            return "Category filter: ON, no categories allowed"
        return "Category filter: ON ({0})".format(", ".join(self.allowed_categories))

    def on_category_filter_checked_changed(self, sender, args):
        if self.building:
            return
        try:
            self.category_filter_enabled = bool(self.chkCategoryFilter.IsChecked)
        except Exception:
            self.category_filter_enabled = True
        self.save_current_settings()
        self.sync_category_filter_ui()
        self.log(self.category_filter_summary())

    def on_category_filter_settings(self, sender, args):
        choices = list(CACHE.category_names)
        for name in list(self.allowed_categories or []):
            if name not in choices:
                choices.append(name)
        choices = sorted(choices)
        class CategoryOption(forms.TemplateListItem):
            @property
            def name(self):
                return str(self.item)
        try:
            options = [CategoryOption(x, checked=(x in (self.allowed_categories or []))) for x in choices]
            picked = forms.SelectFromList.show(options, title="Circuit Hub Category Filter", button_name="Save filter", multiselect=True)
            if picked is None:
                self.log("Category filter edit cancelled.")
                return
            selected = []
            for item in picked:
                try:
                    selected.append(str(item.item))
                except Exception:
                    selected.append(str(item))
        except Exception:
            picked = forms.SelectFromList.show(choices, title="Circuit Hub Category Filter", button_name="Save filter", multiselect=True)
            if picked is None:
                self.log("Category filter edit cancelled.")
                return
            selected = [str(x) for x in picked]
        self.allowed_categories = list(selected)
        self.category_filter_enabled = True
        saved = self.save_current_settings()
        self.sync_category_filter_ui()
        self.log("Saved category filter: {0}. Settings file: {1}.{2}".format(
            ", ".join(self.allowed_categories) if self.allowed_categories else "<none>", SETTINGS_FILE, "" if saved else " WARNING: settings did not save"))

    def on_apply_category_filter_to_selection(self, sender, args):
        try:
            self.filtered_revit_selection_ids("manual filter", require_nonempty=False)
        except UserFacingError as ex:
            show_warning("Circuit Hub", str(ex))

    def filtered_revit_selection_ids(self, action_name, require_nonempty=True):
        selected = list(uidoc.Selection.GetElementIds())
        selected_ids = [x.IntegerValue for x in selected]
        if not selected_ids:
            if require_nonempty:
                raise UserFacingError("Select element(s) in Revit first.")
            self.log("No Revit selection to filter.")
            return []
        if not self.category_filter_enabled:
            return selected_ids
        allowed = set([str(x) for x in (self.allowed_categories or [])])
        allowed_ids = []
        rejected = []
        for eid in selected_ids:
            el = doc.GetElement(ElementId(eid))
            cname = category_name(el)
            if cname in allowed:
                allowed_ids.append(eid)
            else:
                rejected.append((eid, cname))
        uidoc.Selection.SetElementIds(id_list(allowed_ids))
        if rejected:
            summary = {}
            for eid, cname in rejected:
                summary[cname] = summary.get(cname, 0) + 1
            parts = ["{0}: {1}".format(k, summary[k]) for k in sorted(summary.keys())]
            self.log("Category filter removed {0} item(s) before {1}: {2}".format(len(rejected), action_name, "; ".join(parts)))
        else:
            self.log("Category filter: all {0} selected item(s) allowed for {1}.".format(len(allowed_ids), action_name))
        if require_nonempty and not allowed_ids:
            raise UserFacingError("The category filter removed every selected item. Use Category filter... to enable the required Revit categories.")
        return allowed_ids

    # ---------------- UI list ----------------
    def populate_level_filter(self):
        try:
            current = self.level_filter or "<All Levels>"
            self.cmbLevelFilter.Items.Clear()
            self.cmbLevelFilter.Items.Add("<All Levels>")
            self.cmbLevelFilter.Items.Add("<No Level>")
            for name in CACHE.level_names:
                if name != "<No Level>":
                    self.cmbLevelFilter.Items.Add(name)
            selected_index = 0
            for i in range(self.cmbLevelFilter.Items.Count):
                if str(self.cmbLevelFilter.Items[i]) == current:
                    selected_index = i
                    break
            self.cmbLevelFilter.SelectedIndex = selected_index
            self.level_filter = str(self.cmbLevelFilter.SelectedItem)
        except Exception:
            pass

    def on_level_filter_changed(self, sender, args):
        if self.building:
            return
        try:
            self.level_filter = str(self.cmbLevelFilter.SelectedItem) if self.cmbLevelFilter.SelectedItem else "<All Levels>"
        except Exception:
            self.level_filter = "<All Levels>"
        self.save_current_settings()
        self.build_list()

    def on_show_empty_panels_changed(self, sender, args):
        if self.building:
            return
        try:
            self.show_empty_panels = bool(self.chkShowEmptyPanels.IsChecked)
        except Exception:
            self.show_empty_panels = False
        self.save_current_settings()
        self.build_list()
        self.log("Empty panels are {0}.".format("shown" if self.show_empty_panels else "hidden"))

    def on_auto_refresh_changed(self, sender, args):
        if self.building:
            return
        try:
            self.auto_refresh_enabled = bool(self.chkAutoRefresh.IsChecked)
        except Exception:
            self.auto_refresh_enabled = False
        self.save_current_settings()
        self.log("Auto-refresh after Revit model changes is {0}.".format("ON" if self.auto_refresh_enabled else "OFF"))

    def log(self, msg):
        try:
            self.txtStatus.Text = str(msg)
            self.lbLog.Items.Insert(0, str(msg))
        except Exception:
            print(msg)

    def on_closed(self, sender, args):
        global DOC_CHANGED_HANDLER
        self.save_current_settings()
        try:
            if DOC_CHANGED_HANDLER is not None:
                uidoc.Application.Application.DocumentChanged -= DOC_CHANGED_HANDLER
                DOC_CHANGED_HANDLER = None
        except Exception:
            pass

    def on_preview_key_down(self, sender, args):
        try:
            if args.Key == Key.Z and (Keyboard.Modifiers & ModifierKeys.Control) == ModifierKeys.Control:
                args.Handled = True
                self.on_undo_last(None, None)
        except Exception:
            pass

    def queue_refresh_from_doc_changed(self):
        try:
            # Performance guard: do not rebuild the full circuit cache on every Revit transaction
            # unless the user explicitly enables auto-refresh. CircuitHub actions still refresh
            # immediately after they complete.
            if not self.auto_refresh_enabled:
                return
            if self.doc_refresh_pending:
                return
            self.doc_refresh_pending = True
            def do_refresh():
                self.doc_refresh_pending = False
                try:
                    CACHE.build()
                    self.populate_level_filter()
                    self.sync_category_filter_ui()
                    self.build_list()
                    self.log("Model changed / Undo-Redo detected: refreshed.")
                except Exception:
                    print(traceback.format_exc())
            try:
                self.Dispatcher.BeginInvoke(Action(do_refresh))
            except Exception:
                do_refresh()
        except Exception:
            print(traceback.format_exc())

    def on_undo_last(self, sender, args):
        try:
            cmd_id = RevitCommandId.LookupPostableCommandId(PostableCommand.Undo)
            uidoc.Application.PostCommand(cmd_id)
            if self.auto_refresh_enabled:
                self.log("Undo requested. The list will refresh when Revit reports the document change.")
            else:
                self.log("Undo requested. Auto-refresh is OFF; click Refresh after Revit completes the undo if the tree is stale.")
        except Exception:
            show_error("Circuit Hub Undo Error", traceback.format_exc())

    def on_close(self, sender, args): self.Close()
    def on_refresh(self, sender, args): self.refresh()
    def on_search_changed(self, sender, args): self.build_list()

    def refresh(self):
        CACHE.build()
        self.populate_level_filter()
        self.sync_category_filter_ui()
        self.build_list()

    def make_text(self, text, bold=False, fg=None):
        tb = TextBlock()
        tb.Text = text
        tb.VerticalAlignment = VerticalAlignment.Center
        tb.Margin = Thickness(4, 0, 0, 0)
        if bold: tb.FontWeight = FontWeights.Bold
        if fg is not None: tb.Foreground = fg
        return tb

    def make_small_button(self, text, width, tag, handler):
        b = Button(); b.Content = text; b.Width = width; b.Height = 22; b.Margin = Thickness(0, 0, 4, 0); b.Tag = tag; b.Click += handler
        return b

    def style_item(self, item, tag, hover=False, invalid=False):
        try:
            item.Padding = Thickness(4, 2, 4, 2); item.Margin = Thickness(0, 1, 0, 1)
            item.Foreground = Brushes.Black
            if tag[0] == "panel": item.Background = Brushes.Gainsboro
            elif tag[0] == "unassigned_panel": item.Background = Brushes.LemonChiffon; item.Foreground = Brushes.DarkOrange
            elif tag[0] in ("circuit", "addrow"): item.Background = Brushes.AliceBlue
            elif tag[0] == "element": item.Background = Brushes.White; item.Foreground = Brushes.DimGray
            if invalid:
                item.Background = Brushes.MistyRose; item.Foreground = Brushes.DarkRed; item.FontWeight = FontWeights.Bold
            elif hover:
                item.Background = Brushes.LightGreen; item.Foreground = Brushes.Black; item.FontWeight = FontWeights.Bold
            else:
                item.FontWeight = FontWeights.Normal
        except Exception: pass

    def make_item(self, text, tag):
        item = ListBoxItem(); item.Tag = tag; self.style_item(item, tag)
        sp = StackPanel(); sp.Orientation = Controls.Orientation.Horizontal
        if tag[0] == "circuit":
            sp.Children.Add(self.make_small_button("+", 26, tag, self.on_inline_add_to_circuit)); sp.Children.Add(self.make_text(text, True, None))
        elif tag[0] == "element":
            sp.Children.Add(self.make_small_button("-", 26, tag, self.on_inline_remove_fixture)); sp.Children.Add(self.make_text(text, False, Brushes.DimGray))
        elif tag[0] == "addrow":
            sp.Children.Add(self.make_small_button("+", 26, tag, self.on_inline_add_to_circuit)); sp.Children.Add(self.make_text(text, False, Brushes.DimGray))
        else:
            sp.Children.Add(self.make_text(text, True, None))
        item.Content = sp
        return item

    def build_list(self):
        self.building = True
        flt = (self.tbSearch.Text or "").strip().lower()
        self.lbItems.Items.Clear()
        groups = CACHE.grouped()
        panel_keys = [x for x in groups.keys() if x is not None]
        def panel_sort_key(x):
            if is_unassigned_panel_key(x):
                return (1, x[1].lower())
            return (0, safe_name(CACHE.panels.get(x)).lower())
        panel_keys.sort(key=panel_sort_key)
        if None in groups: panel_keys.append(None)
        active_level = self.level_filter or "<All Levels>"
        row_count = 0; visible_panel_count = 0
        for pid in panel_keys:
            is_unassigned = is_unassigned_panel_key(pid)
            if active_level != "<All Levels>":
                if is_unassigned:
                    if pid[1] != active_level:
                        continue
                elif pid is None:
                    if active_level != "<No Level>": continue
                elif panel_level_name(CACHE.panels.get(pid)) != active_level:
                    continue
            if is_unassigned:
                pname = unassigned_panel_label(pid)
            else:
                pname = safe_name(CACHE.panels.get(pid), "<Unassigned>") if pid is not None else "<Unassigned>"
            circuits = groups.get(pid, [])
            if not is_unassigned and not self.show_empty_panels and len(circuits) == 0:
                continue
            circuits.sort(key=circuit_sort_key)
            visible = []
            panel_match = flt in pname.lower() if flt else True
            for c in circuits:
                if not flt or panel_match or flt in circuit_label(c).lower(): visible.append(c)
            if flt and not panel_match and not visible: continue
            visible_panel_count += 1
            panel_is_open = (pid in self.expanded_panel_ids) or bool(flt)
            picon = "-" if panel_is_open else "+"
            panel_tag = ("unassigned_panel", pid) if is_unassigned else ("panel", pid)
            self.lbItems.Items.Add(self.make_item("{0} PANEL  {1} ({2})".format(picon, pname, len(visible)), panel_tag)); row_count += 1
            if panel_is_open:
                for c in visible:
                    cid = c.Id.IntegerValue
                    icon = "-" if cid in self.expanded_circuit_ids else "+"
                    self.lbItems.Items.Add(self.make_item("  {0} CIRCUIT  {1}".format(icon, circuit_label(c)), ("circuit", cid))); row_count += 1
                    if cid in self.expanded_circuit_ids:
                        try:
                            for el in c.Elements:
                                self.lbItems.Items.Add(self.make_item("      FIXTURE  {0}".format(element_label(el)), ("element", el.Id.IntegerValue, cid))); row_count += 1
                            self.lbItems.Items.Add(self.make_item("      Add selected fixture(s) from Revit view to this circuit", ("addrow", cid))); row_count += 1
                        except Exception: pass
        self.building = False
        self.restore_pending_selection(); self.update_target_from_selection(); self.update_details()
        self.log("Loaded {0} rows across {1} visible panel(s). Level filter: {2}.".format(row_count, visible_panel_count, active_level))

    def selected_tags(self):
        tags = []
        for item in self.lbItems.SelectedItems:
            try:
                if item.Tag: tags.append(item.Tag)
            except Exception: pass
        return tags

    def selected_circuit_ids(self):
        ids = []
        for tag in self.selected_tags():
            if tag[0] == "circuit": ids.append(tag[1])
            elif tag[0] == "element": ids.append(tag[2])
        result = []
        for x in ids:
            if x not in result: result.append(x)
        return result

    def selected_element_ids(self):
        result = []
        for tag in self.selected_tags():
            if tag[0] == "element" and tag[1] not in result: result.append(tag[1])
        return result

    def selected_panel_id_from_tool(self):
        for tag in self.selected_tags():
            if tag and tag[0] == "panel": return tag[1]
        return None

    def restore_pending_selection(self):
        if not self.pending_select_tags: return
        wanted = list(self.pending_select_tags); self.pending_select_tags = []
        for item in self.lbItems.Items:
            try:
                if item.Tag in wanted: item.IsSelected = True
            except Exception: pass

    def on_list_selection_changed(self, sender, args):
        if self.building: return
        self.update_target_from_selection(); self.update_details()

    def update_target_from_selection(self):
        for tag in self.selected_tags():
            if tag[0] == "circuit": self.target_circuit_id = tag[1]; return
            if tag[0] == "element": self.target_circuit_id = tag[2]; return
            if tag[0] == "addrow": self.target_circuit_id = tag[1]; return

    def target_circuit(self):
        if self.target_circuit_id is None:
            self.log("Click a circuit/fixture row first to set the TARGET circuit."); return None
        c = doc.GetElement(ElementId(self.target_circuit_id))
        if c is None: self.log("Target circuit no longer exists. Refresh.")
        return c

    def update_details(self):
        c = doc.GetElement(ElementId(self.target_circuit_id)) if self.target_circuit_id is not None else None
        if c is None:
            self.txtPanel.Text = "Target panel: -"; self.txtCircuit.Text = "Target circuit: -"; self.txtLoad.Text = "Target load: -"
        else:
            base = None
            try: base = c.BaseEquipment
            except Exception: pass
            self.txtPanel.Text = "Target panel: " + (safe_name(base, "<Unassigned>") if base else "<Unassigned>")
            self.txtCircuit.Text = "Target circuit: " + circuit_number(c)
            self.txtLoad.Text = "Target load: " + (get_load_name(c) or "-")
        try:
            self.txtSelectedSummary.Text = "Highlighted rows: {0} circuit row(s), {1} fixture row(s).".format(len([t for t in self.selected_tags() if t[0]=='circuit']), len(self.selected_element_ids()))
        except Exception: pass

    def on_list_double_click(self, sender, args): self.toggle_expand_selected()

    def on_expand_selected(self, sender, args):
        for tag in self.selected_tags():
            if tag[0] == "panel": self.expanded_panel_ids.add(tag[1])
            elif tag[0] == "unassigned_panel": self.expanded_panel_ids.add(tag[1])
            elif tag[0] == "circuit": self.expanded_circuit_ids.add(tag[1])
            elif tag[0] == "element": self.expanded_circuit_ids.add(tag[2])
            elif tag[0] == "addrow": self.expanded_circuit_ids.add(tag[1])
        self.build_list()

    def on_collapse_selected(self, sender, args):
        for tag in self.selected_tags():
            if tag[0] == "panel" and tag[1] in self.expanded_panel_ids: self.expanded_panel_ids.remove(tag[1])
            elif tag[0] == "unassigned_panel" and tag[1] in self.expanded_panel_ids: self.expanded_panel_ids.remove(tag[1])
            elif tag[0] == "circuit" and tag[1] in self.expanded_circuit_ids: self.expanded_circuit_ids.remove(tag[1])
            elif tag[0] == "element" and tag[2] in self.expanded_circuit_ids: self.expanded_circuit_ids.remove(tag[2])
            elif tag[0] == "addrow" and tag[1] in self.expanded_circuit_ids: self.expanded_circuit_ids.remove(tag[1])
        self.build_list()

    def toggle_expand_selected(self):
        changed = False
        for tag in self.selected_tags():
            if tag[0] == "panel" or tag[0] == "unassigned_panel":
                if tag[1] in self.expanded_panel_ids: self.expanded_panel_ids.remove(tag[1])
                else: self.expanded_panel_ids.add(tag[1])
                changed = True
            elif tag[0] == "circuit":
                if tag[1] in self.expanded_circuit_ids: self.expanded_circuit_ids.remove(tag[1])
                else: self.expanded_circuit_ids.add(tag[1])
                changed = True
        if changed: self.build_list()

    def on_show_all_panels(self, sender, args):
        for pid in CACHE.grouped().keys(): self.expanded_panel_ids.add(pid)
        self.build_list()

    def on_show_target_only(self, sender, args):
        if self.target_circuit_id is None: self.log("Click a target circuit first."); return
        self.expanded_panel_ids.clear(); self.expanded_circuit_ids.clear()
        c = doc.GetElement(ElementId(self.target_circuit_id))
        if c:
            self.expanded_panel_ids.add(panel_id_of(c)); self.expanded_circuit_ids.add(self.target_circuit_id); self.pending_select_tags = [("circuit", self.target_circuit_id)]
        self.build_list()

    def on_locate_revit_selection(self, sender, args):
        selected = self.current_revit_selection_ids(False)
        if not selected: self.log("Select fixture(s) in Revit first, then click Locate selection."); return
        self.expanded_panel_ids.clear(); self.expanded_circuit_ids.clear(); self.pending_select_tags = []
        found = 0
        for eid in selected:
            for c in find_circuits_containing(eid):
                cid = c.Id.IntegerValue
                self.expanded_panel_ids.add(panel_id_of(c)); self.expanded_circuit_ids.add(cid); self.pending_select_tags.append(("element", eid, cid)); self.target_circuit_id = cid; found += 1
        if found == 0: self.log("Selected Revit element(s) were not found on any listed circuit."); return
        self.build_list(); self.log("Located {0} Revit-selected fixture/circuit link(s) in the tool.".format(found))

    # ---------------- Alt-drag/drop support ----------------
    def item_from_original_source(self, source):
        try:
            item = ItemsControl.ContainerFromElement(self.lbItems, source)
            if isinstance(item, ListBoxItem):
                return item
        except Exception:
            pass
        current = source
        for i in range(40):
            if current is None:
                return None
            if isinstance(current, ListBoxItem):
                return current
            try:
                parent = VisualTreeHelper.GetParent(current)
                if parent is not None:
                    current = parent
                    continue
            except Exception:
                pass
            try:
                current = current.Parent
                continue
            except Exception:
                pass
            try:
                current = current.TemplatedParent
                continue
            except Exception:
                return None
        return None

    def is_valid_drag_target(self, target_tag, dragged_tags):
        if not target_tag or not dragged_tags:
            return False
        has_circuit = any(t[0] == "circuit" for t in dragged_tags)
        has_fixture = any(t[0] == "element" for t in dragged_tags)
        if target_tag[0] == "panel" and has_circuit:
            return True
        if target_tag[0] == "circuit" and has_fixture:
            return True
        return False

    def clear_drag_highlight(self):
        try:
            if self.drag_hover_item is not None and self.drag_hover_item.Tag:
                self.style_item(self.drag_hover_item, self.drag_hover_item.Tag, False, False)
        except Exception:
            pass
        self.drag_hover_item = None

    def set_drag_highlight(self, item, valid):
        if item is self.drag_hover_item:
            return
        self.clear_drag_highlight()
        self.drag_hover_item = item
        try:
            if item is not None and item.Tag:
                self.style_item(item, item.Tag, valid, not valid)
        except Exception:
            pass

    def on_drag_over(self, sender, args):
        try:
            target_item = self.item_from_original_source(args.OriginalSource)
            valid = False
            if target_item is not None and target_item.Tag:
                valid = self.is_valid_drag_target(target_item.Tag, list(self.drag_tags))
                self.set_drag_highlight(target_item, valid)
            else:
                self.clear_drag_highlight()
            args.Effects = DragDropEffects.Move if valid else getattr(DragDropEffects, "None")
            args.Handled = True
        except Exception:
            self.clear_drag_highlight()

    def on_drag_leave(self, sender, args):
        self.clear_drag_highlight()

    def on_preview_mouse_left_button_down(self, sender, args):
        try:
            if not (Keyboard.IsKeyDown(Key.LeftAlt) or Keyboard.IsKeyDown(Key.RightAlt)):
                self.alt_drag_preserved_tags = []
                return
            item = self.item_from_original_source(args.OriginalSource)
            if item is None or not item.Tag:
                self.alt_drag_preserved_tags = []
                return
            if item.IsSelected:
                tags = self.selected_tags()
                movable = [t for t in tags if t[0] in ("circuit", "element")]
                if movable:
                    self.alt_drag_preserved_tags = movable
                    self.drag_tags = movable
                    args.Handled = True
                    self.log("Alt-drag armed with {0} highlighted row(s).".format(len(movable)))
            else:
                self.alt_drag_preserved_tags = []
        except Exception:
            self.alt_drag_preserved_tags = []

    def on_preview_mouse_move(self, sender, args):
        try:
            if Keyboard.IsKeyDown(Key.LeftAlt) or Keyboard.IsKeyDown(Key.RightAlt):
                if args.LeftButton == MouseButtonState.Pressed:
                    if self.alt_drag_preserved_tags:
                        movable = list(self.alt_drag_preserved_tags)
                    else:
                        movable = [t for t in self.selected_tags() if t[0] in ("circuit", "element")]
                    if movable:
                        self.drag_tags = movable
                        data = DataObject("CircuitHubRows", "move")
                        DragDrop.DoDragDrop(self.lbItems, data, DragDropEffects.Move)
                        self.alt_drag_preserved_tags = []
        except Exception:
            self.log("Drag start failed. Use the buttons instead.")

    def on_drop(self, sender, args):
        try:
            self.clear_drag_highlight()
            if not (Keyboard.IsKeyDown(Key.LeftAlt) or Keyboard.IsKeyDown(Key.RightAlt)):
                self.log("Hold Alt while dropping to move/reassign rows.")
                return
            target_item = self.item_from_original_source(args.OriginalSource)
            if target_item is None or not target_item.Tag:
                self.log("Drop directly onto a panel or circuit row while holding Alt.")
                return
            target = target_item.Tag
            dragged = list(self.drag_tags)
            self.drag_tags = []
            self.alt_drag_preserved_tags = []
            if not dragged:
                self.log("Nothing was dragged.")
                return
            circuit_ids = [t[1] for t in dragged if t[0] == "circuit"]
            fixture_ids = [t[1] for t in dragged if t[0] == "element"]

            if target[0] == "panel" and circuit_ids:
                ignored = len(fixture_ids)
                if ignored:
                    self.log("Alt-drag to panel: moving circuit row(s), ignoring {0} fixture row(s).".format(ignored))
                self.move_circuit_ids_to_panel(circuit_ids, target[1], "Alt-drag Circuit Move")
                return

            if target[0] == "circuit" and fixture_ids:
                target_cid = target[1]
                ignored = len(circuit_ids)
                msg = "Reassign {0} dragged fixture row(s) to this circuit?".format(len(fixture_ids))
                if ignored:
                    msg += "\n\nNote: {0} dragged circuit row(s) will be ignored.".format(ignored)
                if str(MessageBox.Show(msg, "Alt-drag Fixture Reassign", MessageBoxButton.YesNo, MessageBoxImage.Warning)) != "Yes":
                    self.log("Alt-drag reassign cancelled.")
                    return
                def work():
                    target_circuit = doc.GetElement(ElementId(target_cid))
                    validate_phase_compatibility(target_circuit, fixture_ids)
                    removed = 0
                    added = 0
                    for eid in fixture_ids:
                        fixture = doc.GetElement(ElementId(eid))
                        if fixture is None:
                            continue
                        for old_circuit in find_circuits_containing(eid):
                            if old_circuit.Id.IntegerValue == target_cid:
                                continue
                            es_old = ElementSet(); es_old.Insert(fixture)
                            old_circuit.RemoveFromCircuit(es_old)
                            removed += 1
                        es_new = ElementSet(); es_new.Insert(fixture)
                        target_circuit.AddToCircuit(es_new)
                        added += 1
                    return "Alt-drag reassigned {0} fixture row(s) to circuit {1}. Removed {2} old link(s).".format(added, circuit_number(target_circuit), removed)
                self.run_direct("alt_drag_reassign_fixtures", work, True)
                return

            self.log("Alt-drag supports circuit rows onto panel rows, or fixture rows onto circuit rows.")
        except Exception:
            show_error("Circuit Hub Drag Error", traceback.format_exc())

    # ---------------- ExternalEvent action engine ----------------
    def run_direct(self, name, work, refresh=True, transaction=True):
        try:
            if self.pending_external_action is not None:
                show_warning("Circuit Hub", "Another Circuit Hub action is already queued."); return
            self.pending_external_action = (name, work, refresh, transaction)
            self.external_event.Raise(); self.log("Queued: " + name)
        except Exception:
            self.pending_external_action = None; show_error("Circuit Hub Error", traceback.format_exc())

    def execute_pending_external_event(self):
        action = self.pending_external_action; self.pending_external_action = None
        if action is None: return
        name, work, refresh, use_transaction = action
        try:
            msg = run_transaction(name, work) if use_transaction else work()
            self.log(msg)
            if refresh: self.refresh()
        except UserFacingError as ex:
            show_warning("Circuit Hub", str(ex)); self.log("Blocked: " + name)
        except Exception as ex:
            clean_msg = clean_revit_validation_message(str(ex))
            if clean_msg: show_warning("Circuit Hub", clean_msg); self.log("Blocked by Revit validation: " + name); return
            err = traceback.format_exc(); print(err); show_error("Circuit Hub Error", err); self.log("FAILED: " + name)

    def pick_panel_id(self):
        names = []; lookup = {}
        for pid in sorted(CACHE.panels.keys(), key=lambda x: safe_name(CACHE.panels[x]).lower()):
            name = safe_name(CACHE.panels[pid]); names.append(name); lookup[name] = pid
        if not names: self.log("No electrical equipment found."); return None
        picked = forms.SelectFromList.show(names, title="Select panel", button_name="Select", multiselect=False)
        return lookup.get(picked) if picked else None

    def choose_target_panel_id(self):
        pid = self.selected_panel_id_from_tool()
        if pid is not None: return pid, True
        picked = self.pick_panel_id(); return picked, False

    def current_revit_selection_ids(self, apply_filter=True, action_name="action"):
        if apply_filter: return self.filtered_revit_selection_ids(action_name, True)
        return [x.IntegerValue for x in list(uidoc.Selection.GetElementIds())]

    # ---------------- Button handlers ----------------
    def on_inline_remove_fixture(self, sender, args):
        tag = sender.Tag
        if tag and tag[0] == "element": self.remove_fixture_ids_from_circuits([tag[1]], "inline_remove_fixture")

    def on_inline_add_to_circuit(self, sender, args):
        tag = sender.Tag
        if tag[0] == "circuit": cid = tag[1]
        elif tag[0] == "addrow": cid = tag[1]
        else: return
        self.add_or_reassign_selection_to_circuit(cid, "inline_add_to_circuit")

    def add_or_reassign_selection_to_circuit(self, target_cid, transaction_name):
        selected_ids = self.current_revit_selection_ids(True, "add/reassign selected fixture(s) to circuit")
        def work():
            target_circuit = doc.GetElement(ElementId(target_cid)); validate_phase_compatibility(target_circuit, selected_ids)
            removed = 0; added = 0
            for eid in selected_ids:
                fixture = doc.GetElement(ElementId(eid))
                if fixture is None: continue
                for old_circuit in find_circuits_containing(eid):
                    if old_circuit.Id.IntegerValue == target_cid: continue
                    es_old = ElementSet(); es_old.Insert(fixture); old_circuit.RemoveFromCircuit(es_old); removed += 1
                es_new = ElementSet(); es_new.Insert(fixture); target_circuit.AddToCircuit(es_new); added += 1
            self.target_circuit_id = target_cid
            return "Added/reassigned {0} selected fixture(s) to circuit {1}. Removed {2} old link(s).".format(added, circuit_number(target_circuit), removed)
        self.run_direct(transaction_name, work, True)

    def remove_fixture_ids_from_circuits(self, fixture_ids, transaction_name):
        def work():
            removed = 0
            for eid in fixture_ids:
                fixture = doc.GetElement(ElementId(eid))
                if fixture is None: continue
                for c in find_circuits_containing(eid):
                    es = ElementSet(); es.Insert(fixture); c.RemoveFromCircuit(es); removed += 1
            return "Removed {0} fixture-circuit link(s).".format(removed)
        self.run_direct(transaction_name, work, True)

    def on_remove_revit_selection_from_all_circuits(self, sender, args):
        selected_ids = self.current_revit_selection_ids(True, "remove selected fixture(s) from all circuits")
        self.remove_fixture_ids_from_circuits(selected_ids, "remove_selection_from_all_circuits")

    def on_select_rows_in_view(self, sender, args):
        ids = List[ElementId](); count = 0
        for cid in self.selected_circuit_ids():
            c = doc.GetElement(ElementId(cid))
            if c:
                try:
                    for el in c.Elements: ids.Add(el.Id); count += 1
                except Exception: pass
        for eid in self.selected_element_ids(): ids.Add(ElementId(eid)); count += 1
        if count == 0: self.log("Highlight circuit/fixture rows first."); return
        uidoc.Selection.SetElementIds(ids)
        try: uidoc.ShowElements(ids)
        except Exception: pass
        self.log("Selected {0} highlighted-row element(s) in Revit.".format(count))

    def move_circuit_ids_to_panel(self, circuit_ids, panel_id_value, title):
        unique = []
        for cid in circuit_ids:
            if cid not in unique: unique.append(cid)
        panel = doc.GetElement(ElementId(panel_id_value)); panel_name = safe_name(panel, "<selected panel>") if panel else "<selected panel>"
        to_move = []; already = []
        for cid in unique:
            c = doc.GetElement(ElementId(cid))
            if c is None: continue
            if circuit_is_on_panel(c, panel_id_value): already.append(cid)
            else: to_move.append(cid)
        if not to_move: show_warning("Circuit Hub", "All selected circuit(s) are already on panel: {0}.".format(panel_name)); return
        msg = "Move {0} circuit(s) to panel {1}?".format(len(to_move), panel_name)
        if already: msg += "\n\nSkipped because already on target panel: {0}".format(len(already))
        if str(MessageBox.Show(msg, title, MessageBoxButton.YesNo, MessageBoxImage.Warning)) != "Yes": return
        def work():
            panel = doc.GetElement(ElementId(panel_id_value))
            if panel is None: raise UserFacingError("Selected panel no longer exists.")
            moved = 0; skipped_now = 0
            for cid in to_move:
                c = doc.GetElement(ElementId(cid))
                if c is None: continue
                if circuit_is_on_panel(c, panel_id_value): skipped_now += 1; continue
                c.SelectPanel(panel); moved += 1; self.target_circuit_id = cid
            return "Moved {0} circuit(s) to panel {1}. Skipped {2} already on target.".format(moved, safe_name(panel), len(already) + skipped_now)
        self.run_direct("move_circuits_to_panel", work, True)

    def on_move_selected_circuits(self, sender, args):
        circuit_ids = self.selected_circuit_ids()
        if not circuit_ids: self.log("Highlight one or more circuit rows first."); return
        pid = self.pick_panel_id()
        if pid is None: return
        self.move_circuit_ids_to_panel(circuit_ids, pid, "Move Highlighted Circuits")

    def on_remove_selected_fixture_rows(self, sender, args):
        eids = self.selected_element_ids()
        if not eids: self.log("Highlight one or more fixture rows first."); return
        self.remove_fixture_ids_from_circuits(eids, "remove_highlighted_fixture_rows")

    def on_rename_load(self, sender, args):
        c = self.target_circuit()
        if c is None: return
        new_name = forms.ask_for_string(default=get_load_name(c), prompt="New load name", title="Rename Load")
        if new_name is None: return
        cid = c.Id.IntegerValue
        def work():
            c2 = doc.GetElement(ElementId(cid))
            if not set_load_name(c2, new_name): raise UserFacingError("Load Name is read-only.")
            return "Renamed target load to: {0}".format(new_name)
        self.run_direct("rename_load", work, True)

    def on_move_target_panel(self, sender, args):
        c = self.target_circuit()
        if c is None: return
        pid = self.pick_panel_id()
        if pid is None: return
        self.move_circuit_ids_to_panel([c.Id.IntegerValue], pid, "Move Target Circuit")

    def on_pick_panel_in_view_for_selected_fixture_circuits(self, sender, args):
        """Move circuits found from selected Revit fixtures to a panel picked in the model view."""
        selected_ids = self.current_revit_selection_ids(True, "move selected fixture circuits to picked panel")
        circuit_ids = []
        uncircuited_ids = []
        for eid in selected_ids:
            circuits = find_circuits_containing(eid)
            if circuits:
                for c in circuits:
                    cid = c.Id.IntegerValue
                    if cid not in circuit_ids:
                        circuit_ids.append(cid)
            else:
                uncircuited_ids.append(eid)
        if not circuit_ids:
            show_warning("Circuit Hub", "None of the allowed selected elements are currently on circuits. No circuits were moved.")
            self.log("Blocked: no existing circuits found in filtered Revit selection.")
            return

        def work_no_tx():
            picked = uidoc.Selection.PickObject(ObjectType.Element, ElectricalEquipmentSelectionFilter(), "Pick the panel to move the selected fixture circuit(s) to")
            if picked is None:
                return "Panel pick cancelled."
            panel = doc.GetElement(picked.ElementId)
            if panel is None:
                raise UserFacingError("The picked panel could not be resolved.")
            panel_id_value = panel.Id.IntegerValue
            panel_name = safe_name(panel, "<picked panel>")
            to_move = []
            already = []
            for cid in circuit_ids:
                c = doc.GetElement(ElementId(cid))
                if c is None:
                    continue
                if circuit_is_on_panel(c, panel_id_value):
                    already.append(cid)
                else:
                    to_move.append(cid)
            if not to_move:
                return "All selected fixture circuit(s) are already assigned to picked panel {0}. Skipped {1} selected item(s) with no existing circuit.".format(panel_name, len(uncircuited_ids))
            msg = (
                "Move selected fixture circuit(s) to picked panel: {0}\n\n"
                "This is the view-pick version of 'Move selected fixture circuits to panel'.\n"
                "It moves existing circuits only. It does not remove/reapply individual fixtures.\n\n"
                "Existing circuits to move: {1}\n"
                "Already on picked panel, skipped: {2}\n"
                "Selected elements with no existing circuit, skipped: {3}\n\n"
                "Proceed?"
            ).format(panel_name, len(to_move), len(already), len(uncircuited_ids))
            if str(MessageBox.Show(msg, "Move Selected Fixture Circuits To Picked Panel", MessageBoxButton.YesNo, MessageBoxImage.Warning)) != "Yes":
                return "Move to picked panel cancelled."
            def tx_work():
                moved = 0
                skipped_now = 0
                for cid in to_move:
                    c = doc.GetElement(ElementId(cid))
                    if c is None:
                        continue
                    if circuit_is_on_panel(c, panel_id_value):
                        skipped_now += 1
                        continue
                    c.SelectPanel(panel)
                    moved += 1
                    self.target_circuit_id = cid
                return "Moved {0} existing circuit(s) to picked panel {1}. Skipped {2} already on target and {3} selected item(s) with no existing circuit.".format(moved, panel_name, len(already) + skipped_now, len(uncircuited_ids))
            return run_transaction("move_selected_fixture_circuits_to_picked_panel", tx_work)
        self.run_direct("pick_panel_in_view_for_selected_fixture_circuits", work_no_tx, True, transaction=False)

    def on_add_revit_selection(self, sender, args):
        target = self.target_circuit()
        if target is None: return
        selected_ids = self.current_revit_selection_ids(True, "add to circuit")
        target_cid = target.Id.IntegerValue
        def work():
            c = doc.GetElement(ElementId(target_cid)); validate_phase_compatibility(c, selected_ids)
            existing = elements_with_existing_circuits(selected_ids)
            if existing: raise UserFacingError("Add is only for uncircuited fixtures. Use Reassign for fixtures already on circuits.\n" + describe_existing_memberships(existing))
            es, count = element_set_from_ids(selected_ids)
            if count == 0: raise UserFacingError("No valid Revit-selected elements found.")
            c.AddToCircuit(es)
            return "Added {0} uncircuited selected fixture(s) to target circuit {1}.".format(count, circuit_number(c))
        self.run_direct("add_uncircuited_revit_selection", work, True)

    def on_reassign_revit_selection(self, sender, args):
        target = self.target_circuit()
        if target is None: return
        self.add_or_reassign_selection_to_circuit(target.Id.IntegerValue, "reassign_revit_selection")

    def on_move_existing_circuits_to_panel(self, sender, args):
        selected_ids = self.current_revit_selection_ids(True, "move existing circuits to panel")
        panel_id_value, used_tool_panel = self.choose_target_panel_id()
        if panel_id_value is None: return
        selected_existing_circuit_ids = []
        uncircuited_ids = []
        for eid in selected_ids:
            circuits = find_circuits_containing(eid)
            if circuits:
                for c in circuits:
                    cid = c.Id.IntegerValue
                    if cid not in selected_existing_circuit_ids: selected_existing_circuit_ids.append(cid)
            else: uncircuited_ids.append(eid)
        if not selected_existing_circuit_ids:
            show_warning("Circuit Hub", "None of the allowed selected elements are currently on circuits. No circuits were moved."); return
        panel = doc.GetElement(ElementId(panel_id_value)); panel_name = safe_name(panel, "<selected panel>") if panel else "<selected panel>"
        to_move = []; already = []
        for cid in selected_existing_circuit_ids:
            c = doc.GetElement(ElementId(cid))
            if c is None: continue
            if circuit_is_on_panel(c, panel_id_value): already.append(cid)
            else: to_move.append(cid)
        if not to_move:
            show_warning("Circuit Hub", "All existing circuits found in the filtered selection are already assigned to panel: {0}.".format(panel_name)); return
        source_text = "highlighted panel row" if used_tool_panel else "panel picker"
        msg = ("Move existing circuits to panel: {0}\n\nPanel source: {1}\n\nThis moves existing circuits only. It will not merge fixtures or create new circuits.\n\nExisting circuits to move: {2}\nAlready on target, skipped: {3}\nSelected elements with no existing circuit, skipped: {4}\n\nProceed?").format(panel_name, source_text, len(to_move), len(already), len(uncircuited_ids))
        if str(MessageBox.Show(msg, "Move Existing Circuits To Panel", MessageBoxButton.YesNo, MessageBoxImage.Warning)) != "Yes": return
        self.move_circuit_ids_to_panel(to_move, panel_id_value, "Move Existing Circuits To Panel")

    def on_remove_revit_selection(self, sender, args):
        target = self.target_circuit()
        if target is None: return
        selected_ids = self.current_revit_selection_ids(True, "remove from target circuit")
        target_cid = target.Id.IntegerValue
        def work():
            c = doc.GetElement(ElementId(target_cid)); es, count = element_set_from_ids(selected_ids)
            if count == 0: raise UserFacingError("No valid selected elements found.")
            c.RemoveFromCircuit(es)
            return "Removed {0} selected fixture(s) from target circuit {1}.".format(count, circuit_number(c))
        self.run_direct("remove_revit_selection_from_target", work, True)

    def on_create_circuit(self, sender, args):
        selected_ids = self.current_revit_selection_ids(True, "new circuit")
        target_panel_id = self.selected_panel_id_from_tool()
        target_panel_name = None
        if target_panel_id is not None:
            panel = doc.GetElement(ElementId(target_panel_id)); target_panel_name = safe_name(panel, "<selected panel>") if panel else "<selected panel>"
        existing = elements_with_existing_circuits(selected_ids)
        remove_existing = False
        if existing:
            msg = "One or more selected fixtures already has a circuit. Remove from existing circuit(s) and create a new circuit?"
            if target_panel_name: msg += "\n\nThe new circuit will be assigned to panel: {0}".format(target_panel_name)
            msg += "\n\n" + describe_existing_memberships(existing)
            if str(MessageBox.Show(msg, "Create New Circuit", MessageBoxButton.YesNo, MessageBoxImage.Warning)) != "Yes": return
            remove_existing = True
        def work():
            removed = 0
            if remove_existing:
                for eid, circuits in existing:
                    fixture = doc.GetElement(ElementId(eid))
                    if fixture is None: continue
                    for old_circuit in circuits:
                        es_old = ElementSet(); es_old.Insert(fixture); old_circuit.RemoveFromCircuit(es_old); removed += 1
            c = ElectricalSystem.Create(doc, id_list(selected_ids), ElectricalSystemType.PowerCircuit)
            assigned = None
            if target_panel_id is not None:
                panel = doc.GetElement(ElementId(target_panel_id))
                if panel is None: raise UserFacingError("The highlighted target panel no longer exists.")
                c.SelectPanel(panel); assigned = safe_name(panel)
            self.target_circuit_id = c.Id.IntegerValue
            if assigned:
                return "Created circuit {0} with {1} selected element(s), assigned to panel {2}. Removed {3} old link(s).".format(circuit_number(c), len(selected_ids), assigned, removed)
            return "Created circuit {0} with {1} selected element(s). Removed {2} old link(s).".format(circuit_number(c), len(selected_ids), removed)
        self.run_direct("create_circuit", work, True)

    def on_create_schedule(self, sender, args):
        pid = self.pick_panel_id()
        if pid is None: return
        def work():
            view = PanelScheduleView.CreateInstanceView(doc, ElementId(pid))
            return "Created panel schedule: {0}".format(safe_name(view))
        self.run_direct("create_schedule", work, False)


def on_revit_document_changed(sender, args):
    try:
        if APP is not None: APP.queue_refresh_from_doc_changed()
    except Exception: print(traceback.format_exc())


def main():
    global APP, DOC_CHANGED_HANDLER
    if doc is None or doc.IsFamilyDocument:
        MessageBox.Show("Open a Revit project document before running Circuit Hub.", "Circuit Hub", MessageBoxButton.OK, MessageBoxImage.Warning); return
    APP = CircuitHub()
    try:
        DOC_CHANGED_HANDLER = on_revit_document_changed
        uidoc.Application.Application.DocumentChanged += DOC_CHANGED_HANDLER
        APP.log("Document change listener active. Auto-refresh is controlled by the toolbar checkbox.")
    except Exception:
        APP.log("Auto-refresh subscription failed; use Refresh manually if needed.")
    APP.Show()

main()
