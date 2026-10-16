# -*- coding: utf-8 -*-
from __future__ import print_function
import os, json, clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')
clr.AddReference('System')
from System.Collections.ObjectModel import ObservableCollection
from System.Collections.Generic import List
from System.Windows import Visibility, MessageBox
from System.Windows.Controls import DataGridTextColumn, DataGridLength
from System.Windows.Data import Binding
from Autodesk.Revit.DB import FilteredElementCollector, BuiltInCategory, ElementId
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent
from pyrevit import revit, forms, script

SETTINGS_FILE = os.path.join(os.path.expanduser('~'), 'AppData', 'Roaming', 'pyRevit', 'ParameterBrowser.settings.json')
DEFAULT_FIELDS = ['Number', 'Name', 'Level', 'Department']
WINDOW = None

class RowVM(object):
    def __init__(self, eid, values, fields):
        self.ElementId = int(eid)
        self.SearchText = ' | '.join([values.get(f, '') for f in fields]).lower()
        for i, field in enumerate(fields):
            setattr(self, 'P' + str(i), values.get(field, ''))

class Request(object):
    NONE, REFRESH, SELECT, SHOW = range(4)
    def __init__(self):
        self.action = self.NONE
        self.ids = []

class Handler(IExternalEventHandler):
    def __init__(self, request): self.request = request
    def Execute(self, uiapp):
        global WINDOW
        if WINDOW is None: return
        try:
            uidoc = uiapp.ActiveUIDocument
            if self.request.action == Request.REFRESH:
                WINDOW.capture_model(uidoc.Document, uidoc)
                WINDOW.set_status('Model data refreshed')
            elif self.request.action in (Request.SELECT, Request.SHOW):
                ids = List[ElementId]()
                for value in self.request.ids:
                    ids.Add(ElementId(int(value)))
                uidoc.Selection.SetElementIds(ids)
                if self.request.action == Request.SHOW and ids.Count > 0:
                    uidoc.ShowElements(ids)
                WINDOW.set_status('{0} element(s) highlighted in Revit'.format(ids.Count))
        except Exception as ex:
            WINDOW.set_status('ERROR: ' + str(ex))
        finally:
            self.request.action = Request.NONE
    def GetName(self): return 'Parameter Browser Actions'

class Browser(forms.WPFWindow):
    def __init__(self, xaml, doc, uidoc, request, event, handler):
        self.initialising = True
        forms.WPFWindow.__init__(self, xaml)
        self.request = request
        self.event = event
        self.handler = handler
        self.datasets = {}
        self.parameter_names = {}
        self.selected_fields = list(DEFAULT_FIELDS)
        self._load_settings()
        for x in ['Spaces', 'Rooms']: self.cmbSource.Items.Add(x)
        for x in ['Whole project', 'Current view', 'Level']: self.cmbScope.Items.Add(x)
        self.cmbSource.SelectedItem = self.saved_source
        self.cmbScope.SelectedItem = self.saved_scope
        self.Closed += self.on_closed
        # IMPORTANT: initial model read happens synchronously while command API context is valid.
        self.capture_model(doc, uidoc)
        self.initialising = False
        self.rebuild_view()

    def _load_settings(self):
        self.saved_source, self.saved_scope = 'Spaces', 'Whole project'
        try:
            d = json.load(open(SETTINGS_FILE, 'r'))
            self.saved_source = d.get('source', self.saved_source)
            self.saved_scope = d.get('scope', self.saved_scope)
            self.selected_fields = d.get('fields', self.selected_fields)
        except Exception: pass
        if self.saved_source not in ['Spaces','Rooms']: self.saved_source='Spaces'
        if self.saved_scope not in ['Whole project','Current view','Level']: self.saved_scope='Whole project'

    def _save_settings(self):
        try:
            folder=os.path.dirname(SETTINGS_FILE)
            if not os.path.exists(folder): os.makedirs(folder)
            json.dump({'source':str(self.cmbSource.SelectedItem), 'scope':str(self.cmbScope.SelectedItem), 'fields':self.selected_fields}, open(SETTINGS_FILE,'w'), indent=2)
        except Exception: pass

    def capture_model(self, doc, uidoc):
        active_view_id = uidoc.ActiveView.Id
        configs = [('Spaces', BuiltInCategory.OST_MEPSpaces), ('Rooms', BuiltInCategory.OST_Rooms)]
        for label, bic in configs:
            all_elems = list(FilteredElementCollector(doc).OfCategory(bic).WhereElementIsNotElementType())
            try:
                visible_ids = set([x.Id.IntegerValue for x in FilteredElementCollector(doc, active_view_id).OfCategory(bic).WhereElementIsNotElementType()])
            except Exception:
                visible_ids = set()
            pnames = set(['Element Id','Category','Level'])
            for e in all_elems:
                for p in e.Parameters:
                    try:
                        if p.Definition and p.Definition.Name: pnames.add(p.Definition.Name)
                    except Exception: pass
            records=[]
            for e in all_elems:
                level_name=''
                try:
                    lev=doc.GetElement(e.LevelId)
                    if lev: level_name=lev.Name
                except Exception: pass
                records.append({'element':e, 'id':e.Id.IntegerValue, 'level':level_name, 'visible':e.Id.IntegerValue in visible_ids, 'doc':doc})
            self.datasets[label]=records
            self.parameter_names[label]=sorted(pnames, key=lambda x:x.lower())
        self.populate_levels()

    def populate_levels(self):
        old = str(self.cmbLevel.SelectedItem) if self.cmbLevel.SelectedItem else None
        self.cmbLevel.Items.Clear()
        source=str(self.cmbSource.SelectedItem or 'Spaces')
        levels=sorted(set([r['level'] for r in self.datasets.get(source,[]) if r['level']]))
        for x in levels: self.cmbLevel.Items.Add(x)
        if old in levels: self.cmbLevel.SelectedItem=old
        elif self.cmbLevel.Items.Count: self.cmbLevel.SelectedIndex=0
        self.update_level_visibility()

    def value(self, rec, name):
        e, doc = rec['element'], rec['doc']
        if name=='Element Id': return str(rec['id'])
        if name=='Category':
            try: return e.Category.Name
            except Exception: return ''
        if name=='Level': return rec['level']
        p=e.LookupParameter(name)
        if p is None: return ''
        try:
            x=p.AsValueString()
            if x is not None: return str(x)
        except Exception: pass
        try:
            st=str(p.StorageType)
            if 'String' in st: return p.AsString() or ''
            if 'Integer' in st: return str(p.AsInteger())
            if 'Double' in st: return str(p.AsDouble())
            if 'ElementId' in st:
                eid=p.AsElementId(); target=doc.GetElement(eid)
                return target.Name if target else str(eid.IntegerValue)
        except Exception: pass
        return ''

    def rebuild_view(self):
        source=str(self.cmbSource.SelectedItem or 'Spaces')
        available=self.parameter_names.get(source,[])
        self.selected_fields=[f for f in self.selected_fields if f in available]
        if not self.selected_fields:
            self.selected_fields=[f for f in DEFAULT_FIELDS if f in available]
        if not self.selected_fields: self.selected_fields=['Element Id']
        records=list(self.datasets.get(source,[]))
        scope=str(self.cmbScope.SelectedItem or 'Whole project')
        if scope=='Current view': records=[r for r in records if r['visible']]
        elif scope=='Level' and self.cmbLevel.SelectedItem:
            wanted=str(self.cmbLevel.SelectedItem); records=[r for r in records if r['level']==wanted]
        rows=[]
        for rec in records:
            vals={}
            for f in self.selected_fields: vals[f]=self.value(rec,f)
            rows.append(RowVM(rec['id'], vals, self.selected_fields))
        self.all_rows=rows
        self.grid.Columns.Clear()
        for i,f in enumerate(self.selected_fields):
            c=DataGridTextColumn(); c.Header=f; c.Binding=Binding('P'+str(i)); c.IsReadOnly=True; c.MinWidth=80.0; c.Width=DataGridLength(160.0)
            self.grid.Columns.Add(c)
        self.apply_search()
        self.set_status('{0} {1} loaded from model'.format(len(records), source.lower()))
        self._save_settings()

    def apply_search(self):
        text=(self.txtSearch.Text or '').strip().lower()
        tokens=[x for x in text.split() if x]
        rows=self.all_rows if not tokens else [r for r in self.all_rows if all(t in r.SearchText for t in tokens)]
        self.grid.ItemsSource=ObservableCollection[object](rows)
        self.lblCount.Text='{0} shown / {1} loaded'.format(len(rows),len(self.all_rows))

    def update_level_visibility(self):
        show=str(self.cmbScope.SelectedItem)=='Level'
        self.lblLevel.Visibility=Visibility.Visible if show else Visibility.Collapsed
        self.cmbLevel.Visibility=Visibility.Visible if show else Visibility.Collapsed

    def source_changed(self,sender,args):
        if self.initialising: return
        self.populate_levels(); self.rebuild_view()
    def scope_changed(self,sender,args):
        if self.initialising: return
        self.update_level_visibility(); self.rebuild_view()
    def level_changed(self,sender,args):
        if self.initialising: return
        if str(self.cmbScope.SelectedItem)=='Level': self.rebuild_view()
    def search_changed(self,sender,args):
        if not self.initialising and hasattr(self,'all_rows'): self.apply_search()
    def clear_clicked(self,sender,args): self.txtSearch.Text=''

    def choose_columns(self,sender,args):
        source=str(self.cmbSource.SelectedItem or 'Spaces')
        chosen=forms.SelectFromList.show(self.parameter_names.get(source,[]), title='Choose Parameters', multiselect=True, button_name='Apply Columns')
        if chosen is None: return
        if not chosen:
            MessageBox.Show('Select at least one parameter.'); return
        self.selected_fields=list(chosen); self.rebuild_view()

    def selected_ids(self): return [x.ElementId for x in self.grid.SelectedItems]
    def raise_action(self, action):
        ids=self.selected_ids()
        if not ids:
            self.set_status('Select one or more rows first'); return
        self.request.ids=ids; self.request.action=action
        result=self.event.Raise()
        self.set_status('Request sent to Revit: ' + str(result))
    def select_clicked(self,sender,args): self.raise_action(Request.SELECT)
    def show_clicked(self,sender,args): self.raise_action(Request.SHOW)
    def row_double_clicked(self,sender,args): self.raise_action(Request.SHOW)
    def refresh_clicked(self,sender,args):
        self.request.action=Request.REFRESH
        result=self.event.Raise(); self.set_status('Refresh sent to Revit: '+str(result))
    def close_clicked(self,sender,args): self.Close()
    def set_status(self,text): self.lblStatus.Text=text
    def on_closed(self,sender,args):
        global WINDOW
        self._save_settings()
        try: self.event.Dispose()
        except Exception: pass
        WINDOW=None

doc=revit.doc
uidoc=revit.uidoc
request=Request()
handler=Handler(request)
event=ExternalEvent.Create(handler)
WINDOW=Browser(script.get_bundle_file('UI.xaml'),doc,uidoc,request,event,handler)
WINDOW.Show()
