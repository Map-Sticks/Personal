# -*- coding: utf-8 -*-
"""Draw switchlines (detail lines) in the active view between switches/sensors and the light
fittings they switch. Build one or more switch groups in a session, preview them together (each group
in its own colour), then accept, adjust or cancel. Grouping the result is optional."""
from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException
from System.Collections.Generic import List
from System.Windows import Controls, Media, Thickness

import switchline_logic as logic

__title__ = 'Draw\nSwitchlines'

LINE_STYLE_NAME = '!ETC_Switch line'
JOINT_FAMILY_NAME = 'Joint Symbol'
PLAN_VIEW_TYPES = (DB.ViewType.FloorPlan, DB.ViewType.CeilingPlan, DB.ViewType.EngineeringPlan)
PREVIEW_COLOURS = ((220, 0, 0), (0, 90, 230), (0, 150, 60), (150, 0, 170), (230, 120, 0),
                   (0, 160, 170), (120, 80, 0), (230, 0, 140))
COLOUR_NAMES = ('red', 'blue', 'green', 'purple', 'orange', 'teal', 'brown', 'pink')

WINDOW_XAML = 'window.xaml'
CONFIG_DETAIL_GROUPS = 'make_detail_groups'

logger = script.get_logger()


def id_value(element_id):
    value = getattr(element_id, 'Value', None)
    return value if value is not None else element_id.IntegerValue


def role_of(element):
    """'switch' for Lighting Devices, 'fitting' for Lighting Fixtures, else None."""
    cat = element.Category if element is not None else None
    if cat is None or not isinstance(element, DB.FamilyInstance):
        return None
    if not isinstance(element.Location, DB.LocationPoint):
        return None
    value = id_value(cat.Id)
    if value == int(DB.BuiltInCategory.OST_LightingDevices):
        return 'switch'
    if value == int(DB.BuiltInCategory.OST_LightingFixtures):
        return 'fitting'
    return None


class SwitchlineSelectionFilter(ISelectionFilter):
    def __init__(self, visible_ids):
        self.visible_ids = visible_ids

    def AllowElement(self, element):
        return role_of(element) is not None and id_value(element.Id) in self.visible_ids

    def AllowReference(self, reference, position):
        return False


class ViewBasis(object):
    """Converts between model XYZ and the view's 2D (u = right, v = up) drafting coordinates."""

    def __init__(self, view):
        self.right = view.RightDirection
        self.up = view.UpDirection
        level = view.GenLevel
        self.z = level.Elevation if level is not None else 0.0

    def to_uv(self, xyz):
        return (xyz.DotProduct(self.right), xyz.DotProduct(self.up))

    def dir_to_uv(self, vec):
        return (vec.DotProduct(self.right), vec.DotProduct(self.up))

    def to_xyz(self, uv):
        p = self.right.Multiply(uv[0]).Add(self.up.Multiply(uv[1]))
        return DB.XYZ(p.X, p.Y, self.z)


def type_label(doc, element):
    etype = doc.GetElement(element.GetTypeId())
    family = getattr(etype, 'FamilyName', '') or ''
    param = etype.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM) if etype else None
    name = param.AsString() if param else ''
    return '{0} : {1}'.format(family, name or '')


def make_node(doc, element, basis):
    point = element.Location.Point
    u, v = basis.to_uv(point)
    label = type_label(doc, element)
    if role_of(element) == 'fitting':
        return logic.Node(id_value(element.Id), 'fitting', u, v, label=label)
    facing = None
    wall = not logic.is_ceiling_device(label)
    if wall:
        fu, fv = basis.dir_to_uv(element.FacingOrientation)
        length = (fu * fu + fv * fv) ** 0.5
        if length > 0.5:
            # Snap to the nearest view axis; switchlines are orthogonal in the view.
            facing = (1.0 if fu > 0 else -1.0, 0.0) if abs(fu) >= abs(fv) else (0.0, 1.0 if fv > 0 else -1.0)
    return logic.Node(id_value(element.Id), 'switch', u, v, facing=facing, wall=wall, label=label)


def find_line_style(doc):
    lines_cat = doc.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Lines)
    for sub in lines_cat.SubCategories:
        if sub.Name == LINE_STYLE_NAME:
            return sub.GetGraphicsStyle(DB.GraphicsStyleType.Projection)
    return None


def find_joint_symbol(doc):
    collector = DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol) \
        .OfCategory(DB.BuiltInCategory.OST_GenericAnnotation)
    for symbol in collector:
        if symbol.FamilyName == JOINT_FAMILY_NAME:
            return symbol
    return None


def collect_view_context(doc, view, basis):
    """Visible fitting/device ids, all their points (obstacles) and existing switchline segments."""
    visible = set()
    obstacles = []
    for bic in (DB.BuiltInCategory.OST_LightingFixtures, DB.BuiltInCategory.OST_LightingDevices):
        collector = DB.FilteredElementCollector(doc, view.Id).OfCategory(bic).WhereElementIsNotElementType()
        for element in collector:
            visible.add(id_value(element.Id))
            if isinstance(element.Location, DB.LocationPoint):
                obstacles.append(basis.to_uv(element.Location.Point))
    existing = []
    for curve_el in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.CurveElement):
        style = curve_el.LineStyle
        curve = curve_el.GeometryCurve
        if style is None or style.Name != LINE_STYLE_NAME or not isinstance(curve, DB.Line):
            continue
        a = basis.to_uv(curve.GetEndPoint(0))
        b = basis.to_uv(curve.GetEndPoint(1))
        if abs(a[0] - b[0]) < logic.TOL or abs(a[1] - b[1]) < logic.TOL:
            existing.append((a, b))
    return visible, obstacles, existing


def pick_elements(uidoc, doc, visible, prompt):
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element, SwitchlineSelectionFilter(visible), prompt)
    except OperationCanceledException:
        return []
    return [doc.GetElement(r.ElementId) for r in refs]


def preselected_elements(uidoc, doc, visible):
    picked = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
    return [e for e in picked if role_of(e) and id_value(e.Id) in visible]


def group_from_elements(doc, elements, basis, title, previous=None):
    """Build a switch group from picked elements; None if cancelled or incomplete.
    ``previous`` keeps an edited group's mode/layout settings when it is re-picked."""
    switches = [make_node(doc, e, basis) for e in elements if role_of(e) == 'switch']
    fittings = [make_node(doc, e, basis) for e in elements if role_of(e) == 'fitting']
    if not switches or not fittings:
        if elements:
            forms.alert('{0} needs at least one switch/sensor (Lighting Devices) and one light fitting '
                        '(Lighting Fixtures). Nothing was changed.'.format(title))
        return None
    if previous is None:
        return logic.make_group(switches, fittings)
    mode = previous['mode'] if len(fittings) >= 2 else logic.MODE_ALL
    return logic.make_group(switches, fittings, mode, previous['style'],
                            previous['horizontal_first'], previous['side'])


def pick_group(uidoc, doc, visible, basis, title, previous=None):
    prompt = '{0}: select its switches/sensors and the light fittings they switch, then click Finish'.format(title)
    return group_from_elements(doc, pick_elements(uidoc, doc, visible, prompt), basis, title, previous)


def colour_name(index):
    return COLOUR_NAMES[index % len(COLOUR_NAMES)]


def group_label(index, group):
    mode = 'every second fitting (A/B)' if group['mode'] == logic.MODE_ALTERNATE else 'all together'
    return 'Group {0} ({1}): {2} switch/sensor, {3} fitting(s), {4}, {5}'.format(
        index + 1, colour_name(index), len(group['switches']), len(group['fittings']), mode,
        group['style'])


def draw_layout(doc, view, layout, basis, line_style, joint_symbol, group_count):
    """Create the layout; returns a list of created element ids per switch group."""
    created = [[] for _ in range(group_count)]
    min_len = doc.Application.ShortCurveTolerance
    for (a, b), gi in zip(layout['lines'], layout['line_groups']):
        p, q = basis.to_xyz(a), basis.to_xyz(b)
        if p.DistanceTo(q) < min_len:
            continue
        curve = doc.Create.NewDetailCurve(view, DB.Line.CreateBound(p, q))
        curve.LineStyle = line_style
        created[gi].append(curve.Id)
    for (a, b, mid), gi in zip(layout['arcs'], layout['arc_groups']):
        arc = DB.Arc.Create(basis.to_xyz(a), basis.to_xyz(b), basis.to_xyz(mid))
        curve = doc.Create.NewDetailCurve(view, arc)
        curve.LineStyle = line_style
        created[gi].append(curve.Id)
    if joint_symbol is not None and layout['joints']:
        if not joint_symbol.IsActive:
            joint_symbol.Activate()
        for p, gi in zip(layout['joints'], layout['joint_groups']):
            created[gi].append(doc.Create.NewFamilyInstance(basis.to_xyz(p), joint_symbol, view).Id)
    return created


def colour_preview(view, created):
    """Temporary per-group colour so the groups can be told apart; rolled back with the preview."""
    for gi, ids in enumerate(created):
        r, g, b = PREVIEW_COLOURS[gi % len(PREVIEW_COLOURS)]
        ogs = DB.OverrideGraphicSettings()
        ogs.SetProjectionLineColor(DB.Color(r, g, b))
        for element_id in ids:
            view.SetElementOverrides(element_id, ogs)


def clear_preview_colour(view, created):
    blank = DB.OverrideGraphicSettings()
    for ids in created:
        for element_id in ids:
            view.SetElementOverrides(element_id, blank)


class PreviewSession(object):
    """Owns the switch groups and the preview transaction that shows them in the view."""

    def __init__(self, doc, uidoc, view, basis, line_style, joint_symbol, visible, obstacles, existing):
        self.doc = doc
        self.uidoc = uidoc
        self.view = view
        self.basis = basis
        self.line_style = line_style
        self.joint_symbol = joint_symbol
        self.visible = visible
        self.obstacles = obstacles
        self.existing = existing
        self.groups = []
        self.layout = None
        self.error = None
        self.created = []
        self.tx = None
        self.position = None

    def clear(self):
        if self.tx is not None and self.tx.HasStarted() and not self.tx.HasEnded():
            self.tx.RollBack()
        self.tx = None
        self.created = []

    def redraw(self):
        self.clear()
        self.layout = None
        self.error = None
        if not self.groups:
            self.uidoc.RefreshActiveView()
            return
        try:
            layout = logic.build_session(self.groups, scale=self.view.Scale,
                                         existing_segments=self.existing,
                                         extra_obstacles=self.obstacles)
        except ValueError as err:
            self.error = str(err)
            self.uidoc.RefreshActiveView()
            return
        self.tx = DB.Transaction(self.doc, 'Draw Switchlines')
        self.tx.Start()
        try:
            self.created = draw_layout(self.doc, self.view, layout, self.basis, self.line_style,
                                       self.joint_symbol, len(self.groups))
            colour_preview(self.view, self.created)
            self.doc.Regenerate()
        except Exception:
            self.clear()
            raise
        self.layout = layout
        self.uidoc.RefreshActiveView()

    def summary(self):
        if self.error:
            return 'Cannot draw: ' + self.error
        if self.layout is None:
            return 'No switch groups yet. Click "+ Add group...".'
        text = logic.describe(self.layout).split('\n')[-1]
        if self.joint_symbol is None and self.layout['joints']:
            text += '\n"{0}" family not loaded: joints skipped.'.format(JOINT_FAMILY_NAME)
        return text

    def commit(self, make_detail_groups):
        if self.tx is None:
            return False
        try:
            clear_preview_colour(self.view, self.created)
            if make_detail_groups:
                # Regenerating first avoids "group changed outside group edit mode" warnings.
                self.doc.Regenerate()
                for ids in self.created:
                    if ids:
                        self.doc.Create.NewGroup(List[DB.ElementId](ids))
            self.tx.Commit()
        except Exception:
            self.clear()
            raise
        self.tx = None
        return True


def _brush(index):
    r, g, b = PREVIEW_COLOURS[index % len(PREVIEW_COLOURS)]
    return Media.SolidColorBrush(Media.Color.FromRgb(r, g, b))


class SessionWindow(forms.WPFWindow):
    """Modal session panel. Edits redraw the preview at once (modal handlers run in the API
    context). Picking needs Revit's UI, so Add/Re-pick close the window and main() reopens it."""

    def __init__(self, session, selected, make_detail_groups):
        forms.WPFWindow.__init__(self, script.get_bundle_file(WINDOW_XAML))
        self.session = session
        self.action = 'cancel'
        self.selected = selected
        self._loading = False
        if session.position:
            self.Left, self.Top = session.position
        else:
            self.Left, self.Top = 60, 140
        self.txtView.Text = '{0}  (1:{1})'.format(session.view.Name, session.view.Scale)
        self.chkDetailGroups.IsChecked = make_detail_groups
        self.lbGroups.SelectionChanged += self.on_group_selected
        self.cbMode.SelectionChanged += self.on_mode
        self.cbStyle.SelectionChanged += self.on_style
        self.btnFlip.Click += self.on_flip
        self.btnAdd.Click += self.on_add
        self.btnRepick.Click += self.on_repick
        self.btnRemove.Click += self.on_remove
        self.btnAccept.Click += self.on_accept
        self.btnCancel.Click += self.on_cancel
        self.Closing += self.on_closing
        self.refresh_list()

    @property
    def make_detail_groups(self):
        return bool(self.chkDetailGroups.IsChecked)

    def current(self):
        i = self.lbGroups.SelectedIndex
        return (i, self.session.groups[i]) if 0 <= i < len(self.session.groups) else (None, None)

    def refresh_list(self):
        self._loading = True
        try:
            self.lbGroups.Items.Clear()
            for i, g in enumerate(self.session.groups):
                row = Controls.StackPanel()
                row.Orientation = Controls.Orientation.Horizontal
                swatch = Controls.Border()
                swatch.Width = swatch.Height = 12
                swatch.Margin = Thickness(0, 0, 6, 0)
                swatch.Background = _brush(i)
                text = Controls.TextBlock()
                text.Text = group_label(i, g).replace(' ({0})'.format(colour_name(i)), '')
                text.ToolTip = '\n'.join(n.label for n in g['switches'] + g['fittings'] if n.label)
                row.Children.Add(swatch)
                row.Children.Add(text)
                self.lbGroups.Items.Add(row)
            if self.session.groups:
                self.lbGroups.SelectedIndex = min(max(self.selected, 0), len(self.session.groups) - 1)
        finally:
            self._loading = False
        self.load_editor()
        self.txtSummary.Text = self.session.summary()
        self.btnAccept.IsEnabled = self.session.layout is not None

    def load_editor(self):
        i, g = self.current()
        enabled = g is not None
        for control in (self.cbMode, self.cbStyle, self.btnFlip, self.btnRepick, self.btnRemove):
            control.IsEnabled = enabled
        if not enabled:
            return
        self.selected = i
        self._loading = True
        try:
            self.cbMode.SelectedIndex = 1 if g['mode'] == logic.MODE_ALTERNATE else 0
            self.cbMode.IsEnabled = len(g['fittings']) >= 2
            self.cbStyle.SelectedIndex = 1 if g['style'] == logic.STYLE_TRUNK else 0
        finally:
            self._loading = False

    def changed(self):
        self.session.redraw()
        self.refresh_list()

    def on_group_selected(self, sender, args):
        if not self._loading:
            self.load_editor()

    def on_mode(self, sender, args):
        i, g = self.current()
        if self._loading or g is None:
            return
        g['mode'] = logic.MODE_ALTERNATE if self.cbMode.SelectedIndex == 1 else logic.MODE_ALL
        g['style'] = logic.STYLE_TRUNK if g['mode'] == logic.MODE_ALTERNATE else logic.STYLE_CHAIN
        self.changed()

    def on_style(self, sender, args):
        i, g = self.current()
        if self._loading or g is None:
            return
        g['style'] = logic.STYLE_TRUNK if self.cbStyle.SelectedIndex == 1 else logic.STYLE_CHAIN
        self.changed()

    def on_flip(self, sender, args):
        i, g = self.current()
        if g is None:
            return
        if g['style'] == logic.STYLE_TRUNK:
            g['side'] = -g['side']
        else:
            g['horizontal_first'] = not g['horizontal_first']
        self.changed()

    def on_remove(self, sender, args):
        i, g = self.current()
        if g is None:
            return
        del self.session.groups[i]
        self.selected = max(0, i - 1)
        self.changed()

    def finish(self, action):
        self.action = action
        self.Close()

    def on_add(self, sender, args):
        self.finish('add')

    def on_repick(self, sender, args):
        if self.current()[1] is not None:
            self.finish('repick')

    def on_accept(self, sender, args):
        self.finish('accept')

    def on_cancel(self, sender, args):
        self.finish('cancel')

    def on_closing(self, sender, args):
        self.session.position = (self.Left, self.Top)


def main():
    doc = revit.doc
    uidoc = revit.uidoc
    view = doc.ActiveView
    if view is None or view.IsTemplate or view.ViewType not in PLAN_VIEW_TYPES:
        forms.alert('Open a floor plan or reflected ceiling plan and run Draw Switchlines there.\n'
                    'Switchlines are only drawn in the active view.', exitscript=True)
    line_style = find_line_style(doc)
    if line_style is None:
        forms.alert('Line style "{0}" was not found in this project.'.format(LINE_STYLE_NAME),
                    exitscript=True)

    basis = ViewBasis(view)
    visible, obstacles, existing = collect_view_context(doc, view, basis)
    session = PreviewSession(doc, uidoc, view, basis, line_style, find_joint_symbol(doc),
                             visible, obstacles, existing)
    first = preselected_elements(uidoc, doc, visible)
    group = group_from_elements(doc, first, basis, 'Group 1') if first else \
        pick_group(uidoc, doc, visible, basis, 'Group 1')
    if group is None:
        return
    session.groups.append(group)

    config = script.get_config()
    make_detail_groups = bool(config.get_option(CONFIG_DETAIL_GROUPS, False))
    selected = 0
    try:
        while True:
            session.redraw()
            window = SessionWindow(session, selected, make_detail_groups)
            window.show_dialog()
            selected = window.selected
            make_detail_groups = window.make_detail_groups
            if window.action == 'accept':
                session.commit(make_detail_groups)
                setattr(config, CONFIG_DETAIL_GROUPS, make_detail_groups)
                script.save_config()
                return
            # PickObjects runs with no transaction open; the preview returns when the window reopens.
            session.clear()
            if window.action == 'add':
                title = 'Group {0}'.format(len(session.groups) + 1)
                new_group = pick_group(uidoc, doc, visible, basis, title)
                if new_group is not None:
                    session.groups.append(new_group)
                    selected = len(session.groups) - 1
            elif window.action == 'repick':
                old = session.groups[selected]
                new_group = pick_group(uidoc, doc, visible, basis, 'Group {0}'.format(selected + 1), old)
                if new_group is not None:
                    session.groups[selected] = new_group
            else:
                return
    finally:
        session.clear()


if __name__ == '__main__':
    main()
