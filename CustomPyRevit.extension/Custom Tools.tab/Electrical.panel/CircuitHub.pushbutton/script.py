# -*- coding: utf-8 -*-
"""Circuit Hub v8 - no checkboxes, native-style multi-select list UI."""
__title__ = "Circuit\nHub"
__author__ = "Jacques-Michael"
__persistentengine__ = True

import traceback
import re
import os
import json

import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System import Action
from System.Collections.Generic import List
from System.Windows import MessageBox, MessageBoxButton, MessageBoxImage, DragDrop, DragDropEffects, DataObject, Thickness, FontWeights
from System.Windows.Controls import ListBoxItem, ItemsControl
from System.Windows.Input import MouseButtonState, Keyboard, Key, ModifierKeys
from System.Windows.Media import VisualTreeHelper, Brushes
from pyrevit import revit, forms, script
from Autodesk.Revit.DB import ElementId, ElementSet, Transaction, FilteredElementCollector, BuiltInCategory, BuiltInParameter
from Autodesk.Revit.DB.Electrical import ElectricalSystem, ElectricalSystemType, PanelScheduleView, PanelScheduleTemplate
from Autodesk.Revit.UI import RevitCommandId, PostableCommand

doc = revit.doc
uidoc = revit.uidoc
APP = None
DOC_CHANGED_HANDLER = None
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "circuit_hub_settings.json")


def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_settings(data):
    try:
        current = load_settings()
        current.update(data)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(current, f, indent=2, sort_keys=True)
    except Exception:
        pass



def show_error(title, msg):
    try:
        MessageBox.Show(str(msg), title, MessageBoxButton.OK, MessageBoxImage.Error)
    except Exception:
        print(title); print(msg)


def show_warning(title, msg):
    try:
        MessageBox.Show(str(msg), title, MessageBoxButton.OK, MessageBoxImage.Warning)
    except Exception:
        print(title); print(msg)


class UserFacingError(Exception):
    """Expected validation/user workflow issue. Show message only, no traceback."""
    pass


def clean_revit_validation_message(msg):
    """Convert known Revit API validation failures into clean user-facing messages."""
    text = str(msg or "")
    lower = text.lower()
    if "circular connection" in lower:
        return (
            "Revit blocked this panel assignment because it would create a circular electrical connection.\n\n"
            "No circuits were moved.\n\n"
            "This usually means the selected target panel is electrically upstream/downstream of one of the circuits being moved, "
            "or the selected circuit/panel relationship would loop back on itself. Choose a different target panel."
        )
    if "there should be at least one component that can create the specified circuit type" in lower:
        return (
            "Revit could not create a power circuit from the selected element(s).\n\n"
            "The selection may include an annotation, tag, linked item, non-electrical family, or a family without a valid power connector."
        )
    if "cannot create the specified circuit type" in lower:
        return (
            "Revit could not create the requested circuit type from the selected element(s).\n\n"
            "Check that the selected items are valid electrical fixtures/equipment with compatible connectors."
        )
    return None


def as_eid(x):
    return x if isinstance(x, ElementId) else ElementId(int(x))


def id_list(items):
    result = List[ElementId]()
    for x in items:
        result.Add(as_eid(x))
    return result


def element_set_from_ids(items):
    result = ElementSet(); count = 0
    for x in items:
        el = doc.GetElement(as_eid(x))
        if el is not None:
            result.Insert(el); count += 1
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

def panel_level_name(panel):
    """Best-effort panel level name for filtering.
    Electrical equipment usually exposes a level via LevelId or one of the common
    schedule/reference level parameters. If not found, returns <No Level>.
    """
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

    for bip_name in [
        "FAMILY_LEVEL_PARAM",
        "SCHEDULE_LEVEL_PARAM",
        "INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM",
        "RBS_START_LEVEL_PARAM",
    ]:
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


def get_load_name(circuit):
    try:
        p = circuit.get_Parameter(BuiltInParameter.RBS_ELEC_CIRCUIT_NAME)
        return (p.AsString() or "") if p else ""
    except Exception:
        return ""


def set_load_name(circuit, value):
    p = circuit.get_Parameter(BuiltInParameter.RBS_ELEC_CIRCUIT_NAME)
    if p and not p.IsReadOnly:
        p.Set(value or ""); return True
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


def element_label(el):
    cat = "?"
    try:
        if el.Category:
            cat = el.Category.Name
    except Exception:
        pass
    return "{0}: {1} ({2})".format(cat, safe_name(el), el.Id.IntegerValue)


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


# ---------------- Phase / pole validation ----------------
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
    v = param_number(el, names)
    if v is None:
        v = param_number(element_type(el), names)
    return int(round(v)) if v is not None else None


def voltage_of(el):
    names = ["Voltage", "Nominal Voltage", "Supply Voltage"]
    v = param_number(el, names)
    if v is None:
        v = param_number(element_type(el), names)
    return float(v) if v is not None else None


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
        if "single" in txt or "1 phase" in txt or "1-phase" in txt or "1ph" in txt:
            return 1
        if "two" in txt or "2 phase" in txt or "2-phase" in txt or "2ph" in txt:
            return 2
        if "three" in txt or "3 phase" in txt or "3-phase" in txt or "3ph" in txt:
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
    """Direct phase read from element/type. Returns 1, 2, 3, or None."""
    explicit = explicit_phase_value(el)
    if explicit is not None:
        return explicit
    poles = poles_of(el); volts = voltage_of(el)
    if poles is not None:
        if poles <= 1: return 1
        if poles == 2: return 2
        if poles >= 3: return 3
    if volts is not None:
        # Conservative voltage fallback; phase/poles are preferred.
        if 100 <= volts <= 140: return 1
        if 190 <= volts <= 215: return 2
        if 360 <= volts <= 500: return 3
    return None

def phase_class(el):
    """Best-effort phase classification.
    For circuits, if the circuit itself does not expose poles/voltage, infer it
    from the elements already connected to the circuit. This catches many cases
    where Revit hides the useful electrical data on the circuit object itself.
    """
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


def find_circuits_containing(eid_int):
    found = []
    for c in CACHE.circuits:
        try:
            for el in c.Elements:
                if el.Id.IntegerValue == eid_int:
                    found.append(c); break
        except Exception:
            pass
    return found

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
    """If a fixture itself does not expose phase data, infer it from its current circuit.
    Only returns a value when all known existing circuit memberships agree.
    """
    phases = []
    for old_circuit in find_circuits_containing(eid):
        p = phase_class(old_circuit)
        if p is not None:
            phases.append(p)
    unique = sorted(list(set(phases)))
    if len(unique) == 1:
        return unique[0]
    return None


def phase_text(value):
    if value == 1: return "single-phase"
    if value == 2: return "two-phase"
    if value == 3: return "three-phase"
    return "unknown phase"


def validate_phase_compatibility(target_circuit, element_ids):
    """Block obvious single-phase <-> three-phase assignment before committing.
    Runs for Add, Reassign and Alt-drag fixture moves.
    """
    target_phase = phase_class(target_circuit)
    conflicts = []
    unknowns = []
    for eid in element_ids:
        el = doc.GetElement(ElementId(eid))
        if el is None:
            continue
        el_phase = phase_class(el)
        if el_phase is None:
            el_phase = phase_class_from_existing_membership(eid)
        if target_phase is None or el_phase is None:
            unknowns.append(element_label(el))
            continue
        if target_phase != el_phase:
            conflicts.append((element_label(el), el_phase, target_phase))
    if conflicts:
        lines = ["Phase conflict detected before transaction:"]
        for label, el_phase, target_phase in conflicts:
            lines.append("- {0}: fixture is {1}, target circuit is {2}".format(label, phase_text(el_phase), phase_text(target_phase)))
        if unknowns:
            lines.append("")
            lines.append("Phase could not be determined for {0} other selected item(s), but the known conflict above is enough to block this move.".format(len(unknowns)))
        raise UserFacingError("\n".join(lines))


def validate_selection_same_phase(element_ids):
    """Block creating a new circuit from a mixed single-phase / three-phase selection."""
    known = []
    unknowns = []
    for eid in element_ids:
        el = doc.GetElement(ElementId(eid))
        if el is None:
            continue
        p = phase_class(el)
        if p is None:
            p = phase_class_from_existing_membership(eid)
        if p is None:
            unknowns.append(element_label(el))
        else:
            known.append((element_label(el), p))
    unique = sorted(list(set([p for label, p in known])))
    if len(unique) > 1:
        lines = ["Mixed phase selection detected before creating a new circuit:"]
        for label, p in known:
            lines.append("- {0}: {1}".format(label, phase_text(p)))
        if unknowns:
            lines.append("")
            lines.append("Phase could not be determined for {0} other selected item(s).".format(len(unknowns)))
        raise UserFacingError("\n".join(lines))


def group_element_ids_by_phase(element_ids):
    groups = {}
    for eid in element_ids:
        el = doc.GetElement(ElementId(eid))
        if el is None:
            continue
        p = phase_class(el)
        if p is None:
            p = phase_class_from_existing_membership(eid)
        key = p if p is not None else "unknown"
        groups.setdefault(key, []).append(eid)
    return groups


class Cache(object):
    def __init__(self):
        self.panels = {}; self.circuits = []; self.templates = {}; self.level_names = []

    def build(self):
        self.panels = {}; self.circuits = []; self.templates = {}; self.level_names = []
        for panel in (FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_ElectricalEquipment).WhereElementIsNotElementType()):
            self.panels[panel.Id.IntegerValue] = panel
        level_set = set()
        for panel in self.panels.values():
            level_set.add(panel_level_name(panel))
        self.level_names = sorted(list(level_set))
        for es in FilteredElementCollector(doc).OfClass(ElectricalSystem):
            try:
                if es.SystemType == ElectricalSystemType.PowerCircuit:
                    self.circuits.append(es)
            except Exception:
                self.circuits.append(es)
        for tpl in FilteredElementCollector(doc).OfClass(PanelScheduleTemplate):
            self.templates[safe_name(tpl)] = tpl

    def grouped(self):
        groups = {}
        for c in self.circuits:
            groups.setdefault(panel_id_of(c), []).append(c)
        return groups


CACHE = Cache()


class CircuitHub(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, script.get_bundle_file("ui.xaml"))
        self.target_circuit_id = None
        self.expanded_panel_ids = set()
        self.expanded_circuit_ids = set()
        self.pending_select_tags = []
        self.drag_tags = []
        self.drag_hover_item = None
        self.alt_drag_preserved_tags = []
        self.doc_refresh_pending = False
        self.level_filter = "<All Levels>"
        self.building = False
        self.wire_events()
        self.Closed += self.on_closed
        self.PreviewKeyDown += self.on_preview_key_down
        CACHE.build()
        self.populate_level_filter()
        self.build_list()
        self.log("Circuit Hub ready. Click a row to set target; Ctrl/Shift-select rows for multi-actions.")

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
        self.lbItems.SelectionChanged += self.on_list_selection_changed
        self.lbItems.PreviewMouseLeftButtonDown += self.on_preview_mouse_left_button_down
        self.lbItems.PreviewMouseMove += self.on_preview_mouse_move
        self.lbItems.Drop += self.on_drop
        self.lbItems.DragOver += self.on_drag_over
        self.lbItems.DragLeave += self.on_drag_leave
        self.lbItems.MouseDoubleClick += self.on_list_double_click
        self.btnSelectRowsInView.Click += self.on_select_rows_in_view
        self.btnRenameLoad.Click += self.on_rename_load
        self.btnMoveTargetPanel.Click += self.on_move_target_panel
        self.btnMoveSelectedCircuits.Click += self.on_move_selected_circuits
        self.btnAddRevitSelection.Click += self.on_add_revit_selection
        self.btnReassignRevitSelection.Click += self.on_reassign_revit_selection
        self.btnReassignSelectionToPanel.Click += self.on_reassign_revit_selection_to_panel
        self.btnRemoveRevitSelection.Click += self.on_remove_revit_selection
        self.btnRemoveSelectedFixtureRows.Click += self.on_remove_selected_fixture_rows
        self.btnCreateCircuit.Click += self.on_create_circuit
        self.btnCreateSchedule.Click += self.on_create_schedule

    def populate_level_filter(self):
        try:
            current = load_settings().get("level_filter", self.level_filter or "<All Levels>")
            self.cmbLevelFilter.Items.Clear()
            self.cmbLevelFilter.Items.Add("<All Levels>")
            self.cmbLevelFilter.Items.Add("<No Level>")
            for name in CACHE.level_names:
                if name != "<No Level>":
                    self.cmbLevelFilter.Items.Add(name)
            # restore previous selection if it still exists
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
        save_settings({"level_filter": self.level_filter})
        self.build_list()

    def log(self, msg):
        try:
            self.txtStatus.Text = str(msg)
            self.lbLog.Items.Insert(0, str(msg))
        except Exception:
            print(msg)

    def on_closed(self, sender, args):
        global DOC_CHANGED_HANDLER
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
        """Refresh the WPF list after any Revit document change, including Undo/Redo.
        Coalesced through the window Dispatcher so DocumentChanged never tries to rebuild the UI directly.
        """
        try:
            if self.doc_refresh_pending:
                return
            self.doc_refresh_pending = True

            def do_refresh():
                self.doc_refresh_pending = False
                try:
                    CACHE.build()
                    self.populate_level_filter()
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
        """Post Revit's native Undo command. This undoes the latest Revit undo item,
        which will usually be the last Circuit Hub transaction if that was the last model edit.
        """
        try:
            cmd_id = RevitCommandId.LookupPostableCommandId(PostableCommand.Undo)
            uidoc.Application.PostCommand(cmd_id)
            self.log("Undo requested. The list will refresh when Revit reports the document change.")
        except Exception:
            show_error("Circuit Hub Undo Error", traceback.format_exc())

    def on_close(self, sender, args): self.Close()
    def on_refresh(self, sender, args): self.refresh()
    def on_search_changed(self, sender, args): self.build_list()

    def refresh(self):
        CACHE.build(); self.populate_level_filter(); self.build_list()

    def style_item(self, item, tag, hover=False, invalid=False):
        """Visually separate panel/circuit/fixture rows and optionally show drag targets."""
        try:
            item.Padding = Thickness(6, 3, 6, 3)
            item.Margin = Thickness(0, 1, 0, 1)
            item.FontWeight = FontWeights.Normal
            item.Foreground = Brushes.Black
            if tag[0] == "panel":
                item.Background = Brushes.Gainsboro
                item.FontWeight = FontWeights.Bold
            elif tag[0] == "circuit":
                item.Background = Brushes.AliceBlue
                item.FontWeight = FontWeights.SemiBold
            elif tag[0] == "element":
                item.Background = Brushes.White
                item.Foreground = Brushes.DimGray
            if invalid:
                item.Background = Brushes.MistyRose
                item.Foreground = Brushes.DarkRed
                item.FontWeight = FontWeights.Bold
            elif hover:
                item.Background = Brushes.LightGreen
                item.Foreground = Brushes.Black
                item.FontWeight = FontWeights.Bold
        except Exception:
            pass

    def make_item(self, text, tag):
        item = ListBoxItem()
        item.Content = text
        item.Tag = tag
        self.style_item(item, tag, False, False)
        return item

    def build_list(self):
        self.building = True
        flt = (self.tbSearch.Text or "").strip().lower()
        self.lbItems.Items.Clear()
        groups = CACHE.grouped()
        panel_keys = [x for x in groups.keys() if x is not None]
        panel_keys.sort(key=lambda x: safe_name(CACHE.panels.get(x)).lower())
        if None in groups: panel_keys.append(None)

        active_level = self.level_filter or "<All Levels>"
        row_count = 0
        visible_panel_count = 0
        for pid in panel_keys:
            if active_level != "<All Levels>":
                if pid is None:
                    if active_level != "<No Level>":
                        continue
                else:
                    panel = CACHE.panels.get(pid)
                    if panel_level_name(panel) != active_level:
                        continue
            pname = safe_name(CACHE.panels.get(pid), "<Unassigned>") if pid is not None else "<Unassigned>"
            circuits = groups.get(pid, [])
            circuits.sort(key=lambda c: circuit_label(c).lower())
            visible = []
            panel_match = flt in pname.lower() if flt else True
            for c in circuits:
                if not flt or panel_match or flt in circuit_label(c).lower():
                    visible.append(c)
            if flt and not panel_match and not visible:
                continue
            visible_panel_count += 1
            panel_is_open = (pid in self.expanded_panel_ids) or bool(flt)
            picon = "-" if panel_is_open else "+"
            self.lbItems.Items.Add(self.make_item("{0} PANEL  {1} ({2})".format(picon, pname, len(visible)), ("panel", pid)))
            row_count += 1
            if panel_is_open:
                for c in visible:
                    cid = c.Id.IntegerValue
                    icon = "-" if cid in self.expanded_circuit_ids else "+"
                    self.lbItems.Items.Add(self.make_item("  {0} CIRCUIT  {1}".format(icon, circuit_label(c)), ("circuit", cid)))
                    row_count += 1
                    if cid in self.expanded_circuit_ids:
                        try:
                            for el in c.Elements:
                                self.lbItems.Items.Add(self.make_item("      FIXTURE  {0}".format(element_label(el)), ("element", el.Id.IntegerValue, cid)))
                                row_count += 1
                        except Exception:
                            pass
        self.building = False
        self.restore_pending_selection()
        self.update_target_from_selection()
        self.update_details()
        self.log("Loaded {0} rows across {1} visible panel(s). Level filter: {2}.".format(row_count, visible_panel_count, active_level))

    def selected_tags(self):
        tags = []
        for item in self.lbItems.SelectedItems:
            try:
                if item.Tag:
                    tags.append(item.Tag)
            except Exception:
                pass
        return tags

    def selected_circuit_ids(self):
        ids = []
        for tag in self.selected_tags():
            if tag[0] == "circuit": ids.append(tag[1])
            elif tag[0] == "element": ids.append(tag[2])
        # unique preserving order
        result = []
        for x in ids:
            if x not in result: result.append(x)
        return result

    def selected_element_ids(self):
        result = []
        for tag in self.selected_tags():
            if tag[0] == "element" and tag[1] not in result:
                result.append(tag[1])
        return result

    def on_list_selection_changed(self, sender, args):
        if self.building: return
        self.update_target_from_selection()
        self.update_details()

    def update_target_from_selection(self):
        # The most recently/first selected circuit or fixture becomes the target.
        tags = self.selected_tags()
        if not tags:
            return
        for tag in tags:
            if tag[0] == "circuit":
                self.target_circuit_id = tag[1]; return
            if tag[0] == "element":
                self.target_circuit_id = tag[2]; return

    def target_circuit(self):
        if self.target_circuit_id is None:
            self.log("Click a circuit/fixture row in the tool first to set the TARGET circuit.")
            return None
        c = doc.GetElement(ElementId(self.target_circuit_id))
        if c is None:
            self.log("Target circuit no longer exists. Refresh.")
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
        self.txtSelectedSummary.Text = "Highlighted rows: {0} circuit row(s), {1} fixture row(s). Revit selection is separate.".format(len([t for t in self.selected_tags() if t[0]=='circuit']), len(self.selected_element_ids()))

    def on_list_double_click(self, sender, args):
        self.toggle_expand_selected()

    def on_expand_selected(self, sender, args):
        for tag in self.selected_tags():
            if tag[0] == "panel": self.expanded_panel_ids.add(tag[1])
            elif tag[0] == "circuit": self.expanded_circuit_ids.add(tag[1])
            elif tag[0] == "element": self.expanded_circuit_ids.add(tag[2])
        self.build_list()

    def on_collapse_selected(self, sender, args):
        for tag in self.selected_tags():
            if tag[0] == "panel" and tag[1] in self.expanded_panel_ids: self.expanded_panel_ids.remove(tag[1])
            elif tag[0] == "circuit" and tag[1] in self.expanded_circuit_ids: self.expanded_circuit_ids.remove(tag[1])
            elif tag[0] == "element" and tag[2] in self.expanded_circuit_ids: self.expanded_circuit_ids.remove(tag[2])
        self.build_list()

    def toggle_expand_selected(self):
        changed = False
        for tag in self.selected_tags():
            if tag[0] == "panel":
                pid = tag[1]
                if pid in self.expanded_panel_ids: self.expanded_panel_ids.remove(pid)
                else: self.expanded_panel_ids.add(pid)
                changed = True
            elif tag[0] == "circuit":
                cid = tag[1]
                if cid in self.expanded_circuit_ids: self.expanded_circuit_ids.remove(cid)
                else: self.expanded_circuit_ids.add(cid)
                changed = True
            elif tag[0] == "element":
                cid = tag[2]
                if cid in self.expanded_circuit_ids: self.expanded_circuit_ids.remove(cid)
                else: self.expanded_circuit_ids.add(cid)
                changed = True
        if changed: self.build_list()

    def restore_pending_selection(self):
        if not self.pending_select_tags:
            return
        wanted = list(self.pending_select_tags)
        self.pending_select_tags = []
        for item in self.lbItems.Items:
            try:
                if item.Tag in wanted:
                    item.IsSelected = True
            except Exception:
                pass

    def on_show_all_panels(self, sender, args):
        for pid in CACHE.grouped().keys():
            self.expanded_panel_ids.add(pid)
        self.build_list()

    def on_show_target_only(self, sender, args):
        if self.target_circuit_id is None:
            self.log("Click a target circuit first.")
            return
        self.expanded_panel_ids.clear()
        self.expanded_circuit_ids.clear()
        c = doc.GetElement(ElementId(self.target_circuit_id))
        if c:
            self.expanded_panel_ids.add(panel_id_of(c))
            self.expanded_circuit_ids.add(self.target_circuit_id)
            self.pending_select_tags = [("circuit", self.target_circuit_id)]
        self.build_list()

    def on_locate_revit_selection(self, sender, args):
        selected = self.current_revit_selection_ids()
        if not selected:
            self.log("Select fixture(s) in Revit first, then click Locate Revit selection.")
            return
        self.expanded_panel_ids.clear()
        self.expanded_circuit_ids.clear()
        self.pending_select_tags = []
        found = 0
        for eid in selected:
            circuits = find_circuits_containing(eid)
            for c in circuits:
                cid = c.Id.IntegerValue
                self.expanded_panel_ids.add(panel_id_of(c))
                self.expanded_circuit_ids.add(cid)
                self.pending_select_tags.append(("element", eid, cid))
                self.target_circuit_id = cid
                found += 1
        if found == 0:
            self.log("Selected Revit element(s) were not found on any listed circuit.")
            return
        self.build_list()
        self.log("Located {0} Revit-selected fixture/circuit link(s) in the tool.".format(found))

    def item_from_original_source(self, source):
        """Resolve the ListBoxItem under the mouse during drop.
        OriginalSource is often a TextBlock/Border inside the ListBoxItem, so
        walking Parent alone is unreliable. ContainerFromElement + visual-tree
        fallback makes Alt-drag target detection much more reliable.
        """
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
            dragged = list(self.drag_tags)
            valid = False
            if target_item is not None and target_item.Tag:
                valid = self.is_valid_drag_target(target_item.Tag, dragged)
                self.set_drag_highlight(target_item, valid)
            else:
                self.clear_drag_highlight()
            if valid:
                args.Effects = DragDropEffects.Move
            else:
                args.Effects = getattr(DragDropEffects, "None")
            args.Handled = True
        except Exception:
            self.clear_drag_highlight()

    def on_drag_leave(self, sender, args):
        self.clear_drag_highlight()

    def on_preview_mouse_left_button_down(self, sender, args):
        """Preserve an existing multi-selection when the user Alt-drags.
        Without this, WPF ListBox changes the selection to the row under the
        mouse before PreviewMouseMove starts the drag.
        """
        try:
            if not (Keyboard.IsKeyDown(Key.LeftAlt) or Keyboard.IsKeyDown(Key.RightAlt)):
                self.alt_drag_preserved_tags = []
                return
            item = self.item_from_original_source(args.OriginalSource)
            if item is None or not item.Tag:
                self.alt_drag_preserved_tags = []
                return
            # If the user starts dragging from an already highlighted row,
            # keep the whole highlighted set intact and suppress the normal
            # ListBox mouse-down selection change.
            if item.IsSelected:
                tags = self.selected_tags()
                movable = [t for t in tags if t[0] in ("circuit", "element")]
                if movable:
                    self.alt_drag_preserved_tags = movable
                    self.drag_tags = movable
                    args.Handled = True
                    self.log("Alt-drag armed with {0} highlighted row(s).".format(len(movable)))
            else:
                # Let normal selection happen if Alt-drag begins on an unselected row.
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
                        tags = self.selected_tags()
                        movable = [t for t in tags if t[0] in ("circuit", "element")]
                    if movable:
                        self.drag_tags = movable
                        data = DataObject("CircuitHubRows", "move")
                        DragDrop.DoDragDrop(self.lbItems, data, DragDropEffects.Move)
                        self.alt_drag_preserved_tags = []
        except Exception:
            self.log("Drag start failed. Use buttons instead.")

    def on_drop(self, sender, args):
        try:
            self.clear_drag_highlight()
            if not (Keyboard.IsKeyDown(Key.LeftAlt) or Keyboard.IsKeyDown(Key.RightAlt)):
                self.log("Hold Alt while dropping to allow circuit/fixture moves.")
                return
            target_item = self.item_from_original_source(args.OriginalSource)
            if target_item is None or not target_item.Tag:
                self.log("Drop target was not a valid row. Drop directly onto the panel or circuit text while holding Alt.")
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
                pid = target[1]
                ignored = len(fixture_ids)
                msg = "Move {0} dragged circuit(s) to this panel?".format(len(circuit_ids))
                if ignored:
                    msg += "\n\nNote: {0} dragged fixture row(s) will be ignored because fixtures must be dropped onto a circuit row.".format(ignored)
                if str(MessageBox.Show(msg, "Alt-drag Circuit Move", MessageBoxButton.YesNo, MessageBoxImage.Warning)) != "Yes":
                    self.log("Alt-drag move cancelled.")
                    return
                def work():
                    panel = doc.GetElement(ElementId(pid)); moved = 0
                    for cid in circuit_ids:
                        c = doc.GetElement(ElementId(cid))
                        if c: c.SelectPanel(panel); moved += 1
                    return "Alt-drag moved {0} circuit(s) to {1}. Ignored {2} fixture row(s).".format(moved, safe_name(panel), ignored)
                self.run_direct("alt_drag_move_circuits", work, True)
                return

            if target[0] == "circuit" and fixture_ids:
                target_cid = target[1]
                ignored = len(circuit_ids)
                msg = "Reassign {0} dragged fixture(s) to this circuit?".format(len(fixture_ids))
                if ignored:
                    msg += "\n\nNote: {0} dragged circuit row(s) will be ignored because circuits must be dropped onto a panel row.".format(ignored)
                if str(MessageBox.Show(msg, "Alt-drag Fixture Reassign", MessageBoxButton.YesNo, MessageBoxImage.Warning)) != "Yes":
                    self.log("Alt-drag reassign cancelled.")
                    return
                def work():
                    target_circuit = doc.GetElement(ElementId(target_cid)); validate_phase_compatibility(target_circuit, fixture_ids)
                    removed = 0; added = 0
                    for eid in fixture_ids:
                        fixture = doc.GetElement(ElementId(eid))
                        if fixture is None: continue
                        for old_c in find_circuits_containing(eid):
                            if old_c.Id.IntegerValue == target_cid: continue
                            es_old = ElementSet(); es_old.Insert(fixture); old_c.RemoveFromCircuit(es_old); removed += 1
                        es_new = ElementSet(); es_new.Insert(fixture); target_circuit.AddToCircuit(es_new); added += 1
                    return "Alt-drag reassigned {0} fixture(s) to circuit {1}. Removed {2} old link(s). Ignored {3} circuit row(s).".format(added, circuit_number(target_circuit), removed, ignored)
                self.run_direct("alt_drag_reassign_fixtures", work, True)
                return

            self.log("Alt-drag supports multiple circuit rows onto one panel row, and multiple fixture rows onto one circuit row. Mixed selections are allowed; incompatible row types are ignored after confirmation.")
        except Exception:
            show_error("Circuit Hub Drag Error", traceback.format_exc())

    def run_direct(self, name, work, refresh=True):
        try:
            msg = run_transaction(name, work); self.log(msg)
            if refresh: self.refresh()
        except UserFacingError as ex:
            msg = str(ex)
            show_warning("Circuit Hub", msg)
            self.log("Blocked: " + name)
        except Exception as ex:
            clean_msg = clean_revit_validation_message(str(ex))
            if clean_msg:
                show_warning("Circuit Hub", clean_msg)
                self.log("Blocked by Revit validation: " + name)
                return
            err = traceback.format_exc(); print(err); show_error("Circuit Hub Error", err); self.log("FAILED: " + name)

    def selected_panel_id_from_tool(self):
        """Return the first highlighted panel row id, if the user has preselected a panel in the tool."""
        try:
            for tag in self.selected_tags():
                if tag and tag[0] == "panel":
                    return tag[1]
        except Exception:
            pass
        return None

    def choose_target_panel_id(self):
        """Use highlighted panel row first. If no panel row is highlighted, show the normal panel picker."""
        pid = self.selected_panel_id_from_tool()
        if pid is not None:
            return pid, True
        picked = self.pick_panel_id()
        return picked, False

    def pick_panel_id(self):
        names = []; lookup = {}
        for pid in sorted(CACHE.panels.keys(), key=lambda x: safe_name(CACHE.panels[x]).lower()):
            name = safe_name(CACHE.panels[pid]); names.append(name); lookup[name] = pid
        if not names: self.log("No electrical equipment found."); return None
        picked = forms.SelectFromList.show(names, title="Select panel", button_name="Select", multiselect=False)
        return lookup.get(picked) if picked else None

    # ---------------- Row selection actions ----------------
    def on_select_rows_in_view(self, sender, args):
        ids = List[ElementId](); count = 0
        for cid in self.selected_circuit_ids():
            c = doc.GetElement(ElementId(cid))
            if c:
                try:
                    for el in c.Elements: ids.Add(el.Id); count += 1
                except Exception: pass
        for eid in self.selected_element_ids():
            el = doc.GetElement(ElementId(eid))
            if el: ids.Add(el.Id); count += 1
        if count == 0: self.log("Highlight circuit/fixture rows in the tool first."); return
        uidoc.Selection.SetElementIds(ids)
        try: uidoc.ShowElements(ids)
        except Exception: pass
        self.log("Selected {0} highlighted-row element(s) in Revit.".format(count))

    def on_move_selected_circuits(self, sender, args):
        circuit_ids = self.selected_circuit_ids()
        if not circuit_ids: self.log("Highlight one or more circuit rows first."); return
        pid = self.pick_panel_id()
        if pid is None: self.log("Move cancelled."); return
        def work():
            panel = doc.GetElement(ElementId(pid)); moved = 0
            for cid in circuit_ids:
                c = doc.GetElement(ElementId(cid))
                if c: c.SelectPanel(panel); moved += 1
            return "Moved {0} highlighted circuit(s) to {1}.".format(moved, safe_name(panel))
        self.run_direct("move_highlighted_circuits", work, True)

    def on_remove_selected_fixture_rows(self, sender, args):
        eids = self.selected_element_ids()
        if not eids: self.log("Highlight one or more fixture rows first."); return
        def work():
            removed = 0
            for eid in eids:
                fixture = doc.GetElement(ElementId(eid))
                if fixture is None: continue
                for c in find_circuits_containing(eid):
                    es = ElementSet(); es.Insert(fixture); c.RemoveFromCircuit(es); removed += 1
            return "Removed {0} highlighted fixture-circuit link(s).".format(removed)
        self.run_direct("remove_highlighted_fixture_rows", work, True)

    # ---------------- Target circuit actions ----------------
    def on_rename_load(self, sender, args):
        c = self.target_circuit()
        if c is None: return
        new_name = forms.ask_for_string(default=get_load_name(c), prompt="New load name", title="Rename Load")
        if new_name is None: self.log("Rename cancelled."); return
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
        if pid is None: self.log("Move cancelled."); return
        cid = c.Id.IntegerValue
        def work():
            c2 = doc.GetElement(ElementId(cid)); panel = doc.GetElement(ElementId(pid)); c2.SelectPanel(panel)
            return "Moved target circuit {0} to {1}.".format(circuit_number(c2), safe_name(panel))
        self.run_direct("move_target_circuit", work, True)

    # ---------------- Revit selection fixture actions ----------------
    def current_revit_selection_ids(self):
        return [x.IntegerValue for x in list(uidoc.Selection.GetElementIds())]

    def on_add_revit_selection(self, sender, args):
        target = self.target_circuit()
        if target is None: return
        selected_ids = self.current_revit_selection_ids()
        if not selected_ids: self.log("Select fixture(s) in the Revit view first."); return
        target_cid = target.Id.IntegerValue
        def work():
            c = doc.GetElement(ElementId(target_cid)); validate_phase_compatibility(c, selected_ids)
            existing = elements_with_existing_circuits(selected_ids)
            if existing:
                raise UserFacingError("Add is only for uncircuited fixtures. Use Reassign for fixtures already on circuits.\n" + describe_existing_memberships(existing))
            es, count = element_set_from_ids(selected_ids)
            if count == 0: raise UserFacingError("No valid Revit-selected elements found.")
            c.AddToCircuit(es)
            return "Added {0} uncircuited Revit-selected fixture(s) to target circuit {1}.".format(count, circuit_number(c))
        self.run_direct("add_uncircuited_revit_selection", work, True)

    def on_reassign_revit_selection(self, sender, args):
        target = self.target_circuit()
        if target is None: return
        selected_ids = self.current_revit_selection_ids()
        if not selected_ids: self.log("Select fixture(s) in the Revit view first."); return
        target_cid = target.Id.IntegerValue
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
            return "Reassigned {0} Revit-selected fixture(s) to target circuit {1}. Removed {2} old link(s).".format(added, circuit_number(target_circuit), removed)
        self.run_direct("reassign_revit_selection", work, True)

    def on_reassign_revit_selection_to_panel(self, sender, args):
        """Move existing circuits for the Revit-selected fixtures to a target panel.

        If the user has highlighted a panel row in the tool, that panel is used.
        Otherwise the panel picker is shown. Circuits already assigned to the
        target panel are skipped and reported. Selected elements without an
        existing circuit are skipped.
        """
        selected_ids = self.current_revit_selection_ids()
        if not selected_ids:
            self.log("Select fixture(s) in the Revit view first.")
            return

        panel_id_value, used_tool_panel = self.choose_target_panel_id()
        if panel_id_value is None:
            self.log("Panel reassignment cancelled.")
            return

        selected_existing_circuit_ids = []
        uncircuited_ids = []
        for eid in selected_ids:
            circuits = find_circuits_containing(eid)
            if circuits:
                for c in circuits:
                    cid = c.Id.IntegerValue
                    if cid not in selected_existing_circuit_ids:
                        selected_existing_circuit_ids.append(cid)
            else:
                uncircuited_ids.append(eid)

        if not selected_existing_circuit_ids:
            show_warning(
                "Circuit Hub",
                "None of the selected Revit elements are currently on circuits.\n\n"
                "This command only moves existing circuits to another panel.\n"
                "No circuits were moved."
            )
            self.log("Blocked: no existing circuits found in Revit selection.")
            return

        circuits_to_move_ids = []
        already_on_target_ids = []
        missing_circuit_ids = []
        for cid in selected_existing_circuit_ids:
            circuit = doc.GetElement(ElementId(cid))
            if circuit is None:
                missing_circuit_ids.append(cid)
                continue
            if circuit_is_on_panel(circuit, panel_id_value):
                already_on_target_ids.append(cid)
            else:
                circuits_to_move_ids.append(cid)

        panel = doc.GetElement(ElementId(panel_id_value))
        panel_name = safe_name(panel, "<selected panel>") if panel else "<selected panel>"

        if selected_existing_circuit_ids and not circuits_to_move_ids:
            show_warning(
                "Circuit Hub",
                "All existing circuits found in the Revit selection are already assigned to panel: {0}.\n\n"
                "No circuits need to be moved.\n\n"
                "Selected elements with no existing circuit, skipped: {1}"
                .format(panel_name, len(uncircuited_ids))
            )
            self.log("Skipped: selected circuits are already on target panel {0}.".format(panel_name))
            return

        source_text = "highlighted panel row" if used_tool_panel else "panel picker"
        msg = (
            "Move existing circuits to panel: {0}\n\n"
            "Panel source: {1}\n\n"
            "This will move each existing circuit containing the selected fixture(s) to the target panel.\n"
            "It will NOT merge selected fixtures into one circuit.\n"
            "It will NOT create new circuits from uncircuited or non-circuitable selected elements.\n\n"
            "Existing circuits to move: {2}\n"
            "Existing circuits already on target panel, skipped: {3}\n"
            "Selected elements with no existing circuit, skipped: {4}\n"
            "Missing/deleted circuits, skipped: {5}\n\n"
            "Proceed?"
        ).format(
            panel_name,
            source_text,
            len(circuits_to_move_ids),
            len(already_on_target_ids),
            len(uncircuited_ids),
            len(missing_circuit_ids)
        )

        result = MessageBox.Show(msg, "Move Existing Circuits To Panel", MessageBoxButton.YesNo, MessageBoxImage.Warning)
        if str(result) != "Yes":
            self.log("Panel circuit move cancelled.")
            return

        def work():
            panel = doc.GetElement(ElementId(panel_id_value))
            if panel is None:
                raise UserFacingError("Selected panel no longer exists.")

            moved = 0
            skipped_now_on_target = 0
            skipped_missing = 0
            for cid in circuits_to_move_ids:
                circuit = doc.GetElement(ElementId(cid))
                if circuit is None:
                    skipped_missing += 1
                    continue
                if circuit_is_on_panel(circuit, panel_id_value):
                    skipped_now_on_target += 1
                    continue
                circuit.SelectPanel(panel)
                moved += 1
                self.target_circuit_id = cid

            detail = ""
            total_already = len(already_on_target_ids) + skipped_now_on_target
            if total_already:
                detail += " Skipped {0} circuit(s) already on target panel.".format(total_already)
            if uncircuited_ids:
                detail += " Skipped {0} selected element(s) with no existing circuit.".format(len(uncircuited_ids))
            total_missing = len(missing_circuit_ids) + skipped_missing
            if total_missing:
                detail += " Skipped {0} missing existing circuit(s).".format(total_missing)

            return "Moved {0} existing circuit(s) to panel {1}.{2}".format(moved, safe_name(panel), detail)

        self.run_direct("move_selected_existing_circuits_to_panel", work, True)

    def on_remove_revit_selection(self, sender, args):
        target = self.target_circuit()
        if target is None: return
        selected_ids = self.current_revit_selection_ids()
        if not selected_ids: self.log("Select fixture(s) in the Revit view first."); return
        target_cid = target.Id.IntegerValue
        def work():
            c = doc.GetElement(ElementId(target_cid)); es, count = element_set_from_ids(selected_ids)
            if count == 0: raise UserFacingError("No valid Revit-selected elements found.")
            c.RemoveFromCircuit(es)
            return "Removed {0} Revit-selected fixture(s) from target circuit {1}.".format(count, circuit_number(c))
        self.run_direct("remove_revit_selection", work, True)

    def on_create_circuit(self, sender, args):
        selected_ids = self.current_revit_selection_ids()
        if not selected_ids: self.log("Select electrical component(s) in Revit first."); return

        existing = elements_with_existing_circuits(selected_ids)
        remove_existing = False
        if existing:
            msg = ("One or more selected fixtures already has a circuit.\n\n"
                   "Do you want Circuit Hub to remove the selected fixtures from their existing circuit(s) and create a new circuit from the same selection?\n\n"
                   + describe_existing_memberships(existing))
            result = MessageBox.Show(msg, "Create New Circuit", MessageBoxButton.YesNo, MessageBoxImage.Warning)
            if str(result) != "Yes":
                self.log("Create circuit cancelled because selected fixture(s) already have circuits.")
                return
            remove_existing = True

        def work():
            validate_selection_same_phase(selected_ids)
            removed = 0
            if remove_existing:
                for eid, circuits in existing:
                    fixture = doc.GetElement(ElementId(eid))
                    if fixture is None:
                        continue
                    for old_circuit in circuits:
                        es_old = ElementSet(); es_old.Insert(fixture)
                        old_circuit.RemoveFromCircuit(es_old)
                        removed += 1
            c = ElectricalSystem.Create(doc, id_list(selected_ids), ElectricalSystemType.PowerCircuit)
            self.target_circuit_id = c.Id.IntegerValue
            return "Created new circuit {0} with {1} Revit-selected element(s). Removed {2} old circuit link(s).".format(circuit_number(c), len(selected_ids), removed)
        self.run_direct("create_circuit_from_selection", work, True)

    def on_create_schedule(self, sender, args):
        pid = self.pick_panel_id()
        if pid is None: self.log("Schedule cancelled."); return
        def work():
            view = PanelScheduleView.CreateInstanceView(doc, ElementId(pid))
            return "Created panel schedule: {0}".format(safe_name(view))
        self.run_direct("create_schedule", work, False)


def on_revit_document_changed(sender, args):
    try:
        if APP is not None:
            APP.queue_refresh_from_doc_changed()
    except Exception:
        print(traceback.format_exc())


def main():
    global APP
    if doc is None or doc.IsFamilyDocument:
        MessageBox.Show("Open a Revit project document before running Circuit Hub.", "Circuit Hub", MessageBoxButton.OK, MessageBoxImage.Warning)
        return
    global DOC_CHANGED_HANDLER
    APP = CircuitHub()
    try:
        DOC_CHANGED_HANDLER = on_revit_document_changed
        uidoc.Application.Application.DocumentChanged += DOC_CHANGED_HANDLER
        APP.log("Auto-refresh on Revit Undo/Redo/document changes enabled.")
    except Exception:
        APP.log("Auto-refresh subscription failed; use Refresh manually if needed.")
    APP.Show()

main()
