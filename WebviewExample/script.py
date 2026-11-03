# -*- coding: utf-8 -*-
"""Universal Project Data Scheduler for pyRevit 7 / WebView2.

Builds an interactive client-side schedule from the active project. The Python
side reads Revit once; WebView2 then handles columns, grouping, filters,
aggregations, presets, selection links, and CSV export without reopening Revit.
"""
from __future__ import print_function

import json
import math

from pyrevit import DB, revit, script

DOC = revit.doc
OUT = script.get_output()
OUT.set_title("Universal Project Data Scheduler")
OUT.set_width(1500)
OUT.set_height(900)

MAX_ELEMENTS = 50000
MAX_PARAMS_PER_ELEMENT = 300
EMPTY = ""


def safe_text(value):
    if value is None:
        return EMPTY
    try:
        return unicode(value)  # noqa: F821 - IronPython 2
    except NameError:
        return str(value)
    except Exception:
        try:
            return str(value)
        except Exception:
            return EMPTY


def id_value(eid):
    """Return ElementId as text so IronPython json never receives Int64/long."""
    try:
        return safe_text(eid.Value)
    except Exception:
        try:
            return safe_text(eid.IntegerValue)
        except Exception:
            return "-1"


def category_name(el):
    try:
        return safe_text(el.Category.Name)
    except Exception:
        return "<No category>"


def parameter_value(param):
    if param is None or not param.HasValue:
        return EMPTY
    try:
        shown = param.AsValueString()
        if shown:
            return safe_text(shown)
    except Exception:
        pass
    try:
        st = param.StorageType
        if st == DB.StorageType.String:
            return safe_text(param.AsString())
        if st == DB.StorageType.Integer:
            return int(param.AsInteger())
        if st == DB.StorageType.Double:
            value = param.AsDouble()
            return round(value, 8) if not math.isnan(value) else EMPTY
        if st == DB.StorageType.ElementId:
            eid = param.AsElementId()
            target = DOC.GetElement(eid)
            return safe_text(target.Name) if target else id_value(eid)
    except Exception:
        pass
    return EMPTY


def parameter_name(param):
    try:
        return safe_text(param.Definition.Name)
    except Exception:
        return EMPTY


def point_of(el):
    try:
        loc = el.Location
        if isinstance(loc, DB.LocationPoint):
            return loc.Point
        if isinstance(loc, DB.LocationCurve):
            return loc.Curve.Evaluate(0.5, True)
    except Exception:
        pass
    try:
        bb = el.get_BoundingBox(None)
        if bb:
            return (bb.Min + bb.Max) * 0.5
    except Exception:
        pass
    return None


def phase_for(el):
    try:
        p = el.get_Parameter(DB.BuiltInParameter.PHASE_CREATED)
        if p:
            return DOC.GetElement(p.AsElementId())
    except Exception:
        pass
    try:
        phases = list(DOC.Phases)
        return phases[-1] if phases else None
    except Exception:
        return None


def room_for(el):
    phase = phase_for(el)
    try:
        if isinstance(el, DB.FamilyInstance) and phase:
            room = el.Room[phase]
            if room:
                return room
    except Exception:
        pass
    pt = point_of(el)
    if pt:
        try:
            return DOC.GetRoomAtPoint(pt, phase) if phase else DOC.GetRoomAtPoint(pt)
        except Exception:
            try:
                return DOC.GetRoomAtPoint(pt)
            except Exception:
                pass
    return None


def space_for(el):
    phase = phase_for(el)
    try:
        if isinstance(el, DB.FamilyInstance) and phase:
            space = el.Space[phase]
            if space:
                return space
    except Exception:
        pass
    pt = point_of(el)
    if pt:
        try:
            return DOC.GetSpaceAtPoint(pt, phase) if phase else DOC.GetSpaceAtPoint(pt)
        except Exception:
            try:
                return DOC.GetSpaceAtPoint(pt)
            except Exception:
                pass
    return None


def spatial_values(prefix, spatial):
    values = {
        prefix + " :: Number": EMPTY,
        prefix + " :: Name": EMPTY,
        prefix + " :: Level": EMPTY,
        prefix + " :: Element Id": EMPTY,
    }
    if not spatial:
        return values
    values[prefix + " :: Element Id"] = id_value(spatial.Id)
    try:
        values[prefix + " :: Number"] = safe_text(spatial.Number)
    except Exception:
        pass
    try:
        values[prefix + " :: Name"] = safe_text(spatial.Name)
    except Exception:
        pass
    try:
        level = DOC.GetElement(spatial.LevelId)
        values[prefix + " :: Level"] = safe_text(level.Name) if level else EMPTY
    except Exception:
        pass
    return values


def collect_parameters(el):
    result = {}
    count = 0
    try:
        for p in el.Parameters:
            if count >= MAX_PARAMS_PER_ELEMENT:
                break
            name = parameter_name(p)
            if not name:
                continue
            result["Instance :: " + name] = parameter_value(p)
            count += 1
    except Exception:
        pass
    try:
        typ = DOC.GetElement(el.GetTypeId())
        if typ:
            result["Identity :: Type Name"] = safe_text(typ.Name)
            for p in typ.Parameters:
                if count >= MAX_PARAMS_PER_ELEMENT:
                    break
                name = parameter_name(p)
                if not name:
                    continue
                key = "Type :: " + name
                if key not in result:
                    result[key] = parameter_value(p)
                count += 1
    except Exception:
        pass
    return result


def record_for(el):
    data = {
        "Identity :: Element Id": id_value(el.Id),
        "Identity :: Category": category_name(el),
        "Identity :: Class": safe_text(el.GetType().Name),
        "Identity :: Family": EMPTY,
        "Identity :: Type Name": EMPTY,
        "Identity :: Workset": EMPTY,
        "Identity :: Level": EMPTY,
    }
    try:
        data["Identity :: Family"] = safe_text(el.Symbol.Family.Name)
        data["Identity :: Type Name"] = safe_text(el.Symbol.Name)
    except Exception:
        pass
    try:
        data["Identity :: Workset"] = safe_text(DOC.GetWorksetTable().GetWorkset(el.WorksetId).Name)
    except Exception:
        pass
    try:
        level = DOC.GetElement(el.LevelId)
        data["Identity :: Level"] = safe_text(level.Name) if level else EMPTY
    except Exception:
        pass
    data.update(collect_parameters(el))
    data.update(spatial_values("Room", room_for(el)))
    data.update(spatial_values("Space", space_for(el)))
    return {"id": id_value(el.Id), "category": category_name(el), "values": data}


collector = DB.FilteredElementCollector(DOC).WhereElementIsNotElementType()
records = []
for el in collector:
    try:
        if el.Category is None:
            continue
        records.append(record_for(el))
        if len(records) >= MAX_ELEMENTS:
            break
    except Exception:
        continue

payload = {
    "document": safe_text(DOC.Title),
    "records": records,
    "truncated": len(records) >= MAX_ELEMENTS,
    "limit": MAX_ELEMENTS,
}
def json_default(value):
    """Final safeguard for CLR numeric/string objects under IronPython."""
    try:
        return int(value)
    except Exception:
        return safe_text(value)


json_payload = json.dumps(
    payload,
    ensure_ascii=False,
    default=json_default
).replace("</", "<\\/")

OUT.print_html(r'''
<div id="uds-app"></div>
<script id="uds-data" type="application/json">''' + json_payload + r'''</script>
''')

OUT.add_style(r'''
:root{--bg:#f4f6f8;--panel:#fff;--line:#dfe4ea;--text:#17202a;--muted:#657180;--blue:#2166d1;--blue2:#e9f1ff;--danger:#b42318;--shadow:0 1px 3px rgba(16,24,40,.08)}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font:13px "Segoe UI",Arial,sans-serif}.uds{min-height:100vh}.top{position:sticky;top:0;z-index:8;display:flex;align-items:center;justify-content:space-between;padding:14px 20px;background:#14213d;color:#fff}.brand{font-size:18px;font-weight:700}.sub{font-size:11px;color:#bec8d8;margin-top:3px}.toolbar{display:flex;gap:8px}.btn{border:1px solid var(--line);border-radius:7px;background:#fff;color:#253044;padding:7px 11px;font-weight:600;cursor:pointer}.btn:hover{border-color:#9db8e8}.btn.primary{background:var(--blue);border-color:var(--blue);color:#fff}.layout{display:grid;grid-template-columns:340px minmax(0,1fr);gap:14px;padding:14px}.sidebar{display:flex;flex-direction:column;gap:12px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow)}.ph{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;border-bottom:1px solid var(--line);font-weight:700}.pb{padding:12px}.search,.select,.text{width:100%;border:1px solid #cfd6df;border-radius:7px;padding:8px;background:#fff}.checklist{max-height:210px;overflow:auto;margin-top:8px}.check{display:flex;gap:8px;align-items:center;padding:5px 2px}.section-title{margin:12px 0 6px;font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}.chips{display:flex;flex-wrap:wrap;gap:5px;min-height:34px;padding:6px;border:1px dashed #cfd6df;border-radius:7px}.chip{display:inline-flex;align-items:center;gap:6px;max-width:100%;padding:5px 7px;border-radius:6px;background:var(--blue2);color:#174c9c}.chip button{border:0;background:transparent;cursor:pointer;color:#174c9c}.field-list{max-height:250px;overflow:auto;border:1px solid var(--line);border-radius:7px}.field{padding:6px 8px;border-bottom:1px solid #edf0f3;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.field:hover{background:#f3f7fd}.field.selected{background:#dceaff;font-weight:650}.field-actions{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:8px 0}.preset-backdrop{position:fixed;inset:0;z-index:30;background:rgba(10,20,35,.45);display:flex;align-items:center;justify-content:center}.preset-modal{width:480px;max-height:75vh;background:#fff;border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,.25);overflow:hidden}.preset-body{padding:14px;max-height:55vh;overflow:auto}.preset-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px;border:1px solid var(--line);border-radius:8px;margin-bottom:7px}.preset-name{font-weight:650}.modal-actions{display:flex;justify-content:flex-end;gap:7px;padding:12px 14px;border-top:1px solid var(--line)}.main{min-width:0}.status{display:flex;gap:12px;align-items:center;padding:10px 12px;color:var(--muted)}.tablewrap{overflow:auto;max-height:calc(100vh - 160px)}table{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%}th,td{padding:8px 10px;border-right:1px solid #e5e9ee;border-bottom:1px solid #e5e9ee;white-space:nowrap;max-width:360px;overflow:hidden;text-overflow:ellipsis}th{position:sticky;top:0;z-index:3;background:#eef3f9;text-align:left;font-size:11px}tbody tr:hover{background:#f6f9fd}.num{text-align:right;font-variant-numeric:tabular-nums}.rowlink{color:var(--blue);text-decoration:none;font-weight:600}.empty{padding:60px;text-align:center;color:var(--muted)}.filter-row{display:grid;grid-template-columns:1fr 90px 1fr 30px;gap:6px;margin-bottom:6px}.notice{margin:12px;padding:9px;border-radius:7px;background:#fff6e5;color:#7a4b00}@media(max-width:980px){.layout{grid-template-columns:1fr}.tablewrap{max-height:650px}}
''')

OUT.inject_script(r'''
(function(){
const DATA=JSON.parse(document.getElementById('uds-data').textContent);const S={categories:[],columns:['Identity :: Category','Identity :: Family','Identity :: Type Name','Space :: Number','Space :: Name'],groups:['Space :: Number','Space :: Name','Identity :: Category','Identity :: Family','Identity :: Type Name'],filters:[],aggregate:true,search:'',selectedField:''};
const app=document.getElementById('uds-app');const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const allFields=[...new Set(DATA.records.flatMap(r=>Object.keys(r.values)))].sort((a,b)=>a.localeCompare(b));const cats=[...new Set(DATA.records.map(r=>r.category))].sort();
function shell(){app.innerHTML=`<div class="uds"><div class="top"><div><div class="brand">Universal Project Data Scheduler</div><div class="sub">${esc(DATA.document)} · ${DATA.records.length.toLocaleString()} elements loaded</div></div><div class="toolbar"><button class="btn" id="savePreset">Save preset</button><button class="btn" id="loadPreset">Load preset</button><button class="btn" id="csv">Export CSV</button><button class="btn primary" id="refresh">Rebuild table</button></div></div>${DATA.truncated?`<div class="notice">The project exceeded the safety limit. Only the first ${DATA.limit.toLocaleString()} elements were loaded.</div>`:''}<div class="layout"><aside class="sidebar"><div class="panel"><div class="ph">1. Source categories</div><div class="pb"><input class="search" id="catSearch" placeholder="Search categories"><div class="checklist" id="cats"></div></div></div><div class="panel"><div class="ph">2. Fields</div><div class="pb"><input class="search" id="fieldSearch" placeholder="Search instance, type, room or space fields"><div class="section-title">Columns</div><div class="chips" id="columns"></div><div class="section-title">Group by</div><div class="chips" id="groups"></div><div class="field-actions"><button class="btn" id="addColumn">Add selected as column</button><button class="btn" id="addGroup">Add selected as group</button></div><label class="check"><input type="checkbox" id="aggregate" checked> Aggregate matching rows and show quantity</label><div class="field-list" id="fields"></div></div></div><div class="panel"><div class="ph">3. Filters <button class="btn" id="addFilter">Add</button></div><div class="pb" id="filters"></div></div></aside><main class="main panel"><div class="status"><input class="search" id="resultSearch" placeholder="Search results"><span id="count"></span></div><div class="tablewrap" id="table"></div></main></div></div>`;bind();renderCats();renderFields();renderChips();renderFilters();build();}
function bind(){document.getElementById('catSearch').oninput=renderCats;document.getElementById('fieldSearch').oninput=renderFields;document.getElementById('resultSearch').oninput=e=>{S.search=e.target.value.toLowerCase();build()};document.getElementById('aggregate').onchange=e=>{S.aggregate=e.target.checked;build()};document.getElementById('refresh').onclick=build;document.getElementById('addFilter').onclick=()=>{S.filters.push({field:allFields[0],op:'contains',value:''});renderFilters()};document.getElementById('csv').onclick=exportCSV;document.getElementById('savePreset').onclick=savePreset;document.getElementById('loadPreset').onclick=loadPreset;document.getElementById('addColumn').onclick=()=>addSelected(S.columns);document.getElementById('addGroup').onclick=()=>addSelected(S.groups);}
function renderCats(){const q=document.getElementById('catSearch').value.toLowerCase();document.getElementById('cats').innerHTML=cats.filter(x=>x.toLowerCase().includes(q)).map(c=>`<label class="check"><input type="checkbox" data-cat="${esc(c)}" ${S.categories.includes(c)?'checked':''}>${esc(c)}</label>`).join('');document.querySelectorAll('[data-cat]').forEach(x=>x.onchange=()=>{x.checked?S.categories.push(x.dataset.cat):S.categories=S.categories.filter(c=>c!==x.dataset.cat);build()})}
function addSelected(list){if(!S.selectedField){alert('Select a field first.');return}if(!list.includes(S.selectedField))list.push(S.selectedField);renderChips();build()}
function renderFields(){const q=document.getElementById('fieldSearch').value.toLowerCase();document.getElementById('fields').innerHTML=allFields.filter(f=>f.toLowerCase().includes(q)).map(f=>`<div class="field ${S.selectedField===f?'selected':''}" title="Select field. Double-click adds a column; Shift+double-click adds a group." data-field="${esc(f)}">${esc(f)}</div>`).join('');document.querySelectorAll('[data-field]').forEach(x=>{x.onclick=()=>{S.selectedField=x.dataset.field;renderFields()};x.ondblclick=e=>{S.selectedField=x.dataset.field;addSelected(e.shiftKey?S.groups:S.columns)}})}
function chips(id,list){document.getElementById(id).innerHTML=list.map((f,i)=>`<span class="chip" title="${esc(f)}">${esc(f)}<button data-list="${id}" data-i="${i}">×</button></span>`).join('')||'<span style="color:#8a94a3">None selected</span>'}
function renderChips(){chips('columns',S.columns);chips('groups',S.groups);document.querySelectorAll('.chip button').forEach(b=>b.onclick=()=>{(b.dataset.list==='columns'?S.columns:S.groups).splice(+b.dataset.i,1);renderChips();build()})}
function renderFilters(){const ops=['contains','equals','not equals','starts with','is blank','is not blank','greater than','less than'];document.getElementById('filters').innerHTML=S.filters.map((f,i)=>`<div class="filter-row"><select class="select" data-ff="${i}">${allFields.map(x=>`<option ${x===f.field?'selected':''}>${esc(x)}</option>`).join('')}</select><select class="select" data-fo="${i}">${ops.map(x=>`<option ${x===f.op?'selected':''}>${x}</option>`).join('')}</select><input class="text" data-fv="${i}" value="${esc(f.value)}"><button class="btn" data-fd="${i}">×</button></div>`).join('')||'<span style="color:#8a94a3">No filters</span>';document.querySelectorAll('[data-ff]').forEach(x=>x.onchange=()=>{S.filters[+x.dataset.ff].field=x.value;build()});document.querySelectorAll('[data-fo]').forEach(x=>x.onchange=()=>{S.filters[+x.dataset.fo].op=x.value;build()});document.querySelectorAll('[data-fv]').forEach(x=>x.oninput=()=>{S.filters[+x.dataset.fv].value=x.value;build()});document.querySelectorAll('[data-fd]').forEach(x=>x.onclick=()=>{S.filters.splice(+x.dataset.fd,1);renderFilters();build()})}
function match(v,op,t){v=String(v??'');t=String(t??'');const a=v.toLowerCase(),b=t.toLowerCase();if(op==='contains')return a.includes(b);if(op==='equals')return a===b;if(op==='not equals')return a!==b;if(op==='starts with')return a.startsWith(b);if(op==='is blank')return !a.trim();if(op==='is not blank')return !!a.trim();if(op==='greater than')return Number(v)>Number(t);if(op==='less than')return Number(v)<Number(t);return true}
function source(){return DATA.records.filter(r=>(!S.categories.length||S.categories.includes(r.category))&&S.filters.every(f=>match(r.values[f.field],f.op,f.value)))}
function rows(){const src=source();if(!S.aggregate)return src.map(r=>({values:r.values,ids:[r.id],qty:1}));const fields=S.groups.length?S.groups:S.columns;const map=new Map();src.forEach(r=>{const key=JSON.stringify(fields.map(f=>r.values[f]??''));if(!map.has(key))map.set(key,{values:r.values,ids:[],qty:0});const g=map.get(key);g.ids.push(r.id);g.qty++});return [...map.values()]}
function link(ids,label,show){if(!ids.length)return '';let parts=ids.map(id=>'element[]='+encodeURIComponent(id));let url='revit://outputhelpers?&command=select&'+parts.join('&')+'&show='+(show?'true':'false');if(url.length>=1950)return `<span title="Too many elements for one Revit link">${ids.length} elements</span>`;return `<a class="rowlink" href="${url}">${esc(label)}</a>`}
function build(){let rr=rows();const cols=[...(S.aggregate?['__qty']:[]),...S.columns.filter((x,i,a)=>a.indexOf(x)===i),'__actions'];if(S.search)rr=rr.filter(r=>cols.some(c=>c==='__qty'?String(r.qty).includes(S.search):String(r.values[c]??'').toLowerCase().includes(S.search)));document.getElementById('count').textContent=`${rr.length.toLocaleString()} rows`;if(!cols.length){document.getElementById('table').innerHTML='<div class="empty">Choose at least one field.</div>';return}let h='<table><thead><tr>'+cols.map(c=>`<th>${c==='__qty'?'Quantity':c==='__actions'?'Revit':esc(c)}</th>`).join('')+'</tr></thead><tbody>';h+=rr.slice(0,5000).map(r=>'<tr>'+cols.map(c=>{if(c==='__qty')return `<td class="num">${r.qty}</td>`;if(c==='__actions')return `<td>${link(r.ids,'Select',false)} · ${link(r.ids,'Show',true)}</td>`;return `<td title="${esc(r.values[c]??'')}">${esc(r.values[c]??'')}</td>`}).join('')+'</tr>').join('');h+='</tbody></table>';if(rr.length>5000)h+='<div class="notice">Preview limited to 5,000 rows. CSV export includes all rows.</div>';document.getElementById('table').innerHTML=h;window.__udsRows=rr;window.__udsCols=cols}
function exportCSV(){const rr=window.__udsRows||rows(),cols=(window.__udsCols||[]).filter(c=>c!=='__actions');const q=v=>'"'+String(v??'').replace(/"/g,'""')+'"';const lines=[cols.map(c=>q(c==='__qty'?'Quantity':c)).join(',')];rr.forEach(r=>lines.push(cols.map(c=>q(c==='__qty'?r.qty:r.values[c])).join(',')));const blob=new Blob(['\ufeff'+lines.join('\r\n')],{type:'text/csv;charset=utf-8'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=(DATA.document||'Revit')+'_schedule.csv';a.click();URL.revokeObjectURL(a.href)}
function savePreset(){const name=prompt('Preset name');if(!name)return;localStorage.setItem('uds:'+name,JSON.stringify(S));alert('Preset saved: '+name)}
function closePreset(){const x=document.getElementById('presetModal');if(x)x.remove()}
function applyPreset(name){const raw=localStorage.getItem('uds:'+name);if(!raw)return;Object.assign(S,JSON.parse(raw));closePreset();shell()}
function deletePreset(name){if(!confirm('Delete preset '+name+'?'))return;localStorage.removeItem('uds:'+name);loadPreset()}
function loadPreset(){closePreset();const names=Object.keys(localStorage).filter(k=>k.startsWith('uds:')).map(k=>k.slice(4)).sort();if(!names.length){alert('No saved presets.');return}const modal=document.createElement('div');modal.id='presetModal';modal.className='preset-backdrop';modal.innerHTML=`<div class="preset-modal"><div class="ph">Load preset</div><div class="preset-body">${names.map(n=>`<div class="preset-row"><span class="preset-name">${esc(n)}</span><span><button class="btn primary" data-pl="${esc(n)}">Load</button> <button class="btn" data-pd="${esc(n)}">Delete</button></span></div>`).join('')}</div><div class="modal-actions"><button class="btn" id="presetClose">Close</button></div></div>`;document.body.appendChild(modal);document.getElementById('presetClose').onclick=closePreset;document.querySelectorAll('[data-pl]').forEach(b=>b.onclick=()=>applyPreset(b.dataset.pl));document.querySelectorAll('[data-pd]').forEach(b=>b.onclick=()=>deletePreset(b.dataset.pd));modal.onclick=e=>{if(e.target===modal)closePreset()}}
shell();
})();
''')
