# -*- coding: utf-8 -*-
"""Modeless, searchable browser for Space and Room parameters and the elements inside them.

Modeless rules (see docs/pyrevit-patterns.md):
  * __persistentengine__ keeps the engine alive so the window's handlers keep working.
  * The window only works on cached plain-Python data. Every Revit read (refresh, loading
    extra parameter columns) and every selection change runs in ExternalEvent.Execute.
  * The tool never modifies the model, so it never opens a transaction.
"""
__title__ = "Parameter\nBrowser"
__doc__ = "Modeless searchable parameter browser for Spaces and Rooms, listing the elements in each."
__persistentengine__ = True

import os
import json
import traceback
from functools import wraps

import clr
clr.AddReference("System.Data")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
from System import Int32, Int64
from System.Collections.Generic import List
from System.Data import DataTable
from System.Windows import Visibility
from System.Windows.Controls import (DataGridTextColumn, DataGridLength, DataGridRow, ItemsControl,
                                     TreeViewItem)
from System.Windows.Data import Binding

from pyrevit import DB, UI, HOST_APP, forms, revit, script

import parameter_browser_logic as logic

logger = script.get_logger()
XAML_PATH = script.get_bundle_file("UI.xaml")
USE_64_BIT_IDS = int(HOST_APP.version) >= 2024
SOURCE_CATEGORIES = {
    "Spaces": DB.BuiltInCategory.OST_MEPSpaces,
    "Rooms": DB.BuiltInCategory.OST_Rooms,
}
SETTINGS_FILE = os.path.join(
    os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming"),
    "pyRevit", "ParameterBrowser.settings.json")

WINDOW = globals().get("WINDOW")


class UserError(Exception):
    pass


def load_settings():
    try:
        with open(SETTINGS_FILE, "r") as fp:
            return json.load(fp)
    except Exception:
        return {}


def save_settings(data):
    try:
        folder = os.path.dirname(SETTINGS_FILE)
        if not os.path.exists(folder):
            os.makedirs(folder)
        with open(SETTINGS_FILE, "w") as fp:
            json.dump(data, fp, indent=2)
    except Exception:
        logger.debug("Could not save settings: {}".format(traceback.format_exc()))


def as_eid(value):
    return DB.ElementId(Int64(value) if USE_64_BIT_IDS else Int32(value))


def ui_handler(func):
    """Never let an exception escape a WPF event handler of a modeless window."""
    @wraps(func)
    def wrapper(self, *args):
        try:
            return func(self, *args)
        except Exception as ex:
            logger.error(traceback.format_exc())
            self.set_status("ERROR: {}".format(ex))
    return wrapper


# --- Revit reads: only call these from the command itself or ExternalEvent.Execute ----------
def element_name(doc, element_id):
    try:
        target = doc.GetElement(element_id)
        return target.Name if target is not None else None
    except Exception:
        return None


def read_values(doc, element, rec, fields):
    for field in fields:
        if field in logic.PSEUDO_FIELDS:
            continue
        value = u""
        if element is not None:
            try:
                value = logic.format_parameter_value(
                    element.LookupParameter(field), lambda eid: element_name(doc, eid))
            except Exception:
                value = u""
        rec["values"][field] = value


def read_source(doc, category, view_id, wanted_fields):
    """Return (records, sorted parameter names) for one category as plain Python data."""
    elements = list(DB.FilteredElementCollector(doc).OfCategory(category)
                    .WhereElementIsNotElementType().ToElements())
    visible = set()
    if view_id is not None:
        try:
            for e in (DB.FilteredElementCollector(doc, view_id).OfCategory(category)
                      .WhereElementIsNotElementType().ToElementIds()):
                visible.add(logic.element_id_value(e))
        except Exception:
            # Sheets, schedules and other non-graphical views cannot be filtered by view.
            pass

    names = set(logic.PSEUDO_FIELDS)
    for e in elements:
        for p in e.Parameters:
            try:
                if p.Definition is not None and p.Definition.Name:
                    names.add(logic.to_text(p.Definition.Name))
            except Exception:
                pass
    names = sorted(names, key=lambda x: x.lower())
    fields = logic.resolve_fields(wanted_fields, names)

    level_names = {}
    records = []
    for e in elements:
        eid = logic.element_id_value(e.Id)
        level_key = logic.element_id_value(e.LevelId)
        if level_key not in level_names:
            level_names[level_key] = logic.to_text(element_name(doc, e.LevelId) or u"")
        try:
            category_name = logic.to_text(e.Category.Name)
        except Exception:
            category_name = u""
        rec = {"id": eid, "level": level_names[level_key], "visible": eid in visible,
               "category": category_name, "label": spatial_label(e, eid), "values": {}, "counts": {}}
        read_values(doc, e, rec, fields)
        records.append(rec)
    return records, names


def spatial_label(element, eid):
    number = name = u""
    try:
        number = logic.to_text(element.Number)
    except Exception:
        pass
    try:
        name = logic.format_parameter_value(element.get_Parameter(DB.BuiltInParameter.ROOM_NAME))
    except Exception:
        pass
    return u" ".join([x for x in (number, name) if x]) or u"Id {}".format(eid)


def spatial_phases(doc, category):
    """Phases that the project's Spaces/Rooms of this category are placed in, latest phase first."""
    found = {}
    for e in (DB.FilteredElementCollector(doc).OfCategory(category)
              .WhereElementIsNotElementType().ToElements()):
        phase_id = None
        try:
            param = e.get_Parameter(DB.BuiltInParameter.ROOM_PHASE)
            if param is not None and param.HasValue:
                phase_id = param.AsElementId()
        except Exception:
            phase_id = None
        if phase_id is None or logic.element_id_value(phase_id) < 0:
            phase_id = e.CreatedPhaseId
        key = logic.element_id_value(phase_id)
        if key >= 0 and key not in found:
            found[key] = phase_id
    sequence = [logic.element_id_value(p.Id) for p in doc.Phases]
    phases = []
    for key in logic.order_phase_ids(list(found.keys()), sequence):
        phase = doc.GetElement(found[key])
        if phase is not None:
            phases.append(phase)
    return phases


class SpatialLookup(object):
    """FamilyInstance -> Space/Room id using get_Space(phase)/get_Room(phase) with explicit phases.

    The phase-less .Space/.Room properties are ambiguous in IronPython and only use the project's
    last phase, so they are not used.
    """

    def __init__(self, method_name, phases):
        self.method_name = method_name
        self.property_name = method_name[len("get_"):]
        self.phases = phases
        self.found = 0
        self.errors = 0
        self.first_error = None

    def lookup(self, element):
        for phase in self.phases:
            try:
                target = getattr(element, self.method_name)(phase)
            except Exception as ex:
                try:
                    target = getattr(element, self.property_name)[phase]
                except Exception:
                    self.errors += 1
                    if self.first_error is None:
                        self.first_error = logic.to_text(ex)
                    continue
            if target is not None:
                self.found += 1
                return logic.element_id_value(target.Id)
        return None

    def summary(self, noun):
        if not self.phases:
            return u"No {} found in this model.".format(noun)
        text = u"{} matched in phase: {}".format(
            noun.capitalize(), u", ".join([logic.to_text(p.Name) for p in self.phases]))
        if self.errors:
            text += u" ({} lookup error(s), first: {})".format(self.errors, self.first_error)
        return text


def read_contents(doc, space_lookup, room_lookup):
    """Family instances of CONTENT_CATEGORIES with the Space/Room each one is in."""
    items = []
    level_names = {}
    for label, bic_name in logic.CONTENT_CATEGORIES:
        bic = getattr(DB.BuiltInCategory, bic_name, None)
        if bic is None:
            continue
        try:
            elements = (DB.FilteredElementCollector(doc).OfCategory(bic)
                        .WhereElementIsNotElementType().ToElements())
        except Exception:
            continue
        for e in elements:
            if not isinstance(e, DB.FamilyInstance):
                continue
            level_key = logic.element_id_value(e.LevelId)
            if level_key not in level_names:
                level_names[level_key] = logic.to_text(element_name(doc, e.LevelId) or u"")
            try:
                family = logic.to_text(e.Symbol.FamilyName)
            except Exception:
                family = u""
            try:
                mark = logic.format_parameter_value(e.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK))
            except Exception:
                mark = u""
            items.append({
                "id": logic.element_id_value(e.Id),
                "category": label,
                "family": family,
                "type": logic.to_text(e.Name),
                "mark": mark,
                "level": level_names[level_key],
                "space": space_lookup.lookup(e),
                "room": room_lookup.lookup(e),
            })
    return items


class BrowserEventHandler(UI.IExternalEventHandler):
    def __init__(self, window):
        self.window = window

    def Execute(self, uiapp):
        try:
            self.window.run_queued(uiapp)
        except Exception:
            logger.error(traceback.format_exc())

    def GetName(self):
        return "Parameter Browser"


class ParameterBrowser(forms.WPFWindow):
    def __init__(self, doc, uidoc):
        self.initialising = True
        self.closed = False
        forms.WPFWindow.__init__(self, XAML_PATH)
        self.queue = logic.ActionQueue()
        self.handler = BrowserEventHandler(self)
        # Must be created while the command still has a valid API context.
        self.ext_event = UI.ExternalEvent.Create(self.handler)

        self.doc = None
        self.doc_title = u""
        self.view_name = u""
        self.records = {}
        self.param_names = {}
        self.pending_fields = set()
        self.table = None
        self.contents = {}
        self.contents_ids = []
        self.phase_notes = {}
        settings = load_settings()
        source, scope, self.fields = logic.normalize_settings(settings)
        self.content_categories = logic.normalize_content_categories(
            settings.get("content_categories") if isinstance(settings, dict) else None)

        for name in logic.SOURCES:
            self.cmbSource.Items.Add(name)
        for name in logic.SCOPES:
            self.cmbScope.Items.Add(name)
        self.cmbSource.SelectedItem = source
        self.cmbScope.SelectedItem = scope
        self.Closed += self.on_closed

        self.capture_model(doc, uidoc)
        self.initialising = False
        self.populate_levels()
        self.rebuild_view()

    # --- state helpers ----------------------------------------------------------------------
    def current_source(self):
        return str(self.cmbSource.SelectedItem or logic.SOURCES[0])

    def current_scope(self):
        return str(self.cmbScope.SelectedItem or logic.SCOPES[0])

    def current_level(self):
        return logic.to_text(self.cmbLevel.SelectedItem) if self.cmbLevel.SelectedItem is not None else None

    def set_status(self, text):
        try:
            self.lblStatus.Text = text
        except Exception:
            pass

    def persist_settings(self):
        save_settings({"source": self.current_source(), "scope": self.current_scope(),
                       "fields": list(self.fields), "content_categories": list(self.content_categories)})

    # --- ExternalEvent plumbing -------------------------------------------------------------
    def run_in_revit(self, label, action):
        """Queue action(uiapp); it runs in Revit's API context once Revit is idle."""
        if self.closed:
            return False
        self.queue.add(label, action)
        result = self.ext_event.Raise()
        if result not in (UI.ExternalEventRequest.Accepted, UI.ExternalEventRequest.Pending):
            self.queue.clear()
            self.pending_fields = set()
            self.set_status("Revit could not queue '{}' ({}). Try again.".format(label, result))
            return False
        return True

    def run_queued(self, uiapp):
        if self.closed:
            self.queue.clear()
            return
        self.queue.drain(uiapp, self.on_action_error)

    def on_action_error(self, label, ex):
        if isinstance(ex, UserError):
            self.set_status(logic.to_text(ex))
            return
        logger.error("{} failed:\n{}".format(label, traceback.format_exc()))
        self.set_status("ERROR in {}: {}".format(label, ex))

    def cached_doc(self):
        if self.doc is None or not self.doc.IsValidObject:
            raise UserError("The project this list was read from is closed. Click Refresh Model.")
        return self.doc

    def active_uidoc(self, uiapp):
        doc = self.cached_doc()
        uidoc = uiapp.ActiveUIDocument
        if uidoc is None or not doc.Equals(uidoc.Document):
            raise UserError("Switch back to '{}' or click Refresh Model to load the active project."
                            .format(self.doc_title))
        return uidoc

    # --- Model reads (API context only) -----------------------------------------------------
    def capture_model(self, doc, uidoc):
        view = uidoc.ActiveView
        view_id = view.Id if view is not None else None
        lookups = {
            "Spaces": SpatialLookup("get_Space", spatial_phases(doc, SOURCE_CATEGORIES["Spaces"])),
            "Rooms": SpatialLookup("get_Room", spatial_phases(doc, SOURCE_CATEGORIES["Rooms"])),
        }
        items = read_contents(doc, lookups["Spaces"], lookups["Rooms"])
        self.phase_notes = dict((k, v.summary(k.lower())) for k, v in lookups.items())
        for source, category in SOURCE_CATEGORIES.items():
            records, names = read_source(doc, category, view_id, self.fields)
            index = logic.index_contents(items, "space" if source == "Spaces" else "room")
            for rec in records:
                rec["counts"] = logic.category_counts(index.get(rec["id"], []))
            self.records[source] = records
            self.param_names[source] = sorted(names + logic.count_fields(), key=lambda x: x.lower())
            self.contents[source] = index
        self.doc = doc
        self.doc_title = logic.to_text(doc.Title)
        self.view_name = logic.to_text(view.Name) if view is not None else u""
        self.pending_fields = set()

    def refresh_from_revit(self, uiapp):
        uidoc = uiapp.ActiveUIDocument
        if uidoc is None or uidoc.Document.IsFamilyDocument:
            raise UserError("Open a project (not a family) and click Refresh Model again.")
        self.capture_model(uidoc.Document, uidoc)
        self.populate_levels()
        self.rebuild_view()

    def load_fields_from_revit(self, uiapp, source, fields):
        try:
            doc = self.cached_doc()
            for rec in self.records.get(source, []):
                read_values(doc, doc.GetElement(as_eid(rec["id"])), rec, fields)
        finally:
            self.pending_fields.difference_update([(source, f) for f in fields])
        self.rebuild_view()

    def select_in_revit(self, uiapp, element_ids, show):
        uidoc = self.active_uidoc(uiapp)
        doc = uidoc.Document
        ids = List[DB.ElementId]()
        for value in element_ids:
            element = doc.GetElement(as_eid(value))
            if element is not None:
                ids.Add(element.Id)
        if ids.Count == 0:
            raise UserError("Those elements no longer exist in the model. Click Refresh Model.")
        uidoc.Selection.SetElementIds(ids)
        if show:
            try:
                uidoc.ShowElements(ids)
            except Exception as ex:
                self.set_status("{} selected, but Revit could not show them: {}".format(ids.Count, ex))
                return
        self.set_status("{} element(s) {} in Revit".format(ids.Count, "shown" if show else "selected"))

    # --- Cached-data UI (no Revit API) ------------------------------------------------------
    def populate_levels(self):
        old = self.current_level()
        was_initialising = self.initialising
        self.initialising = True
        try:
            self.cmbLevel.Items.Clear()
            levels = logic.level_options(self.records.get(self.current_source(), []))
            for name in levels:
                self.cmbLevel.Items.Add(name)
            if old in levels:
                self.cmbLevel.SelectedItem = old
            elif levels:
                self.cmbLevel.SelectedIndex = 0
        finally:
            self.initialising = was_initialising
        self.update_level_visibility()

    def update_level_visibility(self):
        state = Visibility.Visible if self.current_scope() == "Level" else Visibility.Collapsed
        self.lblLevel.Visibility = state
        self.cmbLevel.Visibility = state

    def rebuild_view(self):
        source = self.current_source()
        all_records = self.records.get(source, [])
        fields = logic.resolve_fields(self.fields, self.param_names.get(source, []))

        missing = [f for f in logic.missing_fields(all_records, fields)
                   if (source, f) not in self.pending_fields]
        if missing:
            self.pending_fields.update([(source, f) for f in missing])
            if self.run_in_revit("Load parameters",
                                 lambda uiapp: self.load_fields_from_revit(uiapp, source, missing)):
                self.set_status("Reading {} from the model...".format(", ".join(missing)))
            return
        if any((source, f) in self.pending_fields for f in fields):
            return

        records = logic.filter_records(all_records, self.current_scope(), self.current_level())
        table = DataTable("Rows")
        table.Columns.Add(logic.ID_COLUMN)
        table.Columns.Add(logic.SEARCH_COLUMN)
        for i, field in enumerate(fields):
            if logic.is_count_field(field):
                # Typed column so counts sort numerically.
                table.Columns.Add("P{}".format(i), clr.GetClrType(Int32))
            else:
                table.Columns.Add("P{}".format(i))
        table.BeginLoadData()
        for rec in records:
            values = [logic.record_value(rec, f) for f in fields]
            row = table.NewRow()
            row[logic.ID_COLUMN] = logic.to_text(rec["id"])
            row[logic.SEARCH_COLUMN] = logic.search_text(values)
            for i, value in enumerate(values):
                row["P{}".format(i)] = value
            table.Rows.Add(row)
        table.EndLoadData()

        self.grid.ItemsSource = None
        self.grid.Columns.Clear()
        for i, field in enumerate(fields):
            column = DataGridTextColumn()
            column.Header = field
            column.Binding = Binding("P{}".format(i))
            column.SortMemberPath = "P{}".format(i)
            column.IsReadOnly = True
            column.MinWidth = 60.0
            column.Width = DataGridLength(160.0)
            self.grid.Columns.Add(column)
        self.table = table
        self.grid.ItemsSource = table.DefaultView
        self.apply_search()
        self.update_contents()

        scope = self.current_scope()
        if scope == "Current view":
            scope = u"view '{}' (as of last refresh)".format(self.view_name)
        elif scope == "Level":
            scope = u"level '{}'".format(self.current_level() or u"-")
        else:
            scope = u"whole project"
        self.set_status(u"{} {} in {} - {}".format(len(records), source.lower(), scope, self.doc_title))
        self.persist_settings()

    def apply_search(self):
        if self.table is None:
            return
        view = self.table.DefaultView
        view.RowFilter = logic.build_row_filter(self.txtSearch.Text or u"")
        self.lblCount.Text = "{} shown / {} loaded".format(view.Count, self.table.Rows.Count)

    def selected_element_ids(self):
        ids = []
        for item in self.grid.SelectedItems:
            try:
                ids.append(int(item.Row[logic.ID_COLUMN]))
            except Exception:
                pass
        return ids

    def request_selection(self, show):
        ids = self.selected_element_ids()
        if not ids:
            self.set_status("Select one or more rows first.")
            return
        if self.run_in_revit("Show elements" if show else "Select elements",
                             lambda uiapp: self.select_in_revit(uiapp, ids, show)):
            self.set_status("Sending {} element(s) to Revit...".format(len(ids)))

    # --- Contents side panel (cached data only) ---------------------------------------------
    def singular(self):
        return "space" if self.current_source() == "Spaces" else "room"

    def update_contents(self):
        source = self.current_source()
        index = self.contents.get(source, {})
        noun = self.singular()
        if self.chkUnassigned.IsChecked:
            items = index.get(None, [])
            header = u"Not in any {} - {} element(s)".format(noun, len(items))
        else:
            ids = self.selected_element_ids()
            items = []
            for eid in ids:
                items.extend(index.get(eid, []))
            if not ids:
                header = u"Select a {} to list the elements in it.".format(noun)
            elif len(ids) == 1:
                rec = [r for r in self.records.get(source, []) if r["id"] == ids[0]]
                header = u"{} - {} element(s)".format(rec[0]["label"] if rec else ids[0], len(items))
            else:
                header = u"{} {}s selected - {} element(s)".format(len(ids), noun, len(items))
        groups = logic.group_contents(items, self.content_categories)
        self.contents_ids = [i for _, ids_, _ in groups for i in ids_]
        if len(self.contents_ids) != len(items):
            header += u" ({} hidden by Categories)".format(len(items) - len(self.contents_ids))
        note = self.phase_notes.get(source)
        self.lblContents.Text = header + (u"\n" + note if note else u"")

        self.treeContents.Items.Clear()
        for category, cat_ids, types in groups:
            cat_node = self.tree_node(u"{} ({})".format(category, len(cat_ids)), cat_ids)
            cat_node.IsExpanded = True
            for type_name, type_ids, instances in types:
                type_node = self.tree_node(u"{} ({})".format(type_name, len(type_ids)), type_ids)
                for label, iid in instances:
                    type_node.Items.Add(self.tree_node(label, [iid]))
                cat_node.Items.Add(type_node)
            self.treeContents.Items.Add(cat_node)

    @staticmethod
    def tree_node(header, ids):
        node = TreeViewItem()
        node.Header = header
        node.Tag = list(ids)
        return node

    def contents_selection(self):
        """Ids of the highlighted tree node, or everything listed when nothing is highlighted."""
        node = self.treeContents.SelectedItem
        if node is not None and node.Tag:
            return list(node.Tag)
        return list(self.contents_ids)

    def request_contents(self, show):
        ids = self.contents_selection()
        if not ids:
            self.set_status("No elements listed.")
            return
        if self.run_in_revit("Show elements" if show else "Select elements",
                             lambda uiapp: self.select_in_revit(uiapp, ids, show)):
            self.set_status("Sending {} element(s) to Revit...".format(len(ids)))

    # --- WPF event handlers (names referenced from UI.xaml) ---------------------------------
    @ui_handler
    def grid_selection_changed(self, sender, args):
        if not self.initialising and not self.chkUnassigned.IsChecked:
            self.update_contents()

    @ui_handler
    def unassigned_toggled(self, sender, args):
        if not self.initialising:
            self.update_contents()

    @ui_handler
    def choose_categories(self, sender, args):
        items = [forms.TemplateListItem(c, checked=(c in self.content_categories)) for c in logic.CONTENT_LABELS]
        chosen = forms.SelectFromList.show(items, title="Categories listed in the panel", multiselect=True,
                                           button_name="Apply")
        if chosen is None:
            return
        self.content_categories = logic.normalize_content_categories([logic.to_text(c) for c in chosen])
        self.persist_settings()
        self.update_contents()

    @ui_handler
    def contents_select_clicked(self, sender, args):
        self.request_contents(False)

    @ui_handler
    def contents_show_clicked(self, sender, args):
        self.request_contents(True)

    @ui_handler
    def source_changed(self, sender, args):
        if self.initialising:
            return
        self.populate_levels()
        self.rebuild_view()

    @ui_handler
    def scope_changed(self, sender, args):
        if self.initialising:
            return
        self.update_level_visibility()
        self.rebuild_view()

    @ui_handler
    def level_changed(self, sender, args):
        if self.initialising or self.current_scope() != "Level":
            return
        self.rebuild_view()

    @ui_handler
    def search_changed(self, sender, args):
        if not self.initialising:
            self.apply_search()

    @ui_handler
    def clear_clicked(self, sender, args):
        self.txtSearch.Text = ""

    @ui_handler
    def choose_columns(self, sender, args):
        names = self.param_names.get(self.current_source(), [])
        current = logic.resolve_fields(self.fields, names)
        items = [forms.TemplateListItem(n, checked=(n in current))
                 for n in logic.column_choices(names, current)]
        chosen = forms.SelectFromList.show(items, title="Choose Parameters", multiselect=True,
                                           button_name="Apply Columns")
        if chosen is None:
            return
        if not chosen:
            self.set_status("Select at least one parameter.")
            return
        self.fields = [logic.to_text(x) for x in chosen]
        self.rebuild_view()

    @ui_handler
    def refresh_clicked(self, sender, args):
        if self.run_in_revit("Refresh model", self.refresh_from_revit):
            self.set_status("Reading the model...")

    @ui_handler
    def select_clicked(self, sender, args):
        self.request_selection(False)

    @ui_handler
    def show_clicked(self, sender, args):
        self.request_selection(True)

    @ui_handler
    def row_double_clicked(self, sender, args):
        # Ignore double-clicks on headers and scrollbars.
        if isinstance(ItemsControl.ContainerFromElement(self.grid, args.OriginalSource), DataGridRow):
            self.request_selection(True)

    @ui_handler
    def close_clicked(self, sender, args):
        self.Close()

    def on_closed(self, sender, args):
        global WINDOW
        self.closed = True
        self.queue.clear()
        self.persist_settings()
        try:
            self.ext_event.Dispose()
        except Exception:
            pass
        if WINDOW is self:
            WINDOW = None


def main():
    global WINDOW
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Open a Revit project before running Parameter Browser.", title="Parameter Browser")
        return
    if WINDOW is not None and not WINDOW.closed:
        try:
            WINDOW.Close()
        except Exception:
            pass
    WINDOW = ParameterBrowser(doc, revit.uidoc)
    WINDOW.show(modal=False)


main()
