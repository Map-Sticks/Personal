# -*- coding: utf-8 -*-
"""Ultimate Modeless Revit Browser & Scheduler.

Modeless WPF rules (docs/pyrevit-patterns.md):
  * __persistentengine__ = True keeps handlers alive.
  * handle_esc=False and no IsCancel="True" so Esc never closes the window.
  * Every Revit API read/write/delete runs inside IExternalEventHandler.Execute.
  * Active refresh subscribes to DocumentChanged (O(k) set intersection only),
    debounced via DispatcherTimer, and unsubscribes cleanly on Close / DocumentClosing.
"""
__title__ = "Browser &\nScheduler"
__doc__ = ("Ultimate modeless Revit Browser & Scheduler: universal search, multi-category "
           "takeoffs/totals, staged inline/bulk editing, safe cascade-aware delete, "
           "protected Excel export/import with 3-way diff, O(k) active refresh, and presets.")
__persistentengine__ = True

import os
import time
import traceback
from functools import wraps

import clr
clr.AddReference("System.Data")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
from System import Action, Int32, TimeSpan
from System.Collections.Generic import List
from System.Data import DataTable
from System.Windows import Clipboard, GridLength, GridUnitType, Visibility
from System.Windows.Controls import (
    DataGridEditAction, DataGridLength, DataGridRow, DataGridTextColumn,
    ItemsControl, TextBox, TreeViewItem,
)
from System.Windows.Data import Binding
from System.Windows.Threading import DispatcherTimer

from pyrevit import DB, UI, forms, revit, script

import browser_state as state
import diff_engine
import excel_io
import preset_store
import query_engine
import revit_adapter as adapter

logger = script.get_logger()
XAML_PATH = script.get_bundle_file("UI.xaml")
WINDOW = globals().get("WINDOW")


class UserError(Exception):
    pass


def ui_handler(func):
    """Never let an exception escape a WPF event handler of a modeless window."""
    @wraps(func)
    def wrapper(self, *args):
        try:
            return func(self, *args)
        except Exception as ex:
            logger.error(traceback.format_exc())
            self.set_status(u"ERROR: {}".format(ex))
    return wrapper


class BrowserEventHandler(UI.IExternalEventHandler):
    def __init__(self, window):
        self.window = window

    def Execute(self, uiapp):
        try:
            self.window.run_queued(uiapp)
        except Exception:
            logger.error(traceback.format_exc())

    def GetName(self):
        return "Revit Browser & Scheduler"


class UltimateBrowserWindow(forms.WPFWindow):
    def __init__(self, uiapp, doc, uidoc):
        self.initialising = True
        self.closed = False
        forms.WPFWindow.__init__(self, XAML_PATH, handle_esc=False)

        self.uiapp = uiapp
        self.doc = doc
        self.queue = state.ActionQueue()
        self.handler = BrowserEventHandler(self)
        self.ext_event = UI.ExternalEvent.Create(self.handler)

        self.meta = adapter.read_doc_metadata(doc)
        self.doc_title = self.meta["doc_title"]
        self.store = preset_store.PresetStore()
        self.presets = self.store.list_presets()

        self.records = []
        self.records_by_key = {}
        self.displayed_records = []
        self.grouped_rows = []
        self.available_columns = list(state.PSEUDO_FIELDS) + state.count_fields()
        self.contents_by_spatial = {}
        self.contents_ids = []
        self.revit_selection_ids = []

        self.staged = state.StagedEdits()
        self.delta = state.DeltaTracker()
        self.table = None
        self.active_col_keys = []
        self._events_attached = False
        self._sync_sel_attached = False

        # Active preset / schedule configuration
        self.selected_categories = [u"Lighting Fixtures", u"Electrical Fixtures", u"Fire Alarm Devices"]
        self.selected_special_sources = []
        self.selected_space_ids = []
        self.columns = [u"Category", u"Family", u"Type", u"Level", u"I:Mark", u"Space", u"Edit Status"]
        self.group_by = [u"Category", u"Family", u"Type"]
        self.sort_column = u"Category"
        self.sort_ascending = True
        self.protect_excel = True

        # Debounce timer for O(k) active refresh (300 ms)
        self.debounce_timer = DispatcherTimer()
        self.debounce_timer.Interval = TimeSpan.FromMilliseconds(300)
        self.debounce_timer.Tick += self.on_debounce_tick

        for sc in state.SCOPES:
            self.cmbScope.Items.Add(sc)
        for rm in state.REFRESH_MODES:
            self.cmbRefreshMode.Items.Add(rm)

        self.populate_presets_dropdown()
        self.cmbScope.SelectedItem = state.SCOPES[0]
        self.cmbRefreshMode.SelectedItem = state.REFRESH_MODES[0]

        self.Closed += self.on_closed
        self.attach_revit_events()

        self.initialising = False
        if self.presets:
            self.cmbPreset.SelectedIndex = 0
            self.apply_preset_config(self.presets[0], trigger_load=False)
        self.update_scope_controls()
        self.update_side_panel_visibility()
        self.update_staged_buttons()
        self.set_status(u"Ready on '{}'. Choose a Preset or Sources, then click Load / Refresh."
                        .format(self.doc_title))

    # --- Revit event lifecycle (Active Refresh & Selection Sync) ----------------------------
    def attach_revit_events(self):
        if self._events_attached or self.uiapp is None:
            return
        try:
            self.uiapp.Application.DocumentChanged += self.on_document_changed
            self.uiapp.ViewActivated += self.on_view_activated
            self.uiapp.Application.DocumentClosing += self.on_document_closing
            self._events_attached = True
        except Exception:
            logger.debug("Could not attach Revit events:\n{}".format(traceback.format_exc()))

    def detach_revit_events(self):
        if self._events_attached and self.uiapp is not None:
            try:
                self.uiapp.Application.DocumentChanged -= self.on_document_changed
            except Exception:
                pass
            try:
                self.uiapp.ViewActivated -= self.on_view_activated
            except Exception:
                pass
            try:
                self.uiapp.Application.DocumentClosing -= self.on_document_closing
            except Exception:
                pass
            self._events_attached = False
        self.set_selection_sync(False)

    def set_selection_sync(self, enabled):
        if self.uiapp is None:
            return
        if enabled and not self._sync_sel_attached:
            try:
                self.uiapp.SelectionChanged += self.on_revit_selection_changed
                self._sync_sel_attached = True
            except Exception:
                self.set_status(u"Selection sync requires Revit 2023+.")
        elif not enabled and self._sync_sel_attached:
            try:
                self.uiapp.SelectionChanged -= self.on_revit_selection_changed
            except Exception:
                pass
            self._sync_sel_attached = False

    def on_document_changed(self, sender, args):
        """Strictly O(k) set intersection — zero collectors or UI rebuilds here."""
        if self.closed or not self.records:
            return
        try:
            if self.doc is None or not self.doc.IsValidObject:
                return
            changed_doc = args.GetDocument()
            if changed_doc is None or not self.doc.Equals(changed_doc):
                return
            tx_names = list(args.GetTransactionNames())
            added = [state.element_id_value(i) for i in args.GetAddedElementIds()]
            modified = [state.element_id_value(i) for i in args.GetModifiedElementIds()]
            deleted = [state.element_id_value(i) for i in args.GetDeletedElementIds()]
            if self.delta.record_document_changed(added, modified, deleted, tx_names):
                self.Dispatcher.BeginInvoke(Action(self._schedule_delta_ui))
        except Exception:
            pass

    def _schedule_delta_ui(self):
        if self.closed:
            return
        mode = self.current_refresh_mode()
        if mode == u"Manual":
            self._show_delta_badge()
            return
        if self.delta.storm_triggered or mode == u"Notify Only":
            self._show_delta_badge()
            return
        # Live (Auto): reset 300 ms debounce timer.
        self.debounce_timer.Stop()
        self.debounce_timer.Start()

    def _show_delta_badge(self):
        if self.delta.storm_triggered:
            self.btnApplyDelta.Content = u"Many Changes — Click to Refresh"
        else:
            self.btnApplyDelta.Content = u"{} Listed Change(s) — Refresh".format(self.delta.pending_total())
        self.btnApplyDelta.Visibility = Visibility.Visible

    def on_debounce_tick(self, sender, args):
        self.debounce_timer.Stop()
        if self.closed:
            return
        if self.delta.storm_triggered:
            self._show_delta_badge()
            return
        if self.delta.pending_total() > 0:
            self.run_in_revit("Incremental refresh", self.apply_delta_from_revit)

    def on_view_activated(self, sender, args):
        if self.closed:
            return
        try:
            new_doc = args.Document
            if new_doc is not None and self.doc is not None and not self.doc.Equals(new_doc):
                title = state.to_text(new_doc.Title)
                self.Dispatcher.BeginInvoke(Action(
                    lambda: self.set_status(
                        u"Switched to '{}'. Click Load / Refresh to load the active project."
                        .format(title))))
        except Exception:
            pass

    def on_document_closing(self, sender, args):
        try:
            closing_doc = args.Document
            if self.doc is not None and closing_doc is not None and self.doc.Equals(closing_doc):
                self.doc = None
                self.delta.clear_pending()
                self.Dispatcher.BeginInvoke(Action(
                    lambda: self.set_status(u"Project closed. Open a project and click Load / Refresh.")))
        except Exception:
            pass

    def on_revit_selection_changed(self, sender, args):
        if self.closed:
            return
        try:
            ids = [state.element_id_value(i) for i in args.GetSelectedElements()]
            self.revit_selection_ids = ids
            self.Dispatcher.BeginInvoke(Action(lambda: self._highlight_ids_in_grid(ids)))
        except Exception:
            pass

    def _highlight_ids_in_grid(self, element_ids):
        if self.closed or self.chkGrouped.IsChecked or self.table is None:
            return
        target_keys = set(state.make_row_key(eid) for eid in element_ids)
        was_init = self.initialising
        self.initialising = True
        try:
            self.grid.SelectedItems.Clear()
            for item in self.grid.Items:
                try:
                    if item.Row[state.ID_COLUMN] in target_keys:
                        self.grid.SelectedItems.Add(item)
                except Exception:
                    continue
        finally:
            self.initialising = was_init

    # --- ExternalEvent queue helpers --------------------------------------------------------
    def run_in_revit(self, label, action):
        if self.closed:
            return False
        self.queue.add(label, action)
        result = self.ext_event.Raise()
        if result not in (UI.ExternalEventRequest.Accepted, UI.ExternalEventRequest.Pending):
            self.queue.clear()
            self.set_status(u"Revit could not queue '{}' ({}). Try again.".format(label, result))
            return False
        return True

    def run_queued(self, uiapp):
        if self.closed:
            self.queue.clear()
            return
        self.queue.drain(uiapp, self.on_action_error)

    def on_action_error(self, label, ex):
        if isinstance(ex, UserError):
            self.set_status(state.to_text(ex))
            return
        logger.error("{} failed:\n{}".format(label, traceback.format_exc()))
        self.set_status(u"ERROR in {}: {}".format(label, ex))

    def active_uidoc(self, uiapp, allow_switch=False):
        uidoc = uiapp.ActiveUIDocument
        if uidoc is None or uidoc.Document.IsFamilyDocument:
            raise UserError(u"Open a Revit project (not a family document) first.")
        if not allow_switch and self.doc is not None and self.doc.IsValidObject:
            if not self.doc.Equals(uidoc.Document):
                raise UserError(u"Switch back to '{}' or click Load / Refresh to load the active project."
                                .format(self.doc_title))
        return uidoc

    # --- State accessors --------------------------------------------------------------------
    def current_scope(self):
        return state.to_text(self.cmbScope.SelectedItem or state.SCOPES[0])

    def current_scope_value(self):
        val = self.cmbScopeValue.SelectedItem
        return state.to_text(val) if val is not None else None

    def current_refresh_mode(self):
        return state.to_text(self.cmbRefreshMode.SelectedItem or state.REFRESH_MODES[0])

    def set_status(self, text):
        try:
            self.lblStatus.Text = state.to_text(text)
        except Exception:
            pass

    def _active_category_ids(self):
        cat_map = self.meta.get("cat_id_by_name", {})
        return [cat_map[c] for c in self.selected_categories if c in cat_map]

    def effective_value(self, rec, col_key):
        staged_val = self.staged.get_value(rec["row_key"], col_key)
        if staged_val is not None:
            return staged_val
        return state.record_value(rec, col_key)

    def update_staged_buttons(self):
        cnt = self.staged.count()
        self.btnApplyEdits.Content = u"Apply Edits ({})".format(cnt)
        self.btnApplyEdits.IsEnabled = cnt > 0
        self.btnRevertEdits.IsEnabled = cnt > 0

    def update_scope_controls(self):
        scope = self.current_scope()
        self.btnPickSpaces.Visibility = (
            Visibility.Visible if scope == u"Selected Spaces / Rooms" else Visibility.Collapsed)
        if scope in (u"Level", u"Workset"):
            options = self.meta["levels"] if scope == u"Level" else self.meta["worksets"]
            prev = self.current_scope_value()
            was_init = self.initialising
            self.initialising = True
            try:
                self.cmbScopeValue.Items.Clear()
                for opt in options:
                    self.cmbScopeValue.Items.Add(opt)
                if prev in options:
                    self.cmbScopeValue.SelectedItem = prev
                elif options:
                    self.cmbScopeValue.SelectedIndex = 0
            finally:
                self.initialising = was_init
            self.cmbScopeValue.Visibility = Visibility.Visible
        else:
            self.cmbScopeValue.Visibility = Visibility.Collapsed

    def update_side_panel_visibility(self):
        show = bool(self.chkShowSidePanel.IsChecked)
        self.sidePanel.Visibility = Visibility.Visible if show else Visibility.Collapsed
        self.splitter.Visibility = Visibility.Visible if show else Visibility.Collapsed
        self.colSplitter.Width = GridLength(6.0) if show else GridLength(0.0)
        self.colSidePanel.Width = GridLength(360.0, GridUnitType.Pixel) if show else GridLength(0.0)

    # --- Presets ----------------------------------------------------------------------------
    def populate_presets_dropdown(self, select_name=None):
        was_init = self.initialising
        self.initialising = True
        try:
            self.cmbPreset.Items.Clear()
            for p in self.presets:
                label = p["name"] + (u" [Built-in]" if p.get("builtin") else u"")
                self.cmbPreset.Items.Add(label)
            if select_name:
                for idx, p in enumerate(self.presets):
                    if p["name"] == select_name:
                        self.cmbPreset.SelectedIndex = idx
                        break
        finally:
            self.initialising = was_init

    def current_as_preset(self, name):
        return preset_store.make_preset(
            name=name,
            categories=self.selected_categories,
            special_sources=self.selected_special_sources,
            include_instances=True,
            include_types=bool(self.chkTypes.IsChecked),
            include_links=bool(self.chkLinks.IsChecked),
            scope=self.current_scope(),
            level=self.current_scope_value() if self.current_scope() == u"Level" else None,
            workset=self.current_scope_value() if self.current_scope() == u"Workset" else None,
            selected_space_ids=self.selected_space_ids,
            columns=self.columns,
            query=self.txtSearch.Text or u"",
            sort_column=self.sort_column,
            sort_ascending=self.sort_ascending,
            grouped_mode=bool(self.chkGrouped.IsChecked),
            group_by=self.group_by,
            show_contents_panel=bool(self.chkShowSidePanel.IsChecked),
            refresh_mode=self.current_refresh_mode(),
            protect_excel=self.protect_excel,
        )

    def apply_preset_config(self, preset, trigger_load=True):
        was_init = self.initialising
        self.initialising = True
        try:
            self.selected_categories = list(preset.get("categories") or [])
            self.selected_special_sources = list(preset.get("special_sources") or [])
            self.chkTypes.IsChecked = bool(preset.get("include_types"))
            self.chkLinks.IsChecked = bool(preset.get("include_links"))
            self.cmbScope.SelectedItem = preset.get("scope", state.SCOPES[0])
            self.selected_space_ids = list(preset.get("selected_space_ids") or [])
            self.columns = list(preset.get("columns") or [u"Category", u"Family", u"Type", u"Level", u"I:Mark"])
            self.txtSearch.Text = preset.get("query", u"")
            self.sort_column = preset.get("sort_column")
            self.sort_ascending = bool(preset.get("sort_ascending", True))
            self.chkGrouped.IsChecked = bool(preset.get("grouped_mode"))
            self.group_by = list(preset.get("group_by") or [u"Category", u"Family", u"Type"])
            self.chkShowSidePanel.IsChecked = bool(preset.get("show_contents_panel"))
            self.cmbRefreshMode.SelectedItem = preset.get("refresh_mode", state.REFRESH_MODES[0])
            self.protect_excel = bool(preset.get("protect_excel", True))
        finally:
            self.initialising = was_init
        self.update_scope_controls()
        self.update_side_panel_visibility()
        if trigger_load:
            self.run_in_revit("Load preset", self.load_from_revit)

    # --- Revit API actions (executed in ExternalEvent.Execute) ------------------------------
    def load_from_revit(self, uiapp):
        uidoc = self.active_uidoc(uiapp, allow_switch=True)
        doc = uidoc.Document
        started = time.time()
        self.doc = doc
        self.meta = adapter.read_doc_metadata(doc)
        self.doc_title = self.meta["doc_title"]
        try:
            self.revit_selection_ids = [
                state.element_id_value(i) for i in uidoc.Selection.GetElementIds()
            ]
        except Exception:
            self.revit_selection_ids = []

        cat_ids = self._active_category_ids()
        if not cat_ids and not self.selected_special_sources:
            raise UserError(u"Select at least one Category or Special Source first (click Sources...).")

        records, available, contents = adapter.collect_schedule_records(
            doc, uidoc, cat_ids, self.selected_special_sources, self.columns,
            scope=self.current_scope(),
            include_instances=True,
            include_types=bool(self.chkTypes.IsChecked),
            include_links=bool(self.chkLinks.IsChecked),
            compute_spatial_counts=bool(self.chkShowSidePanel.IsChecked),
        )
        self.records = records
        self.records_by_key = dict((r["row_key"], r) for r in records)
        self.available_columns = available
        self.contents_by_spatial = contents
        self.staged.clear()
        self.btnApplyDelta.Visibility = Visibility.Collapsed

        watch_types = any(state.is_type_param_key(ck) for ck in self.columns)
        self.delta.rebuild_index(records, active_category_ids=cat_ids, watch_types=watch_types)

        self.update_scope_controls()
        self.update_staged_buttons()
        self.rebuild_grid()
        self.set_status(
            u"Loaded {} element(s) from '{}' in {:.2f}s. Active refresh watching {} host ID(s)."
            .format(len(records), self.doc_title, time.time() - started,
                    len(self.delta.tracked_element_ids)))

    def apply_delta_from_revit(self, uiapp):
        """O(k) incremental patch: reads ONLY the dirty ElementIds from Revit."""
        uidoc = self.active_uidoc(uiapp)
        doc = uidoc.Document
        added, modified, deleted, type_modified, storm = self.delta.pop_delta()
        self.btnApplyDelta.Visibility = Visibility.Collapsed
        if storm:
            self.load_from_revit(uiapp)
            return

        started = time.time()
        cat_id_set = set(self._active_category_ids())
        phases = adapter._all_phases_latest_first(doc)
        ws_table = doc.GetWorksetTable() if doc.IsWorkshared else None
        conflicts = 0
        removed_count = 0
        updated_count = 0
        added_count = 0

        # 1. Remove deleted elements in O(d).
        if deleted:
            del_keys = set(state.make_row_key(eid) for eid in deleted)
            self.records = [r for r in self.records if r["row_key"] not in del_keys]
            for dk in del_keys:
                if dk in self.records_by_key:
                    del self.records_by_key[dk]
                    removed_count += 1
            self.staged.remove_for_rows(del_keys)

        # 2. Expand type_modified -> affected host elements.
        if type_modified:
            for r in self.records:
                if not r.get("is_linked") and r.get("type_id") in type_modified:
                    modified.add(r["id"])

        # 3. Re-read ONLY modified elements in O(m).
        for eid in modified:
            rk = state.make_row_key(eid)
            if rk not in self.records_by_key:
                continue
            el = doc.GetElement(adapter.as_eid(eid))
            if el is None:
                continue
            new_rec = adapter.build_element_record(
                doc, el, self.columns, visible_ids=None, phases=phases,
                is_linked=False, link_name=u"Host", workset_table=ws_table,
            )
            new_rec["counts"] = self.records_by_key[rk].get("counts", {})
            conflicts += self.staged.mark_external_update(new_rec)
            self.records_by_key[rk] = new_rec
            updated_count += 1

        # 4. Check added elements in O(a).
        for eid in added:
            rk = state.make_row_key(eid)
            if rk in self.records_by_key:
                continue
            el = doc.GetElement(adapter.as_eid(eid))
            if el is None or el.Category is None:
                continue
            cid = state.element_id_value(el.Category.Id)
            if cid not in cat_id_set:
                continue
            if isinstance(el, DB.ElementType) and not bool(self.chkTypes.IsChecked):
                continue
            new_rec = adapter.build_element_record(
                doc, el, self.columns, visible_ids=None, phases=phases,
                is_linked=False, link_name=u"Host", workset_table=ws_table,
            )
            self.records.append(new_rec)
            self.records_by_key[rk] = new_rec
            added_count += 1

        # Rebuild list order from records_by_key
        self.records = [self.records_by_key[r["row_key"]] for r in self.records if r["row_key"] in self.records_by_key]
        watch_types = any(state.is_type_param_key(ck) for ck in self.columns)
        self.delta.rebuild_index(self.records, active_category_ids=list(cat_id_set), watch_types=watch_types)
        self.update_staged_buttons()
        self.rebuild_grid()
        msg = u"Active refresh patched +{} / ~{} / -{} in {:.0f}ms.".format(
            added_count, updated_count, removed_count, (time.time() - started) * 1000.0)
        if conflicts:
            msg += u" WARNING: {} staged edit(s) conflicted with model changes.".format(conflicts)
        self.set_status(msg)

    def apply_staged_to_revit(self, uiapp):
        uidoc = self.active_uidoc(uiapp)
        doc = uidoc.Document
        edits = self.staged.as_list()
        if not edits:
            return
        type_edits = [e for e in edits if e.get("is_type")]
        warn = u"Apply {} staged parameter edit(s) to '{}'?".format(len(edits), self.doc_title)
        if type_edits:
            warn += u"\n\nIncludes {} TYPE parameter edit(s) which affect ALL instances of those types.".format(
                len(type_edits))
        if not forms.alert(warn, title="Confirm Parameter Edits", yes=True, no=True):
            self.set_status(u"Apply cancelled. Staged edits preserved.")
            return

        res = adapter.apply_parameter_changes(doc, edits)
        self.staged.clear()
        self.update_staged_buttons()
        # Re-read only affected elements
        for eid in res["affected_ids"]:
            self.delta.pending_modified.add(eid)
        self.apply_delta_from_revit(uiapp)
        msg = u"Applied {} edit(s) in one undoable transaction.".format(res["applied"])
        if res["failed"]:
            msg += u" {} failed (first: {}).".format(len(res["failed"]), res["failed"][0][2])
        self.set_status(msg)

    def delete_selected_from_revit(self, uiapp, selected_records):
        uidoc = self.active_uidoc(uiapp)
        doc = uidoc.Document
        allowed_ids = []
        blocked = []
        warnings = []
        for rec in selected_records:
            ok, level, reason = state.evaluate_delete_safety(rec, allow_pinned=False)
            if not ok:
                blocked.append(u"Id {}: {}".format(rec["id"], reason))
            else:
                allowed_ids.append(rec["id"])
                if level == "warn":
                    warnings.append(u"Id {}: {}".format(rec["id"], reason))
        if not allowed_ids:
            forms.alert(u"None of the selected elements can be deleted:\n\n" + u"\n".join(blocked[:15]),
                        title="Delete Blocked")
            return

        impact = adapter.preview_delete_cascade(doc, allowed_ids)
        if impact.get("error"):
            forms.alert(u"Delete preview failed:\n{}".format(impact["error"]), title="Delete Preview Error")
            return

        lines = [
            u"Selected elements to delete: {}".format(impact["selected_count"]),
            u"Cascade-deleted dependent/hosted elements: {}".format(impact["dependent_count"]),
            u"TOTAL elements Revit will delete: {}".format(impact["total_deleted"]),
        ]
        if blocked:
            lines.append(u"\nSkipped (protected/linked/pinned/owned/grouped): {}".format(len(blocked)))
        if warnings:
            lines.append(u"\nWarnings:\n" + u"\n".join(warnings[:8]))
        lines.append(u"\nProceed with deletion (single Undo entry)?")

        if not forms.alert(u"\n".join(lines), title="Confirm Safe Delete", yes=True, no=True):
            self.set_status(u"Delete cancelled.")
            return

        deleted_count, err = adapter.delete_elements(doc, allowed_ids)
        if err:
            raise UserError(err)
        for eid in allowed_ids:
            self.delta.pending_deleted.add(eid)
        self.apply_delta_from_revit(uiapp)
        self.set_status(u"Deleted {} element(s) (including dependents). Press Ctrl+Z in Revit to undo."
                        .format(deleted_count))

    def import_excel_in_revit(self, uiapp, filepath):
        uidoc = self.active_uidoc(uiapp)
        doc = uidoc.Document
        wb = excel_io.read_schedule_xlsx(filepath)
        plan = diff_engine.build_import_plan(wb, self.records, allow_pinned=False)
        s = plan["summary"]
        ready = [c for c in plan["changes"] if c["status"] == "ready"]
        blocked = [c for c in plan["changes"] if c["status"] == "blocked"]
        conflicts = [c for c in plan["changes"] if c["status"] in ("conflict", "type_conflict")]

        report = [
            u"Excel Import 3-Way Analysis:",
            u"  Ready to apply: {}".format(s["ready"]),
            u"  Blocked (Non-modifiable: Read-Only / Pseudo / Linked / Group / Pinned / Owned): {}".format(s["blocked"]),
            u"  Conflicts (Model changed since export): {}".format(s["conflicts"]),
            u"  Type Conflicts (Different values for same Type): {}".format(s["type_conflicts"]),
            u"  Missing elements (not in loaded schedule): {}".format(s["missing_elements"]),
            u"  Unchanged cells: {}".format(s["unchanged"]),
        ]
        if blocked:
            report.append(u"\nSample blocked (will NOT be modified):")
            for b in blocked[:6]:
                report.append(u"  Row {} [{}] {} -> {}".format(
                    b["excel_row"], b["column_label"], b["new_value"], b["reason"]))
        if conflicts:
            report.append(u"\nSample conflicts (skipped for safety):")
            for c in conflicts[:6]:
                report.append(u"  Row {} [{}] -> {}".format(
                    c["excel_row"], c["column_label"], c["reason"]))

        if not ready:
            forms.alert(u"\n".join(report) + u"\n\nNo safe editable changes to apply.",
                        title="Excel Import Analysis")
            return

        report.append(u"\nApply the {} safe change(s) to '{}' in one undoable transaction?".format(
            len(ready), self.doc_title))
        if not forms.alert(u"\n".join(report), title="Confirm Excel Import", yes=True, no=True):
            self.set_status(u"Excel import cancelled.")
            return

        res = adapter.apply_parameter_changes(
            doc, ready, tx_name=state.TX_PREFIX + u" Excel Import")
        for eid in res["affected_ids"]:
            self.delta.pending_modified.add(eid)
        self.apply_delta_from_revit(uiapp)
        msg = u"Excel import applied {} change(s); skipped {} blocked and {} conflict(s).".format(
            res["applied"], s["blocked"], s["conflicts"] + s["type_conflicts"])
        if res["failed"]:
            msg += u" {} failed (first: {}).".format(len(res["failed"]), res["failed"][0][2])
        self.set_status(msg)

    def navigate_in_revit(self, uiapp, element_ids, mode):
        uidoc = self.active_uidoc(uiapp)
        doc = uidoc.Document
        if mode == "reset_isolate":
            view = uidoc.ActiveView
            if view is not None:
                with revit.Transaction(state.TX_PREFIX + u" Reset Isolate", doc=doc):
                    view.DisableTemporaryViewMode(DB.TemporaryViewMode.TemporaryHideIsolate)
            self.set_status(u"Temporary isolate reset.")
            return

        ids = List[DB.ElementId]()
        for val in element_ids:
            el = doc.GetElement(adapter.as_eid(val))
            if el is not None:
                ids.Add(el.Id)
        if ids.Count == 0:
            raise UserError(u"Selected elements no longer exist or are linked-only.")

        if mode == "open_view":
            first = doc.GetElement(ids[0])
            if isinstance(first, DB.View):
                uidoc.ActiveView = first
                self.set_status(u"Opened view '{}'.".format(adapter.safe_name(first)))
                return
            raise UserError(u"Select a View or Sheet row to open it.")

        uidoc.Selection.SetElementIds(ids)
        if mode == "show":
            uidoc.ShowElements(ids)
            self.set_status(u"Shown {} element(s) in Revit.".format(ids.Count))
        elif mode == "isolate":
            view = uidoc.ActiveView
            if view is None:
                raise UserError(u"No active view to isolate in.")
            with revit.Transaction(state.TX_PREFIX + u" Isolate Elements", doc=doc):
                view.IsolateElementsTemporary(ids)
            self.set_status(u"Isolated {} element(s) in '{}'.".format(ids.Count, adapter.safe_name(view)))
        else:
            self.set_status(u"Selected {} element(s) in Revit.".format(ids.Count))

    # --- Cached UI rendering (no Revit API calls) -------------------------------------------
    def rebuild_grid(self):
        scope = self.current_scope()
        scoped = query_engine.filter_records_by_scope(
            self.records, scope,
            level=self.current_scope_value() if scope == u"Level" else None,
            workset=self.current_scope_value() if scope == u"Workset" else None,
            selected_space_ids=self.selected_space_ids,
            selection_ids=self.revit_selection_ids,
        )
        filtered = query_engine.filter_records_by_query(
            scoped, self.txtSearch.Text or u"", self.columns, value_getter=self.effective_value)
        ordered = query_engine.sort_records(
            filtered, self.sort_column, ascending=self.sort_ascending, value_getter=self.effective_value)
        self.displayed_records = ordered

        grouped_mode = bool(self.chkGrouped.IsChecked)
        table = DataTable("Schedule")
        table.Columns.Add(state.ID_COLUMN)

        if grouped_mode:
            self.grouped_rows = query_engine.build_grouped_takeoff(
                ordered, self.columns, self.group_by, value_getter=self.effective_value)
            display_cols = [u"__COUNT__"] + list(self.columns)
            self.active_col_keys = display_cols
            table.Columns.Add("C0", clr.GetClrType(Int32))
            for idx in range(1, len(display_cols)):
                table.Columns.Add("C{}".format(idx))
            table.BeginLoadData()
            for r_i, grp in enumerate(self.grouped_rows):
                row = table.NewRow()
                row[state.ID_COLUMN] = u"G:{}".format(r_i)
                row["C0"] = grp["count"]
                for idx, ck in enumerate(self.columns, start=1):
                    row["C{}".format(idx)] = grp["values"].get(ck, u"")
                table.Rows.Add(row)
            table.EndLoadData()
        else:
            self.grouped_rows = []
            display_cols = list(self.columns)
            self.active_col_keys = display_cols
            for idx, ck in enumerate(display_cols):
                if state.is_count_key(ck):
                    table.Columns.Add("C{}".format(idx), clr.GetClrType(Int32))
                else:
                    table.Columns.Add("C{}".format(idx))
            table.BeginLoadData()
            for rec in ordered:
                row = table.NewRow()
                row[state.ID_COLUMN] = rec["row_key"]
                for idx, ck in enumerate(display_cols):
                    row["C{}".format(idx)] = self.effective_value(rec, ck)
                table.Rows.Add(row)
            table.EndLoadData()

        self.grid.ItemsSource = None
        self.grid.Columns.Clear()
        for idx, ck in enumerate(display_cols):
            col = DataGridTextColumn()
            col.Header = u"Count" if ck == u"__COUNT__" else state.column_display_label(ck)
            col.Binding = Binding("C{}".format(idx))
            col.SortMemberPath = "C{}".format(idx)
            col.MinWidth = 65.0
            col.Width = DataGridLength(90.0 if ck == u"__COUNT__" else 150.0)
            col.IsReadOnly = grouped_mode or (ck == u"__COUNT__") or state.is_pseudo_key(ck) or state.is_count_key(ck)
            self.grid.Columns.Add(col)

        self.table = table
        self.grid.ItemsSource = table.DefaultView

        totals = query_engine.compute_grand_totals(ordered, self.columns, value_getter=self.effective_value)
        sum_parts = [u"{}: {}".format(state.column_display_label(k), v)
                     for k, v in list(totals["sums"].items())[:4]]
        self.lblTotals.Text = (u"Total Count: {}{}".format(
            totals["count"], (u"  |  " + u"  |  ".join(sum_parts)) if sum_parts else u""))
        if grouped_mode:
            self.lblCount.Text = u"{} groups ({} elements) / {} loaded".format(
                len(self.grouped_rows), len(ordered), len(self.records))
        else:
            self.lblCount.Text = u"{} shown / {} loaded".format(len(ordered), len(self.records))
        self.update_side_panel()

    def selected_records(self):
        out = []
        for item in self.grid.SelectedItems:
            try:
                key = state.to_text(item.Row[state.ID_COLUMN])
                if key.startswith(u"G:"):
                    idx = int(key[2:])
                    if 0 <= idx < len(self.grouped_rows):
                        for rk in self.grouped_rows[idx]["row_keys"]:
                            rec = self.records_by_key.get(rk)
                            if rec is not None:
                                out.append(rec)
                else:
                    rec = self.records_by_key.get(key)
                    if rec is not None:
                        out.append(rec)
            except Exception:
                continue
        return out

    def selected_host_ids(self):
        return list(dict.fromkeys(
            r["id"] for r in self.selected_records() if not r.get("is_linked") and r.get("id") is not None
        ))

    def update_side_panel(self):
        if not self.chkShowSidePanel.IsChecked:
            return
        sel = self.selected_records()
        items = []
        for rec in sel:
            items.extend(self.contents_by_spatial.get(rec["id"], []))
        self.treeContents.Items.Clear()
        self.contents_ids = [it["id"] for it in items]
        self.lblContents.Text = u"{} selected row(s) -> {} contained element(s)".format(len(sel), len(items))
        by_cat = {}
        for it in items:
            by_cat.setdefault(it["category"], []).append(it)
        for cat in sorted(by_cat.keys(), key=lambda x: x.lower()):
            members = by_cat[cat]
            cat_node = TreeViewItem()
            cat_node.Header = u"{} ({})".format(cat, len(members))
            cat_node.Tag = [m["id"] for m in members]
            cat_node.IsExpanded = True
            for m in members:
                leaf = TreeViewItem()
                leaf.Header = u"{} : {} (Mark {} · Id {})".format(
                    m["family"], m["type"], m["mark"] or u"-", m["id"])
                leaf.Tag = [m["id"]]
                cat_node.Items.Add(leaf)
            self.treeContents.Items.Add(cat_node)

    # --- WPF Event Handlers (matched to UI.xaml) --------------------------------------------
    @ui_handler
    def preset_changed(self, sender, args):
        if self.initialising:
            return
        idx = self.cmbPreset.SelectedIndex
        if 0 <= idx < len(self.presets):
            self.apply_preset_config(self.presets[idx], trigger_load=True)

    @ui_handler
    def preset_save_clicked(self, sender, args):
        default_name = u"My Schedule Preset"
        idx = self.cmbPreset.SelectedIndex
        if 0 <= idx < len(self.presets) and not self.presets[idx].get("builtin"):
            default_name = self.presets[idx]["name"]
        name = forms.ask_for_string(default=default_name, prompt="Preset name:", title="Save Preset")
        if not name or not name.strip():
            return
        saved = self.store.save_preset(self.current_as_preset(name.strip()))
        self.presets = self.store.list_presets()
        self.populate_presets_dropdown(select_name=saved["name"])
        self.set_status(u"Saved preset '{}'.".format(saved["name"]))

    @ui_handler
    def preset_manage_clicked(self, sender, args):
        action = forms.CommandSwitchWindow.show(
            ["Delete User Preset", "Export Preset to JSON", "Import Preset from JSON"],
            message="Manage Presets",
        )
        if not action:
            return
        if action == "Delete User Preset":
            user_presets = [p["name"] for p in self.presets if not p.get("builtin")]
            if not user_presets:
                self.set_status(u"No user presets to delete.")
                return
            pick = forms.SelectFromList.show(user_presets, title="Delete Preset", button_name="Delete")
            if pick and self.store.delete_preset(pick):
                self.presets = self.store.list_presets()
                self.populate_presets_dropdown()
                self.set_status(u"Deleted preset '{}'.".format(pick))
        elif action == "Export Preset to JSON":
            path = forms.save_file(file_ext="json", default_name="schedule_preset.json")
            if path:
                self.store.export_preset(self.current_as_preset(u"Exported Preset"), path)
                self.set_status(u"Exported preset to {}".format(path))
        elif action == "Import Preset from JSON":
            path = forms.pick_file(file_ext="json")
            if path:
                imported = self.store.import_preset(path)
                self.presets = self.store.list_presets()
                self.populate_presets_dropdown(select_name=imported["name"])
                self.apply_preset_config(imported, trigger_load=True)

    @ui_handler
    def choose_sources_clicked(self, sender, args):
        items = []
        for s in state.SPECIAL_SOURCES:
            items.append(forms.TemplateListItem(u"[Special] " + s, checked=(s in self.selected_special_sources)))
        for cname, _cid, ctype in self.meta.get("categories", []):
            items.append(forms.TemplateListItem(cname, checked=(cname in self.selected_categories)))
        chosen = forms.SelectFromList.show(
            items, title="Choose Categories & Special Sources (any combination)",
            multiselect=True, button_name="Apply Sources",
        )
        if chosen is None:
            return
        specials = []
        cats = []
        for c in chosen:
            txt = state.to_text(c)
            if txt.startswith(u"[Special] "):
                specials.append(txt[len(u"[Special] "):])
            else:
                cats.append(txt)
        self.selected_special_sources = specials
        self.selected_categories = cats
        self.run_in_revit("Load sources", self.load_from_revit)

    @ui_handler
    def choose_columns_clicked(self, sender, args):
        chosen_set = set(self.columns)
        ordered_avail = list(self.columns) + [c for c in self.available_columns if c not in chosen_set]
        items = [
            forms.TemplateListItem(state.column_display_label(k), checked=(k in chosen_set))
            for k in ordered_avail
        ]
        label_to_key = dict((state.column_display_label(k), k) for k in ordered_avail)
        picked = forms.SelectFromList.show(
            items, title="Choose Columns ([T] = Type Parameter)",
            multiselect=True, button_name="Apply Columns",
        )
        if not picked:
            return
        self.columns = [label_to_key[state.to_text(lbl)] for lbl in picked if state.to_text(lbl) in label_to_key]
        self.run_in_revit("Load columns", self.load_from_revit)

    @ui_handler
    def pick_spaces_clicked(self, sender, args):
        choices = self.meta.get("spatial_choices", [])
        if not choices:
            self.set_status(u"No Spaces or Rooms found in this project.")
            return
        chosen_set = set(self.selected_space_ids)
        items = [forms.TemplateListItem(c["label"], checked=(c["id"] in chosen_set)) for c in choices]
        label_to_id = dict((c["label"], c["id"]) for c in choices)
        picked = forms.SelectFromList.show(
            items, title="Select Combination of Spaces / Rooms",
            multiselect=True, button_name="Apply Spaces",
        )
        if picked is None:
            return
        self.selected_space_ids = [label_to_id[state.to_text(p)] for p in picked if state.to_text(p) in label_to_id]
        self.rebuild_grid()
        self.set_status(u"Filtered to {} selected Space(s)/Room(s).".format(len(self.selected_space_ids)))

    @ui_handler
    def scope_changed(self, sender, args):
        if self.initialising:
            return
        self.update_scope_controls()
        if self.current_scope() == u"Active View":
            self.run_in_revit("Load active view", self.load_from_revit)
        else:
            self.rebuild_grid()

    @ui_handler
    def scope_value_changed(self, sender, args):
        if not self.initialising:
            self.rebuild_grid()

    @ui_handler
    def refresh_clicked(self, sender, args):
        self.run_in_revit("Load / Refresh", self.load_from_revit)

    @ui_handler
    def refresh_mode_changed(self, sender, args):
        if not self.initialising:
            self.set_status(u"Active refresh mode: {}.".format(self.current_refresh_mode()))

    @ui_handler
    def apply_delta_clicked(self, sender, args):
        self.run_in_revit("Refresh pending changes", self.apply_delta_from_revit)

    @ui_handler
    def search_changed(self, sender, args):
        if not self.initialising:
            self.rebuild_grid()

    @ui_handler
    def clear_search_clicked(self, sender, args):
        self.txtSearch.Text = u""

    @ui_handler
    def grouped_toggled(self, sender, args):
        if not self.initialising:
            self.rebuild_grid()

    @ui_handler
    def choose_group_by_clicked(self, sender, args):
        items = [
            forms.TemplateListItem(state.column_display_label(k), checked=(k in self.group_by))
            for k in self.columns
        ]
        label_to_key = dict((state.column_display_label(k), k) for k in self.columns)
        picked = forms.SelectFromList.show(
            items, title="Group By Columns (for Takeoffs & Totals)",
            multiselect=True, button_name="Apply Grouping",
        )
        if picked:
            self.group_by = [label_to_key[state.to_text(p)] for p in picked if state.to_text(p) in label_to_key]
            self.chkGrouped.IsChecked = True
            self.rebuild_grid()

    @ui_handler
    def sync_selection_toggled(self, sender, args):
        if not self.initialising:
            self.set_selection_sync(bool(self.chkSyncSelection.IsChecked))

    @ui_handler
    def side_panel_toggled(self, sender, args):
        if not self.initialising:
            self.update_side_panel_visibility()
            if self.chkShowSidePanel.IsChecked and not self.contents_by_spatial:
                self.run_in_revit("Load spatial contents", self.load_from_revit)
            else:
                self.update_side_panel()

    # --- Inline & Bulk Editing --------------------------------------------------------------
    def _col_key_for_grid_column(self, column):
        """Resolve internal col_key via SortMemberPath ('C<idx>'), safe after column reordering."""
        if column is None:
            return None
        path = state.to_text(getattr(column, "SortMemberPath", u""))
        if path.startswith(u"C") and path[1:].isdigit():
            idx = int(path[1:])
            if 0 <= idx < len(self.active_col_keys):
                return self.active_col_keys[idx]
        return None

    @ui_handler
    def grid_beginning_edit(self, sender, args):
        if self.chkGrouped.IsChecked:
            args.Cancel = True
            self.set_status(u"Switch off Group / Totals mode to edit individual cells.")
            return
        col_key = self._col_key_for_grid_column(args.Column)
        if col_key is None:
            args.Cancel = True
            return
        rk = state.to_text(args.Row.Item.Row[state.ID_COLUMN])
        rec = self.records_by_key.get(rk)
        if rec is None:
            args.Cancel = True
            return
        ok, _code, reason = state.evaluate_cell_modifiability(rec, col_key)
        if not ok:
            args.Cancel = True
            self.set_status(u"Cannot edit: {}".format(reason))

    @ui_handler
    def grid_cell_edit_ending(self, sender, args):
        if self.chkGrouped.IsChecked:
            return
        if args.EditAction != DataGridEditAction.Commit:
            return
        col_key = self._col_key_for_grid_column(args.Column)
        if col_key is None:
            return
        rk = state.to_text(args.Row.Item.Row[state.ID_COLUMN])
        rec = self.records_by_key.get(rk)
        if rec is None:
            return
        editor = args.EditingElement
        new_val = state.to_text(editor.Text) if isinstance(editor, TextBox) else u""
        ok, msg = self.staged.stage(rec, col_key, new_val)
        if not ok:
            args.Cancel = True
            self.set_status(u"Edit rejected: {}".format(msg))
            return
        self.update_staged_buttons()
        if state.is_type_param_key(col_key):
            blast = state.type_blast_radius(self.records, rec.get("type_id"))
            self.set_status(u"Staged TYPE edit ({} instance(s) affected). Click Apply Edits to commit."
                            .format(blast))
        else:
            self.set_status(u"Staged edit ({} pending). Click Apply Edits to commit.".format(self.staged.count()))

    def _pick_editable_column(self, title):
        editable_cols = [c for c in self.columns if not state.is_pseudo_key(c) and not state.is_count_key(c)]
        if not editable_cols:
            self.set_status(u"No editable parameter columns are currently displayed.")
            return None
        labels = [state.column_display_label(c) for c in editable_cols]
        picked = forms.SelectFromList.show(labels, title=title, button_name="Select Column")
        if not picked:
            return None
        return editable_cols[labels.index(state.to_text(picked))]

    @ui_handler
    def bulk_set_clicked(self, sender, args):
        sel = self.selected_records()
        if not sel:
            self.set_status(u"Select one or more rows first.")
            return
        col_key = self._pick_editable_column("Bulk Set — Choose Column")
        if not col_key:
            return
        val = forms.ask_for_string(prompt=u"New value for '{}' ({} rows):".format(
            state.column_display_label(col_key), len(sel)), title="Bulk Set")
        if val is None:
            return
        staged, skipped = self.staged.stage_set_value(sel, col_key, val)
        self.update_staged_buttons()
        self.rebuild_grid()
        self.set_status(u"Bulk staged {} cell(s), skipped {} non-modifiable.".format(staged, len(skipped)))

    @ui_handler
    def find_replace_clicked(self, sender, args):
        sel = self.selected_records() or self.displayed_records
        if not sel:
            self.set_status(u"No rows loaded.")
            return
        col_key = self._pick_editable_column("Find & Replace — Choose Column")
        if not col_key:
            return
        find_txt = forms.ask_for_string(prompt="Find text:", title="Find & Replace")
        if not find_txt:
            return
        repl_txt = forms.ask_for_string(prompt="Replace with:", title="Find & Replace")
        if repl_txt is None:
            return
        staged, skipped = self.staged.stage_find_replace(sel, col_key, find_txt, repl_txt)
        self.update_staged_buttons()
        self.rebuild_grid()
        self.set_status(u"Find & Replace staged {} cell(s), skipped {}.".format(staged, len(skipped)))

    @ui_handler
    def auto_number_clicked(self, sender, args):
        sel = self.selected_records()
        if not sel:
            self.set_status(u"Select rows in the order you want to number them.")
            return
        col_key = self._pick_editable_column("Auto-Number — Choose Column")
        if not col_key:
            return
        prefix = forms.ask_for_string(default="EL-", prompt="Prefix (e.g. DB-, L-, F-):", title="Auto-Number")
        if prefix is None:
            return
        start_str = forms.ask_for_string(default="1", prompt="Start number:", title="Auto-Number")
        if not start_str:
            return
        try:
            start_num = int(start_str)
        except Exception:
            self.set_status(u"Start number must be an integer.")
            return
        staged, skipped = self.staged.stage_auto_number(sel, col_key, prefix=prefix, start=start_num, padding=2)
        self.update_staged_buttons()
        self.rebuild_grid()
        self.set_status(u"Auto-numbered {} cell(s), skipped {}.".format(staged, len(skipped)))

    @ui_handler
    def paste_clipboard_clicked(self, sender, args):
        sel = self.selected_records()
        if not sel:
            self.set_status(u"Select target rows first before pasting.")
            return
        if not Clipboard.ContainsText():
            self.set_status(u"Clipboard does not contain text.")
            return
        col_key = self._pick_editable_column("Paste Clipboard — Starting Column")
        if not col_key:
            return
        start_idx = self.columns.index(col_key)
        target_cols = self.columns[start_idx:]
        staged, skipped = self.staged.stage_paste_tsv(sel, target_cols, Clipboard.GetText())
        self.update_staged_buttons()
        self.rebuild_grid()
        self.set_status(u"Pasted {} cell(s), skipped {} non-modifiable.".format(staged, len(skipped)))

    @ui_handler
    def apply_edits_clicked(self, sender, args):
        if self.staged.count() == 0:
            return
        self.run_in_revit("Apply parameter edits", self.apply_staged_to_revit)

    @ui_handler
    def revert_edits_clicked(self, sender, args):
        self.staged.clear()
        self.update_staged_buttons()
        self.rebuild_grid()
        self.set_status(u"Reverted all staged edits.")

    @ui_handler
    def delete_selected_clicked(self, sender, args):
        sel = self.selected_records()
        if not sel:
            self.set_status(u"Select one or more rows to delete.")
            return
        self.run_in_revit("Safe delete elements", lambda uiapp: self.delete_selected_from_revit(uiapp, sel))

    # --- Excel Export / Import --------------------------------------------------------------
    @ui_handler
    def export_excel_clicked(self, sender, args):
        if not self.displayed_records:
            self.set_status(u"Load a schedule before exporting.")
            return
        default_name = u"{}_Schedule.xlsx".format(self.doc_title.replace(u" ", u"_"))
        path = forms.save_file(file_ext="xlsx", default_name=default_name)
        if not path:
            return
        res = excel_io.export_schedule_xlsx(
            path, self.displayed_records, self.columns,
            doc_meta=self.meta, protect_sheet=self.protect_excel,
        )
        self.set_status(
            u"Exported {} rows ({} editable, {} locked cells) to {}.".format(
                res["rows"], res["editable_cells"], res["locked_cells"], res["path"]))

    @ui_handler
    def import_excel_clicked(self, sender, args):
        if not self.records:
            self.set_status(u"Load the matching schedule first so elements can be compared.")
            return
        path = forms.pick_file(file_ext="xlsx")
        if not path:
            return
        self.run_in_revit("Import Excel schedule", lambda uiapp: self.import_excel_in_revit(uiapp, path))

    # --- Navigation & Close -----------------------------------------------------------------
    @ui_handler
    def grid_selection_changed(self, sender, args):
        if not self.initialising and self.chkShowSidePanel.IsChecked:
            self.update_side_panel()

    @ui_handler
    def row_double_clicked(self, sender, args):
        if isinstance(ItemsControl.ContainerFromElement(self.grid, args.OriginalSource), DataGridRow):
            # Do not steal focus / zoom Revit when double-clicking an editable cell to type.
            cur_col = getattr(self.grid.CurrentCell, "Column", None)
            if not self.chkGrouped.IsChecked and cur_col is not None and not cur_col.IsReadOnly:
                return
            ids = self.selected_host_ids()
            if ids:
                self.run_in_revit("Show elements", lambda uiapp: self.navigate_in_revit(uiapp, ids, "show"))

    @ui_handler
    def select_clicked(self, sender, args):
        ids = self.selected_host_ids()
        if ids:
            self.run_in_revit("Select elements", lambda uiapp: self.navigate_in_revit(uiapp, ids, "select"))

    @ui_handler
    def show_clicked(self, sender, args):
        ids = self.selected_host_ids()
        if ids:
            self.run_in_revit("Show elements", lambda uiapp: self.navigate_in_revit(uiapp, ids, "show"))

    @ui_handler
    def isolate_clicked(self, sender, args):
        ids = self.selected_host_ids()
        if ids:
            self.run_in_revit("Isolate elements", lambda uiapp: self.navigate_in_revit(uiapp, ids, "isolate"))

    @ui_handler
    def reset_isolate_clicked(self, sender, args):
        self.run_in_revit("Reset isolate", lambda uiapp: self.navigate_in_revit(uiapp, [], "reset_isolate"))

    @ui_handler
    def open_view_clicked(self, sender, args):
        ids = self.selected_host_ids()
        if ids:
            self.run_in_revit("Open view", lambda uiapp: self.navigate_in_revit(uiapp, ids, "open_view"))

    @ui_handler
    def contents_select_clicked(self, sender, args):
        node = self.treeContents.SelectedItem
        ids = list(node.Tag) if (node is not None and node.Tag) else list(self.contents_ids)
        if ids:
            self.run_in_revit("Select contents", lambda uiapp: self.navigate_in_revit(uiapp, ids, "select"))

    @ui_handler
    def contents_show_clicked(self, sender, args):
        node = self.treeContents.SelectedItem
        ids = list(node.Tag) if (node is not None and node.Tag) else list(self.contents_ids)
        if ids:
            self.run_in_revit("Show contents", lambda uiapp: self.navigate_in_revit(uiapp, ids, "show"))

    @ui_handler
    def close_clicked(self, sender, args):
        self.Close()

    def on_closed(self, sender, args):
        global WINDOW
        self.closed = True
        try:
            self.debounce_timer.Stop()
        except Exception:
            pass
        self.detach_revit_events()
        self.queue.clear()
        try:
            self.ext_event.Dispose()
        except Exception:
            pass
        if WINDOW is self:
            WINDOW = None


def main():
    global WINDOW
    doc = revit.doc
    uidoc = revit.uidoc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Open a Revit project before running Revit Browser & Scheduler.",
                    title="Revit Browser & Scheduler")
        return
    if WINDOW is not None and not WINDOW.closed:
        try:
            WINDOW.Close()
        except Exception:
            pass
    uiapp = __revit__  # noqa: F821 (pyRevit host UIApplication)
    WINDOW = UltimateBrowserWindow(uiapp, doc, uidoc)
    WINDOW.show(modal=False)


main()
