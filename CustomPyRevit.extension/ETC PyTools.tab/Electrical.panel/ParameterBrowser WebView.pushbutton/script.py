# -*- coding: utf-8 -*-
"""Parameter Browser in pyRevit 7's WebView2 output window (read-only).

Same pattern as the working WebView example: the command reads the model once (valid API
context), then hands plain data plus app.js/app.css to the pyRevit output window and ends.
No Python runs after that, so there is no persistent engine, ExternalEvent or custom WebView2
host. In the window:
  * filtering, sorting, columns and the contents tree run in JavaScript on the cached data;
  * Select / Show navigate to revit://outputhelpers?command=select..., handled by pyRevit;
  * Refresh model navigates to revit://outputhelpers?command=button... which re-runs this
    button; the new run closes the old window (close_others).
"""
__title__ = "Parameter Browser\nWebView"
__doc__ = "Searchable Space/Room parameter browser in the pyRevit 7 WebView2 output window. Read-only."

import datetime
import io

import pyrevit
from pyrevit import DB, EXEC_PARAMS, forms, revit, script

import parameter_browser_logic as logic
import parameter_browser_webview_data as pbdata

TITLE = "Parameter Browser WebView"
SOURCE_CATEGORIES = {
    "Spaces": DB.BuiltInCategory.OST_MEPSpaces,
    "Rooms": DB.BuiltInCategory.OST_Rooms,
}


def uses_webview_output(output):
    """pyRevit 7+ renders output with WebView2 (and output.renderer raises NotImplementedError)."""
    try:
        if int(getattr(pyrevit, "VERSION_MAJOR", 0)) >= 7:
            return True
    except Exception:
        pass
    try:
        output.renderer
    except NotImplementedError:
        return True
    except Exception:
        return False
    return False


def bundle_text(name):
    with io.open(script.get_bundle_file(name), "r", encoding="utf-8") as fp:
        return fp.read()


def command_control_id():
    try:
        return EXEC_PARAMS.command_controlid or u""
    except Exception:
        return u""


# --- Revit reads (same calls as ParameterBrowser.pushbutton) ---------------------------------
def element_name(doc, element_id):
    try:
        target = doc.GetElement(element_id)
        return target.Name if target is not None else None
    except Exception:
        return None


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


def read_source(doc, category, view_id):
    """Records with every parameter value (the browser cannot ask Revit for more later)."""
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

    def lookup(eid):
        return element_name(doc, eid)

    names = set()
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
        values = pbdata.read_parameter_values(e, lookup)
        names.update(values.keys())
        records.append({"id": eid, "level": level_names[level_key], "visible": eid in visible,
                        "category": category_name, "label": spatial_label(e, eid),
                        "values": values, "counts": {}})
    return records, pbdata.source_fields(names)


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


def read_contents(doc, space_lookup, room_lookup, progress):
    """Family instances of CONTENT_CATEGORIES with the Space/Room each one is in."""
    items = []
    level_names = {}
    for label, bic_name in logic.CONTENT_CATEGORIES:
        progress()
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


def read_model(doc, uidoc, output):
    total = len(logic.CONTENT_CATEGORIES) + len(SOURCE_CATEGORIES) + 1
    step = [0]

    def progress():
        step[0] += 1
        try:
            output.update_progress(step[0], total)
        except Exception:
            pass

    view = uidoc.ActiveView
    view_id = view.Id if view is not None else None
    progress()
    lookups = {
        "Spaces": SpatialLookup("get_Space", spatial_phases(doc, SOURCE_CATEGORIES["Spaces"])),
        "Rooms": SpatialLookup("get_Room", spatial_phases(doc, SOURCE_CATEGORIES["Rooms"])),
    }
    items = read_contents(doc, lookups["Spaces"], lookups["Rooms"], progress)
    sources = {}
    for source in logic.SOURCES:
        progress()
        records, fields = read_source(doc, SOURCE_CATEGORIES[source], view_id)
        index = logic.index_contents(items, "space" if source == "Spaces" else "room")
        for rec in records:
            rec["counts"] = logic.category_counts(index.get(rec["id"], []))
        sources[source] = {"records": records, "fields": fields}
    phase_notes = dict((k, v.summary(k.lower())) for k, v in lookups.items())
    view_name = logic.to_text(view.Name) if view is not None else u""
    return sources, items, phase_notes, view_name


def main():
    output = script.get_output()
    if not uses_webview_output(output):
        forms.alert("This button needs pyRevit 7 or newer (WebView2 output window).\n"
                    "Use the 'Parameter Browser' button on older pyRevit versions.", title=TITLE)
        return
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Open a Revit project before running Parameter Browser.", title=TITLE)
        return

    # Refresh model re-runs this button; replace the previous browser window.
    output.close_others()
    output.set_title(u"Parameter Browser - {}".format(logic.to_text(doc.Title)))
    output.set_width(1500)
    output.set_height(860)

    sources, items, phase_notes, view_name = read_model(doc, revit.uidoc, output)
    payload = pbdata.build_payload(
        document=doc.Title,
        view=view_name,
        sources=sources,
        contents=items,
        phase_notes=phase_notes,
        settings=pbdata.load_settings(pbdata.settings_path()),
        control_id=command_control_id(),
        generated=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    try:
        output.hide_progress()
    except Exception:
        pass
    output.add_style(bundle_text("app.css"))
    # inject_script (unlike print_html) does not rewrite :emoji: codes or newlines in the data.
    output.inject_script(pbdata.data_script(payload) + bundle_text("app.js"), body=True)


main()
