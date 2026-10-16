# -*- coding: utf-8 -*-
"""Modeless, searchable browser for Space and Room parameters.

Modeless rules (see docs/pyrevit-patterns.md):
  * __persistentengine__ keeps the engine alive so the window's handlers keep working.
  * The window only works on cached plain-Python data. Every Revit read (refresh, loading
    extra parameter columns) and every selection change runs in ExternalEvent.Execute.
  * The tool never modifies the model, so it never opens a transaction.
"""
__title__ = "Parameter\nBrowser"
__doc__ = "Modeless searchable parameter browser for Spaces and Rooms."
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
from System.Windows.Controls import DataGridTextColumn, DataGridLength, DataGridRow, ItemsControl
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
               "category": category_name, "values": {}}
        read_values(doc, e, rec, fields)
        records.append(rec)
    return records, names


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
        source, scope, self.fields = logic.normalize_settings(load_settings())

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
                       "fields": list(self.fields)})

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
        for source, category in SOURCE_CATEGORIES.items():
            records, names = read_source(doc, category, view_id, self.fields)
            self.records[source] = records
            self.param_names[source] = names
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
            raise UserError("The selected rows no longer exist in the model. Click Refresh Model.")
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
        for i in range(len(fields)):
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

    # --- WPF event handlers (names referenced from UI.xaml) ---------------------------------
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
