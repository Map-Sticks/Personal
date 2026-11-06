# -*- coding: utf-8 -*-
from __future__ import division, print_function
import os
from pyrevit import revit, DB, forms, script

doc = revit.doc
sheet = doc.ActiveView
if not isinstance(sheet, DB.ViewSheet):
    forms.alert('Open the generated sheet first.', exitscript=True, title='Align Elevation Rows')


def mm_to_ft(value):
    try:
        return DB.UnitUtils.ConvertToInternalUnits(float(value), DB.UnitTypeId.Millimeters)
    except Exception:
        return DB.UnitUtils.ConvertToInternalUnits(float(value), DB.DisplayUnitType.DUT_MILLIMETERS)


def elevation_viewports():
    result = []
    for viewport_id in sheet.GetAllViewports():
        viewport = doc.GetElement(viewport_id)
        view = doc.GetElement(viewport.ViewId)
        if view and view.ViewType == DB.ViewType.Elevation:
            result.append(viewport)
    return result


def size(viewport):
    outline = viewport.GetBoxOutline()
    return (outline.MaximumPoint.X - outline.MinimumPoint.X,
            outline.MaximumPoint.Y - outline.MinimumPoint.Y)


def group_rows(viewports, tolerance):
    # Preserve present row membership. Compare box-bottom Y values rather than
    # viewport centres because unequal elevation heights skew centre positions.
    ordered = sorted(viewports,
                     key=lambda vp: vp.GetBoxOutline().MinimumPoint.Y,
                     reverse=True)
    rows = []
    for viewport in ordered:
        bottom = viewport.GetBoxOutline().MinimumPoint.Y
        match = None
        nearest = None
        for row in rows:
            reference = sum(vp.GetBoxOutline().MinimumPoint.Y for vp in row) / float(len(row))
            distance = abs(bottom - reference)
            if distance <= tolerance and (nearest is None or distance < nearest):
                match = row
                nearest = distance
        if match is None:
            rows.append([viewport])
        else:
            match.append(viewport)
    for row in rows:
        row.sort(key=lambda vp: vp.GetBoxCenter().X)
    return rows


def align_row(row, mode, horizontal_gap):
    # Keep the row's current horizontal centre and current average vertical datum.
    widths = [size(vp)[0] for vp in row]
    total_width = sum(widths) + horizontal_gap * max(0, len(row) - 1)
    row_centre_x = sum(vp.GetBoxCenter().X for vp in row) / float(len(row))
    box_bottoms = [vp.GetBoxOutline().MinimumPoint.Y for vp in row]
    box_tops = [vp.GetBoxOutline().MaximumPoint.Y for vp in row]
    box_centres = [vp.GetBoxCenter().Y for vp in row]
    target_bottom = sum(box_bottoms) / float(len(box_bottoms))
    target_top = sum(box_tops) / float(len(box_tops))
    target_centre = sum(box_centres) / float(len(box_centres))
    x = row_centre_x - total_width / 2.0
    for viewport in row:
        width, height = size(viewport)
        if mode == 'Viewport bottoms':
            centre_y = target_bottom + height / 2.0
        elif mode == 'Viewport tops':
            centre_y = target_top - height / 2.0
        else:
            centre_y = target_centre
        viewport.SetBoxCenter(DB.XYZ(x + width / 2.0, centre_y, 0.0))
        x += width + horizontal_gap


class Window(forms.WPFWindow):
    def __init__(self, xaml):
        forms.WPFWindow.__init__(self, xaml)
        self.config = script.get_config('align_existing_elevation_rows')
        self.mode.ItemsSource = ['Viewport bottoms', 'Viewport centres', 'Viewport tops']
        self.mode.SelectedIndex = 0
        self.tolerance.Text = str(self._get('tolerance', '80'))
        self.gap.Text = str(self._get('gap', '20'))
        self.result = False

    def _get(self, key, default):
        try:
            value = getattr(self.config, key)
            return default if value is None else value
        except Exception:
            return default

    def apply(self, sender, args):
        try:
            self.tolerance_value = mm_to_ft(self.tolerance.Text)
            self.gap_value = mm_to_ft(self.gap.Text)
        except Exception:
            forms.alert('Tolerance and gap must be valid millimetre values.', title='Align Elevation Rows')
            return
        self.mode_value = self.mode.SelectedItem
        self.config.tolerance = self.tolerance.Text
        self.config.gap = self.gap.Text
        script.save_config()
        self.result = True
        self.Close()

    def cancel(self, sender, args):
        self.Close()


viewports = elevation_viewports()
if not viewports:
    forms.alert('No elevation viewports were found on the active sheet.', exitscript=True, title='Align Elevation Rows')
window = Window(os.path.join(os.path.dirname(__file__), 'ui.xaml'))
window.ShowDialog()
if not window.result:
    script.exit()

with revit.Transaction('Align every existing elevation row'):
    doc.Regenerate()
    rows = group_rows(viewports, window.tolerance_value)
    for row in rows:
        align_row(row, window.mode_value, window.gap_value)
    doc.Regenerate()

forms.alert('Aligned {} elevation viewport(s) across {} detected row(s).'.format(len(viewports), len(rows)), title='Align Elevation Rows')
