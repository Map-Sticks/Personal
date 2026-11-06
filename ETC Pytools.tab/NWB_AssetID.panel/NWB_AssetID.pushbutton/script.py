# -*- coding: utf-8 -*-
"""NWB Asset ID Generator (pyRevit, IronPython 2.7)

Generates NWB_AssetID = AssetTypeCode-Building-Level-#### with a per
(AssetTypeCode-Building-Level) sequential counter.

- Building: manual placeholder (default "M") OR copied from a param (e.g. NWB_WBS01).
- Level: copied from a param (default NWB_WBS02).
- Counter resets per AssetTypeCode-Building-Level group.
- Maintainability rule (schema): only Type 1 & 2 get a real sequential ID.
  When "Only Maint. Type 1 & 2" is ticked, every OTHER element (Type 3, blank,
  etc.) is written "N/A" instead of being skipped.
- Non-destructive by default ("Fill blanks only").
- INVISIBLE copy-guard via Extensible Storage: stores each element's own
  UniqueId. A copied element carries the SOURCE's UniqueId but has a new
  UniqueId of its own -> mismatch -> detected as a copy and re-sequenced.
- Preview table + CSV export BEFORE writing. Apply runs in one transaction.

Requires: NWB_AssetID as an instance TEXT parameter on the elements.
"""

__title__ = "NWB\nAsset ID"
__author__ = "ETC / Jacques-Michael Oosthuizen"

import re
import os
import datetime

import clr
# --- .NET assembly references (IronPython does NOT auto-load these) ---
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("System")
clr.AddReference("System.Data")
clr.AddReference("System.Xml")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from Autodesk.Revit.DB import (
    FilteredElementCollector, Transaction, ElementId, StorageType,
)
from Autodesk.Revit.DB.ExtensibleStorage import (
    Schema, SchemaBuilder, AccessLevel, Entity,
)

from System import Guid, String
from System.Data import DataTable
from System.IO import StringReader
from System.Xml import XmlReader
from System.Windows.Markup import XamlReader

from pyrevit import revit, forms, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

# ----------------------------------------------------------------------------
# Extensible Storage - the invisible "owner" guard
# ----------------------------------------------------------------------------
SCHEMA_GUID = Guid("9E5D2A14-3B7C-4F81-A6E2-1C0F7B9D4E33")
SCHEMA_NAME = "NWB_AssetIdOwner"
FIELD_OWNER = "OwnerUniqueId"
VENDOR_ID = "NWBHP"

_schema_cache = [None]


def get_schema():
    if _schema_cache[0] is not None:
        return _schema_cache[0]
    existing = Schema.Lookup(SCHEMA_GUID)
    if existing is not None:
        _schema_cache[0] = existing
        return existing
    sb = SchemaBuilder(SCHEMA_GUID)
    sb.SetSchemaName(SCHEMA_NAME)
    sb.SetReadAccessLevel(AccessLevel.Public)
    sb.SetWriteAccessLevel(AccessLevel.Vendor)
    sb.SetVendorId(VENDOR_ID)
    sb.AddSimpleField(FIELD_OWNER, String)
    schema = sb.Finish()
    _schema_cache[0] = schema
    return schema


def read_owner(elem):
    try:
        ent = elem.GetEntity(get_schema())
        if ent is None or not ent.IsValid():
            return None
        val = ent.Get[String](FIELD_OWNER)
        return val if val else None
    except Exception:
        return None


def write_owner(elem):
    try:
        ent = Entity(get_schema())
        ent.Set[String](FIELD_OWNER, elem.UniqueId)
        elem.SetEntity(ent)
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------------
# Parameter helpers
# ----------------------------------------------------------------------------
def num_to_text(v):
    if v is None:
        return u""
    if isinstance(v, bool):
        return u"Yes" if v else u"No"
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return repr(v)
    if isinstance(v, int):
        return str(v)
    return str(v).strip()


def get_param(elem, pname):
    try:
        return elem.LookupParameter(pname)
    except Exception:
        return None


def get_text(elem, pname):
    p = get_param(elem, pname)
    if p is None:
        return u""
    try:
        st = p.StorageType
        if st == StorageType.String:
            s = p.AsString()
            return num_to_text(s) if s else u""
        if st == StorageType.Integer:
            return num_to_text(p.AsInteger())
        if st == StorageType.Double:
            vs = p.AsValueString()
            return vs.strip() if vs else num_to_text(p.AsDouble())
        vs = p.AsValueString()
        return vs.strip() if vs else u""
    except Exception:
        return u""


def set_text(elem, pname, value):
    p = get_param(elem, pname)
    if p is None or p.IsReadOnly:
        return False
    try:
        p.Set(u"" if value is None else str(value))
        return True
    except Exception:
        return False


def list_text_params(sample_elems, limit=40):
    names = set()
    for e in sample_elems[:limit]:
        try:
            for p in e.Parameters:
                try:
                    if p.StorageType in (StorageType.String, StorageType.Integer):
                        d = p.Definition
                        if d is not None:
                            names.add(d.Name)
                except Exception:
                    continue
        except Exception:
            continue
    return sorted(names)


# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
KEY_PARAM = "NWB_AssetTypeCode"
ID_PARAM = "NWB_AssetID"
MAINT_PARAM = "NWB_MaintainabilityType"
NA_VALUE = "N/A"
DEFAULT_BUILDING = "M"
DEFAULT_LEVEL_PARAM = "NWB_WBS02"
DEFAULT_BUILDING_PARAM = "NWB_WBS01"


# ----------------------------------------------------------------------------
# Collection
# ----------------------------------------------------------------------------
def collect_elements(scope):
    if scope == "selection":
        ids = list(uidoc.Selection.GetElementIds())
        elems = [doc.GetElement(i) for i in ids]
    elif scope == "view":
        elems = list(FilteredElementCollector(doc, doc.ActiveView.Id)
                     .WhereElementIsNotElementType().ToElements())
    else:
        elems = list(FilteredElementCollector(doc)
                     .WhereElementIsNotElementType().ToElements())
    out = []
    for e in elems:
        if e is None:
            continue
        if get_text(e, KEY_PARAM):
            out.append(e)
    return out


# ----------------------------------------------------------------------------
# Core engine
# ----------------------------------------------------------------------------
class Row(object):
    __slots__ = ("elem", "eid", "code", "prefix", "existing", "owner",
                 "status", "new_id", "action")

    def __init__(self, elem):
        self.elem = elem
        self.eid = elem.Id.IntegerValue
        self.code = ""
        self.prefix = ""
        self.existing = ""
        self.owner = None
        self.status = ""
        self.new_id = ""
        self.action = ""


def compute_prefix(elem, opts):
    code = get_text(elem, KEY_PARAM)
    if opts["building_mode"] == "manual":
        building = opts["building_manual"]
    else:
        building = get_text(elem, opts["building_param"])
    if opts["level_mode"] == "param":
        level = get_text(elem, opts["level_param"])
    else:
        level = opts["level_manual"]
    return code, u"{0}-{1}-{2}".format(code, building, level)


SEQ_RE = re.compile(r"^(?P<prefix>.+)-(?P<seq>\d+)$")


def parse_existing(existing_id):
    if not existing_id:
        return None, None
    m = SEQ_RE.match(existing_id.strip())
    if not m:
        return None, None
    try:
        return m.group("prefix"), int(m.group("seq"))
    except Exception:
        return None, None


def is_na(txt):
    return txt is not None and txt.strip().upper() in ("N/A", "NA")


def build_plan(elems, opts):
    """Classify every element, then assign IDs. Non-1&2 elements (when the
    maint filter is on) are marked to receive 'N/A'. Nothing is written here."""
    maint_only = opts["maint_only"]
    mode = opts["write_mode"]      # blanks / all / dupes
    start = opts["seq_start"]
    pad = opts["seq_pad"]

    rows = []
    seq_rows = []   # elements that get real sequential IDs

    for e in elems:
        r = Row(e)
        r.code, r.prefix = compute_prefix(e, opts)
        r.existing = get_text(e, ID_PARAM)
        r.owner = read_owner(e)
        own_uid = e.UniqueId

        # --- N/A branch: not a Type 1 or 2 asset ---
        if maint_only:
            mt = get_text(e, MAINT_PARAM)
            if mt not in ("1", "2"):
                r.status = "NA"
                if mode == "dupes":
                    r.action = "Skip (N/A pass off)"
                elif mode == "blanks" and r.existing and not is_na(r.existing):
                    # a real existing value in blanks mode -> leave it
                    r.action = "Keep"
                    r.new_id = r.existing
                elif is_na(r.existing):
                    r.action = "Keep"
                    r.new_id = NA_VALUE
                else:
                    r.new_id = NA_VALUE
                    r.action = "Write N/A"
                rows.append(r)
                continue

        # --- normal classification for Type 1 & 2 (or filter off) ---
        ex_prefix, ex_seq = parse_existing(r.existing)
        if not r.existing or is_na(r.existing):
            r.status = "NEW"
        elif r.owner and r.owner != own_uid:
            r.status = "COPY"
        elif ex_prefix is None:
            r.status = "INVALID"
        elif ex_prefix != r.prefix:
            r.status = "PREFIX_CHANGED"
        else:
            r.status = "OWNED"
        rows.append(r)
        seq_rows.append(r)

    # group the sequential rows by prefix
    groups = {}
    for r in seq_rows:
        groups.setdefault(r.prefix, []).append(r)

    for prefix, grp in groups.items():
        grp_sorted = sorted(grp, key=lambda x: x.eid)

        if mode == "all":
            n = start
            for r in grp_sorted:
                r.new_id = u"{0}-{1}".format(prefix, str(n).zfill(pad))
                r.action = "Re-sequence (all)"
                n += 1
            continue

        reserved = set()
        owned_by_seq = {}
        for r in grp_sorted:
            if r.status == "OWNED":
                _, seq = parse_existing(r.existing)
                if seq in owned_by_seq:
                    r.status = "DUP"
                else:
                    owned_by_seq[seq] = r
                    reserved.add(seq)
                    r.action = "Keep"
                    r.new_id = r.existing

        next_seq = (max(reserved) + 1) if reserved else start

        def take_next(_n):
            while _n in reserved:
                _n += 1
            reserved.add(_n)
            return _n

        for r in grp_sorted:
            if r.status == "OWNED":
                continue
            if mode == "dupes" and r.status == "NEW":
                r.action = "Skip (blank)"
                continue
            s = take_next(next_seq)
            next_seq = s + 1
            r.new_id = u"{0}-{1}".format(prefix, str(s).zfill(pad))
            r.action = {
                "NEW": "Assign",
                "COPY": "Copy detected -> re-sequence",
                "DUP": "Duplicate -> re-sequence",
                "PREFIX_CHANGED": "Level/Bldg changed -> re-sequence",
                "INVALID": "Invalid id -> re-sequence",
            }.get(r.status, "Assign")

    return rows


def apply_plan(rows):
    written = 0
    stamped = 0
    na_written = 0
    failed = []
    t = Transaction(doc, "NWB Asset ID")
    t.Start()
    try:
        for r in rows:
            if r.action in ("Keep", "") or r.action.startswith("Skip"):
                if r.action == "Keep" and r.status != "NA" and not r.owner:
                    if write_owner(r.elem):
                        stamped += 1
                continue
            if not r.new_id:
                continue
            if set_text(r.elem, ID_PARAM, r.new_id):
                if r.new_id == NA_VALUE:
                    na_written += 1
                    # N/A elements are not unique assets -> no owner guard
                else:
                    written += 1
                    if write_owner(r.elem):
                        stamped += 1
            else:
                failed.append(r.eid)
        t.Commit()
    except Exception as ex:
        t.RollBack()
        raise ex
    return written, na_written, stamped, failed


# ----------------------------------------------------------------------------
# Preview DataTable + CSV
# ----------------------------------------------------------------------------
def rows_to_datatable(rows):
    dt = DataTable("preview")
    for col in ("ElementId", "Category", "Prefix", "Existing", "Action", "New ID"):
        dt.Columns.Add(col)
    for r in sorted(rows, key=lambda x: (x.prefix, x.eid)):
        try:
            cat = r.elem.Category.Name if r.elem.Category else ""
        except Exception:
            cat = ""
        dt.Rows.Add(str(r.eid), cat, r.prefix, r.existing, r.action, r.new_id)
    return dt


def export_csv(rows):
    fname = "NWB_AssetID_preview_{0}.csv".format(
        datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    path = os.path.join(os.path.expanduser("~"), "Desktop", fname)
    try:
        f = open(path, "w")
        try:
            f.write("ElementId,Category,Prefix,Existing,Action,NewID\n")
            for r in sorted(rows, key=lambda x: (x.prefix, x.eid)):
                try:
                    cat = r.elem.Category.Name if r.elem.Category else ""
                except Exception:
                    cat = ""
                f.write(u",".join([
                    str(r.eid), u'"%s"' % cat, r.prefix, r.existing,
                    u'"%s"' % r.action, r.new_id]) + "\n")
        finally:
            f.close()
        return path
    except Exception:
        return None


# ----------------------------------------------------------------------------
# WPF form (loaded via .NET XamlReader - no 'wpf' module dependency)
# ----------------------------------------------------------------------------
XAML = u"""
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="NWB Asset ID Generator" Height="640" Width="720"
        WindowStartupLocation="CenterScreen">
  <Grid Margin="12">
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>

    <StackPanel Grid.Row="0" Orientation="Horizontal">
      <GroupBox Header="Scope" Width="210" Margin="0,0,8,0">
        <StackPanel Margin="6">
          <RadioButton x:Name="scSel" Content="Selected elements" IsChecked="True"/>
          <RadioButton x:Name="scView" Content="Active view"/>
          <RadioButton x:Name="scModel" Content="Whole model"/>
          <CheckBox x:Name="chkMaint" Content="Only Maint. Type 1 &amp; 2 (rest = N/A)" IsChecked="True" Margin="0,8,0,0"/>
        </StackPanel>
      </GroupBox>

      <GroupBox Header="Building segment" Width="230" Margin="0,0,8,0">
        <StackPanel Margin="6">
          <RadioButton x:Name="bMan" Content="Manual value" IsChecked="True"/>
          <TextBox x:Name="bManVal" Text="M" Margin="18,2,0,6"/>
          <RadioButton x:Name="bPar" Content="Copy from parameter"/>
          <ComboBox x:Name="bParVal" Margin="18,2,0,0"/>
        </StackPanel>
      </GroupBox>

      <GroupBox Header="Level segment" Width="230">
        <StackPanel Margin="6">
          <RadioButton x:Name="lPar" Content="Copy from parameter" IsChecked="True"/>
          <ComboBox x:Name="lParVal" Margin="18,2,0,6"/>
          <RadioButton x:Name="lMan" Content="Manual value"/>
          <TextBox x:Name="lManVal" Text="" Margin="18,2,0,0"/>
        </StackPanel>
      </GroupBox>
    </StackPanel>

    <StackPanel Grid.Row="1" Orientation="Horizontal" Margin="0,8,0,8">
      <GroupBox Header="Sequence" Width="300" Margin="0,0,8,0">
        <StackPanel Orientation="Horizontal" Margin="6">
          <TextBlock Text="Padding:" VerticalAlignment="Center"/>
          <TextBox x:Name="seqPad" Text="4" Width="40" Margin="4,0,16,0"/>
          <TextBlock Text="Start at:" VerticalAlignment="Center"/>
          <TextBox x:Name="seqStart" Text="1" Width="40" Margin="4,0,0,0"/>
        </StackPanel>
      </GroupBox>
      <GroupBox Header="Write mode" Width="380">
        <StackPanel Orientation="Horizontal" Margin="6">
          <RadioButton x:Name="wmBlanks" Content="Fill blanks only" IsChecked="True" Margin="0,0,10,0"/>
          <RadioButton x:Name="wmDupes" Content="Fix dupes only" Margin="0,0,10,0"/>
          <RadioButton x:Name="wmAll" Content="Re-sequence ALL"/>
        </StackPanel>
      </GroupBox>
    </StackPanel>

    <DataGrid Grid.Row="2" x:Name="grid" AutoGenerateColumns="True"
              IsReadOnly="True" HeadersVisibility="Column"
              CanUserAddRows="False"/>

    <StackPanel Grid.Row="3" Orientation="Horizontal" HorizontalAlignment="Right"
                Margin="0,10,0,0">
      <TextBlock x:Name="summary" VerticalAlignment="Center"
                 Margin="0,0,16,0" Foreground="#444"/>
      <Button x:Name="btnPreview" Content="Preview" Width="90" Margin="0,0,8,0"/>
      <Button x:Name="btnCsv" Content="Export CSV" Width="90" Margin="0,0,8,0"/>
      <Button x:Name="btnApply" Content="Apply" Width="90" Margin="0,0,8,0"
              IsEnabled="False"/>
      <Button x:Name="btnCancel" Content="Cancel" Width="90"/>
    </StackPanel>
  </Grid>
</Window>
"""


class AssetIdWindow(object):
    def __init__(self):
        self.window = XamlReader.Load(XmlReader.Create(StringReader(XAML)))
        self._rows = None
        self._elems = collect_elements("selection")

        n = self.window.FindName
        self.scSel = n("scSel"); self.scView = n("scView"); self.scModel = n("scModel")
        self.chkMaint = n("chkMaint")
        self.bMan = n("bMan"); self.bManVal = n("bManVal")
        self.bPar = n("bPar"); self.bParVal = n("bParVal")
        self.lPar = n("lPar"); self.lParVal = n("lParVal")
        self.lMan = n("lMan"); self.lManVal = n("lManVal")
        self.seqPad = n("seqPad"); self.seqStart = n("seqStart")
        self.wmBlanks = n("wmBlanks"); self.wmDupes = n("wmDupes"); self.wmAll = n("wmAll")
        self.grid = n("grid"); self.summary = n("summary")
        self.btnPreview = n("btnPreview"); self.btnCsv = n("btnCsv")
        self.btnApply = n("btnApply"); self.btnCancel = n("btnCancel")

        params = list_text_params(self._elems) if self._elems else []
        if not params:
            params = [DEFAULT_BUILDING_PARAM, DEFAULT_LEVEL_PARAM]
        for p in params:
            self.bParVal.Items.Add(p)
            self.lParVal.Items.Add(p)
        self._select_combo(self.bParVal, DEFAULT_BUILDING_PARAM)
        self._select_combo(self.lParVal, DEFAULT_LEVEL_PARAM)

        self.btnPreview.Click += self.on_preview
        self.btnApply.Click += self.on_apply
        self.btnCsv.Click += self.on_csv
        self.btnCancel.Click += self.on_cancel

    def ShowDialog(self):
        return self.window.ShowDialog()

    def Close(self):
        self.window.Close()

    def _select_combo(self, combo, value):
        for i in range(combo.Items.Count):
            if str(combo.Items[i]) == value:
                combo.SelectedIndex = i
                return
        if combo.Items.Count:
            combo.SelectedIndex = 0

    def read_opts(self):
        scope = ("selection" if self.scSel.IsChecked
                 else "view" if self.scView.IsChecked else "model")
        write_mode = ("all" if self.wmAll.IsChecked
                      else "dupes" if self.wmDupes.IsChecked else "blanks")
        try:
            pad = max(1, int(self.seqPad.Text))
        except Exception:
            pad = 4
        try:
            start = max(0, int(self.seqStart.Text))
        except Exception:
            start = 1
        return {
            "scope": scope,
            "maint_only": bool(self.chkMaint.IsChecked),
            "building_mode": "manual" if self.bMan.IsChecked else "param",
            "building_manual": (self.bManVal.Text or "").strip(),
            "building_param": str(self.bParVal.SelectedItem or DEFAULT_BUILDING_PARAM),
            "level_mode": "param" if self.lPar.IsChecked else "manual",
            "level_param": str(self.lParVal.SelectedItem or DEFAULT_LEVEL_PARAM),
            "level_manual": (self.lManVal.Text or "").strip(),
            "seq_pad": pad,
            "seq_start": start,
            "write_mode": write_mode,
        }

    def on_preview(self, sender, args):
        opts = self.read_opts()
        elems = collect_elements(opts["scope"])
        if not elems:
            forms.alert("No candidate elements found (need %s populated)."
                        % KEY_PARAM, title="Nothing to do")
            return
        self._rows = build_plan(elems, opts)
        self.grid.ItemsSource = rows_to_datatable(self._rows).DefaultView

        n_assign = sum(1 for r in self._rows if r.action == "Assign")
        n_na = sum(1 for r in self._rows if r.action == "Write N/A")
        n_keep = sum(1 for r in self._rows if r.action == "Keep")
        n_reseq = sum(1 for r in self._rows if "re-sequence" in r.action.lower())
        n_skip = sum(1 for r in self._rows if r.action.startswith("Skip"))
        self.summary.Text = ("Assign %d | N/A %d | Keep %d | Re-seq %d | Skip %d"
                             % (n_assign, n_na, n_keep, n_reseq, n_skip))
        self.btnApply.IsEnabled = True

    def on_csv(self, sender, args):
        if not self._rows:
            self.on_preview(sender, args)
        if not self._rows:
            return
        path = export_csv(self._rows)
        forms.alert("Saved:\n%s" % path if path else "CSV export failed.",
                    title="Export")

    def on_apply(self, sender, args):
        if not self._rows:
            forms.alert("Run Preview first.", title="Apply")
            return
        written, na_written, stamped, failed = apply_plan(self._rows)
        msg = ("Wrote %d sequential IDs.\nWrote 'N/A' to %d elements.\n"
               "Stamped %d copy-guards." % (written, na_written, stamped))
        if failed:
            msg += "\nFailed on %d elements (read-only param?)." % len(failed)
        forms.alert(msg, title="Done")
        self.Close()

    def on_cancel(self, sender, args):
        self.Close()


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    if get_schema() is None:
        forms.alert("Could not initialise Extensible Storage schema.",
                    title="Error")
    else:
        AssetIdWindow().ShowDialog()
