# -*- coding: utf-8 -*-
from __future__ import division,print_function
import os,sys,math
from pyrevit import revit,DB,forms,script
from System.Collections.ObjectModel import ObservableCollection
LIB=os.path.join(os.path.dirname(__file__),'lib')
if LIB not in sys.path:sys.path.insert(0,LIB)
from elevation_generator import generate_space_elevations
doc=revit.doc;out=script.get_output();log=script.get_logger()
def iv(x):
 try:return x.Value
 except:return x.IntegerValue
def mm(x):
 try:return DB.UnitUtils.ConvertToInternalUnits(float(x),DB.UnitTypeId.Millimeters)
 except:return DB.UnitUtils.ConvertToInternalUnits(float(x),DB.DisplayUnitType.DUT_MILLIMETERS)
def name(x):
 try:return DB.Element.Name.GetValue(x) or ''
 except:
  try:
   p=x.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM);return p.AsString() or '' if p else ''
  except:return ''
def txt(s,b,n):
 try:
  p=s.get_Parameter(b);return p.AsString() or p.AsValueString() or '' if p else ''
 except:return ''
def num(s):return txt(s,DB.BuiltInParameter.ROOM_NUMBER,'Number')
def sname(s):return txt(s,DB.BuiltInParameter.ROOM_NAME,'Name')
def lev(s):return doc.GetElement(s.LevelId)
def uniq_view(n):
 used=set(v.Name for v in DB.FilteredElementCollector(doc).OfClass(DB.View));x=n;i=2
 while x in used:x='{} ({})'.format(n,i);i+=1
 return x
def uniq_sheet(n):
 used=set(s.SheetNumber for s in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet));x=n;i=2
 while x in used:x='{}-{}'.format(n,i);i+=1
 return x
def coll_spaces():
 r=[]
 for s in DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_MEPSpaces).WhereElementIsNotElementType():
  try:
   if s.Area>0 and s.Location:r.append(s)
  except:pass
 return sorted(r,key=lambda x:('{} {} {}'.format(lev(x).Name,num(x),sname(x))).lower())
def types(f):return sorted([x for x in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType) if x.ViewFamily==f],key=lambda x:name(x).lower())
def temps(vt):return sorted([x for x in DB.FilteredElementCollector(doc).OfClass(DB.View) if x.IsTemplate and x.ViewType==vt],key=lambda x:x.Name.lower())
def bounds(s):
 pts=[];vals=[]
 for lp in s.GetBoundarySegments(DB.SpatialElementBoundaryOptions()) or []:
  for bs in lp:
   c=bs.GetCurve();a=c.GetEndPoint(0);b=c.GetEndPoint(1);pts.extend([a,b]);dx=b.X-a.X;dy=b.Y-a.Y;l=(dx*dx+dy*dy)**0.5
   if l>mm(100):vals.append((math.atan2(dy,dx),l))
 return pts,vals
def autoang(g):
 d=[];q=math.pi/2
 for s in g:
  for a,l in bounds(s)[1]:
   c=a-round(a/q)*q
   while c>math.pi/4:c-=q
   while c<-math.pi/4:c+=q
   d.append((c,l))
 if not d:return 0
 buckets={}
 for c,l in d:k=int(round(math.degrees(c)/2));buckets[k]=buckets.get(k,0)+l
 win=max(buckets,key=buckets.get);v=[x for x in d if abs(math.degrees(x[0])/2-win)<=1];t=sum(x[1] for x in v);return sum(x[0]*x[1] for x in v)/t
def cropdata(g,a,buf):
 pts=[]
 for s in g:pts+=bounds(s)[0]
 c=math.cos(a);q=math.sin(a);u=[p.X*c+p.Y*q for p in pts];v=[-p.X*q+p.Y*c for p in pts];u0,u1=min(u)-buf,max(u)+buf;v0,v1=min(v)-buf,max(v)+buf;uc=(u0+u1)/2;vc=(v0+v1)/2
 return DB.XYZ(uc*c-vc*q,uc*q+vc*c,pts[0].Z),u1-u0,v1-v0
def setcrop(v,c,w,h,a):
 z=v.GenLevel.Elevation;b=DB.BoundingBoxXYZ();b.Min=DB.XYZ(c.X-w/2,c.Y-h/2,z-mm(1000));b.Max=DB.XYZ(c.X+w/2,c.Y+h/2,z+mm(1000));v.CropBoxActive=True;v.CropBoxVisible=False;v.CropBox=b
 # Stage 3 keeps proven crop rotation omitted here only when near zero. Non-zero uses hidden crop diff.
 if abs(a)<1e-8:return
 doc.Regenerate();v.CropBoxVisible=False;doc.Regenerate();before=set(iv(x.Id) for x in DB.FilteredElementCollector(doc,v.Id));v.CropBoxVisible=True;doc.Regenerate();axis=DB.Line.CreateBound(DB.XYZ(c.X,c.Y,z-10),DB.XYZ(c.X,c.Y,z+10))
 for x in DB.FilteredElementCollector(doc,v.Id):
  if iv(x.Id) not in before:
   try:DB.ElementTransformUtils.RotateElement(doc,x.Id,axis,a);v.CropBoxVisible=False;return
   except:pass
def tbtypes():return list(DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_TitleBlocks).WhereElementIsElementType())
def label(x):
 try:return '{} : {}'.format(x.FamilyName,name(x))
 except:return name(x)
def viewport_types():
 result=[]
 for item in DB.FilteredElementCollector(doc).OfClass(DB.ElementType):
  try:
   if item.Category and iv(item.Category.Id)==int(DB.BuiltInCategory.OST_Viewports):result.append(item)
  except:pass
 return sorted(result,key=lambda x:name(x).lower())

VIEWPORT_TYPE_MAP={}

def make_sheet(base,number,tid):
 sh=DB.ViewSheet.Create(doc,tid);sh.SheetNumber=uniq_sheet(number);sh.Name=base;return sh
def sheet_area(sh,review,w,h,left_margin,right_margin,top_margin,bottom_margin,reserved_right):
 if review:
  x0,y0,x1,y1=0.0,0.0,mm(w),mm(h)
 else:
  boxes=[]
  for item in DB.FilteredElementCollector(doc,sh.Id).OfCategory(DB.BuiltInCategory.OST_TitleBlocks).WhereElementIsNotElementType():
   box=item.get_BoundingBox(sh)
   if box:boxes.append(box)
  if boxes:
   x0=min(b.Min.X for b in boxes);y0=min(b.Min.Y for b in boxes);x1=max(b.Max.X for b in boxes);y1=max(b.Max.Y for b in boxes)
  else:
   x0,y0,x1,y1=0.0,0.0,mm(841),mm(594)
 return (x0+left_margin,y0+bottom_margin,x1-right_margin-reserved_right,y1-top_margin)

def vp_size(vp):
 o=vp.GetBoxOutline();return o.MaximumPoint.X-o.MinimumPoint.X,o.MaximumPoint.Y-o.MinimumPoint.Y

def create_viewport_unmeasured(sheet,view,centre):
 if not DB.Viewport.CanAddViewToSheet(doc,sheet.Id,view.Id):return None
 vp=DB.Viewport.Create(doc,sheet.Id,view.Id,centre)
 chosen=VIEWPORT_TYPE_MAP.get(view.ViewType)
 if chosen:
  try:vp.ChangeTypeId(chosen.Id)
  except:pass
 return vp

def anchor_y(vp):
 """Use Revit's view anchor where exposed; fall back to box bottom."""
 try:
  return vp.GetPositionAtViewAnchor().Y
 except:
  try:return vp.GetBoxOutline().MinimumPoint.Y
  except:return vp.GetBoxCenter().Y

def row_width(row,hgap):
 return sum(item[1][0] for item in row)+hgap*max(0,len(row)-1)

def build_balanced_rows(measured,available_width,hgap):
 rows=[];current=[];used=0.0
 for item in measured:
  needed=item[1][0] if not current else hgap+item[1][0]
  if current and used+needed>available_width:
   rows.append(current);current=[];used=0.0;needed=item[1][0]
  current.append(item);used+=needed
 if current:rows.append(current)
 if len(rows)>=2 and len(rows[-2])>1:
  while len(rows[-2])>1:
   before=abs(row_width(rows[-2],hgap)-row_width(rows[-1],hgap));candidate=rows[-2][-1]
   proposed=[candidate]+rows[-1]
   after=abs(row_width(rows[-2][:-1],hgap)-row_width(proposed,hgap))
   if after>=before or row_width(proposed,hgap)>available_width:break
   rows[-2].pop();rows[-1].insert(0,candidate)
 return rows

def position_row(row,x0,x1,bottom,hgap,align_anchor):
 total=row_width(row,hgap);x=x0+max(0.0,(x1-x0-total)/2.0)
 target_anchor=None
 if align_anchor and row:
  # Keep anchor near the intended row bottom while making every viewport's
  # Revit view anchor exactly equal. This is the programmatic equivalent of
  # the manual alignment snap.
  target_anchor=anchor_y(row[0][0])+(bottom-row[0][0].GetBoxOutline().MinimumPoint.Y)
 for vp,size,view in row:
  w,h=size
  if target_anchor is None:
   cy=bottom+h/2.0
  else:
   cy=vp.GetBoxCenter().Y+(target_anchor-anchor_y(vp))
  vp.SetBoxCenter(DB.XYZ(x+w/2.0,cy,0));x+=w+hgap

def create_and_measure(sheet,views,centre):
 """Create every viewport first, then regenerate and measure once."""
 created=[];failed=[]
 for view in views:
  vp=create_viewport_unmeasured(sheet,view,centre)
  if vp:created.append((vp,view))
  else:failed.append(view)
 doc.Regenerate()
 return [(vp,vp_size(vp),view) for vp,view in created],failed

def pack_first_sheet(sheet,plans,elevations,area,hgap,vgap):
 x0,y0,x1,y1=area;centre=DB.XYZ((x0+x1)/2.0,(y0+y1)/2.0,0);placed=[]
 # Batch-create every candidate viewport on this sheet before the first regen.
 plan_items,plan_failed=create_and_measure(sheet,plans,centre)
 elev_items,elev_failed=create_and_measure(sheet,elevations,centre)
 remaining=plan_failed+elev_failed
 if plan_items:
  maxh=max(item[1][1] for item in plan_items);bottom=y1-maxh
  position_row(plan_items,x0,x1,bottom,hgap,False);placed.extend(item[0] for item in plan_items);elev_top=bottom-vgap
 else:elev_top=y1
 rows=build_balanced_rows(elev_items,x1-x0,hgap);y=elev_top
 for row in rows:
  rowh=max(item[1][1] for item in row)
  if y-rowh<y0:
   for vp,size,view in row:doc.Delete(vp.Id);remaining.append(view)
   continue
  position_row(row,x0,x1,y-rowh,hgap,True);placed.extend(item[0] for item in row);y-=rowh+vgap
 # A single final regeneration updates all movements on this sheet.
 doc.Regenerate()
 return placed,remaining

def pack_elevation_sheet(sheet,elevations,area,hgap,vgap):
 x0,y0,x1,y1=area;centre=DB.XYZ((x0+x1)/2.0,(y0+y1)/2.0,0)
 measured,remaining=create_and_measure(sheet,elevations,centre)
 rows=build_balanced_rows(measured,x1-x0,hgap);y=y1;placed=[]
 for row in rows:
  rowh=max(item[1][1] for item in row)
  if y-rowh<y0:
   for vp,size,view in row:doc.Delete(vp.Id);remaining.append(view)
   continue
  position_row(row,x0,x1,y-rowh,hgap,True);placed.extend(item[0] for item in row);y-=rowh+vgap
 doc.Regenerate()
 return placed,remaining

class Row(object):
 def __init__(self,s):self.Space=s;self.IsSelected=False;self.Level=lev(s).Name;self.Number=num(s);self.Name=sname(s)
class W(forms.WPFWindow):
 def __init__(self,x):
  forms.WPFWindow.__init__(self,x);self.cfg=script.get_config('space_plans_stage36');self.all=[Row(s) for s in SPS];self.rows=ObservableCollection[object]();self.show(self.all)
  self.make_floor.IsChecked=self.getbool('make_floor',True);self.make_rcp.IsChecked=self.getbool('make_rcp',True);self.make_elev.IsChecked=self.getbool('make_elev',True);self.make_sheets.IsChecked=self.getbool('make_sheets',True)
  self.scale.Text=str(self.get('scale','50'));self.buffer.Text=str(self.get('buffer','500'));self.escale.Text=str(self.get('escale','50'))
  self.elevtype.ItemsSource=[name(x) for x in ETS];self.pick(self.elevtype,self.get('etype',''))
  self.floor_template.ItemsSource=['<None>']+[x.Name for x in FTS];self.rcp_template.ItemsSource=['<None>']+[x.Name for x in RTS];self.elev_template.ItemsSource=['<None>']+[x.Name for x in EVTS];self.pick(self.floor_template,self.get('ft','<None>'));self.pick(self.rcp_template,self.get('rt','<None>'));self.pick(self.elev_template,self.get('evt','<None>'))
  vnames=['<Default>']+[name(x) for x in VPTS]
  self.plan_vptype.ItemsSource=vnames;self.rcp_vptype.ItemsSource=vnames;self.elev_vptype.ItemsSource=vnames;self.pick(self.plan_vptype,self.get('pvp','<Default>'));self.pick(self.rcp_vptype,self.get('rvp','<Default>'));self.pick(self.elev_vptype,self.get('evp','<Default>'))
  self.titleblock.ItemsSource=[label(x) for x in TBS];self.pick(self.titleblock,self.get('tb',''));self.sheetmode.ItemsSource=['Documentation','Review - No Title Block'];self.pick(self.sheetmode,self.get('mode','Documentation'))
  for n,d in [('minheight','2700'),('side','100'),('bottom','100'),('top','100'),('depth','400'),('merge','2500'),('duplicate','400'),('angtol','1.5'),('minwall','300'),('left_margin','20'),('right_margin','20'),('top_margin','20'),('bottom_margin','20'),('reserved_right','110'),('hgap','20'),('vgap','25'),('width','841'),('height','594'),('prefix','A-')]:getattr(self,n).Text=str(self.get(n,d))
  self.start.Text='7001';self.grouped.IsChecked=self.getbool('grouped',False);self.individual.IsChecked=not self.grouped.IsChecked;self.result=False
  self.options_changed(None,None)
 def get(self,n,d):
  try:return getattr(self.cfg,n)
  except:return d
 def getbool(self,n,d):
  v=self.get(n,d);return v if isinstance(v,bool) else str(v).lower() in ('true','1','yes')
 def pick(self,c,v):
  c.SelectedIndex=0 if c.Items.Count else -1
  for i,x in enumerate(c.ItemsSource):
   if str(x)==str(v):c.SelectedIndex=i;break
 def show(self,r):
  self.rows.Clear()
  for x in r:self.rows.Add(x)
  self.grid.ItemsSource=self.rows
 def search(self,sender,args):
  q=(self.searchbox.Text or '').lower();self.show([r for r in self.all if q in '{} {} {}'.format(r.Level,r.Number,r.Name).lower()])
 def clear(self,sender,args):self.searchbox.Text=''
 def allon(self,sender,args):
  for r in self.rows:r.IsSelected=True
  self.grid.Items.Refresh()
 def alloff(self,sender,args):
  for r in self.rows:r.IsSelected=False
  self.grid.Items.Refresh()
 def options_changed(self,sender,args):
  if not hasattr(self,'floor_panel'):return
  self.floor_panel.IsEnabled=bool(self.make_floor.IsChecked)
  self.rcp_panel.IsEnabled=bool(self.make_rcp.IsChecked)
  self.elevation_panel.IsEnabled=bool(self.make_elev.IsChecked)
  self.sheet_panel.IsEnabled=bool(self.make_sheets.IsChecked)
 def go(self,sender,args):
  selected=[r.Space for r in self.all if r.IsSelected]
  if not selected:return forms.alert('Select at least one Space.')
  self.mkf=bool(self.make_floor.IsChecked);self.mkr=bool(self.make_rcp.IsChecked);self.mke=bool(self.make_elev.IsChecked);self.mks=bool(self.make_sheets.IsChecked)
  if not (self.mkf or self.mkr or self.mke):return forms.alert('Select at least one view type to generate.')
  self.isgroup=bool(self.grouped.IsChecked)
  if self.isgroup and len(selected)<2:return forms.alert('Grouped mode requires at least two Spaces.')
  if self.isgroup and len(set(iv(x.LevelId) for x in selected))>1:return forms.alert('Grouped Spaces must share one Level.')
  if (self.mkf or self.mke) and not FPT:return forms.alert('A Floor Plan View Family Type is required for Floor Plans or elevation marker hosting.')
  if self.mkr and not CPT:return forms.alert('A Ceiling Plan View Family Type is required for RCP generation.')
  if self.mke and not ETS:return forms.alert('An Elevation View Family Type is required for elevation generation.')
  if self.mks and self.sheetmode.SelectedItem=='Documentation' and not TBS:return forms.alert('A loaded title block type is required for Documentation sheets.')
  try:
   self.pscale=int(self.scale.Text);self.buf=mm(float(self.buffer.Text));self.es=int(self.escale.Text);self.first=int(self.start.Text);self.cw=float(self.width.Text);self.ch=float(self.height.Text)
   self.leftft=mm(float(self.left_margin.Text));self.rightft=mm(float(self.right_margin.Text));self.topft=mm(float(self.top_margin.Text));self.bottomft=mm(float(self.bottom_margin.Text));self.reserveft=mm(float(self.reserved_right.Text));self.hgapft=mm(float(self.hgap.Text));self.vgapft=mm(float(self.vgap.Text))
   self.esettings={'view_scale':self.es,'min_elev_height_mm':float(self.minheight.Text),'offset_side_mm':float(self.side.Text),'offset_bottom_mm':float(self.bottom.Text),'offset_top_mm':float(self.top.Text),'view_depth_mm':float(self.depth.Text),'merge_gap_mm':float(self.merge.Text),'duplicate_wall_tol_mm':float(self.duplicate.Text),'angle_tol_deg':float(self.angtol.Text),'min_wall_mm':float(self.minwall.Text)}
  except:return forms.alert('All numerical settings must be valid.')
  self.sel=selected;self.review=self.sheetmode.SelectedItem=='Review - No Title Block'
  self.tid=DB.ElementId.InvalidElementId if (not self.mks or self.review) else TBS[self.titleblock.SelectedIndex].Id
  self.et=ETS[self.elevtype.SelectedIndex] if self.mke and self.elevtype.SelectedIndex>=0 else None
  self.ft=None if self.floor_template.SelectedIndex<=0 else FTS[self.floor_template.SelectedIndex-1]
  self.rt=None if self.rcp_template.SelectedIndex<=0 else RTS[self.rcp_template.SelectedIndex-1]
  self.evt=None if self.elev_template.SelectedIndex<=0 else EVTS[self.elev_template.SelectedIndex-1]
  self.groupname=(self.group_name.Text or '').strip() or ' + '.join(num(x) for x in selected);self.pref=self.prefix.Text
  def vt(combo):return None if combo.SelectedIndex<=0 else VPTS[combo.SelectedIndex-1]
  self.pvp=vt(self.plan_vptype);self.rvp=vt(self.rcp_vptype);self.evp=vt(self.elev_vptype)
  values={'make_floor':self.mkf,'make_rcp':self.mkr,'make_elev':self.mke,'make_sheets':self.mks,'scale':self.scale.Text,'buffer':self.buffer.Text,'escale':self.escale.Text,'etype':str(self.elevtype.SelectedItem),'ft':str(self.floor_template.SelectedItem),'rt':str(self.rcp_template.SelectedItem),'evt':str(self.elev_template.SelectedItem),'pvp':str(self.plan_vptype.SelectedItem),'rvp':str(self.rcp_vptype.SelectedItem),'evp':str(self.elev_vptype.SelectedItem),'tb':str(self.titleblock.SelectedItem),'mode':str(self.sheetmode.SelectedItem),'grouped':self.isgroup}
  for n in ['minheight','side','bottom','top','depth','merge','duplicate','angtol','minwall','left_margin','right_margin','top_margin','bottom_margin','reserved_right','hgap','vgap','width','height','prefix']:values[n]=getattr(self,n).Text
  for k,v in values.items():setattr(self.cfg,k,v)
  script.save_config();self.result=True;self.Close()
 def cancel(self,sender,args):self.Close()

SPS=coll_spaces();FPT=types(DB.ViewFamily.FloorPlan);CPT=types(DB.ViewFamily.CeilingPlan);ETS=types(DB.ViewFamily.Elevation);FTS=temps(DB.ViewType.FloorPlan);RTS=temps(DB.ViewType.CeilingPlan);EVTS=temps(DB.ViewType.Elevation);TBS=tbtypes();VPTS=viewport_types()
if not SPS:forms.alert('No placed MEP Spaces were found.',exitscript=True)
w=W(os.path.join(os.path.dirname(__file__),'ui.xaml'));w.ShowDialog()
if not w.result:script.exit()
VIEWPORT_TYPE_MAP[DB.ViewType.FloorPlan]=w.pvp;VIEWPORT_TYPE_MAP[DB.ViewType.CeilingPlan]=w.rvp;VIEWPORT_TYPE_MAP[DB.ViewType.Elevation]=w.evp
counts={'floor':0,'rcp':0,'elev':0,'sheet':0};errors=[]
def generate_group(group,base,index):
 a=autoang(group);c,ww,hh=cropdata(group,a,w.buf);levelid=group[0].LevelId;plans=[];host=None
 if w.mkf or w.mke:
  host=DB.ViewPlan.Create(doc,FPT[0].Id,levelid);host.Name=uniq_view(base+(' - Floor Plan' if w.mkf else ' - _Elevation Host'));host.Scale=w.pscale;setcrop(host,c,ww,hh,a)
  if w.mkf:
   if w.ft:host.ViewTemplateId=w.ft.Id
   plans.append(host);counts['floor']+=1
 if w.mkr:
  rcp=DB.ViewPlan.Create(doc,CPT[0].Id,levelid);rcp.Name=uniq_view(base+' - RCP');rcp.Scale=w.pscale;setcrop(rcp,c,ww,hh,a)
  if w.rt:rcp.ViewTemplateId=w.rt.Id
  plans.append(rcp);counts['rcp']+=1
 elevations=[]
 if w.mke:
  for space in group:
   result=generate_space_elevations(doc,host,space,w.et,w.evt,w.esettings,out);current=[item['view'] for item in result['created_views']];elevations.extend(current);counts['elev']+=len(current)
   for error in result['errors']:errors.append('{}: {}'.format(num(space),error))
 if not w.mks:
  out.print_md('### {}'.format(base));out.print_md('- Generated views only: Floor Plan {}; RCP {}; Elevations {}'.format(1 if w.mkf else 0,1 if w.mkr else 0,len(elevations)))
  return
 views_to_place=plans+elevations
 if not views_to_place:return
 remaining=elevations[:];number=('REVIEW-' if w.review else w.pref)+str(w.first+index*10);sheet=make_sheet(base+' - Room Layout',number,w.tid);counts['sheet']+=1;area=sheet_area(sheet,w.review,w.cw,w.ch,w.leftft,w.rightft,w.topft,w.bottomft,0 if w.review else w.reserveft);placed,remaining=pack_first_sheet(sheet,plans,remaining,area,w.hgapft,w.vgapft)
 if not placed and views_to_place:raise Exception('Selected views do not fit the selected usable area.')
 overflow_index=1
 while remaining:
  suffix=' - Elevations {:02d}'.format(overflow_index);number=('REVIEW-' if w.review else w.pref)+str(w.first+index*10+overflow_index);sheet=make_sheet(base+suffix,number,w.tid);counts['sheet']+=1;area=sheet_area(sheet,w.review,w.cw,w.ch,w.leftft,w.rightft,w.topft,w.bottomft,0 if w.review else w.reserveft);placed,remaining=pack_elevation_sheet(sheet,remaining,area,w.hgapft,w.vgapft)
  if not placed and remaining:raise Exception('No elevation fits the selected usable area.')
  overflow_index+=1
 out.print_md('### {}'.format(base));out.print_md('- Floor Plan: {}; RCP: {}; Elevations: {}; Sheets: {}'.format(1 if w.mkf else 0,1 if w.mkr else 0,len(elevations),overflow_index))

with revit.TransactionGroup('Space Plans Stage 3.6 View Options'):
 groups=[(w.sel,w.groupname)] if w.isgroup else [([s],'{} - {}'.format(num(s),sname(s))) for s in w.sel]
 for index,(group,base) in enumerate(groups):
  try:
   with revit.Transaction('Create {}'.format(base)):generate_group(group,base,index)
  except Exception as ex:errors.append('{}: {}'.format(base,ex));log.exception('Stage 3.6 failed '+base)
msg='Created {floor} Floor Plan(s), {rcp} RCP(s), {elev} Elevation(s), and {sheet} Sheet(s).'.format(**counts)
if errors:msg+='\n\nFailures:\n'+'\n'.join(errors[:15])
forms.alert(msg,title='Space Plans Stage 3.6')
