# -*- coding: utf-8 -*-
"""Template: modeless WPF window that safely modifies the model.

Pattern (copy this for every modeless tool):
  * __persistentengine__ = True keeps the script engine alive after the
    script returns; without it the window's buttons die ("dead button").
  * A modeless window runs OUTSIDE a Revit API context, so model changes
    must go through ExternalEvent + IExternalEventHandler.
  * Transactions are opened inside the handler's Execute(), never in the
    window's click handlers.

IronPython 2.7 compatible (no f-strings, no type hints).
"""
__title__ = "Modeless\nWindow"
__author__ = "Custom"
__persistentengine__ = True

import os.path as op

from pyrevit import DB, UI, forms, revit, script

logger = script.get_logger()
XAML_PATH = op.join(op.dirname(__file__), "window.xaml")


class ApiActionHandler(UI.IExternalEventHandler):
    """Runs one queued callable inside a valid Revit API context."""

    def __init__(self):
        self.action = None

    def Execute(self, uiapp):
        action, self.action = self.action, None
        if action is None:
            return
        try:
            action(uiapp)
        except Exception as ex:
            logger.error("Action failed: {}".format(ex))
            forms.alert("Action failed:\n{}".format(ex), title="Error")

    def GetName(self):
        return "ApiActionHandler"


class ModelessWindow(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, XAML_PATH)
        self.handler = ApiActionHandler()
        # Must be created here, while a valid API context exists.
        self.ext_event = UI.ExternalEvent.Create(self.handler)
        self.refresh_selection_count()

    def run_in_revit(self, action):
        """Queue action(uiapp) to run in Revit's API context."""
        self.handler.action = action
        self.ext_event.Raise()

    # --- UI event handlers (names referenced from window.xaml) -----------
    def refresh_click(self, sender, args):
        self.refresh_selection_count()

    def apply_click(self, sender, args):
        text = self.comment_box.Text
        self.run_in_revit(lambda uiapp: self.write_comments(uiapp, text))

    def close_click(self, sender, args):
        self.Close()

    # --- UI-only work (read-only access to selection is safe here) -------
    def refresh_selection_count(self):
        try:
            count = len(revit.get_selection())
        except Exception:
            count = 0
        self.status_text.Text = "{} element(s) selected".format(count)

    # --- Model work: only ever called from ApiActionHandler.Execute -------
    def write_comments(self, uiapp, text):
        uidoc = uiapp.ActiveUIDocument
        if uidoc is None:
            self.status_text.Text = "No active document."
            return
        doc = uidoc.Document
        ids = list(uidoc.Selection.GetElementIds())
        if not ids:
            self.status_text.Text = "Nothing selected."
            return

        changed = 0
        with revit.Transaction("Set Comments", doc=doc):
            for eid in ids:
                param = doc.GetElement(eid).get_Parameter(
                    DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS
                )
                if param is not None and not param.IsReadOnly:
                    param.Set(text)
                    changed += 1
        self.status_text.Text = "Updated {} of {} element(s)".format(changed, len(ids))


# Keep a module-level reference so the window is not garbage collected.
window = ModelessWindow()
window.show(modal=False)
