# -*- coding: utf-8 -*-
"""Find Linked Fixtures - Stage 2.2
Wall / Ceiling / Ceiling(Face) modes.
Face mode hosts on reference planes in host model.
"""
__persistentengine__ = True

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

import os, json, math, traceback, System
from System.Collections.Generic import List
from System.Windows import Thickness, Window, WindowStartupLocation, Visibility
from System.Windows.Controls import (
    StackPanel, TextBlock, CheckBox, ComboBox,
    ComboBoxItem, TextBox, Button, Orientation,
    ScrollViewer)
from System.Windows.Media import Brushes

from pyrevit import revit, forms, script
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, FamilySymbol, Transaction,
    RevitLinkInstance, XYZ, BuiltInParameter, LocationPoint, ElementId,
    SpatialElementBoundaryOptions, Line, ElementTransformUtils,
    ReferencePlane)
from Autodesk.Revit.DB.Structure import StructuralType

doc    = revit.doc
uidoc  = revit.uidoc
output = script.get_output()
FT     = 1.0 / 304.8
SETTINGS_FILE = os.path.join(os.path.dirname(__file__),
    "find_linked_fixtures_settings.json")

CATS = [
    ("catElectricalFixtures",   BuiltInCategory.OST_ElectricalFixtures,   True),
    ("catDataDevices",          BuiltInCategory.OST_DataDevices,          True),
    ("catCommunicationDevices", BuiltInCategory.OST_CommunicationDevices, True),
    ("catLightingFixtures",     BuiltInCategory.OST_LightingFixtures,     False),
    ("catLightingDevices",      BuiltInCategory.OST_LightingDevices,      False),
    ("catFireAlarmDevices",     BuiltInCategory.OST_FireAlarmDevices,     False),
    ("catSecurityDevices",      BuiltInCategory.OST_SecurityDevices,      False),
    ("catElectricalEquipment",  BuiltInCategory.OST_ElectricalEquipment,  False),
    ("catGenericModel",         BuiltInCategory.OST_GenericModel,         False),
    ("catSpecialityEquipment",  BuiltInCategory.OST_SpecialityEquipment,  False),
]
R_NOLOC="Rejected - no LocationPoint"; R_LEVEL="Rejected - wrong level"
R_SPACE="Rejected - outside space/tolerance"; R_NAME="Rejected - name filter"
R_DUP="Rejected - duplicate"; ACC="Accepted"

MODE_WALL  = "Wall"
MODE_CEIL  = "Ceiling"
MODE_CFACE = "Ceiling (Face)"
ALL_MODES  = [MODE_WALL, MODE_CEIL, MODE_CFACE]
RP_AUTO    = "Auto-match"

def mm2ft(mm): return float(mm)*FT
def ft2mm(ft): return float(ft)*304.8
def pstr(e,b):
    try:
        p=e.get_Parameter(b)
        if p and p.HasValue: return p.AsString()
    except Exception: pass
    return None
def ename(e):
    if e is None: return "<None>"
    try:
        n=getattr(e,"Name",None)
        if n: return n
    except Exception: pass
    return pstr(e,BuiltInParameter.ALL_MODEL_TYPE_NAME) or str(e.Id.IntegerValue)
def tname(s): return pstr(s,BuiltInParameter.SYMBOL_NAME_PARAM) or ename(s)
def sdisplay(s): return u"{} :: {}".format(ename(s.Family),tname(s))
def pfloat(t,lo=None,hi=None):
    try: v=float(str(t).strip())
    except Exception: raise ValueError("'{}' not a number.".format(t))
    if lo is not None and v<lo: raise ValueError("{}<min {}.".format(v,lo))
    if hi is not None and v>hi: raise ValueError("{}>max {}.".format(v,hi))
    return v
def pkw(t): return [x.strip().upper() for x in str(t or "").split(',') if x.strip()]
def has_any(t,ws):
    if not ws: return True
    u=(t or"").upper(); return any(w in u for w in ws)
def has_none(t,ws):
    if not ws: return True
    u=(t or"").upper(); return all(w not in u for w in ws)
def stcol(tb,c):
    try: tb.Foreground=getattr(Brushes,c)
    except Exception: pass
def tag_inst(inst,note):
    for b in [BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS,BuiltInParameter.ALL_MODEL_MARK]:
        try:
            p=inst.get_Parameter(b)
            if p and not p.IsReadOnly: p.Set(note); return True
        except Exception: pass
    return False
def sel_show(ids):
    if ids is None or ids.Count==0: return False,"Nothing to select."
    try: uidoc.Selection.SetElementIds(ids)
    except Exception as ex: return False,"Selection failed: {}".format(ex)
    try: uidoc.ShowElements(ids); return True,"Selected + zoomed."
    except Exception: return True,"Selected. Zoom manually."

def space_center(sp):
    try:
        loc=sp.Location
        if isinstance(loc,LocationPoint): return loc.Point
    except Exception: pass
    bb=sp.get_BoundingBox(None)
    if bb: return(bb.Min+bb.Max)*0.5
    return XYZ(0,0,0)
def space_polys(sp):
    polys=[]
    try: loops=sp.GetBoundarySegments(SpatialElementBoundaryOptions())
    except Exception: return polys
    if not loops: return polys
    for loop in loops:
        pts=[]
        for seg in loop:
            try:
                c=seg.GetCurve()
                for i in range(8):
                    p=c.Evaluate(float(i)/8.0,True); pts.append((p.X,p.Y))
            except Exception: pass
        if len(pts)>=3: polys.append(pts)
    return polys
def pip(px,py,poly):
    inside=False;n=len(poly)
    if n<3: return False
    j=n-1
    for i in range(n):
        xi,yi=poly[i];xj,yj=poly[j]
        if(yi>py)!=(yj>py):
            d=yj-yi
            if abs(d)>1e-12:
                xc=(xj-xi)*(py-yi)/d+xi
                if px<xc: inside=not inside
        j=i
    return inside
def inside_polys(pt,polys): return any(pip(pt.X,pt.Y,p) for p in polys)
def dseg(px,py,x1,y1,x2,y2):
    dx=x2-x1;dy=y2-y1;ls=dx*dx+dy*dy
    if ls<1e-12: return math.sqrt((px-x1)**2+(py-y1)**2)
    t=max(0.0,min(1.0,((px-x1)*dx+(py-y1)*dy)/ls))
    return math.sqrt((px-x1-t*dx)**2+(py-y1-t*dy)**2)
def min_dist_polys(pt,polys):
    best=1e99
    for poly in polys:
        n=len(poly)
        for i in range(n):
            x1,y1=poly[i];x2,y2=poly[(i+1)%n]
            d=dseg(pt.X,pt.Y,x1,y1,x2,y2)
            if d<best: best=d
    return best
def get_bsegs(sp):
    try: return sp.GetBoundarySegments(SpatialElementBoundaryOptions())
    except Exception: return None

import re as _re
def _extract_trailing_number(name):
    m=_re.search(r'(\d+)\s*$', name or "")
    return m.group(1) if m else None
def _extract_level_number(level_name):
    m=_re.search(r'(\d+)', level_name or "")
    return m.group(1) if m else None

class SI(object):
    def __init__(s,sp):
        s.space=sp;s.name=ename(sp);s.num=getattr(sp,"Number","") or ""
        s.disp=u"{} - {}".format(s.num,s.name) if s.num else s.name
class FR(object):
    def __init__(s,li,le,pt,st,rsn,lvid):
        s.li_id=li.Id.IntegerValue;s.le_id=le.Id.IntegerValue if le else -1
        s.cat=""
        try: s.cat=le.Category.Name
        except Exception: pass
        s.fam="";s.typ=""
        try:
            t=le.Document.GetElement(le.GetTypeId())
            s.fam=ename(t.Family) if hasattr(t,"Family") else ename(t)
            s.typ=tname(t)
        except Exception: s.fam=ename(le);s.typ=""
        s.dname=u"{} :: {}".format(s.fam,s.typ)
        s.pt=pt;s.status=st;s.reason=rsn;s.lvid=lvid
        s.sym_id=None;s.h_mm=300.0;s.mode=MODE_WALL;s.rp_name=RP_AUTO
        s.linked_h_mm=0.0
        s.chk=(st==ACC);s.placed=None;s.err=None;s.ctrls={}
class RuleData(object):
    def __init__(s,kw="",sym_id=None,h=300.0,mode=MODE_WALL,rp=RP_AUTO):
        s.kw_text=kw;s.sym_id=sym_id;s.h_mm=h;s.mode=mode;s.rp_name=rp;s.ctrls={}

def defs():
    cs={c:d for c,_,d in CATS}
    return {"scan_tol":300.0,"wall_dist":600.0,"def_h":300.0,
            "av_only":True,"ign_dup":True,"verbose":False,
            "inc_kw":"","exc_kw":"","lvl_mode":"Same Level Name",
            "cats":cs,"maps":{},"last_link":"",
            "auto_sel":True,"tag":True,"rules":[],"visible_families":[]
            }
def load_s():
    d=defs()
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE,'r') as f: ld=json.load(f)
            if isinstance(ld,dict):
                d.update(ld)
                if not isinstance(d.get("cats"),dict): d["cats"]=defs()["cats"]
                if not isinstance(d.get("rules"),list): d["rules"]=[]
    except Exception: pass
    return d
def save_s(d):
    try:
        with open(SETTINGS_FILE,'w') as f: json.dump(d,f,indent=2,sort_keys=True)
        return True,None
    except Exception as ex: return False,str(ex)

class Req(object):
    def __init__(s): s.fn=None;s.pl=None
REQ=Req()
class Hnd(IExternalEventHandler):
    def Execute(s,uiapp):
        try:
            if REQ.fn: REQ.fn(REQ.pl)
        except Exception:
            try: forms.alert(traceback.format_exc(),title="Error")
            except Exception: pass
        finally: REQ.fn=None;REQ.pl=None
    def GetName(s): return "FLF22"
EE=ExternalEvent.Create(Hnd())
def rev(fn,pl=None): REQ.fn=fn;REQ.pl=pl;EE.Raise()

class Win(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self,script.get_bundle_file("ui.xaml"))
        self.S=load_s()
        self._sps=[];self._smap={};self._csi=None
        self._lbd={};self._sbd={}
        self._rows=[];self._lpi=List[ElementId]()
        self._rules=[]
        self._ref_planes=[]
        self._all_symbols = []
        self._visible_families = set()
        self._boundary_warned=False
        self._apply_s();self._wire()
        rev(lambda u:u._init_load(),self)

    def _wire(self):
        self.btnClose.Click+=lambda s,a:self.Close()
        self.btnReloadSpaces.Click+=lambda s,a:rev(lambda u:u._load_sp(),self)
        self.btnSaveSettings.Click+=self._on_save
        self.btnResetSettings.Click+=self._on_reset
        self.cbActiveView.Checked+=lambda s,a:rev(lambda u:u._load_sp(),self)
        self.cbActiveView.Unchecked+=lambda s,a:rev(lambda u:u._load_sp(),self)
        self.tbSpaceSearch.TextChanged+=lambda s,a:self._rebuild()
        self.lbSpaces.SelectionChanged+=self._on_sp
        self.btnScan.Click+=lambda s,a:rev(lambda u:u._scan(),self)
        self.btnClearResults.Click+=lambda s,a:self._clear()
        self.btnCheckAll.Click+=lambda s,a:self._chk_all(True)
        self.btnUncheckAll.Click+=lambda s,a:self._chk_all(False)
        self.btnPlaceChecked.Click+=lambda s,a:rev(lambda u:u._place(),self)
        self.btnUndoLast.Click+=lambda s,a:rev(lambda u:u._undo(),self)
        self.btnSelectLast.Click+=lambda s,a:rev(lambda u:u._sel_last(),self)
        self.btnAddRule.Click+=lambda s,a:self._add_rule()
        self.btnApplyRules.Click+=lambda s,a:self._apply_rules_to_rows()
        def _on_filter_click(s, a):
            try:
                self._show_family_filter_dialog()
            except Exception as ex:
                forms.alert("Filter dialog error:\n\n{}".format(traceback.format_exc()), title="Error")
        self.btnFilterFamilies.Click += _on_filter_click
        self.Closed+=lambda s,a:self._on_save(s,a,q=True)

    def _ct(self,cmb):
        try:
            i=cmb.SelectedItem
            if i is None: return ""
            return str(getattr(i,"Content",i))
        except Exception: return ""
    def _st(self,t): self.txtStatus.Text=t

    def _apply_s(self):
        s=self.S
        self.tbScanTolerance.Text=str(s.get("scan_tol",300.0))
        self.tbWallSearchDistance.Text=str(s.get("wall_dist",600.0))
        self.tbDefaultHeight.Text=str(s.get("def_h",300.0))
        self.tbIncludeKeywords.Text=s.get("inc_kw","")
        self.tbExcludeKeywords.Text=s.get("exc_kw","")
        self.cbActiveView.IsChecked=bool(s.get("av_only",True))
        self.cbIgnoreDuplicates.IsChecked=bool(s.get("ign_dup",True))
        self.cbVerboseDiag.IsChecked=bool(s.get("verbose",False))
        self.cbAutoSelectZoom.IsChecked=bool(s.get("auto_sel",True))
        self.cbTagPlaced.IsChecked=bool(s.get("tag",True))
        for c,_,d in CATS:
            try: getattr(self,c).IsChecked=bool(s.get("cats",{}).get(c,d))
            except Exception: pass

    def _coll_s(self):
        d=defs()
        try:
            d["scan_tol"]=float(self.tbScanTolerance.Text)
            d["wall_dist"]=float(self.tbWallSearchDistance.Text)
            d["def_h"]=float(self.tbDefaultHeight.Text)
        except Exception: pass
        d["av_only"]=bool(self.cbActiveView.IsChecked)
        d["ign_dup"]=bool(self.cbIgnoreDuplicates.IsChecked)
        d["verbose"]=bool(self.cbVerboseDiag.IsChecked)
        d["auto_sel"]=bool(self.cbAutoSelectZoom.IsChecked)
        d["tag"]=bool(self.cbTagPlaced.IsChecked)
        d["inc_kw"]=self.tbIncludeKeywords.Text or ""
        d["exc_kw"]=self.tbExcludeKeywords.Text or ""
        d["lvl_mode"]=self._ct(self.cmbLevelMode) or "Same Level Name"
        d["cats"]={}
        for c,_,df in CATS: d["cats"][c]=bool(getattr(self,c).IsChecked)
        d["maps"]=self.S.get("maps",{})
        d["last_link"]=self._ct(self.cmbLinkInstance) or ""
        d["rules"]=self._collect_rules_data()
        d["visible_families"] = list(self._visible_families) if self._visible_families else []
        self.S=d;return d

    def _on_save(self,s=None,a=None,q=False):
        ok,err=save_s(self._coll_s())
        if not q: self._st("Settings saved." if ok else "Save failed.")
    def _on_reset(self,s,a):
        self.S=defs();self._apply_s()
        self._rules=[];self.spRules.Children.Clear()
        self._st("Settings reset.")

    # ---- ref planes ----
    def _load_ref_planes(self):
        self._ref_planes=[]
        for rp in FilteredElementCollector(doc).OfClass(ReferencePlane):
            n=ename(rp)
            if n and n!="<None>":
                self._ref_planes.append((n,rp))
        self._ref_planes.sort(key=lambda x:x[0])

    def _rp_combo_items(self):
        return [RP_AUTO]+[n for n,_ in self._ref_planes]

    def _find_rp_by_name(self,name):
        for n,rp in self._ref_planes:
            if n==name: return rp
        return None

    def _auto_match_ref_plane(self,height_mm,level_name):
        h_str=str(int(round(height_mm)))
        lvl_num=_extract_level_number(level_name)
        if lvl_num:
            for n,rp in self._ref_planes:
                if lvl_num in n and h_str in n:
                    return n,rp
        for n,rp in self._ref_planes:
            if h_str in n:
                return n,rp
        return None,None

    # ---- rules ----
    def _add_rule(self,kw="",sym_id=None,h=300.0,mode=MODE_WALL,rp=RP_AUTO):
        rd=RuleData(kw,sym_id,h,mode,rp)
        p=StackPanel(Orientation=Orientation.Horizontal,Margin=Thickness(2))
        tb_kw=TextBox(Text=kw,Width=300,Margin=Thickness(0,0,4,0),
            ToolTip="Comma-separated keywords. ANY match triggers this rule.")
        p.Children.Add(tb_kw)
        cmb=ComboBox(Width=320,Margin=Thickness(0,0,4,0))
        cmb.Items.Add("<not set>")
        for d in sorted(self._sbd): cmb.Items.Add(d)
        if sym_id:
            for d,sym in self._sbd.items():
                if sym.Id.IntegerValue==sym_id: cmb.SelectedItem=d;break
        if cmb.SelectedItem is None: cmb.SelectedIndex=0
        p.Children.Add(cmb)
        tb_h=TextBox(Text=str(h),Width=70,Margin=Thickness(0,0,4,0))
        p.Children.Add(tb_h)
        cmb_mode=ComboBox(Width=110,Margin=Thickness(0,0,4,0))
        for m in ALL_MODES: cmb_mode.Items.Add(m)
        cmb_mode.SelectedItem=mode if mode in ALL_MODES else MODE_WALL
        p.Children.Add(cmb_mode)
        cmb_rp=ComboBox(Width=180,Margin=Thickness(0,0,4,0))
        for item in self._rp_combo_items(): cmb_rp.Items.Add(item)
        cmb_rp.SelectedItem=rp if rp in self._rp_combo_items() else RP_AUTO
        p.Children.Add(cmb_rp)
        bx=Button(Content="X",Width=30)
        bx.Click+=lambda s,a,r=rd,pn=p:self._remove_rule(r,pn)
        p.Children.Add(bx)
        rd.ctrls={"kw":tb_kw,"cmb":cmb,"h":tb_h,"mode":cmb_mode,"rp":cmb_rp,"panel":p}
        self._rules.append(rd);self.spRules.Children.Add(p)
    def _show_family_filter_dialog(self):
        fam_counts = {}
        for s in self._all_symbols:
            fn = ename(s.Family)
            fam_counts[fn] = fam_counts.get(fn, 0) + 1
        fam_names = sorted(fam_counts.keys())

        dlg = Window()
        dlg.Title = "Filter Families"
        dlg.Width = 450
        dlg.Height = 550
        dlg.WindowStartupLocation = WindowStartupLocation.CenterScreen

        root = StackPanel(Margin=Thickness(10))

        tb_search = TextBox(Margin=Thickness(0, 0, 0, 8))
        tb_search.ToolTip = "Type to filter family names"
        root.Children.Add(tb_search)

        sv = ScrollViewer(Margin=Thickness(0, 0, 0, 8))
        sv.Height = 380
        sv.VerticalScrollBarVisibility = System.Windows.Controls.ScrollBarVisibility.Auto
        sp_checks = StackPanel()
        sv.Content = sp_checks
        root.Children.Add(sv)

        checks = []
        show_all = len(self._visible_families) == 0
        for fn in fam_names:
            cb = CheckBox()
            cb.Content = "{} ({} types)".format(fn, fam_counts[fn])
            cb.Tag = fn
            cb.IsChecked = show_all or (fn in self._visible_families)
            cb.Margin = Thickness(0, 2, 0, 2)
            sp_checks.Children.Add(cb)
            checks.append(cb)

        def on_search(s, a):
            filt = (tb_search.Text or "").lower()
            for cb in checks:
                if filt and filt not in cb.Tag.lower():
                    cb.Visibility = Visibility.Collapsed
                else:
                    cb.Visibility = Visibility.Visible
        tb_search.TextChanged += on_search

        btn_panel = StackPanel(Orientation=Orientation.Horizontal)
        btn_all = Button(Content="Check All", Width=80, Margin=Thickness(0, 0, 6, 0))
        btn_none = Button(Content="Uncheck All", Width=90, Margin=Thickness(0, 0, 6, 0))
        btn_ok = Button(Content="OK", Width=70, Margin=Thickness(0, 0, 6, 0))
        btn_cancel = Button(Content="Cancel", Width=70)

        def chk_all(s, a):
            for cb in checks:
                cb.IsChecked = True
        def chk_none(s, a):
            for cb in checks:
                cb.IsChecked = False

        result = [False]

        def on_ok(s, a):
            result[0] = True
            dlg.Close()
        def on_cancel(s, a):
            dlg.Close()

        btn_all.Click += chk_all
        btn_none.Click += chk_none
        btn_ok.Click += on_ok
        btn_cancel.Click += on_cancel

        btn_panel.Children.Add(btn_all)
        btn_panel.Children.Add(btn_none)
        btn_panel.Children.Add(btn_ok)
        btn_panel.Children.Add(btn_cancel)
        root.Children.Add(btn_panel)

        dlg.Content = root
        dlg.ShowDialog()

        if result[0]:
            self._visible_families = set()
            all_checked = True
            for cb in checks:
                if cb.IsChecked:
                    self._visible_families.add(cb.Tag)
                else:
                    all_checked = False
            if all_checked:
                self._visible_families = set()
            self._rebuild_sbd()
            self._on_save(q=True)
            self._st("Family filter updated.")
            
    def _remove_rule(self,rule,panel):
        if rule in self._rules: self._rules.remove(rule)
        try: self.spRules.Children.Remove(panel)
        except Exception: pass

    def _collect_rules_data(self):
        out=[]
        for rd in self._rules:
            kw_text=rd.ctrls["kw"].Text
            sym=self._sbd.get(self._ct(rd.ctrls["cmb"]))
            sid=sym.Id.IntegerValue if sym else None
            try: h=float(rd.ctrls["h"].Text)
            except Exception: h=300.0
            mode=self._ct(rd.ctrls["mode"]) or MODE_WALL
            rp=self._ct(rd.ctrls["rp"]) or RP_AUTO
            out.append({"kw":kw_text,"target":sid,"h":h,"mode":mode,"rp":rp})
        return out

    def _load_rules_from_settings(self):
        self._rules=[];self.spRules.Children.Clear()
        for rd in self.S.get("rules",[]):
            self._add_rule(kw=rd.get("kw",""),sym_id=rd.get("target"),
                           h=rd.get("h",300.0),mode=rd.get("mode",MODE_WALL),
                           rp=rd.get("rp",RP_AUTO))

    def _match_rule(self,hay):
        up=hay.upper()
        for rd in self._rules:
            kw_text=rd.ctrls["kw"].Text
            keywords=[x.strip().upper() for x in kw_text.split(',') if x.strip()]
            if not keywords: continue
            for kw in keywords:
                if kw in up: return rd
        return None

    def _apply_rule_to_row(self,row,rd):
        sym=self._sbd.get(self._ct(rd.ctrls["cmb"]))
        if sym:
            for d,s in self._sbd.items():
                if s.Id.IntegerValue==sym.Id.IntegerValue:
                    row.ctrls["cmb"].SelectedItem=d;row.sym_id=sym.Id.IntegerValue;break
        try:
            h=float(rd.ctrls["h"].Text);row.ctrls["ht"].Text=str(h);row.h_mm=h
        except Exception: pass
        try:
            mode=self._ct(rd.ctrls["mode"]) or MODE_WALL
            row.ctrls["mode"].SelectedItem=mode;row.mode=mode
        except Exception: pass
        try:
            rp=self._ct(rd.ctrls["rp"]) or RP_AUTO
            row.ctrls["rp"].SelectedItem=rp;row.rp_name=rp
        except Exception: pass

    def _apply_rules_to_rows(self):
        count=0
        for row in self._rows:
            if row.status!=ACC: continue
            hay=u"{} {} {}".format(row.fam,row.typ,row.cat)
            rd=self._match_rule(hay)
            if rd: self._apply_rule_to_row(row,rd);count+=1
        self._st("Rules applied to {} fixture(s).".format(count))

    # ---- load ----
    def _init_load(self):
        self._load_sym();self._load_lnk();self._load_sp()
        self._load_ref_planes();self._load_rules_from_settings()
        self._st("Ready. {} ref planes loaded.".format(len(self._ref_planes)))

    def _load_sym(self):
        self._all_symbols = list(
            FilteredElementCollector(doc).OfClass(FamilySymbol)
            .OfCategory(BuiltInCategory.OST_ElectricalFixtures)
            .WhereElementIsElementType())
        self._all_symbols.sort(key=lambda x: sdisplay(x).lower())
        saved = self.S.get("visible_families", [])
        if saved:
            self._visible_families = set(saved)
        else:
            self._visible_families = set()
        self._rebuild_sbd()
    def _rebuild_sbd(self):
        if self._visible_families:
            filtered = [s for s in self._all_symbols
                        if ename(s.Family) in self._visible_families]
        else:
            filtered = self._all_symbols
        self._sbd = {sdisplay(s): s for s in filtered}
        total_fams = len(set(ename(s.Family) for s in self._all_symbols))
        shown_fams = len(set(ename(s.Family) for s in filtered))
        self.txtFamilyCount.Text = "Showing {} of {} families".format(
            shown_fams, total_fams)
    
    def _load_lnk(self):
        self.cmbLinkInstance.Items.Clear()
        self._lbd = {}

        last = self.S.get("last_link", "")
        sel = None
        added = 0
        skipped = 0
        reasons = []

        for li in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
            try:
                ld = li.GetLinkDocument()

                # show even unloaded links (so user knows they exist)
                li_name = ename(li)
                if ld is None:
                    label = u"{} [UNLOADED]".format(li_name)
                else:
                    label = u"{} [{}]".format(li_name, ld.Title)

                self._lbd[label] = li
                self.cmbLinkInstance.Items.Add(label)
                added += 1

                if label == last:
                    sel = label

            except Exception as ex:
                skipped += 1
                reasons.append(str(ex))

        if self.cmbLinkInstance.Items.Count > 0:
            self.cmbLinkInstance.SelectedItem = sel or self.cmbLinkInstance.Items[0]

        # Optional: show what happened in status (helps future debugging)
        self._st("Links loaded: {} (skipped: {})".format(added, skipped))

    def _load_sp(self):
        self._sps=[];self._smap={}
        col=FilteredElementCollector(doc)
        try:
            if bool(self.cbActiveView.IsChecked):
                col=FilteredElementCollector(doc,doc.ActiveView.Id)
        except Exception: pass
        for sp in col.OfCategory(BuiltInCategory.OST_MEPSpaces).WhereElementIsNotElementType():
            try:
                if sp.Area<=0: continue
            except Exception: pass
            self._sps.append(SI(sp))
        self._sps.sort(key=lambda x:(x.num,x.name))
        self._rebuild();self._st("Loaded {} spaces.".format(len(self._sps)))
    def _rebuild(self):
        f=(self.tbSpaceSearch.Text or "").lower()
        self.lbSpaces.Items.Clear();self._smap={}
        for si in self._sps:
            if f and f not in si.disp.lower(): continue
            self.lbSpaces.Items.Add(si.disp);self._smap[si.disp]=si
    def _on_sp(self,s,a):
        d=self.lbSpaces.SelectedItem
        self._csi=self._smap.get(d) if d in self._smap else None
        if self._csi: self._st("Space: {}".format(self._csi.disp))

    # ---- scan ----
    def _sel_link(self): return self._lbd.get(self._ct(self.cmbLinkInstance))
    def _sel_cats(self): return [b for c,b,_ in CATS if bool(getattr(self,c).IsChecked)]
    def _val_scan(self):
        if not self._csi: return False,"No space selected.",None
        sp=self._csi.space
        if doc.GetElement(sp.Id) is None: return False,"Space deleted.",None
        if not get_bsegs(sp): return False,"No boundary segments.",None
        lk=self._sel_link()
        if lk is None: return False,"No link selected.",None
        if lk.GetLinkDocument() is None: return False,"Link unloaded.",None
        if not self._sel_cats(): return False,"No categories.",None
        try:
            st=pfloat(self.tbScanTolerance.Text,0,5000)
            wd=pfloat(self.tbWallSearchDistance.Text,0,5000)
            dh=pfloat(self.tbDefaultHeight.Text,-5000,10000)
        except Exception as ex: return False,str(ex),None
        return True,"",{"sp":sp,"lk":lk,"st":st,"wd":wd,"dh":dh}

    def _scan(self):
        ok,msg,ctx=self._val_scan()
        if not ok:
            forms.alert("Cannot scan.\n\n{}".format(msg),title="Find Linked Fixtures")
            self._st("Scan cancelled.");return
        sp=ctx["sp"];lk=ctx["lk"];ld=lk.GetLinkDocument()
        trf=lk.GetTotalTransform();polys=space_polys(sp)
        tol=mm2ft(ctx["st"]);dh=ctx["dh"]
        iw=pkw(self.tbIncludeKeywords.Text);ew=pkw(self.tbExcludeKeywords.Text)
        sl=(self._ct(self.cmbLevelMode)!="Ignore Level")
        ig=bool(self.cbIgnoreDuplicates.IsChecked)
        hl=doc.GetElement(sp.LevelId) if sp.LevelId else None
        hln=ename(hl) if hl else ""
        seen=set();rows=[];cts={}
        def inc(k): cts[k]=cts.get(k,0)+1
        for bic in self._sel_cats():
            try: els=FilteredElementCollector(ld).OfCategory(bic).WhereElementIsNotElementType()
            except Exception: continue
            for e in els:
                loc=None
                try: loc=e.Location
                except Exception: pass
                if not isinstance(loc,LocationPoint):
                    inc(R_NOLOC)
                    if bool(self.cbVerboseDiag.IsChecked):
                        rows.append(FR(lk,e,XYZ(0,0,0),R_NOLOC,"No LocationPoint.",sp.LevelId.IntegerValue))
                    continue
                ph=trf.OfPoint(loc.Point)
                row=FR(lk,e,ph,ACC,"",sp.LevelId.IntegerValue);row.h_mm=dh
                row.linked_h_mm = 0.0
                st=ACC;rsn=""
                if sl:
                    try:
                        lv=ld.GetElement(e.LevelId);ln=ename(lv)
                        if hln and ln and ln!=hln: st=R_LEVEL;rsn="'{}' != '{}'.".format(ln,hln)
                    except Exception: pass
                hay=u"{} {} {}".format(row.fam,row.typ,row.cat)
                if st==ACC and(not has_any(hay,iw) or not has_none(hay,ew)):
                    st=R_NAME;rsn="Keyword filter."
                if st==ACC:
                    ins=inside_polys(ph,polys)
                    nr=not ins and min_dist_polys(ph,polys)<=tol
                    if not ins and not nr: st=R_SPACE;rsn="Outside space+tol."
                if st==ACC and ig:
                    k=(row.fam,row.typ,int(round(ph.X*304.8)),int(round(ph.Y*304.8)),int(round(ph.Z*304.8)))
                    if k in seen: st=R_DUP;rsn="Duplicate."
                    else: seen.add(k)
                row.status=st;row.reason=rsn
                if st==ACC:
                    inc(ACC);self._amap(row);rows.append(row)
                else:
                    inc(st)
                    if bool(self.cbVerboseDiag.IsChecked): rows.append(row)
        self._rows=rows;self._render()
        self._apply_rules_to_rows()
        self._summ(cts);self._on_save(q=True)

    def _summ(self,cts):
        a=cts.get(ACC,0);ps=["{} accepted".format(a)]
        for k in [R_LEVEL,R_SPACE,R_NOLOC,R_NAME,R_DUP]:
            n=cts.get(k,0)
            if n: ps.append("{} {}".format(n,k.replace("Rejected - ","")))
        t="Scan: "+"; ".join(ps);self.txtSummary.Text=t;self._st(t)

    # ---- render ----
    def _clear(self):
        self._rows=[];self.spResults.Children.Clear()
        self.spResults.Children.Add(TextBlock(Text="Cleared.",Foreground=Brushes.Gray,Margin=Thickness(4)))
        self.txtSummary.Text="No results."
    def _chk_all(self,v):
        for r in self._rows:
            try: r.ctrls["cb"].IsChecked=bool(v) and r.status==ACC
            except Exception: pass
    def _render(self):
        self.spResults.Children.Clear()
        if not self._rows:
            self.spResults.Children.Add(TextBlock(Text="None accepted.",Foreground=Brushes.Gray,Margin=Thickness(4)));return
        h=StackPanel(Orientation=Orientation.Horizontal,Margin=Thickness(2))
        for t,w in [("Use",40),("Linked fixture",290),("Target type",320),("Height",70),("Mode",110),("Ref Plane",180),("Status",130),("Reason",280)]:
            h.Children.Add(TextBlock(Text=t,Width=w,FontWeight=System.Windows.FontWeights.Bold))
        self.spResults.Children.Add(h)
        for r in self._rows: self.spResults.Children.Add(self._rpanel(r))

    def _rpanel(self,r):
        p=StackPanel(Orientation=Orientation.Horizontal,Margin=Thickness(2))
        cb=CheckBox(Width=40,IsChecked=r.chk and r.status==ACC,IsEnabled=(r.status==ACC))
        p.Children.Add(cb)
        lh_text = ""
        try:
            lh_text = " | {}".format(r._diag)
        except Exception:
            pass
        p.Children.Add(TextBlock(
            Text=u"{}\n{} / {}".format(r.dname, r.cat, r.le_id),
            Width=290,
            TextWrapping=System.Windows.TextWrapping.Wrap))
        cmb=ComboBox(Width=320,IsEnabled=(r.status==ACC))
        cmb.Items.Add("<Select target family/type>")
        for d in sorted(self._sbd): cmb.Items.Add(d)
        if r.sym_id:
            for d,sym in self._sbd.items():
                if sym.Id.IntegerValue==r.sym_id: cmb.SelectedItem=d;break
        if cmb.SelectedItem is None: cmb.SelectedIndex=0
        cmb.SelectionChanged+=lambda s,a,rr=r,cc=cmb:self._on_map(rr,cc)
        p.Children.Add(cmb)
        ht=TextBox(Text=str(r.h_mm),Width=70,IsEnabled=(r.status==ACC))
        p.Children.Add(ht)
        mode_cmb=ComboBox(Width=110,IsEnabled=(r.status==ACC))
        for m in ALL_MODES: mode_cmb.Items.Add(m)
        mode_cmb.SelectedItem=r.mode if r.mode in ALL_MODES else MODE_WALL
        p.Children.Add(mode_cmb)
        rp_cmb=ComboBox(Width=180,IsEnabled=(r.status==ACC))
        for item in self._rp_combo_items(): rp_cmb.Items.Add(item)
        rp_cmb.SelectedItem=r.rp_name if r.rp_name in self._rp_combo_items() else RP_AUTO
        p.Children.Add(rp_cmb)
        st=TextBlock(Text=r.status,Width=130,TextWrapping=System.Windows.TextWrapping.Wrap)
        stcol(st,"DarkGreen" if r.status==ACC else "DarkRed")
        p.Children.Add(st)
        rsn=TextBlock(Text=r.reason or "",Width=280,TextWrapping=System.Windows.TextWrapping.Wrap)
        p.Children.Add(rsn)
        r.ctrls={"cb":cb,"cmb":cmb,"ht":ht,"mode":mode_cmb,"rp":rp_cmb,"st":st,"rsn":rsn}
        return p

    def _amap(self,r):
        k=u"{} :: {}".format(r.fam,r.typ)
        sid=self.S.get("maps",{}).get(k)
        if sid: r.sym_id=sid
    def _on_map(self,r,cmb):
        sym=self._sbd.get(self._ct(cmb))
        if sym:
            r.sym_id=sym.Id.IntegerValue
            self.S.setdefault("maps",{})[u"{} :: {}".format(r.fam,r.typ)]=sym.Id.IntegerValue

    # ---- placement ----
    def _calc_inward(self,curve,proj_pt,sp_center):
        s=curve.GetEndPoint(0);e=curve.GetEndPoint(1)
        dx=e.X-s.X;dy=e.Y-s.Y;L=math.sqrt(dx*dx+dy*dy)
        if L<1e-12:
            vx=sp_center.X-proj_pt.X;vy=sp_center.Y-proj_pt.Y
            vl=math.sqrt(vx*vx+vy*vy)
            if vl<1e-12: return XYZ(1,0,0)
            return XYZ(vx/vl,vy/vl,0)
        n1x=dy/L;n1y=-dx/L;n2x=-dy/L;n2y=dx/L
        tcx=sp_center.X-proj_pt.X;tcy=sp_center.Y-proj_pt.Y
        if(n1x*tcx+n1y*tcy)>=(n2x*tcx+n2y*tcy): return XYZ(n1x,n1y,0)
        return XYZ(n2x,n2y,0)

    def _rotate_to_face_inward(self,inst,place_pt,inward_dir):
        try: facing=inst.FacingOrientation
        except Exception: return
        fx=facing.X;fy=facing.Y
        ix=-inward_dir.X;iy=-inward_dir.Y
        cross=fx*iy-fy*ix;dot=fx*ix+fy*iy
        angle=math.atan2(cross,dot)
        if abs(angle)<0.01: return
        axis=Line.CreateBound(
            XYZ(place_pt.X,place_pt.Y,place_pt.Z),
            XYZ(place_pt.X,place_pt.Y,place_pt.Z+1.0))
        ElementTransformUtils.RotateElement(doc,inst.Id,axis,angle)

    def _find_wall(self,sp,ld,pt,max_ft):
        segs=get_bsegs(sp)
        if not segs: return None
        best_wall=None;best_bnd=None;bd_w=1e99;bd_b=1e99
        for loop in segs:
            for seg in loop:
                try:
                    c=seg.GetCurve()
                    res=c.Project(XYZ(pt.X,pt.Y,c.GetEndPoint(0).Z))
                    if not res: continue
                    p=res.XYZPoint;d=math.sqrt((p.X-pt.X)**2+(p.Y-pt.Y)**2)
                    lid=seg.LinkElementId
                    if lid is not None and lid!=ElementId.InvalidElementId:
                        try:
                            w=ld.GetElement(lid)
                            if w is not None and w.GetType().Name=="Wall":
                                if d<bd_w: bd_w=d;best_wall=(c,p,w,d,"wall")
                        except Exception: pass
                    if d<bd_b: bd_b=d;best_bnd=(c,p,None,d,"boundary")
                except Exception: pass
        if best_wall is not None and bd_w<=max_ft: return best_wall
        if best_bnd is not None and bd_b<=max_ft: return best_bnd
        return None

    def _vrow(self,r):
        if r.status!=ACC: return False,"Not accepted."
        try:
            if not bool(r.ctrls["cb"].IsChecked): return False,"Unchecked."
        except Exception: return False,"Unchecked."
        sym=self._sbd.get(self._ct(r.ctrls["cmb"]))
        if sym is None: return False,"No target type."
        try: pfloat(r.ctrls["ht"].Text,-5000,10000)
        except Exception as ex: return False,str(ex)
        r.sym_id=sym.Id.IntegerValue;r.h_mm=float(r.ctrls["ht"].Text)
        r.mode=self._ct(r.ctrls["mode"]) or MODE_WALL
        r.rp_name=self._ct(r.ctrls["rp"]) or RP_AUTO
        return True,""

    def _place(self):
        if not self._rows:
            forms.alert("No results.",title="Find Linked Fixtures");return
        try: wd=pfloat(self.tbWallSearchDistance.Text,0,5000)
        except Exception as ex:
            forms.alert("Bad wall distance.\n{}".format(ex),title="Find Linked Fixtures");return
        tg=bool(self.cbTagPlaced.IsChecked);au=bool(self.cbAutoSelectZoom.IsChecked)
        self._boundary_warned=False
        placed=[];fails=[]
        tx=Transaction(doc,"Place linked fixtures");tx.Start()
        try:
            for r in self._rows:
                ok,msg=self._vrow(r)
                if not ok:
                    if msg!="Unchecked.": fails.append((r,msg));self._rfail(r,msg)
                    continue
                sym=self._sbd.get(self._ct(r.ctrls["cmb"]))
                try:
                    nid,dbg=self._place1(r,sym,wd,tg)
                    if nid:
                        if dbg and dbg.get("source")=="boundary" and not self._boundary_warned:
                            self._boundary_warned=True
                            forms.alert("Some fixtures placed on boundary lines (wall not in scanned link).\nPlacement continues.",title="Boundary Fallback")
                        placed.append((nid,dbg));r.placed=nid.IntegerValue
                        self._rok(r,"Placed {}".format(nid.IntegerValue))
                    else:
                        fails.append((r,r.err or "Unknown."));self._rfail(r,r.err or "Unknown.")
                except Exception as ex:
                    fails.append((r,str(ex)));self._rfail(r,str(ex))
            tx.Commit()
        except Exception: tx.RollBack();raise
        self._lpi=List[ElementId]()
        for eid,_ in placed: self._lpi.Add(eid)
        self.btnUndoLast.IsEnabled=len(placed)>0
        self.btnSelectLast.IsEnabled=len(placed)>0
        if au and self._lpi.Count>0:
            sel_ok,sel_msg=sel_show(self._lpi)
            self._st(sel_msg)
        self._on_save(q=True);self._report(placed,fails)

    def _place1(self,row,sym,wall_mm,do_tag):
        row.err=None
        si=self._csi
        if si is None: row.err="No space.";return None,None
        sp=si.space
        li=doc.GetElement(ElementId(row.li_id))
        if li is None or li.GetLinkDocument() is None:
            row.err="Link unavailable.";return None,None
        view=doc.ActiveView;level=None
        try: level=doc.GetElement(view.GenLevel.Id)
        except Exception: pass
        if level is None:
            row.err="Active view has no level.";return None,None
        if not sym.IsActive: sym.Activate();doc.Regenerate()

        mode=row.mode or MODE_WALL

        # ==== CEILING (FACE) — host on reference plane ====
        if mode==MODE_CFACE:
            rp_name=row.rp_name or RP_AUTO
            rp_elem=None
            used_rp_name=""
            if rp_name==RP_AUTO:
                lvl_name=ename(level)
                matched_name,rp_elem=self._auto_match_ref_plane(row.h_mm,lvl_name)
                used_rp_name=matched_name or ""
            else:
                rp_elem=self._find_rp_by_name(rp_name)
                used_rp_name=rp_name
            if rp_elem is None:
                avail=", ".join([n for n,_ in self._ref_planes[:10]])
                row.err="No ref plane found for height {} (tried '{}').\nAvailable: {}".format(
                    int(row.h_mm),rp_name,avail)
                return None,None
            try:
                from Autodesk.Revit.DB import SketchPlane
                plane = rp_elem.GetPlane()
                plane_z = plane.Origin.Z
                sk_plane = SketchPlane.Create(doc, plane)
                pp = XYZ(row.pt.X, row.pt.Y, plane_z)
                inst = doc.Create.NewFamilyInstance(
                    pp, sym, XYZ.BasisX, sk_plane,
                    StructuralType.NonStructural)
            except Exception as ex:
                row.err = "Ceiling(Face) placement failed: {}".format(ex)
                return None, None
            if do_tag:
                try: tag_inst(inst,"Find Linked Fixtures (link id {})".format(row.le_id))
                except Exception: pass
            dbg={"method":"RefPlane","source":"refplane","space":si.disp,"level":ename(level),
                 "height":row.h_mm,"wall_mm":0.0,"x":ft2mm(pp.X),"y":ft2mm(pp.Y),"z":ft2mm(pp.Z),
                 "inward":"n/a","mode":MODE_CFACE,"rp":used_rp_name}
            return inst.Id,dbg

        # ==== CEILING (level-based) ====
        if mode==MODE_CEIL:
            pp=XYZ(row.pt.X,row.pt.Y,level.Elevation)
            try: inst=doc.Create.NewFamilyInstance(pp,sym,level,StructuralType.NonStructural)
            except Exception as ex:
                row.err="Placement failed: {}".format(ex);return None,None
            try:
                ep=inst.get_Parameter(BuiltInParameter.INSTANCE_ELEVATION_PARAM)
                if ep and not ep.IsReadOnly: ep.Set(mm2ft(row.h_mm))
            except Exception: pass
            if do_tag:
                try: tag_inst(inst,"Find Linked Fixtures (link id {})".format(row.le_id))
                except Exception: pass
            dbg={"method":"LevelBased","source":"ceiling","space":si.disp,"level":ename(level),
                 "height":row.h_mm,"wall_mm":0.0,"x":ft2mm(pp.X),"y":ft2mm(pp.Y),"z":ft2mm(pp.Z),
                 "inward":"n/a","mode":MODE_CEIL,"rp":""}
            return inst.Id,dbg

        # ==== WALL ====
        best=self._find_wall(sp,li.GetLinkDocument(),row.pt,mm2ft(wall_mm))
        if best is None:
            row.err="No wall or boundary within {} mm.".format(wall_mm);return None,None
        curve,proj_pt,wall,dist_ft,source=best
        sc=space_center(sp);inward=self._calc_inward(curve,proj_pt,sc)
        pp=XYZ(proj_pt.X,proj_pt.Y,level.Elevation)
        try: inst=doc.Create.NewFamilyInstance(pp,sym,level,StructuralType.NonStructural)
        except Exception as ex:
            row.err="Placement failed: {}".format(ex);return None,None
        try:
            ep=inst.get_Parameter(BuiltInParameter.INSTANCE_ELEVATION_PARAM)
            if ep and not ep.IsReadOnly: ep.Set(mm2ft(row.h_mm))
        except Exception: pass
        self._rotate_to_face_inward(inst,pp,inward)
        if do_tag:
            try: tag_inst(inst,"Find Linked Fixtures (link id {})".format(row.le_id))
            except Exception: pass
        dbg={"method":"LevelBased","source":source,"space":si.disp,"level":ename(level),
             "height":row.h_mm,"wall_mm":ft2mm(dist_ft),"x":ft2mm(pp.X),"y":ft2mm(pp.Y),"z":ft2mm(pp.Z),
             "inward":"({:.2f},{:.2f})".format(inward.X,inward.Y),"mode":MODE_WALL,"rp":""}
        return inst.Id,dbg

    def _rok(self,r,m):
        r.ctrls["st"].Text="Placed";stcol(r.ctrls["st"],"DarkGreen");r.ctrls["rsn"].Text=m
    def _rfail(self,r,m):
        r.ctrls["st"].Text="Failed";stcol(r.ctrls["st"],"DarkRed");r.ctrls["rsn"].Text=m

    def _report(self,placed,fails):
        msg="Placed {}.".format(len(placed))
        if fails: msg+=" {} failed.".format(len(fails))
        self._st(msg);self.txtSummary.Text=msg
        output.print_md("## Placement Report")
        output.print_md("- Placed: **{}**".format(len(placed)))
        output.print_md("- Failed: **{}**".format(len(fails)))
        if placed:
            output.print_md("### Details")
            for eid,d in placed:
                rp_info=" rp=`{}`".format(d.get("rp")) if d.get("rp") else ""
                output.print_md("- **{}** | {} | mode={} | lvl `{}` | h {}{}".format(
                    eid.IntegerValue,d.get("source"),d.get("mode"),d.get("level"),d.get("height"),rp_info))
        if fails:
            output.print_md("### Failures")
            for r,rsn in fails: output.print_md("- `{}` / link `{}`: {}".format(r.dname,r.le_id,rsn))

    def _sel_last(self):
        if self._lpi is None or self._lpi.Count==0:
            forms.alert("Nothing placed yet.",title="Find Linked Fixtures");return
        ok,m=sel_show(self._lpi);self._st(m)
        if not ok: forms.alert(m,title="Find Linked Fixtures")
    def _undo(self):
        if self._lpi is None or self._lpi.Count==0: return
        tx=Transaction(doc,"Undo linked fixture placement");tx.Start()
        try:
            doc.Delete(self._lpi);tx.Commit()
            self._st("Deleted {}.".format(self._lpi.Count))
            self._lpi=List[ElementId]();self.btnUndoLast.IsEnabled=False;self.btnSelectLast.IsEnabled=False
        except Exception: tx.RollBack();raise

ui=Win()
ui.Show()
