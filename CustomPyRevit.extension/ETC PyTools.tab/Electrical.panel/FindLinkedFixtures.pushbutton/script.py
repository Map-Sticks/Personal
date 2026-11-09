# -*- coding: utf-8 -*-
"""Find Linked Fixtures - selectable linked and host family categories.
Wall / Ceiling / Ceiling(Face) modes.
Face mode hosts on reference planes in host model.
"""
__persistentengine__ = True

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

import os, json, math, traceback, System
import io
import shutil
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
    ReferencePlane, SubTransaction, TransactionStatus, FamilyPlacementType,
    Options, ViewDetailLevel, Solid, Mesh, GeometryInstance)
from Autodesk.Revit.DB.Structure import StructuralType
from Autodesk.Revit.DB.ExtensibleStorage import (
    AccessLevel, DataStorage, Entity, Schema, SchemaBuilder)

doc    = revit.doc
uidoc  = revit.uidoc
output = script.get_output()
logger = script.get_logger()
FT     = 1.0 / 304.8
SETTINGS_FILENAME = "find_linked_fixtures_settings.json"
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), SETTINGS_FILENAME)
SETTINGS_SCHEMA_ID = "62c080d4-e64b-48fc-b6aa-61c4598f9b37"
SETTINGS_SCHEMA_NAME = "MapSticksFindLinkedFixturesSettings"
SETTINGS_FIELD = "Payload"
SETTINGS_VERSION = 1

# Retain the old keys to migrate existing settings without changing defaults.
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
LEVEL_MATCH = "Match linked level elevation"
LEVEL_VOLUME = "Space volume only"

def mm2ft(mm): return float(mm)*FT
def ft2mm(ft): return float(ft)*304.8
def id_value(eid):
    return eid.Value if hasattr(eid,"Value") else eid.IntegerValue
def category_key(category): return str(id_value(category.Id))
def level_mode(value):
    if value in ("Ignore Level",LEVEL_VOLUME): return LEVEL_VOLUME
    return LEVEL_MATCH
def linked_height_mm(point,level):
    return ft2mm(point.Z-level.ProjectElevation)
def geometry_z_range(element,transform):
    """Host-coordinate (min Z, max Z) of the element's visible 3D geometry, or None.
    Families can offset their 3D internally (e.g. an instance 'Height' parameter) while
    the insertion point stays at the level, so the insertion Z alone can be misleading."""
    rng=[None,None]
    def add(point):
        z=transform.OfPoint(point).Z
        if rng[0] is None or z<rng[0]: rng[0]=z
        if rng[1] is None or z>rng[1]: rng[1]=z
    def add_mesh(mesh):
        for i in range(mesh.NumTriangles):
            tri=mesh.get_Triangle(i)
            for k in range(3): add(tri.get_Vertex(k))
    def walk(geometry):
        for obj in geometry:
            if isinstance(obj,Solid):
                if obj.Faces.Size==0: continue
                for face in obj.Faces:
                    mesh=face.Triangulate()
                    if mesh is not None: add_mesh(mesh)
            elif isinstance(obj,GeometryInstance):
                walk(obj.GetInstanceGeometry())
            elif isinstance(obj,Mesh):
                add_mesh(obj)
    options=Options();options.DetailLevel=ViewDetailLevel.Fine
    geometry=element.get_Geometry(options)
    if geometry is not None: walk(geometry)
    if rng[0] is None:
        box=element.get_BoundingBox(None)
        if box is None: return None
        for x in (box.Min.X,box.Max.X):
            for y in (box.Min.Y,box.Max.Y):
                for z in (box.Min.Z,box.Max.Z):
                    add(box.Transform.OfPoint(XYZ(x,y,z)))
    return rng[0],rng[1]
def linked_height_text(insert_mm,geom_lo_mm,geom_hi_mm,level_name):
    ins="n/a" if insert_mm is None else "{:.1f}".format(insert_mm)
    tip=[u"Heights above Space level '{}' (mm); display only.".format(level_name),
         u"Ins = family insertion point (level + offset)."]
    if geom_lo_mm is None or geom_hi_mm is None:
        tip.append(u"3D geometry could not be read.")
        return u"Ins {}\n3D n/a".format(ins),u"\n".join(tip)
    tip.append(u"3D = bottom / top of visible geometry; includes any offset built into the family.")
    tip.append(u"3D centre = {:.1f}".format((geom_lo_mm+geom_hi_mm)/2.0))
    return (u"Ins {}\n3D {:.1f} / {:.1f}".format(ins,geom_lo_mm,geom_hi_mm),
            u"\n".join(tip))
def linked_level_matches(transform,location,linked_level,host_level):
    base=transform.OfPoint(XYZ(location.X,location.Y,linked_level.ProjectElevation))
    return abs(base.Z-host_level.ProjectElevation)<=mm2ft(1.0)
def set_placement_height(inst,level,height_mm):
    parameter=inst.get_Parameter(BuiltInParameter.INSTANCE_ELEVATION_PARAM)
    if parameter and not parameter.IsReadOnly:
        parameter.Set(mm2ft(height_mm))
    doc.Regenerate()
    location=inst.Location
    expected=level.ProjectElevation+mm2ft(height_mm)
    if not isinstance(location,LocationPoint) or abs(location.Point.Z-expected)>mm2ft(1.0):
        raise ValueError("Placed device elevation does not match the requested Space-relative height (1 mm tolerance).")
def selected_category_keys(settings):
    saved=settings.get("scan_categories")
    if isinstance(saved,list): return set(saved)
    legacy=settings.get("cats",{})
    return set(str(int(b)) for c,b,d in CATS if legacy.get(c,d))
def placement_error(placement_type,mode):
    expected=(FamilyPlacementType.WorkPlaneBased if mode==MODE_CFACE
              else FamilyPlacementType.OneLevelBased)
    if placement_type!=expected:
        return "Family placement type '{}' is not supported in {} mode. Use {} families.".format(
            placement_type,mode,expected)
    return None
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
    return pstr(e,BuiltInParameter.ALL_MODEL_TYPE_NAME) or str(id_value(e.Id))
def tname(s): return pstr(s,BuiltInParameter.SYMBOL_NAME_PARAM) or ename(s)
def sdisplay(s): return u"{} :: {}".format(ename(s.Family),tname(s))
def pfloat(t,lo=None,hi=None):
    try: v=float(str(t).strip())
    except Exception: raise ValueError("'{}' not a number.".format(t))
    if math.isnan(v) or math.isinf(v): raise ValueError("Enter a finite number.")
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
    loops=sp.GetBoundarySegments(SpatialElementBoundaryOptions())
    if not loops: raise ValueError("The selected Space has no boundary loops.")
    for loop in loops:
        pts=[]
        for seg in loop:
            samples=list(seg.GetCurve().Tessellate())
            if len(samples)<2: raise ValueError("A Space boundary could not be tessellated.")
            for p in samples[:-1]:
                xy=(p.X,p.Y)
                if not pts or xy!=pts[-1]: pts.append(xy)
        if len(pts)<3: raise ValueError("A Space boundary loop has fewer than three vertices.")
        polys.append(pts)
    if not polys: raise ValueError("The selected Space has no usable boundary loops.")
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
def inside_polys(pt,polys):
    return sum(1 for p in polys if pip(pt.X,pt.Y,p))%2==1
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
def inside_space(sp,pt,polys,tol):
    depth=sum(1 for poly in polys if pip(pt.X,pt.Y,poly))
    # Tolerance never admits points inside an inner void.
    if depth and depth%2==0: return False
    if sp.IsPointInSpace(pt): return True
    if depth or tol<=0: return False
    if min_dist_polys(pt,polys)>tol: return False
    epsilon=min(mm2ft(1.0),tol*0.1)
    for poly in polys:
        for i in range(len(poly)):
            ax,ay=poly[i];bx,by=poly[(i+1)%len(poly)]
            dx=bx-ax;dy=by-ay;length2=dx*dx+dy*dy
            if length2<1e-12: continue
            t=max(0.0,min(1.0,((pt.X-ax)*dx+(pt.Y-ay)*dy)/length2))
            qx=ax+t*dx;qy=ay+t*dy
            if math.sqrt((pt.X-qx)**2+(pt.Y-qy)**2)>tol: continue
            length=math.sqrt(length2)
            for sign in (-1.0,1.0):
                candidate=XYZ(qx-sign*dy/length*epsilon,
                              qy+sign*dx/length*epsilon,pt.Z)
                if inside_polys(candidate,polys) and sp.IsPointInSpace(candidate):
                    return True
    return False
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
        s.space=sp
        s.name=pstr(sp,BuiltInParameter.ROOM_NAME)
        if not s.name:
            s.name=ename(sp)
            logger.warning("Space {} has no readable Name parameter; using element name.".format(id_value(sp.Id)))
        s.num=getattr(sp,"Number","") or ""
        level=doc.GetElement(sp.LevelId)
        s.level_name=ename(level) if level else "<No level>"
        s.disp=u"{} - {} [{}] ({})".format(s.num,s.name,s.level_name,id_value(sp.Id))
        s.search=u"{} {} {}".format(s.num,s.name,s.level_name).lower()
class FR(object):
    def __init__(s,li,le,pt,st,rsn,lvid):
        s.li_id=id_value(li.Id);s.le_id=id_value(le.Id) if le else -1
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
        s.linked_h_mm=None;s.geom_lo_mm=None;s.geom_hi_mm=None;s.space_level_name=""
        s.chk=(st==ACC);s.placed=None;s.err=None;s.ctrls={}
class RuleData(object):
    def __init__(s,kw="",sym_id=None,h=300.0,mode=MODE_WALL,rp=RP_AUTO):
        s.kw_text=kw;s.sym_id=sym_id;s.h_mm=h;s.mode=mode;s.rp_name=rp;s.ctrls={}

def defs():
    cs={c:d for c,_,d in CATS}
    return {"scan_tol":300.0,"wall_dist":600.0,"def_h":300.0,
            "av_only":True,"ign_dup":True,"verbose":False,
            "inc_kw":"","exc_kw":"","lvl_mode":LEVEL_MATCH,
            "cats":cs,"scan_categories":None,"target_categories":None,
            "maps":{},"last_link":"",
            "auto_sel":True,"tag":True,"rules":[],"visible_families":[]
            }
def settings_id(value):
    if value is None: return None
    text=str(value)
    if not _re.match(r"^-?\d+\Z",text):
        raise ValueError("Invalid saved target ID: {!r}".format(value))
    return int(text)

def settings_for_json(settings):
    data=dict(settings)
    # IronPython's JSON encoder can reject boxed Int64/long; never pass IDs to it.
    data["maps"]={key:(str(settings_id(value)) if value is not None else None)
                  for key,value in settings.get("maps",{}).items()}
    data["rules"]=[]
    for rule in settings.get("rules",[]):
        saved=dict(rule)
        if "target" in saved and saved["target"] is not None:
            saved["target"]=str(settings_id(saved["target"]))
        data["rules"].append(saved)
    return data

def validated_settings(ld):
    d=defs()
    if not isinstance(ld,dict): raise ValueError("Settings must be a JSON object.")
    for key in ("cats","maps"):
        if key in ld and not isinstance(ld[key],dict):
            raise ValueError("'{}' must be an object.".format(key))
    for key in ("rules","visible_families","scan_categories","target_categories"):
        if key in ld and not (key.endswith("categories") and ld[key] is None):
            if not isinstance(ld[key],list):
                raise ValueError("'{}' must be a list.".format(key))
    if any(not isinstance(rule,dict) for rule in ld.get("rules",[])):
        raise ValueError("Each mapping rule must be an object.")
    for key,lo,hi in (("scan_tol",0,5000),("wall_dist",0,5000),("def_h",-5000,10000)):
        if key in ld: pfloat(ld[key],lo,hi)
    d.update(ld)
    d["maps"]={key:settings_id(value) for key,value in d["maps"].items()}
    d["rules"]=[dict(rule) for rule in d["rules"]]
    for rule in d["rules"]:
        if "target" in rule: rule["target"]=settings_id(rule["target"])
        if "h" in rule: pfloat(rule["h"],-5000,10000)
    return d

def load_s(path=None):
    source=path or SETTINGS_FILE
    try:
        if path is None and not os.path.exists(source): return defs()
        with io.open(source,"r",encoding="utf-8-sig") as f: ld=json.load(f)
        return validated_settings(ld)
    except Exception as ex:
        raise ValueError("Could not load settings from:\n{}\n\n{}\n\nThe file has not been overwritten.".format(source,ex))

def previous_bundle_settings_file():
    return os.path.abspath(os.path.join(
        os.path.dirname(__file__),os.pardir,os.pardir,os.pardir,
        "Custom Tools.tab","Electrical.panel","FindLinkedFixtures.pushbutton",SETTINGS_FILENAME))

def settings_draft_file():
    previous=previous_bundle_settings_file()
    current_exists=os.path.exists(SETTINGS_FILE)
    previous_exists=os.path.exists(previous)
    if current_exists and previous_exists:
        with io.open(SETTINGS_FILE,"rb") as current_file:
            with io.open(previous,"rb") as previous_file:
                different=current_file.read()!=previous_file.read()
        if different:
            previous_choice="Use previous Custom Tools settings"
            current_choice="Use current ETC PyTools settings"
            choice=forms.alert(
                "Two different settings files exist:\n\nPrevious: {}\n\nCurrent: {}\n\n"
                "Choose the JSON draft to load. Neither file will be overwritten.".format(
                    previous,SETTINGS_FILE),
                title="Find Linked Fixtures settings",
                options=[previous_choice,current_choice,"Cancel"])
            if choice==previous_choice: return previous
            if choice==current_choice: return SETTINGS_FILE
            raise ValueError("Settings selection cancelled. No settings were changed; "
                             "use Reload RVT to choose again.")
    if current_exists: return SETTINGS_FILE
    if previous_exists: return previous
    return None

def settings_json(data):
    return json.dumps(settings_for_json(validated_settings(data)),
                      indent=2,sort_keys=True,allow_nan=False)

def save_s(d,path):
    try:
        payload=settings_json(d)
        backup=path+".bak"
        if os.path.exists(path) and not os.path.exists(backup):
            shutil.copy2(path,backup)
        with open(path,'w') as f: f.write(payload)
        if settings_json(load_s(path))!=payload:
            raise ValueError("JSON export read-back did not match. Keep the current window open.")
        return True,None
    except Exception as ex: return False,str(ex)

def model_settings_payload(data):
    return json.dumps({"version":SETTINGS_VERSION,
                       "settings":settings_for_json(validated_settings(data))},
                      sort_keys=True,allow_nan=False)

def decode_model_settings(payload):
    envelope=json.loads(payload)
    if (not isinstance(envelope,dict) or envelope.get("version")!=SETTINGS_VERSION
            or isinstance(envelope.get("version"),bool)):
        raise ValueError("Unsupported or damaged RVT settings version. Nothing has been overwritten.")
    return validated_settings(envelope.get("settings"))

def model_settings_record(document):
    schema=Schema.Lookup(System.Guid(SETTINGS_SCHEMA_ID))
    if schema is None: return None,None,None
    records=[]
    for storage in FilteredElementCollector(document).OfClass(DataStorage):
        entity=storage.GetEntity(schema)
        if entity.IsValid():
            field=schema.GetField(SETTINGS_FIELD)
            if field is None: raise ValueError("RVT settings schema has no Payload field.")
            payload=entity.Get[System.String](field)
            if not payload: raise ValueError("RVT settings record is empty or damaged.")
            records.append((storage,schema,payload))
    if len(records)>1:
        raise ValueError("Multiple RVT settings records found. Export window settings and resolve the duplicate records before saving.")
    return records[0] if records else (None,schema,None)

def load_model_settings(document):
    storage,schema,payload=model_settings_record(document)
    return (decode_model_settings(payload) if payload is not None else None),payload

def save_model_settings(document,data,expected_payload):
    payload=model_settings_payload(data)
    if document.IsReadOnly: raise ValueError("The RVT is read-only. Settings were not saved.")
    tx=Transaction(document,"Save Find Linked Fixtures settings")
    try:
        if tx.Start()!=TransactionStatus.Started:
            raise ValueError("Could not start the RVT settings transaction.")
        storage,schema,current=model_settings_record(document)
        if current!=expected_payload:
            raise ValueError("RVT settings changed since this window loaded (another window, user or Undo).\n"
                             "Export Settings to preserve your edits, then use Reload RVT before saving again.")
        if schema is None:
            builder=SchemaBuilder(System.Guid(SETTINGS_SCHEMA_ID))
            builder.SetSchemaName(SETTINGS_SCHEMA_NAME)
            builder.SetReadAccessLevel(AccessLevel.Public)
            builder.SetWriteAccessLevel(AccessLevel.Public)
            builder.AddSimpleField(SETTINGS_FIELD,System.String)
            schema=builder.Finish()
        if storage is None: storage=DataStorage.Create(document)
        entity=Entity(schema)
        entity.Set[System.String](schema.GetField(SETTINGS_FIELD),payload)
        storage.SetEntity(entity)
        document.Regenerate()
        if model_settings_record(document)[2]!=payload:
            raise ValueError("RVT settings read-back failed before commit.")
        if tx.Commit()!=TransactionStatus.Committed:
            raise ValueError("RVT settings transaction did not commit.")
        restored,stored=load_model_settings(document)
        if stored!=payload or settings_json(restored)!=settings_json(data):
            raise ValueError("RVT settings verification failed after commit. Keep this window open and export your settings.")
        return stored
    except Exception:
        if tx.GetStatus()==TransactionStatus.Started: tx.RollBack()
        raise

class Hnd(IExternalEventHandler):
    def __init__(self,window):
        self.window=window;self.action=None
    def Execute(s,uiapp):
        action,s.action=s.action,None
        if action is None or s.window._closed: return
        try:
            active=uiapp.ActiveUIDocument
            if not doc.IsValidObject or active is None or not active.Document.Equals(doc):
                raise RuntimeError("Activate the project that opened this window, or close and reopen the tool.")
            action(s.window)
        except Exception:
            logger.error(traceback.format_exc())
            forms.alert(traceback.format_exc(),title="Find Linked Fixtures")
    def GetName(s): return "FLF22"
def rev(fn,pl): pl._request(fn)

class Win(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self,script.get_bundle_file("ui.xaml"))
        self.S=defs()
        self._settings_token=None;self._saved_settings=None
        self._settings_source="Waiting for RVT settings"
        self._discard_close=False
        self._settings_ready=False;self._applying_settings=False
        self._closed=False;self._loading_links=False
        self.handler=Hnd(self)
        self.ext_event=ExternalEvent.Create(self.handler)
        self._scan_categories=[];self._target_categories=[]
        self._scan_category_link=""
        self._scan_category_keys=selected_category_keys(self.S)
        saved=self.S.get("target_categories")
        self._target_category_keys=(set(saved) if isinstance(saved,list) else
                                   set([str(int(BuiltInCategory.OST_ElectricalFixtures))]))
        self._symbol_data={}
        self._sps=[];self._smap={};self._csi=None
        self._lbd={};self._sbd={}
        self._rows=[];self._lpi=List[ElementId]()
        self._rules=[]
        self._ref_planes=[]
        self._all_symbols = []
        self._visible_families = set()
        self._boundary_warned=False
        self._apply_s();self._wire()
        rev(lambda u:u._startup_load(),self)

    def _request(self,fn):
        if self._closed or self._applying_settings: return
        if self.handler.action is not None:
            self._st("Another action is pending. Please retry when it finishes.")
            return
        self.handler.action=fn
        try:
            result=self.ext_event.Raise()
            if str(result) not in ("Accepted","Pending"):
                raise RuntimeError("ExternalEvent request was {}. Please retry.".format(result))
        except Exception as ex:
            self.handler.action=None
            logger.error(str(ex));forms.alert(str(ex),title="Find Linked Fixtures")

    def _wire(self):
        self.btnClose.Click+=lambda s,a:self.Close()
        self.btnReloadSpaces.Click+=lambda s,a:rev(lambda u:u._load_sp(),self)
        self.btnSaveSettings.Click+=self._on_save
        self.btnImportSettings.Click+=self._on_import
        self.btnExportSettings.Click+=self._on_export
        self.btnReloadSettings.Click+=lambda s,a:rev(lambda u:u._reload_settings(),self)
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
        self.btnScanCategories.Click+=lambda s,a:self._choose_categories(False)
        self.btnTargetCategories.Click+=lambda s,a:self._choose_categories(True)
        self.cmbLinkInstance.SelectionChanged+=self._on_link_changed
        def _on_filter_click(s, a):
            try:
                self._show_family_filter_dialog()
            except Exception as ex:
                forms.alert("Filter dialog error:\n\n{}".format(traceback.format_exc()), title="Error")
        self.btnFilterFamilies.Click += _on_filter_click
        self.Closing+=self._on_closing
        self.Closed+=self._on_closed

    def _on_closing(self,s,a):
        if self._discard_close or not self._settings_ready: return
        try:
            changed=settings_json(self._coll_s())!=self._saved_settings
        except Exception as ex:
            a.Cancel=True
            choice=forms.alert("Cannot validate window settings:\n{}\n\n"
                               "Cancel to correct the values, or discard window changes.".format(ex),
                               title="Unsaved settings",options=["Discard window changes","Cancel"])
            if choice=="Discard window changes":
                self._discard_close=True;a.Cancel=False
            return
        if not changed: return
        choice=forms.alert("Window settings have not been saved to the RVT.",
                           title="Unsaved Find Linked Fixtures settings",
                           options=["Save to RVT and close","Discard window changes","Cancel"])
        if choice=="Discard window changes":
            self._discard_close=True
        else:
            a.Cancel=True
            if choice=="Save to RVT and close":
                rev(lambda u:u._save_to_model(close_after=True),self)

    def _on_closed(self,s,a):
        self._closed=True

    def _ct(self,cmb):
        try:
            i=cmb.SelectedItem
            if i is None: return ""
            return str(getattr(i,"Content",i))
        except Exception: return ""
    def _st(self,t): self.txtStatus.Text=t

    def _apply_s(self):
        self._applying_settings=True
        try: self._apply_controls()
        finally: self._applying_settings=False

    def _apply_controls(self):
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
        for item in self.cmbLevelMode.Items:
            if str(item.Content)==level_mode(s.get("lvl_mode")):
                self.cmbLevelMode.SelectedItem=item;break

    def _coll_s(self):
        d=dict(self.S)
        d["scan_tol"]=pfloat(self.tbScanTolerance.Text,0,5000)
        d["wall_dist"]=pfloat(self.tbWallSearchDistance.Text,0,5000)
        d["def_h"]=pfloat(self.tbDefaultHeight.Text,-5000,10000)
        d["av_only"]=bool(self.cbActiveView.IsChecked)
        d["ign_dup"]=bool(self.cbIgnoreDuplicates.IsChecked)
        d["verbose"]=bool(self.cbVerboseDiag.IsChecked)
        d["auto_sel"]=bool(self.cbAutoSelectZoom.IsChecked)
        d["tag"]=bool(self.cbTagPlaced.IsChecked)
        d["inc_kw"]=self.tbIncludeKeywords.Text or ""
        d["exc_kw"]=self.tbExcludeKeywords.Text or ""
        d["lvl_mode"]=level_mode(self._ct(self.cmbLevelMode))
        d["cats"]=self.S.get("cats",defs()["cats"])
        d["scan_categories"]=sorted(self._scan_category_keys)
        d["target_categories"]=sorted(self._target_category_keys)
        d["maps"]=self.S.get("maps",{})
        d["last_link"]=self._ct(self.cmbLinkInstance) or ""
        d["rules"]=self._collect_rules_data()
        d["visible_families"] = list(self._visible_families) if self._visible_families else []
        return d

    def _on_save(self,s=None,a=None):
        if not self._settings_ready:
            forms.alert("Settings are not fully loaded. Saving is disabled to protect stored settings.",
                        title="Find Linked Fixtures")
            return
        rev(lambda u:u._save_to_model(),self)

    def _save_to_model(self,close_after=False):
        if not self._settings_ready: raise ValueError("Settings have not finished loading.")
        try:
            data=self._coll_s()
            token=save_model_settings(doc,data,self._settings_token)
        except Exception as ex:
            logger.error("RVT settings save failed: {}".format(ex))
            self._st("NOT SAVED: {}".format(ex))
            forms.alert("Settings were not confirmed saved:\n{}\n\n"
                        "Your edits remain in this window. Export Settings for a backup. "
                        "In workshared models, check ownership of the settings DataStorage.".format(ex),
                        title="RVT settings not saved")
            return
        self.S=data;self._settings_token=token
        self._saved_settings=settings_json(data)
        self._settings_source="RVT Extensible Storage"
        self._settings_summary()
        self._st("Verified in RVT: {} mappings, {} rules. Save/sync the Revit project to persist.".format(
            len(data["maps"]),len(data["rules"])))
        if close_after:
            # Closing the WPF window happens after Execute returns, not within the API handler.
            self._discard_close=True
            self.Dispatcher.BeginInvoke(System.Action(self.Close))

    def _on_export(self,s=None,a=None):
        if not self._settings_ready:
            forms.alert("Settings have not finished loading.",title="Export Settings");return
        path=forms.save_file(file_ext="json",default_name="find_linked_fixtures_settings.json")
        if not path: return
        try: ok,error=save_s(self._coll_s(),path)
        except Exception as ex: ok,error=False,str(ex)
        if not ok:
            logger.error("Settings export failed: {}".format(error))
            forms.alert(str(error),title="Settings export failed")
        else:
            self._st("JSON export verified: {}. RVT settings have not been saved by export.".format(path))

    def _on_import(self,s=None,a=None):
        path=forms.pick_file(file_ext="json",title="Import Find Linked Fixtures settings")
        if not path: return
        rev(lambda u:u._import_settings(path),self)

    def _import_settings(self,path):
        data=load_s(path)
        if not forms.alert(
                "Import {} mappings and {} rules from:\n{}\n\n"
                "This replaces window settings only. Click Save to RVT, then save/sync the project "
                "to persist them. Mappings use IDs from the original host project.".format(
                    len(data["maps"]),len(data["rules"]),path),
                title="Import Settings",yes=True,no=True):
            return
        self._apply_loaded_settings(data,"JSON draft: "+path)

    def _apply_loaded_settings(self,data,source):
        self._settings_ready=False
        self.S=data;self._settings_source=source
        self._scan_category_keys=selected_category_keys(data)
        saved=data.get("target_categories")
        self._target_category_keys=(set(saved) if isinstance(saved,list) else
                                   set([str(int(BuiltInCategory.OST_ElectricalFixtures))]))
        self._clear();self._rules=[];self.spRules.Children.Clear()
        self._apply_s()
        self._init_load()

    def _startup_load(self):
        data,token=load_model_settings(doc)
        saved=settings_json(data) if data is not None else None
        if data is not None:
            source="RVT Extensible Storage"
        else:
            draft=settings_draft_file()
            if draft:
                data=load_s(draft)
                source="Legacy JSON draft (not yet in RVT): "+draft
            else:
                data=defs();source="New settings (not yet in RVT)"
        self._settings_token=token;self._saved_settings=saved
        self._apply_loaded_settings(data,source)

    def _reload_settings(self):
        message="Reload RVT settings and discard unsaved window edits? Export first if needed."
        changed=False
        if self._settings_ready:
            try: changed=settings_json(self._coll_s())!=self._saved_settings
            except ValueError as ex:
                changed=True
                message="Current settings are invalid:\n{}\n\nReload RVT and discard these window edits?".format(ex)
        if changed:
            if not forms.alert(message,
                               title="Reload RVT Settings",yes=True,no=True): return
        self._startup_load()

    def _settings_summary(self):
        data=self._coll_s()
        total=len(data.get("maps",{}))
        ids=set(data["id"] for data in self._symbol_data.values())
        found=sum(1 for sid in data.get("maps",{}).values() if sid in ids)
        self.txtSettingsInfo.Text="Window: {} mappings ({} target IDs found), {} rules | Source: {} | Save to RVT; then save/sync project".format(
            total,found,len(data.get("rules",[])),self._settings_source)
        self.txtSettingsInfo.ToolTip="Legacy JSON is read only during migration. JSON is written only through Export Settings."
    def _on_reset(self,s,a):
        if not self._settings_ready:
            forms.alert("Wait for settings to finish loading before resetting.",title="Reset Settings");return
        if not forms.alert("Clear all window mappings/rules and restore defaults?\n\n"
                           "Stored RVT settings are unchanged until you click Save to RVT.",
                           title="Reset Settings",yes=True,no=True): return
        self.S=defs();self._apply_s()
        self._scan_category_keys=selected_category_keys(self.S)
        self._target_category_keys=set([str(int(BuiltInCategory.OST_ElectricalFixtures))])
        self._visible_families=set()
        self._clear();self._rebuild_sbd();self._update_category_summary()
        self._rules=[];self.spRules.Children.Clear()
        self._settings_summary()
        self._st("Window settings reset. Click Save to RVT only if you want to replace stored settings.")

    def _update_category_summary(self):
        scan=sum(1 for k,n,cid in self._scan_categories if k in self._scan_category_keys)
        target=sum(1 for k,n,cid in self._target_categories if k in self._target_category_keys)
        self.txtCategoryCount.Text="Scan: {} categories | Targets: {} categories".format(scan,target)

    def _choose_categories(self,target):
        if not target and self._scan_category_link!=self._ct(self.cmbLinkInstance):
            rev(lambda u:u._load_scan_categories(),self)
            self._st("Refreshing linked categories. Click Linked Scan Categories again when ready.")
            return
        catalog=self._target_categories if target else self._scan_categories
        keys=self._target_category_keys if target else self._scan_category_keys
        if not catalog:
            forms.alert("No categories available. Select a loaded link or load host families first.",
                        title="Find Linked Fixtures");return
        options=[forms.TemplateListItem(n,checked=k in keys) for k,n,cid in catalog]
        selected=forms.SelectFromList.show(
            options,title="Host family categories" if target else "Linked scan categories",
            multiselect=True,return_all=True,
            button_name="Use categories")
        if selected is None: return
        selected_names=set(item.item for item in selected if item.checked)
        chosen=set(k for k,n,cid in catalog if n in selected_names)
        if target:
            self._target_category_keys=chosen
            # A new target category must not be hidden by an old family-name filter.
            self._visible_families=set()
            self._rebuild_sbd()
        else:
            self._scan_category_keys=chosen
            self._clear()
        self._update_category_summary()
        self._st("Category choices changed. Click Save to RVT to persist.")

    def _on_link_changed(self,s,a):
        if not self._loading_links and not self._applying_settings:
            self._scan_categories=[]
            self._clear();self._update_category_summary()
            rev(lambda u:u._load_scan_categories(),self)

    def _load_scan_categories(self):
        self._scan_categories=[]
        self._scan_category_link=self._ct(self.cmbLinkInstance)
        lk=self._sel_link()
        ld=lk.GetLinkDocument() if lk else None
        if ld:
            for cat in ld.Settings.Categories:
                self._scan_categories.append((category_key(cat),cat.Name,cat.Id))
            self._scan_categories.sort(key=lambda x:x[1].lower())
        self._clear();self._update_category_summary()

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
            for d in self._sbd:
                if self._symbol_data[d]["id"]==sym_id: cmb.SelectedItem=d;break
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
        for data in self._symbol_data.values():
            if data["category"] not in self._target_category_keys: continue
            fn = data["family"]
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
            chosen = set()
            all_checked = True
            for cb in checks:
                if cb.IsChecked:
                    chosen.add(cb.Tag)
                else:
                    all_checked = False
            if all_checked:
                chosen = set()
            elif not chosen:
                forms.alert("Select at least one family, or Cancel to keep the current filter.",
                            title="Find Linked Fixtures")
                return
            self._visible_families = chosen
            self._rebuild_sbd()
            self._st("Family filter updated. Click Save to RVT to persist.")
            
    def _remove_rule(self,rule,panel):
        if rule in self._rules: self._rules.remove(rule)
        try: self.spRules.Children.Remove(panel)
        except Exception: pass

    def _collect_rules_data(self):
        out=[]
        for rd in self._rules:
            kw_text=rd.ctrls["kw"].Text
            data=self._symbol_data.get(self._ct(rd.ctrls["cmb"]))
            sid=data["id"] if data else rd.sym_id
            if not data and any(self._symbol_data[d]["id"]==sid for d in self._sbd):
                sid=None
            rd.sym_id=sid
            h=pfloat(rd.ctrls["h"].Text,-5000,10000)
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
        display=self._ct(rd.ctrls["cmb"])
        if display in self._sbd:
            row.ctrls["cmb"].SelectedItem=display
            row.sym_id=self._symbol_data[display]["id"]
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
        self._settings_ready=False
        self._load_sym();self._load_lnk();self._load_sp()
        self._load_scan_categories()
        self._load_ref_planes();self._load_rules_from_settings()
        self._settings_summary()
        self._settings_ready=True
        self._st("Ready. {} mappings, {} rules; {} ref planes.".format(
            len(self.S.get("maps",{})),len(self._rules),len(self._ref_planes)))

    def _load_sym(self):
        self._all_symbols = list(
            FilteredElementCollector(doc).OfClass(FamilySymbol)
            .WhereElementIsElementType())
        self._symbol_data={};categories={}
        for sym in self._all_symbols:
            cat=sym.Category
            if cat is None: continue
            key=category_key(cat)
            categories[key]=(key,cat.Name,cat.Id)
            sid=id_value(sym.Id)
            display=u"[{}] {} ({})".format(cat.Name,sdisplay(sym),sid)
            self._symbol_data[display]={"symbol":sym,"id":sid,
                "family":ename(sym.Family),"category":key}
        self._target_categories=sorted(categories.values(),key=lambda x:x[1].lower())
        saved = self.S.get("visible_families", [])
        if saved:
            self._visible_families = set(saved)
        else:
            self._visible_families = set()
        self._rebuild_sbd()
    def _rebuild_sbd(self):
        # Snapshot UI edits before replacing combo contents; no API reads here.
        combos=[]
        for rd in self._rules:
            cmb=rd.ctrls["cmb"]
            data=self._symbol_data.get(self._ct(cmb))
            if data: rd.sym_id=data["id"]
            combos.append((cmb,data["id"] if data else rd.sym_id,"<not set>"))
        for row in self._rows:
            cmb=row.ctrls["cmb"]
            data=self._symbol_data.get(self._ct(cmb))
            combos.append((cmb,data["id"] if data else row.sym_id,"<Select target family/type>"))
        available={d:v for d,v in self._symbol_data.items()
                   if v["category"] in self._target_category_keys}
        filtered={d:v for d,v in available.items() if not self._visible_families
                  or v["family"] in self._visible_families}
        self._sbd={d:v["symbol"] for d,v in filtered.items()}
        for cmb,sid,placeholder in combos:
            cmb.Items.Clear();cmb.Items.Add(placeholder)
            cmb.SelectedIndex=0
            for display in sorted(filtered):
                cmb.Items.Add(display)
                if filtered[display]["id"]==sid: cmb.SelectedItem=display
        total_fams = len(set(v["family"] for v in available.values()))
        shown_fams = len(set(v["family"] for v in filtered.values()))
        self.txtFamilyCount.Text = "Showing {} of {} families".format(
            shown_fams, total_fams)
        self._update_category_summary()
    
    def _load_lnk(self):
        self._loading_links=True
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
        self._loading_links=False

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
        terms=(self.tbSpaceSearch.Text or "").lower().split()
        self.lbSpaces.Items.Clear();self._smap={}
        for si in self._sps:
            if not all(term in si.search for term in terms): continue
            self.lbSpaces.Items.Add(si.disp);self._smap[si.disp]=si
    def _on_sp(self,s,a):
        d=self.lbSpaces.SelectedItem
        self._csi=self._smap.get(d) if d in self._smap else None
        self._clear()
        if self._csi: self._st("Space: {}".format(self._csi.disp))

    # ---- scan ----
    def _sel_link(self): return self._lbd.get(self._ct(self.cmbLinkInstance))
    def _sel_cats(self):
        return [cid for k,n,cid in self._scan_categories if k in self._scan_category_keys]
    def _val_scan(self):
        if not self._csi: return False,"No space selected.",None
        sp=self._csi.space
        if doc.GetElement(sp.Id) is None: return False,"Space deleted.",None
        if doc.GetElement(sp.LevelId) is None: return False,"Space has no valid level.",None
        if not get_bsegs(sp): return False,"No boundary segments.",None
        lk=self._sel_link()
        if lk is None: return False,"No link selected.",None
        if lk.GetLinkDocument() is None: return False,"Link unloaded.",None
        if self._scan_category_link!=self._ct(self.cmbLinkInstance):
            self._load_scan_categories()
        if not self._sel_cats(): return False,"No categories.",None
        try:
            st=pfloat(self.tbScanTolerance.Text,0,5000)
            wd=pfloat(self.tbWallSearchDistance.Text,0,5000)
            dh=pfloat(self.tbDefaultHeight.Text,-5000,10000)
        except Exception as ex: return False,str(ex),None
        return True,"",{"sp":sp,"lk":lk,"st":st,"wd":wd,"dh":dh}

    def _scan(self):
        self._clear()
        ok,msg,ctx=self._val_scan()
        if not ok:
            forms.alert("Cannot scan.\n\n{}".format(msg),title="Find Linked Fixtures")
            self._st("Scan cancelled.");return
        sp=ctx["sp"];lk=ctx["lk"];ld=lk.GetLinkDocument()
        trf=lk.GetTotalTransform();polys=space_polys(sp)
        tol=mm2ft(ctx["st"]);dh=ctx["dh"]
        iw=pkw(self.tbIncludeKeywords.Text);ew=pkw(self.tbExcludeKeywords.Text)
        sl=(level_mode(self._ct(self.cmbLevelMode))==LEVEL_MATCH)
        ig=bool(self.cbIgnoreDuplicates.IsChecked)
        hl=doc.GetElement(sp.LevelId) if sp.LevelId else None
        if hl is None: raise ValueError("The selected Space has no valid level.")
        seen=set();rows=[];cts={}
        def inc(k): cts[k]=cts.get(k,0)+1
        for cid in self._sel_cats():
            els=FilteredElementCollector(ld).OfCategoryId(cid).WhereElementIsNotElementType()
            for e in els:
                loc=None
                try: loc=e.Location
                except Exception: pass
                if not isinstance(loc,LocationPoint):
                    inc(R_NOLOC)
                    if bool(self.cbVerboseDiag.IsChecked):
                        rows.append(FR(lk,e,XYZ(0,0,0),R_NOLOC,"No LocationPoint.",id_value(sp.LevelId)))
                    continue
                ph=trf.OfPoint(loc.Point)
                row=FR(lk,e,ph,ACC,"",id_value(sp.LevelId));row.h_mm=dh
                row.linked_h_mm=linked_height_mm(ph,hl)
                row.space_level_name=ename(hl)
                st=ACC;rsn=""
                if sl:
                    lv=ld.GetElement(e.LevelId)
                    if lv is None:
                        st=R_LEVEL;rsn="Linked level unavailable. Use Space volume only to scan by position."
                    elif not linked_level_matches(trf,loc.Point,lv,hl):
                        st=R_LEVEL;rsn="Transformed linked level does not match the Space level elevation (1 mm tolerance)."
                hay=u"{} {} {}".format(row.fam,row.typ,row.cat)
                if st==ACC and(not has_any(hay,iw) or not has_none(hay,ew)):
                    st=R_NAME;rsn="Keyword filter."
                if st==ACC:
                    if not inside_space(sp,ph,polys,tol):
                        st=R_SPACE;rsn="Outside Space volume/horizontal tolerance, or inside a void."
                if st==ACC and ig:
                    k=(row.fam,row.typ,int(round(ph.X*304.8)),int(round(ph.Y*304.8)),int(round(ph.Z*304.8)))
                    if k in seen: st=R_DUP;rsn="Duplicate."
                    else: seen.add(k)
                row.status=st;row.reason=rsn
                if st==ACC:
                    try:
                        zr=geometry_z_range(e,trf)
                    except Exception as ex:
                        zr=None
                        logger.warning("Could not read 3D geometry of linked element {}: {}".format(row.le_id,ex))
                    if zr is not None:
                        row.geom_lo_mm=ft2mm(zr[0]-hl.ProjectElevation)
                        row.geom_hi_mm=ft2mm(zr[1]-hl.ProjectElevation)
                    inc(ACC);self._amap(row);rows.append(row)
                else:
                    inc(st)
                    if bool(self.cbVerboseDiag.IsChecked): rows.append(row)
        self._rows=rows;self._render()
        self._apply_rules_to_rows()
        self._summ(cts)

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
        for t,w in [("Use",40),("Linked fixture",290),("Linked h. mm",150),("Target type",320),("Place h. mm",80),("Mode",110),("Ref Plane",180),("Status",130),("Reason",280)]:
            h.Children.Add(TextBlock(Text=t,Width=w,FontWeight=System.Windows.FontWeights.Bold))
        self.spResults.Children.Add(h)
        for r in self._rows: self.spResults.Children.Add(self._rpanel(r))

    def _rpanel(self,r):
        p=StackPanel(Orientation=Orientation.Horizontal,Margin=Thickness(2))
        cb=CheckBox(Width=40,IsChecked=r.chk and r.status==ACC,IsEnabled=(r.status==ACC))
        p.Children.Add(cb)
        p.Children.Add(TextBlock(
            Text=u"{}\n{} / {}".format(r.dname, r.cat, r.le_id),
            Width=290,
            TextWrapping=System.Windows.TextWrapping.Wrap))
        htxt,htip=linked_height_text(getattr(r,"linked_h_mm",None),getattr(r,"geom_lo_mm",None),
                                     getattr(r,"geom_hi_mm",None),getattr(r,"space_level_name",""))
        p.Children.Add(TextBlock(Text=htxt,Width=150,ToolTip=htip))
        cmb=ComboBox(Width=320,IsEnabled=(r.status==ACC))
        cmb.Items.Add("<Select target family/type>")
        for d in sorted(self._sbd): cmb.Items.Add(d)
        if r.sym_id:
            for d in self._sbd:
                if self._symbol_data[d]["id"]==r.sym_id: cmb.SelectedItem=d;break
        if cmb.SelectedItem is None: cmb.SelectedIndex=0
        cmb.SelectionChanged+=lambda s,a,rr=r,cc=cmb:self._on_map(rr,cc)
        p.Children.Add(cmb)
        ht=TextBox(Text=str(r.h_mm),Width=80,IsEnabled=(r.status==ACC),
                   ToolTip="Placement height above selected Space level (mm). Independent of linked height.")
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
        data=self._symbol_data.get(self._ct(cmb))
        if data and self._ct(cmb) in self._sbd:
            r.sym_id=data["id"]
            self.S.setdefault("maps",{})[u"{} :: {}".format(r.fam,r.typ)]=data["id"]
            self._st("Mapping changed ({} total). Click Save to RVT to persist.".format(len(self.S["maps"])))

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
        if r.placed is not None: return False,"Already placed. Scan again to start a new placement."
        r.sym_id=id_value(sym.Id);r.h_mm=float(r.ctrls["ht"].Text)
        r.mode=self._ct(r.ctrls["mode"]) or MODE_WALL
        r.rp_name=self._ct(r.ctrls["rp"]) or RP_AUTO
        error=placement_error(sym.Family.FamilyPlacementType,r.mode)
        if error: return False,error
        return True,""

    def _place(self):
        if not self._rows:
            forms.alert("No results.",title="Find Linked Fixtures");return
        try: wd=pfloat(self.tbWallSearchDistance.Text,0,5000)
        except Exception as ex:
            forms.alert("Bad wall distance.\n{}".format(ex),title="Find Linked Fixtures");return
        tg=bool(self.cbTagPlaced.IsChecked);au=bool(self.cbAutoSelectZoom.IsChecked)
        self._boundary_warned=False
        placed=[];fails=[];successful_rows=[]
        tx=Transaction(doc,"Place linked fixtures");tx.Start()
        try:
            for r in self._rows:
                ok,msg=self._vrow(r)
                if not ok:
                    if msg!="Unchecked.": fails.append((r,msg));self._rfail(r,msg)
                    continue
                sym=self._sbd.get(self._ct(r.ctrls["cmb"]))
                try:
                    nid,dbg=self._place_isolated(r,sym,wd,tg)
                    if nid:
                        if dbg and dbg.get("source")=="boundary" and not self._boundary_warned:
                            self._boundary_warned=True
                            forms.alert("Some fixtures placed on boundary lines (wall not in scanned link).\nPlacement continues.",title="Boundary Fallback")
                        placed.append((nid,dbg));successful_rows.append((r,nid))
                    else:
                        fails.append((r,r.err or "Unknown."));self._rfail(r,r.err or "Unknown.")
                except Exception as ex:
                    fails.append((r,str(ex)));self._rfail(r,str(ex))
            if tx.Commit()!=TransactionStatus.Committed:
                raise RuntimeError("Placement transaction did not commit. No placements are reported as successful.")
        except Exception:
            if tx.GetStatus()==TransactionStatus.Started: tx.RollBack()
            raise
        for r,nid in successful_rows:
            r.placed=id_value(nid)
            r.ctrls["cb"].IsChecked=False
            self._rok(r,"Placed {}".format(id_value(nid)))
        self._lpi=List[ElementId]()
        for eid,_ in placed: self._lpi.Add(eid)
        self.btnUndoLast.IsEnabled=len(placed)>0
        self.btnSelectLast.IsEnabled=len(placed)>0
        if au and self._lpi.Count>0:
            sel_ok,sel_msg=sel_show(self._lpi)
            self._st(sel_msg)
        self._report(placed,fails)

    def _place_isolated(self,row,sym,wall_mm,do_tag):
        tx=SubTransaction(doc);tx.Start()
        try:
            nid,dbg=self._place1(row,sym,wall_mm,do_tag)
            if nid is None:
                tx.RollBack();return None,None
            if tx.Commit()!=TransactionStatus.Committed:
                raise RuntimeError("Fixture subtransaction did not commit.")
            return nid,dbg
        except Exception:
            if tx.GetStatus()==TransactionStatus.Started: tx.RollBack()
            raise

    def _place1(self,row,sym,wall_mm,do_tag):
        row.err=None
        si=self._csi
        if si is None: row.err="No space.";return None,None
        sp=si.space
        li=doc.GetElement(ElementId(row.li_id))
        if li is None or li.GetLinkDocument() is None:
            row.err="Link unavailable.";return None,None
        level=doc.GetElement(sp.LevelId)
        if level is None:
            row.err="Selected Space has no valid level.";return None,None
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
                plane = rp_elem.GetPlane()
                if abs(plane.Normal.Z)<0.999999:
                    row.err="Ceiling (Face) requires a horizontal reference plane."
                    return None,None
                plane_z = plane.Origin.Z
                pp = XYZ(row.pt.X, row.pt.Y, plane_z)
                inst = doc.Create.NewFamilyInstance(
                    rp_elem.GetReference(),pp,XYZ.BasisX,sym)
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
            pp=XYZ(row.pt.X,row.pt.Y,level.ProjectElevation)
            try: inst=doc.Create.NewFamilyInstance(pp,sym,level,StructuralType.NonStructural)
            except Exception as ex:
                row.err="Placement failed: {}".format(ex);return None,None
            set_placement_height(inst,level,row.h_mm)
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
        pp=XYZ(proj_pt.X,proj_pt.Y,level.ProjectElevation)
        try: inst=doc.Create.NewFamilyInstance(pp,sym,level,StructuralType.NonStructural)
        except Exception as ex:
            row.err="Placement failed: {}".format(ex);return None,None
        set_placement_height(inst,level,row.h_mm)
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
                    id_value(eid),d.get("source"),d.get("mode"),d.get("level"),d.get("height"),rp_info))
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

try:
    ui=Win()
    ui.show(modal=False)
except Exception:
    logger.error(traceback.format_exc())
    forms.alert(traceback.format_exc(),title="Find Linked Fixtures could not open")
