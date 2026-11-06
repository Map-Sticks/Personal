# -*- coding: utf-8 -*-
"""Number Viewports - WPF edition (single-file)

Auto-numbers viewports on the active sheet (or selected sheets in the
Project Browser) in reading order with a configurable WPF dialog.

The XAML is embedded in this file so no external .xaml file is required.
"""

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System.Xml")

from System.IO import StringReader
from System.Xml import XmlReader
from System.Windows.Markup import XamlReader
from System.Windows import Window

from pyrevit import revit, DB, forms, script

doc    = revit.doc
uidoc  = revit.uidoc
output = script.get_output()
logger = script.get_logger()


# ---------------------------------------------------------------------------
# Embedded XAML
# ---------------------------------------------------------------------------
XAML = u"""
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="Number Viewports"
    Height="560" Width="480"
    WindowStartupLocation="CenterScreen"
    ResizeMode="NoResize"
    ShowInTaskbar="False">

    <Window.Resources>
        <Style TargetType="TextBlock">
            <Setter Property="Margin" Value="0,4,6,2"/>
            <Setter Property="VerticalAlignment" Value="Center"/>
        </Style>
        <Style TargetType="TextBox">
            <Setter Property="Height" Value="24"/>
            <Setter Property="Padding" Value="4,2,4,2"/>
            <Setter Property="VerticalContentAlignment" Value="Center"/>
        </Style>
        <Style TargetType="ComboBox">
            <Setter Property="Height" Value="24"/>
        </Style>
        <Style TargetType="Button">
            <Setter Property="Height" Value="30"/>
            <Setter Property="MinWidth" Value="90"/>
            <Setter Property="Margin" Value="6,0,0,0"/>
        </Style>
    </Window.Resources>

    <Grid Margin="14">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>

        <GroupBox Grid.Row="0" Header="Scope" Padding="10">
            <StackPanel>
                <RadioButton x:Name="rbActive"   Content="Active sheet only" IsChecked="True" Margin="0,2,0,2"/>
                <RadioButton x:Name="rbSelected" Content="Sheets selected in Project Browser" Margin="0,2,0,2"/>
            </StackPanel>
        </GroupBox>

        <GroupBox Grid.Row="1" Header="Numbering" Padding="10" Margin="0,8,0,0">
            <Grid>
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="140"/>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="150"/>
                    <ColumnDefinition Width="*"/>
                </Grid.ColumnDefinitions>
                <Grid.RowDefinitions>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="Auto"/>
                </Grid.RowDefinitions>

                <TextBlock Grid.Row="0" Grid.Column="0" Text="Start number:"/>
                <TextBox  Grid.Row="0" Grid.Column="1" x:Name="tbStart" Text="1"/>

                <TextBlock Grid.Row="0" Grid.Column="2" Text="Row tolerance (mm):"/>
                <TextBox  Grid.Row="0" Grid.Column="3" x:Name="tbTol" Text="75"/>

                <TextBlock Grid.Row="1" Grid.Column="0" Text="Prefix:"/>
                <TextBox  Grid.Row="1" Grid.Column="1" x:Name="tbPrefix" Text=""/>

                <TextBlock Grid.Row="1" Grid.Column="2" Text="Suffix:"/>
                <TextBox  Grid.Row="1" Grid.Column="3" x:Name="tbSuffix" Text=""/>

                <TextBlock Grid.Row="2" Grid.Column="0" Text="Pad to digits:"/>
                <ComboBox Grid.Row="2" Grid.Column="1" x:Name="cbPad" SelectedIndex="0">
                    <ComboBoxItem Content="None"/>
                    <ComboBoxItem Content="2 (01, 02...)"/>
                    <ComboBoxItem Content="3 (001, 002...)"/>
                </ComboBox>
            </Grid>
        </GroupBox>

        <GroupBox Grid.Row="2" Header="Reading Direction" Padding="10" Margin="0,8,0,0">
            <Grid>
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="140"/>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="150"/>
                    <ColumnDefinition Width="*"/>
                </Grid.ColumnDefinitions>

                <TextBlock Grid.Column="0" Text="Vertical:"/>
                <ComboBox  Grid.Column="1" x:Name="cbVert" SelectedIndex="0">
                    <ComboBoxItem Content="Top -&gt; Bottom"/>
                    <ComboBoxItem Content="Bottom -&gt; Top"/>
                </ComboBox>

                <TextBlock Grid.Column="2" Text="Horizontal:"/>
                <ComboBox  Grid.Column="3" x:Name="cbHorz" SelectedIndex="0">
                    <ComboBoxItem Content="Left -&gt; Right"/>
                    <ComboBoxItem Content="Right -&gt; Left"/>
                </ComboBox>
            </Grid>
        </GroupBox>

        <GroupBox Grid.Row="3" Header="Preview" Padding="6" Margin="0,8,0,0">
            <ListView x:Name="lvPreview">
                <ListView.View>
                    <GridView>
                        <GridViewColumn Header="Sheet"     Width="130" DisplayMemberBinding="{Binding Sheet}"/>
                        <GridViewColumn Header="View Name" Width="180" DisplayMemberBinding="{Binding ViewName}"/>
                        <GridViewColumn Header="Current"   Width="60"  DisplayMemberBinding="{Binding Current}"/>
                        <GridViewColumn Header="New"       Width="60"  DisplayMemberBinding="{Binding NewNum}"/>
                    </GridView>
                </ListView.View>
            </ListView>
        </GroupBox>

        <StackPanel Grid.Row="4" Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,10,0,0">
            <Button x:Name="btnPreview" Content="Preview"/>
            <Button x:Name="btnApply"   Content="Apply" IsDefault="True"/>
            <Button x:Name="btnCancel"  Content="Cancel" IsCancel="True"/>
        </StackPanel>
    </Grid>
</Window>
"""


# ---------------------------------------------------------------------------
# Geometry / sorting helpers
# ---------------------------------------------------------------------------
MM_PER_FOOT = 304.8

def mm_to_ft(mm):
    return float(mm) / MM_PER_FOOT


def get_viewport_center(vp):
    box = vp.GetBoxOutline()
    mn, mx = box.MinimumPoint, box.MaximumPoint
    return DB.XYZ((mn.X + mx.X) / 2.0,
                  (mn.Y + mx.Y) / 2.0,
                  0.0)


def sort_viewports_reading_order(viewports, row_tol_ft,
                                 top_to_bottom=True,
                                 left_to_right=True):
    items = [(vp, get_viewport_center(vp)) for vp in viewports]
    items.sort(key=lambda t: t[1].Y, reverse=top_to_bottom)

    rows = []
    current_row = []
    row_anchor_y = None
    for vp, c in items:
        if row_anchor_y is None or abs(c.Y - row_anchor_y) <= row_tol_ft:
            current_row.append((vp, c))
            if row_anchor_y is None:
                row_anchor_y = c.Y
        else:
            rows.append(current_row)
            current_row = [(vp, c)]
            row_anchor_y = c.Y
    if current_row:
        rows.append(current_row)

    ordered = []
    for row in rows:
        row.sort(key=lambda t: t[1].X, reverse=not left_to_right)
        ordered.extend([vp for vp, _ in row])
    return ordered


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------
def get_selected_sheets():
    sheets = []
    for eid in uidoc.Selection.GetElementIds():
        el = doc.GetElement(eid)
        if isinstance(el, DB.ViewSheet):
            sheets.append(el)
    return sheets


def get_active_sheet():
    av = doc.ActiveView
    return [av] if isinstance(av, DB.ViewSheet) else []


# ---------------------------------------------------------------------------
# Number formatter
# ---------------------------------------------------------------------------
def format_number(n, prefix, suffix, pad_digits):
    core = str(n)
    if pad_digits and pad_digits > 0:
        core = core.zfill(pad_digits)
    return "{}{}{}".format(prefix or "", core, suffix or "")


# ---------------------------------------------------------------------------
# Plan + apply
# ---------------------------------------------------------------------------
def build_plan(sheets, start, row_tol_ft, top_to_bottom, left_to_right,
               prefix, suffix, pad_digits):
    plan = []
    counter = start
    for sht in sheets:
        vp_ids = sht.GetAllViewports()
        vps = [doc.GetElement(vid) for vid in vp_ids]
        if not vps:
            continue
        ordered = sort_viewports_reading_order(
            vps, row_tol_ft, top_to_bottom, left_to_right
        )
        for vp in ordered:
            cur_param = vp.get_Parameter(DB.BuiltInParameter.VIEWPORT_DETAIL_NUMBER)
            cur_val = cur_param.AsString() if cur_param else ""
            view = doc.GetElement(vp.ViewId)
            plan.append({
                "vp": vp,
                "sheet_str": "{} - {}".format(sht.SheetNumber, sht.Name),
                "view_name": view.Name if view else "?",
                "current": cur_val,
                "new": format_number(counter, prefix, suffix, pad_digits),
            })
            counter += 1
    return plan


def apply_plan(plan):
    # Pass 1: temp values to avoid clashes
    for i, row in enumerate(plan):
        p = row["vp"].get_Parameter(DB.BuiltInParameter.VIEWPORT_DETAIL_NUMBER)
        if p and not p.IsReadOnly:
            p.Set("~TMP_{}".format(i + 1))
    # Pass 2: final values
    for row in plan:
        p = row["vp"].get_Parameter(DB.BuiltInParameter.VIEWPORT_DETAIL_NUMBER)
        if p and not p.IsReadOnly:
            p.Set(row["new"])


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------
def load_window_from_string(xaml_string):
    """Load a WPF Window from an in-memory XAML string."""
    sr = StringReader(xaml_string)
    xr = XmlReader.Create(sr)
    return XamlReader.Load(xr)


class NumberViewportsUI(object):

    def __init__(self):
        self.window = load_window_from_string(XAML)

        # Grab named controls
        self.rbActive   = self.window.FindName("rbActive")
        self.rbSelected = self.window.FindName("rbSelected")
        self.tbStart    = self.window.FindName("tbStart")
        self.tbTol      = self.window.FindName("tbTol")
        self.tbPrefix   = self.window.FindName("tbPrefix")
        self.tbSuffix   = self.window.FindName("tbSuffix")
        self.cbPad      = self.window.FindName("cbPad")
        self.cbVert     = self.window.FindName("cbVert")
        self.cbHorz     = self.window.FindName("cbHorz")
        self.lvPreview  = self.window.FindName("lvPreview")
        self.btnPreview = self.window.FindName("btnPreview")
        self.btnApply   = self.window.FindName("btnApply")
        self.btnCancel  = self.window.FindName("btnCancel")

        self.btnPreview.Click += self.on_preview
        self.btnApply.Click   += self.on_apply
        self.btnCancel.Click  += self.on_cancel

        self._plan = []

        # Smart default: if user has sheets selected, switch mode
        if get_selected_sheets():
            self.rbSelected.IsChecked = True

    # ---- input helpers ----
    def _read_inputs(self):
        try:
            start = int(self.tbStart.Text.strip() or "1")
        except ValueError:
            raise ValueError("Start number must be an integer.")
        try:
            tol_mm = float(self.tbTol.Text.strip() or "75")
        except ValueError:
            raise ValueError("Row tolerance must be a number (mm).")

        pad_map = {0: 0, 1: 2, 2: 3}
        pad = pad_map.get(self.cbPad.SelectedIndex, 0)

        return {
            "start":          start,
            "row_tol_ft":     mm_to_ft(tol_mm),
            "top_to_bottom":  self.cbVert.SelectedIndex == 0,
            "left_to_right":  self.cbHorz.SelectedIndex == 0,
            "prefix":         self.tbPrefix.Text or "",
            "suffix":         self.tbSuffix.Text or "",
            "pad_digits":     pad,
        }

    def _get_sheets(self):
        if self.rbSelected.IsChecked:
            sheets = get_selected_sheets()
            if not sheets:
                forms.alert("No sheets are selected in the Project Browser.",
                            title="Number Viewports")
            return sheets
        sheets = get_active_sheet()
        if not sheets:
            forms.alert("The active view is not a sheet.",
                        title="Number Viewports")
        return sheets

    def _refresh_preview(self, plan):
        rows = []
        for r in plan:
            rows.append({
                "Sheet":    r["sheet_str"],
                "ViewName": r["view_name"],
                "Current":  r["current"],
                "NewNum":   r["new"],
            })
        self.lvPreview.ItemsSource = rows

    # ---- events ----
    def on_preview(self, sender, args):
        try:
            opts = self._read_inputs()
        except ValueError as ex:
            forms.alert(str(ex), title="Invalid input")
            return

        sheets = self._get_sheets()
        if not sheets:
            return

        self._plan = build_plan(sheets, **opts)
        if not self._plan:
            forms.alert("No viewports found on the selected sheet(s).",
                        title="Number Viewports")
            return

        self._refresh_preview(self._plan)

    def on_apply(self, sender, args):
        if not self._plan:
            self.on_preview(sender, args)
            if not self._plan:
                return

        with revit.Transaction("Auto-number Viewports"):
            apply_plan(self._plan)

        count = len(self._plan)
        sheets_touched = len({r["sheet_str"] for r in self._plan})
        output.print_md(
            "**Done.** Renumbered **{}** viewport(s) across **{}** sheet(s)."
            .format(count, sheets_touched)
        )
        self.window.Close()

    def on_cancel(self, sender, args):
        self.window.Close()

    def show(self):
        self.window.ShowDialog()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    NumberViewportsUI().show()