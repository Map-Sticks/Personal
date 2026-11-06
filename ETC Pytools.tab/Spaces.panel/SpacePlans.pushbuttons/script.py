# -*- coding: utf-8 -*-
from __future__ import division, print_function
import math, os
from pyrevit import revit, DB, forms, script
from System.Collections.ObjectModel import ObservableCollection

doc=revit.doc
log=script.get_logger()

def iv(i):
    try:return i.Value
    except:return i.IntegerValue

def mm(v):
    try:return DB.UnitUtils.ConvertToInternalUnits(float(v),DB.UnitTypeId.Millimeters)
    except:return DB.UnitUtils.ConvertToInternalUnits(float(v),DB.DisplayUnitType.DUT_MILLIMETERS)

def nm(e):
    try:return DB.Element.Name.GetValue(e) or ''
    except:
        try:
            p=e.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
            return p.AsString() or '' if p else ''
        except:return ''

def sptext(s,bip,fallback):
    try:
        p=s.get_Parameter(bip)
        if p:return p.AsString() or p.AsValueString() or ''
    except:pass
    try:
        p=s.LookupParameter(fallback)
        return p.AsString() or p.AsValueString() or '' if p else ''
    except:return ''

def snum(s):return sptext(s,DB.BuiltInParameter.ROOM_NUMBER,'Number')
def sname(s):return sptext(s,DB.BuiltInParameter.ROOM_NAME,'Name')
def level(s):return doc.GetElement(s.LevelId)
def safe(t):
    t=t or ''
    for c in ['\\',':','{','}','[',']','|',';','<','>','?','`','~']:t=t.replace(c,'-')
    return t.strip()

def unique_view(base):
    names=set(v.Name for v in DB.FilteredElementCollector(doc).OfClass(DB.View))
    out=base;i=2
    while out in names:out='{} ({})'.format(base,i);i+=1
    return out

def unique_sheet_num(base):
    used=set(x.SheetNumber for x in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet))
    if base not in used:return base
    i=2
    while '{}-{}'.format(base,i) in used:i+=1
    return '{}-{}'.format(base,i)

def collect_spaces():
    out=[]
    for s in DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_MEPSpaces).WhereElementIsNotElementType():
        try:
            if s.Area>1e-9 and s.Location and iv(s.LevelId)>0:out.append(s)
        except:pass
    return sorted(out,key=lambda x:('{} {} {}'.format(level(x).Name,snum(x),sname(x))).lower())

def plan_types(fam):
    return sorted([x for x in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType) if x.ViewFamily==fam],key=lambda x:nm(x).lower())

def templates(vt):
    return sorted([v for v in DB.FilteredElementCollector(doc).OfClass(DB.View) if v.IsTemplate and v.ViewType==vt],key=lambda x:x.Name.lower())

def title_blocks():
    return sorted(list(DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_TitleBlocks).WhereElementIsElementType()),key=lambda x:tb_label(x).lower())

def tb_label(x):
    try:return '{} : {}'.format(x.FamilyName,nm(x))
    except:return nm(x)

def viewport_types():
    out=[]
    for x in DB.FilteredElementCollector(doc).OfClass(DB.ElementType):
        try:
            if x.Category and iv(x.Category.Id)==int(DB.BuiltInCategory.OST_Viewports):out.append(x)
        except:pass
    return sorted(out,key=lambda x:nm(x).lower())

def boundary(s):
    pts=[];segs=[]
    try:
        loops=s.GetBoundarySegments(DB.SpatialElementBoundaryOptions())
        if loops:
            for lp in loops:
                for bs in lp:
                    c=bs.GetCurve();a=c.GetEndPoint(0);b=c.GetEndPoint(1);pts.extend([a,b]);dx=b.X-a.X;dy=b.Y-a.Y;ln=math.sqrt(dx*dx+dy*dy)
                    if ln>mm(100):segs.append((math.atan2(dy,dx),ln))
    except:pass
    return pts,segs

def auto_angle(group):
    vals=[]
    for s in group:vals+=boundary(s)[1]
    if not vals:return 0.0
    q=math.pi/2;buckets={};data=[]
    for a,l in vals:
        c=a-round(a/q)*q
        while c>math.pi/4:c-=q
        while c<-math.pi/4:c+=q
        k=int(round(math.degrees(c)/2.0));buckets[k]=buckets.get(k,0)+l;data.append((c,l,k))
    win=max(buckets,key=buckets.get);chosen=[x for x in data if abs(x[2]-win)<=1];total=sum(x[1] for x in chosen)
    return sum(x[0]*x[1] for x in chosen)/total if total else math.radians(win*2)

def crop_data(group,a,buf):
    pts=[]
    for s in group:pts+=boundary(s)[0]
    if not pts:raise Exception('No usable Space boundary.')
    c=math.cos(a);si=math.sin(a);u=[p.X*c+p.Y*si for p in pts];v=[-p.X*si+p.Y*c for p in pts]
    u0,u1=min(u)-buf,max(u)+buf;v0,v1=min(v)-buf,max(v)+buf;uc=(u0+u1)/2;vc=(v0+v1)/2
    return DB.XYZ(uc*c-vc*si,uc*si+vc*c,pts[0].Z),u1-u0,v1-v0

def view_ids(v):
    return dict((iv(x.Id),x.Id) for x in DB.FilteredElementCollector(doc,v.Id).WhereElementIsNotElementType())

def set_crop(v,c,w,h,a):
    z=v.GenLevel.Elevation;b=DB.BoundingBoxXYZ();b.Min=DB.XYZ(c.X-w/2,c.Y-h/2,z-mm(1000));b.Max=DB.XYZ(c.X+w/2,c.Y+h/2,z+mm(1000));v.CropBoxActive=True;v.CropBoxVisible=False;v.CropBox=b;doc.Regenerate()
    if abs(a)<1e-8:return
    v.CropBoxVisible=False;doc.Regenerate();before=view_ids(v);v.CropBoxVisible=True;doc.Regenerate();after=view_ids(v);axis=DB.Line.CreateBound(DB.XYZ(c.X,c.Y,z-10),DB.XYZ(c.X,c.Y,z+10))
    for k in after:
        if k not in before:
            try:DB.ElementTransformUtils.RotateElement(doc,after[k],axis,a);v.CropBoxVisible=False;return
            except:pass
    raise Exception('Crop region could not be rotated.')

def tight_crop(v,s,buf):
    try:
        loops=s.GetBoundarySegments(DB.SpatialElementBoundaryOptions())
        if not loops:return False
        src=DB.CurveLoop()
        for bs in loops[0]:src.Append(bs.GetCurve())
        mgr=v.GetCropRegionShapeManager()
        for d in (buf,-buf):
            try:
                lp=DB.CurveLoop.CreateViaOffset(src,d,DB.XYZ.BasisZ)
                if mgr.IsCropRegionShapeValid(lp):mgr.SetCropShape(lp);return True
            except:pass
    except Exception as ex:log.warning('Tight crop unavailable; rectangle retained: {}'.format(ex))
    return False

def outline_size(vp):
    o=vp.GetBoxOutline();return o.MaximumPoint.X-o.MinimumPoint.X,o.MaximumPoint.Y-o.MinimumPoint.Y

def canvas_dims(name,cw,ch):
    presets={'A3 Landscape':(420,297),'A2 Landscape':(594,420),'A1 Landscape':(841,594),'A0 Landscape':(1189,841),'Custom':(cw,ch)}
    w,h=presets.get(name,(841,594));return mm(w),mm(h)

def titleblock_bounds(sheet):
    boxes=[]
    for x in DB.FilteredElementCollector(doc,sheet.Id).OfCategory(DB.BuiltInCategory.OST_TitleBlocks).WhereElementIsNotElementType():
        b=x.get_BoundingBox(sheet)
        if b:boxes.append(b)
    if not boxes:return None
    return min(b.Min.X for b in boxes),min(b.Min.Y for b in boxes),max(b.Max.X for b in boxes),max(b.Max.Y for b in boxes)

def place_layout(sheet,views,mode,canvas,custom_w,custom_h,strategy,margin,gap,vptype):
    # Future elevation integration point. Elevation generator can append views here.
    layout_views={'plans':views[:1],'rcps':views[1:2],'elevations':[]}
    ordered=layout_views['plans']+layout_views['rcps']+layout_views['elevations']
    if not ordered:return []
    if mode=='Documentation':
        bounds=titleblock_bounds(sheet)
        if bounds:x0,y0,x1,y1=bounds
        else:x0,y0,x1,y1=0,0,canvas_dims(canvas,custom_w,custom_h)[0],canvas_dims(canvas,custom_w,custom_h)[1]
    else:
        x0,y0=0,0;x1,y1=canvas_dims(canvas,custom_w,custom_h)
    x0+=margin;y0+=margin;x1-=margin;y1-=margin
    vps=[]
    for v in ordered:
        if not DB.Viewport.CanAddViewToSheet(doc,sheet.Id,v.Id):raise Exception('View {} cannot be placed.'.format(v.Name))
        vp=DB.Viewport.Create(doc,sheet.Id,v.Id,DB.XYZ((x0+x1)/2,(y0+y1)/2,0));vps.append(vp)
        if vptype:
            try:vp.ChangeTypeId(vptype.Id)
            except:pass
    doc.Regenerate();sizes=[outline_size(x) for x in vps]
    if len(vps)==1:vps[0].SetBoxCenter(DB.XYZ((x0+x1)/2,(y0+y1)/2,0));return vps
    horizontal=(strategy=='Plans Across Top')
    if strategy=='Compact Automatic':
        horiz_w=sizes[0][0]+gap+sizes[1][0];horiz_h=max(sizes[0][1],sizes[1][1]);vert_w=max(sizes[0][0],sizes[1][0]);vert_h=sizes[0][1]+gap+sizes[1][1]
        horizontal=(horiz_w*horiz_h<=vert_w*vert_h and horiz_w<=x1-x0) or vert_h>y1-y0
    if strategy=='Plans Down Left':horizontal=False
    if horizontal:
        total=sizes[0][0]+gap+sizes[1][0];cy=y1-max(sizes[0][1],sizes[1][1])/2
        vps[0].SetBoxCenter(DB.XYZ((x0+x1)/2-total/2+sizes[0][0]/2,cy,0));vps[1].SetBoxCenter(DB.XYZ((x0+x1)/2+total/2-sizes[1][0]/2,cy,0))
    else:
        total=sizes[0][1]+gap+sizes[1][1];cx=x0+max(sizes[0][0],sizes[1][0])/2
        vps[0].SetBoxCenter(DB.XYZ(cx,(y0+y1)/2+total/2-sizes[0][1]/2,0));vps[1].SetBoxCenter(DB.XYZ(cx,(y0+y1)/2-total/2+sizes[1][1]/2,0))
    return vps

class Row(object):
    def __init__(self,s):self.Space=s;self.IsSelected=False;self.Level=level(s).Name;self.Number=snum(s);self.Name=sname(s)
class Win(forms.WPFWindow):
    def __init__(self,x,ss):
        forms.WPFWindow.__init__(self,x);self.cfg=script.get_config('space_plan_generator');self.all=[Row(s) for s in ss];self.rows=ObservableCollection[object]();self.show(self.all)
        self.scale.ItemsSource=['20','25','50','75','100','125','200'];self.scale.Text=str(self.get('scale','50'));self.buffer.Text=str(self.get('buffer','500'))
        self.rotation.ItemsSource=['Automatic','Project orientation','90 degrees','Custom'];self.rotation.SelectedIndex=int(self.get('rotation',0));self.angle.Text=str(self.get('angle','0'))
        self.cropstyle.ItemsSource=['Rectangular','Follow Space boundary'];self.cropstyle.SelectedIndex=int(self.get('crop',0))
        self.ft.ItemsSource=['<None>']+[x.Name for x in FTS];self.rt.ItemsSource=['<None>']+[x.Name for x in RTS];self.pick(self.ft,self.get('ft','<None>'));self.pick(self.rt,self.get('rt','<None>'))
        self.sheetmode.ItemsSource=['Documentation','Review - No Title Block'];self.pick(self.sheetmode,self.get('sheetmode','Documentation'))
        self.titleblock.ItemsSource=[tb_label(x) for x in TBS];self.pick(self.titleblock,self.get('tb',''))
        self.canvas.ItemsSource=['A3 Landscape','A2 Landscape','A1 Landscape','A0 Landscape','Custom'];self.pick(self.canvas,self.get('canvas','A1 Landscape'))
        self.strategy.ItemsSource=['Plans Across Top','Plans Down Left','Compact Automatic'];self.pick(self.strategy,self.get('strategy','Plans Across Top'))
        self.vptype.ItemsSource=['<Default>']+[nm(x) for x in VPTS];self.pick(self.vptype,self.get('vpt','<Default>'))
        self.margin.Text=str(self.get('margin','20'));self.gap.Text=str(self.get('gap','20'));self.customw.Text=str(self.get('cw','841'));self.customh.Text=str(self.get('ch','594'));self.prefix.Text=str(self.get('prefix','A-'));self.start.Text='7001';self.makesheet.IsChecked=bool(self.get('makesheet',False));self.grouped.IsChecked=bool(self.get('grouped',False));self.individual.IsChecked=not self.grouped.IsChecked;self.result=False;self.mode_change(None,None)
    def get(self,n,d):
        try:return getattr(self.cfg,n)
        except:return d
    def pick(self,c,v):
        c.SelectedIndex=0
        for i,x in enumerate(c.ItemsSource):
            if str(x)==str(v):c.SelectedIndex=i;break
    def show(self,rs):
        self.rows.Clear()
        for r in rs:self.rows.Add(r)
        self.grid.ItemsSource=self.rows
    def search(self,s,a):
        q=(self.searchbox.Text or '').lower();self.show([r for r in self.all if q in '{} {} {}'.format(r.Level,r.Number,r.Name).lower()])
    def clear(self,s,a):self.searchbox.Text=''
    def allon(self,s,a):
        for r in self.rows:r.IsSelected=True
        self.grid.Items.Refresh()
    def alloff(self,s,a):
        for r in self.rows:r.IsSelected=False
        self.grid.Items.Refresh()
    def mode_change(self,s,a):
        if not hasattr(self,'sheetmode'):return
        review=self.sheetmode.SelectedItem=='Review - No Title Block';self.titleblock.IsEnabled=not review;self.canvas.IsEnabled=review;self.customw.IsEnabled=review and self.canvas.SelectedItem=='Custom';self.customh.IsEnabled=self.customw.IsEnabled
    def go(self,s,a):
        sel=[r.Space for r in self.all if r.IsSelected]
        if not sel:return forms.alert('Select at least one Space.')
        self.isgroup=bool(self.grouped.IsChecked)
        if self.isgroup and len(sel)<2:return forms.alert('Grouped mode requires at least two Spaces.')
        if self.isgroup and len(set(iv(x.LevelId) for x in sel))>1:return forms.alert('Grouped Spaces must be on the same Level.')
        try:self.sc=int(self.scale.Text);self.buf=mm(float(self.buffer.Text));self.ang=float(self.angle.Text or 0);self.marginft=mm(float(self.margin.Text));self.gapft=mm(float(self.gap.Text));self.cw=float(self.customw.Text);self.ch=float(self.customh.Text);self.first=int(self.start.Text)
        except:return forms.alert('Scale, buffer, angle, margins, spacing and sheet values must be valid numbers.')
        self.sel=sel;self.mkf=bool(self.floor.IsChecked);self.mkr=bool(self.rcp.IsChecked);self.rot=self.rotation.SelectedIndex;self.tight=self.cropstyle.SelectedIndex;self.mks=bool(self.makesheet.IsChecked);self.smode=self.sheetmode.SelectedItem;self.canvasval=self.canvas.SelectedItem;self.strategyval=self.strategy.SelectedItem;self.prefixval=self.prefix.Text or '';self.groupname=safe(self.groupname.Text) or ' + '.join(snum(x) for x in sel);self.ftval=self.ft.SelectedItem;self.rtval=self.rt.SelectedItem
        if not self.mkf and not self.mkr:return forms.alert('Enable Floor Plan and/or RCP.')
        if self.mks and self.smode=='Documentation' and not TBS:return forms.alert('No loaded title block types were found.')
        self.tbidx=max(0,self.titleblock.SelectedIndex);self.vptidx=self.vptype.SelectedIndex-1
        for n,v in [('scale',self.scale.Text),('buffer',self.buffer.Text),('rotation',self.rot),('angle',self.angle.Text),('crop',self.tight),('grouped',self.isgroup),('ft',str(self.ftval)),('rt',str(self.rtval)),('makesheet',self.mks),('sheetmode',str(self.smode)),('tb',str(self.titleblock.SelectedItem)),('canvas',str(self.canvasval)),('strategy',str(self.strategyval)),('vpt',str(self.vptype.SelectedItem)),('margin',self.margin.Text),('gap',self.gap.Text),('cw',self.customw.Text),('ch',self.customh.Text),('prefix',self.prefixval)]:setattr(self.cfg,n,v)
        script.save_config();self.result=True;self.Close()
    def cancel(self,s,a):self.Close()

SPS=collect_spaces();FPT=plan_types(DB.ViewFamily.FloorPlan);CPT=plan_types(DB.ViewFamily.CeilingPlan);FTS=templates(DB.ViewType.FloorPlan);RTS=templates(DB.ViewType.CeilingPlan);TBS=title_blocks();VPTS=viewport_types()
if not SPS or not FPT or not CPT:forms.alert('Placed Spaces and Floor/RCP view types are required.',exitscript=True)
w=Win(os.path.join(os.path.dirname(__file__),'ui.xaml'),SPS);w.ShowDialog()
if not w.result:script.exit()
ft=dict((x.Name,x) for x in FTS);rt=dict((x.Name,x) for x in RTS);created=[];sheets=[];errors=[]
def calcangle(g):return auto_angle(g) if w.rot==0 else (0 if w.rot==1 else (math.pi/2 if w.rot==2 else math.radians(w.ang)))
def make(g,base,idx):
    a=calcangle(g);c,ww,hh=crop_data(g,a,w.buf);views=[];lev=level(g[0])
    if w.mkf:
        v=DB.ViewPlan.Create(doc,FPT[0].Id,lev.Id);v.Name=unique_view(base+' - Floor Plan');v.Scale=w.sc;set_crop(v,c,ww,hh,a)
        if w.tight and len(g)==1:tight_crop(v,g[0],w.buf)
        if w.ftval in ft:v.ViewTemplateId=ft[w.ftval].Id
        views.append(v);created.append(v)
    if w.mkr:
        v=DB.ViewPlan.Create(doc,CPT[0].Id,lev.Id);v.Name=unique_view(base+' - RCP');v.Scale=w.sc;set_crop(v,c,ww,hh,a)
        if w.tight and len(g)==1:tight_crop(v,g[0],w.buf)
        if w.rtval in rt:v.ViewTemplateId=rt[w.rtval].Id
        views.append(v);created.append(v)
    if w.mks:
        tid=DB.ElementId.InvalidElementId if w.smode=='Review - No Title Block' else TBS[w.tbidx].Id
        sh=DB.ViewSheet.Create(doc,tid);sh.SheetNumber=unique_sheet_num(('REVIEW-' if w.smode.startswith('Review') else w.prefixval)+str(w.first+idx));sh.Name=base+' - Review' if w.smode.startswith('Review') else base
        place_layout(sh,views,'Review' if w.smode.startswith('Review') else 'Documentation',w.canvasval,w.cw,w.ch,w.strategyval,w.marginft,w.gapft,VPTS[w.vptidx] if w.vptidx>=0 else None);sheets.append(sh)
with revit.TransactionGroup('Generate Space layouts and sheets'):
    groups=[(w.sel,w.groupname)] if w.isgroup else [([s],safe('{} - {}'.format(snum(s),sname(s)))) for s in w.sel]
    for i,(g,n) in enumerate(groups):
        try:
            with revit.Transaction('Create {}'.format(n)):make(g,n,i)
        except Exception as ex:errors.append('{}: {}'.format(n,ex));log.exception('Failed {}'.format(n))
msg='Created {} view(s) and {} sheet(s).'.format(len(created),len(sheets))
if errors:msg+='\n\nFailures:\n'+'\n'.join(errors[:15])
forms.alert(msg,title='Space Plans')
