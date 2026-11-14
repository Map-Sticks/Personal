# -*- coding: utf-8 -*-
"""Create one filled region per selected Room or Space in the active plan view."""
__title__ = "Room\nRegions"
__author__ = "Custom"
__persistentengine__ = True
__min_revit_ver__ = 2023
__max_revit_ver__ = 2027

import json
import traceback
from functools import wraps

import clr
clr.AddReference("System.Data")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System import Action, Boolean, Double
from System.Data import DataTable
from System.Windows import WindowState
from System.Windows.Controls import ComboBoxItem, ListBoxItem
from System.Windows.Input import Keyboard, ModifierKeys

from pyrevit import UI, forms, script
from pyrevit.coreutils import envvars

import room_regions_logic as logic
import room_regions_revit as backend


logger = script.get_logger()
XAML_PATH = script.get_bundle_file("ui.xaml")
WINDOW_KEY = "MAP_STICKS_ROOM_REGIONS_WINDOW"
BOUNDARY_CHOICES = ("Finish", "Center", "CoreBoundary", "CoreCenter")


def ui_handler(method):
    @wraps(method)
    def wrapped(self, *args):
        try:
            return method(self, *args)
        except Exception as ex:
            self.report_error(method.__name__, ex)
    return wrapped


class RoomRegionsEventHandler(UI.IExternalEventHandler):
    def __init__(self, window):
        self.window = window

    def Execute(self, uiapp):
        window = self.window
        try:
            window.queue.drain(uiapp, window.report_error)
        finally:
            if not window.closed:
                window.busy = False
                window.update_enabled()

    def GetName(self):
        return "Room Regions"


class RoomRegionsWindow(forms.WPFWindow):
    def __init__(self):
        self.initialising = True
        self.closed = False
        self.busy = False
        self.pending_transaction = False
        self.snapshot = None
        self.table = None
        self.records = {}
        self.anchor = None
        self.last_created = []
        self.created_token = None
        self.startup_warnings = []
        self.config = script.get_config()
        forms.WPFWindow.__init__(self, XAML_PATH)
        self.queue = logic.ActionQueue()
        self.handler = RoomRegionsEventHandler(self)
        self.ext_event = UI.ExternalEvent.Create(self.handler)
        self.load_settings()
        self.initialising = False
        self.update_enabled()
        for warning in self.startup_warnings:
            self.append_log(warning)

    def setting(self, name, default):
        try:
            if not self.config.has_option(name):
                return default
            return getattr(self.config, name)
        except Exception as ex:
            message = "Could not read setting '{}': {}; using its default.".format(name, ex)
            logger.error(message)
            self.startup_warnings.append(message)
            return default

    def boolean_setting(self, name, default):
        value = self.setting(name, default)
        if isinstance(value, bool):
            return value
        message = "Invalid setting '{}'; using its default.".format(name)
        logger.error(message)
        self.startup_warnings.append(message)
        return default

    def load_settings(self):
        self.spacesCheck.IsChecked = self.boolean_setting("host_spaces", True)
        self.roomsCheck.IsChecked = self.boolean_setting("host_rooms", True)
        self.linksCheck.IsChecked = self.boolean_setting("linked_rooms", False)
        self.allLevelsCheck.IsChecked = self.boolean_setting("all_levels", False)
        self.phaseCheck.IsChecked = self.boolean_setting("follow_phase", True)
        self.islandsCheck.IsChecked = self.boolean_setting("include_islands", True)
        self.simplifyCheck.IsChecked = self.boolean_setting("simplify", False)
        boundary = self.setting("boundary", "Finish")
        if boundary not in BOUNDARY_CHOICES:
            self.startup_warnings.append("Invalid boundary setting; using Finish.")
            boundary = "Finish"
        for choice in BOUNDARY_CHOICES:
            self.boundaryBox.Items.Add(choice)
        self.boundaryBox.SelectedItem = boundary
        self.saved_project = self.setting("project_uid", u"")
        self.saved_type = self.setting("type_id", u"")
        saved_links = self.setting("link_ids", u"")
        self.saved_links = None
        if saved_links:
            try:
                links = json.loads(saved_links)
                if not isinstance(links, list) or not all(
                        isinstance(link, (str, logic.text_type)) for link in links):
                    raise ValueError("expected a list of link identifiers")
                self.saved_links = set(links)
            except (TypeError, ValueError) as ex:
                message = "Could not read saved link selection: {}; no links were selected.".format(ex)
                logger.error(message)
                self.startup_warnings.append(message)

    def save_settings(self):
        try:
            self.config.host_spaces = bool(self.spacesCheck.IsChecked)
            self.config.host_rooms = bool(self.roomsCheck.IsChecked)
            self.config.linked_rooms = bool(self.linksCheck.IsChecked)
            self.config.all_levels = bool(self.allLevelsCheck.IsChecked)
            self.config.follow_phase = bool(self.phaseCheck.IsChecked)
            self.config.include_islands = bool(self.islandsCheck.IsChecked)
            self.config.simplify = bool(self.simplifyCheck.IsChecked)
            self.config.boundary = logic.text_type(self.boundaryBox.SelectedItem)
            if self.snapshot and self.snapshot["token"]:
                self.config.project_uid = self.snapshot["token"][0]
                self.config.type_id = self.selected_type_id()
                self.config.link_ids = json.dumps(sorted(self.selected_links()))
            script.save_config()
            if self.snapshot and self.snapshot["token"]:
                self.saved_project = self.config.project_uid
                self.saved_type = self.config.type_id
                self.saved_links = set(self.selected_links())
            return True
        except Exception as ex:
            self.report_error("Save settings", ex)
            return False

    def append_log(self, message):
        text = logic.text_type(message)
        if self.logText.Text:
            self.logText.AppendText(u"\n")
        self.logText.AppendText(text)
        self.logText.ScrollToEnd()

    def set_status(self, message):
        self.statusText.Text = logic.text_type(message)

    def report_error(self, label, error):
        message = u"{}: {}".format(label, error)
        logger.error("{}\n{}".format(message, traceback.format_exc()))
        if not self.closed:
            self.set_status(message)
            self.append_log(message)

    def run_in_revit(self, label, action):
        if self.closed:
            return False
        if self.pending_transaction:
            self.set_status("Resolve Revit's pending failure handling before continuing.")
            return False
        if self.busy:
            self.set_status("Wait for the current operation or cancel its progress dialog.")
            return False
        self.busy = True
        self.update_enabled()
        self.queue.add(label, action)
        try:
            result = self.ext_event.Raise()
        except Exception as ex:
            self.queue.clear()
            self.busy = False
            self.update_enabled()
            self.report_error("Queue {}".format(label), ex)
            return False
        if result not in (UI.ExternalEventRequest.Accepted, UI.ExternalEventRequest.Pending):
            self.queue.clear()
            self.busy = False
            self.update_enabled()
            message = "Revit could not queue {} ({}). Try again.".format(label, result)
            logger.error(message)
            self.set_status(message)
            self.append_log(message)
            return False
        self.set_status("{} queued; waiting for Revit.".format(label))
        return True

    def update_enabled(self):
        if self.closed or self.initialising:
            return
        available = self.snapshot is not None and not self.snapshot["error"]
        writable = available and not self.snapshot["read_only"]
        ready = not self.busy and not self.pending_transaction
        self.workArea.IsEnabled = bool(ready and available)
        self.refreshButton.IsEnabled = ready
        self.loadSourcesButton.IsEnabled = bool(ready and available)
        self.createButton.IsEnabled = bool(
            ready and writable and self.selected_type_id() and self.visible_ticked())
        self.selectCreatedButton.IsEnabled = bool(ready and available and self.last_created)

    def selected_links(self):
        return set(logic.text_type(item.Tag) for item in self.linkList.SelectedItems)

    def selected_source_kinds(self):
        sources = []
        if self.spacesCheck.IsChecked:
            sources.append("space")
        if self.roomsCheck.IsChecked:
            sources.append("room")
        if self.linksCheck.IsChecked:
            sources.append("link")
        return sources

    def selected_type_id(self):
        item = self.typeBox.SelectedItem
        return logic.text_type(item.Tag) if item is not None else u""

    def visible_ticked(self):
        if self.table is None:
            return []
        return [self.records[logic.text_type(row["__key"])]
                for row in self.table.DefaultView if bool(row["__checked"])]

    def update_count(self):
        if self.table is None:
            self.countText.Text = "No sources loaded."
            return
        view = self.table.DefaultView
        checked = sum(1 for row in view if bool(row["__checked"]))
        hidden = sum(1 for row in self.table.Rows if bool(row["__checked"])) - checked
        self.countText.Text = "{} shown; {} ticked{}.".format(
            view.Count, checked, " ({} hidden)".format(hidden) if hidden else "")

    def apply_filter(self):
        if self.initialising or self.table is None:
            return
        self.table.DefaultView.RowFilter = logic.row_filter(
            self.selected_source_kinds(), sorted(self.selected_links()), bool(self.allLevelsCheck.IsChecked),
            bool(self.phaseCheck.IsChecked), self.searchBox.Text)
        self.update_count()
        self.update_enabled()

    def populate_links(self, data, selected):
        self.linkList.Items.Clear()
        for link in data:
            item = ListBoxItem()
            item.Content = link["name"]
            item.Tag = link["id"]
            self.linkList.Items.Add(item)
            item.IsSelected = selected is not None and link["id"] in selected

    def populate_types(self, data, selected):
        self.typeBox.Items.Clear()
        chosen = None
        for region_type in data:
            item = ComboBoxItem()
            item.Content = region_type["name"]
            item.Tag = region_type["id"]
            self.typeBox.Items.Add(item)
            if region_type["id"] == selected:
                chosen = item
        if chosen is not None:
            self.typeBox.SelectedItem = chosen
        elif self.typeBox.Items.Count:
            self.typeBox.SelectedIndex = 0

    def build_table(self, records):
        table = DataTable("RoomRegions")
        table.Columns.Add("__checked", clr.GetClrType(Boolean))
        table.Columns.Add("__level_match", clr.GetClrType(Boolean))
        table.Columns.Add("__phase_match", clr.GetClrType(Boolean))
        table.Columns.Add("__area_value", clr.GetClrType(Double))
        for name in ("__key", "__source", "__link", "__search", "__type",
                     "number", "name", "level", "source", "area", "status"):
            table.Columns.Add(name)
        lookup = {}
        table.BeginLoadData()
        for index, record in enumerate(records):
            key = logic.text_type(index)
            row = table.NewRow()
            row["__checked"] = False
            row["__level_match"] = record["level_match"]
            row["__phase_match"] = record["phase_match"]
            row["__area_value"] = record["area_value"]
            row["__key"] = key
            row["__source"] = record["key"][0]
            row["__link"] = record["key"][2]
            row["__search"] = record["search"]
            row["__type"] = u""
            for name in ("number", "name", "level", "source", "area", "status"):
                row[name] = record[name]
            table.Rows.Add(row)
            lookup[key] = record
        table.EndLoadData()
        self.records = lookup
        self.table = table
        self.sourceGrid.ItemsSource = table.DefaultView
        self.anchor = None
        self.update_assigned_type()

    def update_assigned_type(self):
        if self.table is None:
            return
        item = self.typeBox.SelectedItem
        name = logic.text_type(item.Content) if item is not None else u""
        for row in self.table.Rows:
            row["__type"] = name

    def refresh_from_revit(self, uiapp, scope=None):
        data = backend.capture(uiapp, scope)
        previous = self.snapshot["token"] if self.snapshot else None
        same_document = bool(previous and data["token"] and previous[:2] == data["token"][:2])
        saved_document = bool(data["token"] and data["token"][0] == self.saved_project)
        selected_links = (self.selected_links() if same_document
                          else self.saved_links if saved_document else None)
        selected_type = (self.selected_type_id() if same_document
                         else self.saved_type if saved_document else u"")
        self.initialising = True
        try:
            self.populate_links(data["links"], selected_links)
            self.populate_types(data["types"], selected_type)
            self.build_table(data["records"])
            self.snapshot = data
            self.contextText.Text = u"Project: {}    View: {}".format(
                data["document"] or u"(none)", data["view"] or u"(none)")
        finally:
            self.initialising = False
        self.apply_filter()
        for warning in data["warnings"]:
            self.append_log(warning)
        if data["error"]:
            self.set_status(data["error"])
            self.append_log(data["error"])
        elif not data["types"]:
            self.set_status("No filled-region types are available in this project.")
        elif scope is None:
            self.set_status(
                "Choose sources and links, then click Load selected sources. No rooms or Spaces were scanned.")
        else:
            self.set_status("Sources loaded. Tick visible rows, choose a type, then click Create filled regions.")

    def create_from_revit(self, uiapp, request):
        try:
            result = backend.create(uiapp, request, self.pending_finished)
        except backend.PendingTransaction:
            self.pending_transaction = True
            raise
        self.last_created = result["created"]
        self.created_token = request["token"]
        for status, label, detail in result["report"]:
            self.append_log(u"{} | {} | {}".format(status, label, detail))
        skipped = sum(1 for item in result["report"] if item[0] == "Skipped")
        failed = sum(1 for item in result["report"] if item[0] == "Failed")
        suffix = (" {} Completed regions were kept.".format(result["stopped_reason"])
                  if result["stopped_reason"] else "")
        self.set_status("Created {}; skipped {}; failed {}.{}".format(
            len(result["created"]), skipped, failed, suffix))

    def pending_finished(self, message):
        def display():
            if not self.closed:
                self.pending_transaction = False
                self.set_status(message)
                self.append_log(message)
                self.update_enabled()
        self.Dispatcher.BeginInvoke(Action(display))

    def select_from_revit(self, uiapp, token, ids):
        count, missing = backend.select_created(uiapp, token, ids)
        self.set_status("Selected {} region(s); {} no longer exist.".format(count, missing))

    @ui_handler
    def filter_changed(self, sender, args):
        self.apply_filter()

    @ui_handler
    def scope_changed(self, sender, args):
        self.apply_filter()
        if not self.initialising and self.snapshot is not None:
            self.set_status("Source choices changed. Click Load selected sources to read them.")

    @ui_handler
    def type_changed(self, sender, args):
        if not self.initialising:
            self.update_assigned_type()
            self.update_enabled()

    def apply_row_tick(self, sender, checked):
        if self.table is None:
            return
        current = logic.text_type(sender.DataContext["__key"])
        view = self.table.DefaultView
        keys = [logic.text_type(row["__key"]) for row in view]
        selected = [logic.text_type(row["__key"]) for row in self.sourceGrid.SelectedItems]
        shift = Keyboard.Modifiers & ModifierKeys.Shift == ModifierKeys.Shift
        indexes = logic.tick_indexes(keys, selected, self.anchor, current, shift)
        for index in indexes:
            view[index]["__checked"] = checked
        self.anchor = current
        self.update_count()
        self.update_enabled()

    @ui_handler
    def row_tick_mouse_down(self, sender, args):
        args.Handled = True
        selected = list(self.sourceGrid.SelectedItems)
        table = self.table
        self.apply_row_tick(sender, not bool(sender.DataContext["__checked"]))
        if len(selected) > 1 and any(
                row["__key"] == sender.DataContext["__key"] for row in selected):
            @ui_handler
            def restore_selection(window):
                if window.closed or window.table is not table:
                    return
                window.sourceGrid.SelectedItems.Clear()
                for row in selected:
                    window.sourceGrid.SelectedItems.Add(row)
            # DataGridCell also handles handled mouse events; restore after bubbling.
            self.Dispatcher.BeginInvoke(Action(lambda: restore_selection(self)))

    @ui_handler
    def row_tick_clicked(self, sender, args):
        self.apply_row_tick(sender, bool(sender.IsChecked))

    def set_visible_ticks(self, operation):
        if self.table is None:
            return
        for row in self.table.DefaultView:
            row["__checked"] = not bool(row["__checked"]) if operation == "invert" else operation
        self.update_count()
        self.update_enabled()

    @ui_handler
    def select_all(self, sender, args):
        self.set_visible_ticks(True)

    @ui_handler
    def select_none(self, sender, args):
        self.set_visible_ticks(False)

    @ui_handler
    def select_invert(self, sender, args):
        self.set_visible_ticks("invert")

    @ui_handler
    def refresh_clicked(self, sender, args):
        self.run_in_revit("Refresh", self.refresh_from_revit)

    @ui_handler
    def load_sources_clicked(self, sender, args):
        if self.snapshot is None or not self.snapshot["token"]:
            self.set_status("Refresh active view before loading sources.")
            return
        sources = self.selected_source_kinds()
        links = sorted(self.selected_links())
        if not sources or (sources == ["link"] and not links):
            self.set_status("Choose host sources or select at least one link instance before loading.")
            return
        scope = {"token": self.snapshot["token"], "sources": sources, "link_ids": links}
        self.run_in_revit("Load selected sources",
                          lambda uiapp: self.refresh_from_revit(uiapp, scope))

    @ui_handler
    def create_clicked(self, sender, args):
        selected = self.visible_ticked()
        type_id = self.selected_type_id()
        if not selected or not type_id or self.snapshot is None:
            self.set_status("Tick at least one visible source and choose a filled-region type.")
            return
        request = {
            "token": self.snapshot["token"], "type_id": type_id,
            "sources": [{"key": record["key"], "label": u"{} {} ({})".format(
                record["number"], record["name"], record["source"])} for record in selected],
            "boundary": logic.text_type(self.boundaryBox.SelectedItem),
            "include_islands": bool(self.islandsCheck.IsChecked),
            "simplify": bool(self.simplifyCheck.IsChecked),
            "follow_phase": bool(self.phaseCheck.IsChecked),
        }
        message = (
            "Create up to {} new filled region(s) in the last refreshed view?\n\n"
            "Existing regions are not replaced and repeated runs can overlap. "
            "Off-level, invalid or changed sources will be skipped. Cancelling "
            "keeps completed regions, which can be undone together."
        ).format(len(selected))
        if not forms.alert(message, title="Room Regions", yes=True, no=True):
            self.set_status("Creation cancelled; no model changes were made.")
            return
        self.save_settings()
        self.run_in_revit("Create", lambda uiapp: self.create_from_revit(uiapp, request))

    @ui_handler
    def select_created(self, sender, args):
        ids = list(self.last_created)
        token = self.created_token
        self.run_in_revit("Select created", lambda uiapp: self.select_from_revit(uiapp, token, ids))

    def close_from_revit(self, uiapp):
        if not self.save_settings():
            forms.alert("Room Regions settings could not be saved. See the pyRevit log for details.",
                        title="Room Regions")
        self.ext_event.Dispose()
        self.closed = True
        self.queue.clear()
        self.Close()

    @ui_handler
    def close_clicked(self, sender, args):
        self.Close()

    @ui_handler
    def window_closing(self, sender, args):
        if self.closed:
            return
        args.Cancel = True
        if self.pending_transaction:
            self.set_status("Resolve Revit's pending failure handling before closing.")
            return
        if self.busy:
            self.set_status("Wait for the current operation or cancel its progress dialog.")
            return
        if not self.run_in_revit("Close", self.close_from_revit):
            self.closed = True
            args.Cancel = False
            logger.error("Room Regions closed without disposing its external event.")

    @ui_handler
    def window_closed(self, sender, args):
        self.closed = True
        if envvars.get_pyrevit_env_var(WINDOW_KEY) is self:
            envvars.set_pyrevit_env_var(WINDOW_KEY, False)


def main():
    existing = envvars.get_pyrevit_env_var(WINDOW_KEY)
    if existing and not existing.closed:
        try:
            if existing.WindowState == WindowState.Minimized:
                existing.WindowState = WindowState.Normal
            existing.Activate()
        except Exception as ex:
            logger.error("Could not activate Room Regions:\n{}".format(traceback.format_exc()))
            forms.alert("Could not activate the existing Room Regions window:\n{}".format(ex),
                        title="Room Regions")
        return
    window = RoomRegionsWindow()
    envvars.set_pyrevit_env_var(WINDOW_KEY, window)
    window.show(modal=False)
    window.run_in_revit("Refresh", window.refresh_from_revit)


if __name__ == "__main__":
    main()
