/* Parameter Browser WebView - runs inside the pyRevit 7 output window (WebView2 / Chromium).
 * script.py defines window.PBWV_DATA immediately before this code. All filtering, sorting and
 * tree building happens here on that cached data; Revit is only reached through pyRevit's
 * revit://outputhelpers links (select/show elements, re-run this button to refresh).
 */
(function (root) {
  'use strict';

  var SETTINGS_KEY = 'pyrevit.parameterBrowserWebView.settings.v1';
  var HANDOFF_KEY = 'pyrevit.parameterBrowserWebView.refresh.v1';
  var HANDOFF_MAX_AGE_MS = 10 * 60 * 1000;
  // pyRevit's own links stop at 2000 chars (an old IE limit). Chromium allows ~2 MB; stay well below.
  var MAX_URL_LENGTH = 30000;
  var DEFAULT_COL_WIDTH = 160;
  var MIN_COL_WIDTH = 60;
  var DEFAULT_PANEL_WIDTH = 380;

  // ---------------------------------------------------------------------------------------
  // Pure helpers (no DOM). Mirrors lib/parameter_browser_logic.py; tested with node.
  // ---------------------------------------------------------------------------------------
  function text(value) { return value === null || value === undefined ? '' : String(value); }
  function lower(value) { return text(value).toLowerCase(); }
  function byName(a, b) { a = lower(a); b = lower(b); return a < b ? -1 : (a > b ? 1 : 0); }
  // Exact ordering for non-negative integer id strings (no float rounding of 64-bit ids).
  function compareIds(a, b) { a = text(a); b = text(b); return (a.length - b.length) || (a < b ? -1 : (a > b ? 1 : 0)); }

  function resolveFields(selected, available, defaults) {
    var avail = {};
    (available || []).forEach(function (f) { avail[f] = true; });
    var out = [];
    (selected || []).forEach(function (f) { if (avail[f] && out.indexOf(f) < 0) { out.push(f); } });
    if (!out.length) { out = (defaults || []).filter(function (f) { return avail[f]; }); }
    return out.length ? out : ['Element Id'];
  }

  function levelOptions(records) {
    var seen = {};
    var out = [];
    (records || []).forEach(function (r) {
      if (r.level && !seen[r.level]) { seen[r.level] = true; out.push(r.level); }
    });
    return out.sort(byName);
  }

  function filterRecords(records, scope, level) {
    records = records || [];
    if (scope === 'Current view') { return records.filter(function (r) { return r.visible; }); }
    if (scope === 'Level') {
      if (!level) { return []; }
      return records.filter(function (r) { return r.level === level; });
    }
    return records.slice();
  }

  function isCountField(field, prefix) { return text(field).indexOf(prefix) === 0; }

  function recordValue(rec, field, prefix) {
    if (field === 'Element Id') { return text(rec.id); }
    if (field === 'Category') { return text(rec.category); }
    if (field === 'Level') { return text(rec.level); }
    if (isCountField(field, prefix)) { return (rec.counts || {})[field.slice(prefix.length)] || 0; }
    return text((rec.values || {})[field]);
  }

  function searchTokens(value) { return lower(value).split(/\s+/).filter(function (t) { return t; }); }

  // Every token must appear in one visible column (matches the WPF row filter).
  function matchesTokens(values, tokens) {
    if (!tokens.length) { return true; }
    var haystack = values.map(lower).join(' | ');
    return tokens.every(function (t) { return haystack.indexOf(t) >= 0; });
  }

  var collator = (typeof Intl !== 'undefined' && Intl.Collator)
    ? new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' }) : null;

  function compareValues(a, b, numeric) {
    if (numeric) {
      var na = Number(a), nb = Number(b);
      if (!isNaN(na) && !isNaN(nb) && na !== nb) { return na - nb; }
      return compareIds(a, b);
    }
    a = text(a); b = text(b);
    if (a === b) { return 0; }
    if (!a) { return 1; }
    if (!b) { return -1; }
    return collator ? collator.compare(a, b) : byName(a, b);
  }

  function itemLabel(item) {
    var parts = [];
    if (item.mark) { parts.push('Mark ' + item.mark); }
    if (item.level) { parts.push(item.level); }
    parts.push('Id ' + item.id);
    return parts.join(' \u00b7 ');
  }

  // [{name, ids, types: [{name, ids, items: [{label, id}]}]}] in category order, then name order.
  function groupContents(items, categories) {
    var byCat = {};
    (items || []).forEach(function (item) {
      if (categories.indexOf(item.category) >= 0) { (byCat[item.category] = byCat[item.category] || []).push(item); }
    });
    var result = [];
    categories.forEach(function (category) {
      var catItems = byCat[category];
      if (!catItems || !catItems.length) { return; }
      var byType = {};
      catItems.forEach(function (item) {
        var name = (item.family || '?') + ' : ' + (item.type || '?');
        (byType[name] = byType[name] || []).push(item);
      });
      var catIds = [];
      var types = Object.keys(byType).sort(byName).map(function (name) {
        var members = byType[name].slice().sort(function (a, b) {
          return byName(a.mark, b.mark) || compareIds(a.id, b.id);
        });
        var ids = members.map(function (i) { return i.id; });
        catIds = catIds.concat(ids);
        return { name: name, ids: ids, items: members.map(function (i) { return { label: itemLabel(i), id: i.id }; }) };
      });
      result.push({ name: category, ids: catIds, types: types });
    });
    return result;
  }

  // pyRevit splits element[] on commas (ScriptConsoleUtils.ProcessUrl), which keeps URLs short.
  function selectUrl(ids, show) {
    return 'revit://outputhelpers?command=select&element[]=' + ids.join(',') + '&show=' + (show ? 'true' : 'false');
  }

  function refreshUrl(controlId) {
    return 'revit://outputhelpers?command=button&controlid=' + encodeURIComponent(controlId);
  }

  function delimited(header, rows, sep) {
    function cell(v) {
      v = text(v);
      if (sep === '\t') { return v.replace(/[\t\r\n]+/g, ' '); }
      return /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
    }
    return [header].concat(rows).map(function (r) { return r.map(cell).join(sep); }).join('\r\n');
  }

  function normalizeSettings(saved, seed, defaults) {
    var src = (saved && typeof saved === 'object') ? saved : {};
    function pick(value, options, fallback) { return options.indexOf(value) >= 0 ? value : fallback; }
    var fields = Array.isArray(src.fields)
      ? src.fields.map(text).filter(function (f) { return f.trim(); }) : [];
    var cats = Array.isArray(src.content_categories) ? src.content_categories : seed.content_categories;
    var widths = {};
    if (src.colWidths && typeof src.colWidths === 'object') {
      Object.keys(src.colWidths).forEach(function (k) {
        var w = Number(src.colWidths[k]);
        if (w >= MIN_COL_WIDTH && w <= 2000) { widths[k] = Math.round(w); }
      });
    }
    var panel = Number(src.panelWidth);
    return {
      source: pick(src.source, defaults.sources, seed.source),
      scope: pick(src.scope, defaults.scopes, seed.scope),
      level: text(src.level),
      fields: fields.length ? fields : seed.fields.slice(),
      categories: defaults.categories.filter(function (c) { return (cats || []).indexOf(c) >= 0; }),
      sortField: text(src.sortField),
      sortDir: src.sortDir === -1 ? -1 : 1,
      colWidths: widths,
      panelWidth: panel >= 220 && panel <= 1200 ? Math.round(panel) : DEFAULT_PANEL_WIDTH
    };
  }

  var core = {
    resolveFields: resolveFields, levelOptions: levelOptions, filterRecords: filterRecords,
    isCountField: isCountField, recordValue: recordValue, searchTokens: searchTokens,
    matchesTokens: matchesTokens, compareValues: compareValues, compareIds: compareIds,
    itemLabel: itemLabel, groupContents: groupContents, selectUrl: selectUrl, refreshUrl: refreshUrl,
    delimited: delimited, normalizeSettings: normalizeSettings, MAX_URL_LENGTH: MAX_URL_LENGTH
  };
  root.PBWVCore = core;

  if (typeof document === 'undefined' || root.__pbwvStarted) { return; }
  root.__pbwvStarted = true;

  // ---------------------------------------------------------------------------------------
  // UI
  // ---------------------------------------------------------------------------------------
  var DATA = root.PBWV_DATA;
  var mount = document.getElementById('pbwv-app');
  if (!mount) {
    mount = document.createElement('div');
    mount.id = 'pbwv-app';
    document.body.appendChild(mount);
  }
  document.body.classList.add('pbwv-body');
  if (!DATA || !DATA.sources) {
    mount.innerHTML = '<div class="pb-fatal">Parameter Browser data did not load. Run the button again.</div>';
    return;
  }

  var D = DATA.defaults;
  var PREFIX = D.count_prefix;
  var storage = (function () {
    try { var s = root.localStorage; s.getItem(SETTINGS_KEY); return s; } catch (e) { return null; }
  })();

  function loadJson(key) {
    if (!storage) { return null; }
    try { return JSON.parse(storage.getItem(key) || 'null'); } catch (e) { return null; }
  }

  var S = normalizeSettings(loadJson(SETTINGS_KEY), DATA.settings, D);
  var selected = {};          // id -> true for the current source
  var anchorId = null;        // shift-click range start
  var focusId = null;         // keyboard cursor
  var unassigned = false;
  var treeSel = null;         // highlighted tree node key
  var treeIds = {};           // node key -> ids
  var treeOpen = {};          // node key -> expanded override
  var listedIds = [];         // every id listed in the contents tree
  var view = { fields: [], rows: [], scoped: 0 };
  var suppressHeaderClick = false;
  var dragField = null;

  var contentIndex = {};
  D.sources.forEach(function (source) {
    var key = source === 'Spaces' ? 'space' : 'room';
    var index = {};
    DATA.contents.forEach(function (item) {
      var k = item[key] === null || item[key] === undefined ? '' : item[key];
      (index[k] = index[k] || []).push(item);
    });
    contentIndex[source] = index;
  });

  function save() {
    if (!storage) { return; }
    try {
      storage.setItem(SETTINGS_KEY, JSON.stringify({
        source: S.source, scope: S.scope, level: S.level, fields: S.fields,
        content_categories: S.categories, sortField: S.sortField, sortDir: S.sortDir,
        colWidths: S.colWidths, panelWidth: S.panelWidth
      }));
    } catch (e) { /* storage full or disabled: settings are just not kept */ }
  }

  function esc(value) {
    return text(value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function $(id) { return document.getElementById(id); }
  function records() { return (DATA.sources[S.source] || {}).records || []; }
  function sourceFields() { return (DATA.sources[S.source] || {}).fields || []; }
  function currentFields() { return resolveFields(S.fields, sourceFields(), D.fields); }
  function noun() { return S.source === 'Spaces' ? 'space' : 'room'; }
  function selectedIds() {
    return view.rows.filter(function (r) { return selected[r.rec.id]; }).map(function (r) { return r.rec.id; });
  }

  mount.innerHTML =
    '<div class="pb">' +
    '<header class="pb-top">' +
    '  <div class="pb-brand"><div class="pb-title">Parameter Browser</div>' +
    '    <div class="pb-sub" id="pbMeta"></div></div>' +
    '  <div class="pb-actions">' +
    '    <button class="pb-btn" id="pbCopy" title="Copy the shown rows and columns (paste into Excel)">Copy</button>' +
    '    <button class="pb-btn" id="pbCsv" title="Download the shown rows and columns as CSV">Export CSV</button>' +
    '    <button class="pb-btn pb-primary" id="pbRefresh" title="Re-read Spaces/Rooms and their elements from the active project and view">Refresh model</button>' +
    '  </div>' +
    '</header>' +
    '<div class="pb-toolbar">' +
    '  <label>Source <select id="pbSource"></select></label>' +
    '  <label>Scope <select id="pbScope"></select></label>' +
    '  <label id="pbLevelWrap">Level <select id="pbLevel"></select></label>' +
    '  <button class="pb-btn" id="pbColumns" title="Choose which parameters and \'Count:\' columns are shown">Columns\u2026</button>' +
    '  <div class="pb-search"><input id="pbSearch" type="search" autocomplete="off" spellcheck="false"' +
    '     placeholder="Search - space-separated words; every word must appear in a visible column">' +
    '    <button class="pb-btn" id="pbClear">Clear</button></div>' +
    '</div>' +
    '<div class="pb-main" id="pbMain">' +
    '  <section class="pb-card pb-gridcard">' +
    '    <div class="pb-tablewrap" id="pbTableWrap" tabindex="0"><table class="pb-table" id="pbTable"></table>' +
    '      <div class="pb-empty" id="pbEmpty" hidden></div></div>' +
    '    <footer class="pb-foot">' +
    '      <div><div class="pb-count" id="pbCount"></div><div class="pb-hint">Click, Ctrl+click, Shift+click to select rows \u00b7 double-click to show in Revit</div></div>' +
    '      <div class="pb-foot-actions">' +
    '        <button class="pb-btn" id="pbSelect" title="Select the highlighted rows in Revit">Select in Revit</button>' +
    '        <button class="pb-btn pb-primary" id="pbShow" title="Select and show the highlighted rows in Revit">Show / Zoom</button>' +
    '      </div>' +
    '    </footer>' +
    '  </section>' +
    '  <div class="pb-splitter" id="pbSplitter" title="Drag to resize"></div>' +
    '  <aside class="pb-card pb-contents">' +
    '    <div class="pb-contents-head">' +
    '      <div class="pb-contents-title">Elements in selection</div>' +
    '      <button class="pb-btn pb-small" id="pbCategories" title="Choose which categories are listed in this panel">Categories\u2026</button>' +
    '    </div>' +
    '    <label class="pb-check"><input type="checkbox" id="pbUnassigned"> <span id="pbUnassignedLabel"></span></label>' +
    '    <div class="pb-contents-info" id="pbContentsInfo"></div>' +
    '    <div class="pb-tree" id="pbTree" tabindex="0"></div>' +
    '    <footer class="pb-foot pb-foot-right">' +
    '      <button class="pb-btn" id="pbTreeSelect" title="Select the highlighted node\'s elements (or all listed if none is highlighted)">Select in Revit</button>' +
    '      <button class="pb-btn pb-primary" id="pbTreeShow" title="Select and show the highlighted node\'s elements (or all listed)">Show / Zoom</button>' +
    '    </footer>' +
    '  </aside>' +
    '</div>' +
    '<div class="pb-status"><span id="pbStatus"></span></div>' +
    '<div class="pb-busy" id="pbBusy" hidden><div class="pb-busy-box">Refreshing \u2013 Revit is re-reading the model\u2026</div></div>' +
    '</div>';

  function setStatus(message, isError) {
    var el = $('pbStatus');
    el.textContent = message;
    el.className = isError ? 'pb-error' : '';
  }

  function fillSelect(select, options, value) {
    select.innerHTML = options.map(function (o) {
      return '<option value="' + esc(o) + '"' + (o === value ? ' selected' : '') + '>' + esc(o) + '</option>';
    }).join('');
  }

  function populateLevels() {
    var levels = levelOptions(records());
    if (levels.indexOf(S.level) < 0) { S.level = levels.length ? levels[0] : ''; }
    fillSelect($('pbLevel'), levels, S.level);
    $('pbLevelWrap').hidden = S.scope !== 'Level';
  }

  // --- grid -------------------------------------------------------------------------------
  function computeRows(fields) {
    var scoped = filterRecords(records(), S.scope, S.level);
    var tokens = searchTokens($('pbSearch').value);
    var rows = [];
    scoped.forEach(function (rec) {
      var values = fields.map(function (f) { return recordValue(rec, f, PREFIX); });
      if (matchesTokens(values, tokens)) { rows.push({ rec: rec, values: values }); }
    });
    var col = fields.indexOf(S.sortField);
    if (col >= 0) {
      var numeric = isCountField(S.sortField, PREFIX) || S.sortField === 'Element Id';
      rows.sort(function (a, b) { return S.sortDir * compareValues(a.values[col], b.values[col], numeric); });
    }
    view.scoped = scoped.length;
    return rows;
  }

  function renderGrid() {
    var fields = currentFields();
    var rows = computeRows(fields);
    view.fields = fields;
    view.rows = rows;
    var visible = {};
    rows.forEach(function (r) { visible[r.rec.id] = true; });
    Object.keys(selected).forEach(function (id) { if (!visible[id]) { delete selected[id]; } });

    var widths = fields.map(function (f) { return S.colWidths[f] || DEFAULT_COL_WIDTH; });
    var total = widths.reduce(function (a, b) { return a + b; }, 0);
    var html = '<colgroup>' + widths.map(function (w) { return '<col style="width:' + w + 'px">'; }).join('') + '</colgroup>';
    html += '<thead><tr>' + fields.map(function (f, i) {
      var sorted = f === S.sortField;
      return '<th draggable="true" data-col="' + i + '" class="' + (isCountField(f, PREFIX) ? 'num ' : '') +
        (sorted ? 'sorted' : '') + '" title="' + esc(f) + ' - click to sort, drag to reorder">' +
        '<span class="pb-th-label">' + esc(f) + '</span>' +
        (sorted ? '<span class="pb-sort">' + (S.sortDir > 0 ? '\u25B2' : '\u25BC') + '</span>' : '') +
        '<span class="pb-grip" data-grip="' + i + '"></span></th>';
    }).join('') + '</tr></thead><tbody>';
    html += rows.map(function (r) {
      var id = r.rec.id;
      return '<tr data-id="' + esc(id) + '"' + (selected[id] ? ' class="sel"' : '') + '>' + r.values.map(function (v, i) {
        var num = isCountField(fields[i], PREFIX);
        return '<td' + (num ? ' class="num' + (v ? '' : ' zero') + '"' : ' title="' + esc(v) + '"') + '>' + esc(v) + '</td>';
      }).join('') + '</tr>';
    }).join('') + '</tbody>';
    var table = $('pbTable');
    table.style.width = total + 'px';
    table.innerHTML = html;

    var empty = $('pbEmpty');
    if (!rows.length) {
      empty.hidden = false;
      empty.textContent = !records().length ? 'No ' + S.source.toLowerCase() + ' in this model.'
        : (view.scoped ? 'No rows match the search.' : 'No ' + S.source.toLowerCase() + ' in this scope.');
    } else {
      empty.hidden = true;
    }
    updateCount();
    updateContents();
    setStatus(scopeSummary());
  }

  function scopeSummary() {
    var where = S.scope === 'Current view' ? "view '" + DATA.view + "' (as of last refresh)"
      : (S.scope === 'Level' ? "level '" + (S.level || '-') + "'" : 'whole project');
    return view.scoped + ' ' + S.source.toLowerCase() + ' in ' + where + ' - ' + DATA.document;
  }

  function updateCount() {
    var n = selectedIds().length;
    $('pbCount').textContent = view.rows.length + ' shown / ' + view.scoped + ' loaded' + (n ? ' \u00b7 ' + n + ' selected' : '');
  }

  function paintSelection() {
    var trs = $('pbTable').tBodies[0] ? $('pbTable').tBodies[0].rows : [];
    for (var i = 0; i < trs.length; i++) {
      trs[i].classList.toggle('sel', !!selected[trs[i].getAttribute('data-id')]);
      trs[i].classList.toggle('cursor', trs[i].getAttribute('data-id') === focusId);
    }
    updateCount();
    if (!unassigned) { updateContents(); }
  }

  function rowIndex(id) {
    for (var i = 0; i < view.rows.length; i++) { if (view.rows[i].rec.id === id) { return i; } }
    return -1;
  }

  function selectRange(fromId, toId, keep) {
    var a = rowIndex(fromId), b = rowIndex(toId);
    if (a < 0) { a = b; }
    if (!keep) { selected = {}; }
    for (var i = Math.min(a, b); i <= Math.max(a, b); i++) { selected[view.rows[i].rec.id] = true; }
  }

  function clickRow(id, e) {
    var additive = e.ctrlKey || e.metaKey;
    if (e.shiftKey && anchorId !== null) {
      selectRange(anchorId, id, additive);
    } else if (additive) {
      if (selected[id]) { delete selected[id]; } else { selected[id] = true; }
      anchorId = id;
    } else {
      selected = {};
      selected[id] = true;
      anchorId = id;
    }
    focusId = id;
    paintSelection();
  }

  function moveCursor(step, extend) {
    if (!view.rows.length) { return; }
    var i = rowIndex(focusId);
    i = i < 0 ? (step > 0 ? 0 : view.rows.length - 1) : Math.max(0, Math.min(view.rows.length - 1, i + step));
    var id = view.rows[i].rec.id;
    if (extend && anchorId !== null) { selectRange(anchorId, id, false); } else { selected = {}; selected[id] = true; anchorId = id; }
    focusId = id;
    paintSelection();
    var tr = $('pbTable').querySelector('tr[data-id="' + id + '"]');
    if (tr) { tr.scrollIntoView({ block: 'nearest' }); }
  }

  // --- contents tree ----------------------------------------------------------------------
  function updateContents() {
    var index = contentIndex[S.source] || {};
    var items = [];
    var header;
    $('pbUnassignedLabel').textContent = 'Show elements not in any ' + (S.source === 'Spaces' ? 'Space' : 'Room');
    if (unassigned) {
      items = index[''] || [];
      header = 'Not in any ' + noun() + ' - ' + items.length + ' element(s)';
    } else {
      var ids = selectedIds();
      ids.forEach(function (id) { items = items.concat(index[id] || []); });
      if (!ids.length) {
        header = 'Select a ' + noun() + ' to list the elements in it.';
      } else if (ids.length === 1) {
        var row = view.rows[rowIndex(ids[0])];
        header = (row ? row.rec.label : ids[0]) + ' - ' + items.length + ' element(s)';
      } else {
        header = ids.length + ' ' + noun() + 's selected - ' + items.length + ' element(s)';
      }
    }
    var groups = groupContents(items, S.categories);
    listedIds = [];
    groups.forEach(function (g) { listedIds = listedIds.concat(g.ids); });
    if (listedIds.length !== items.length) {
      header += ' (' + (items.length - listedIds.length) + ' hidden by Categories)';
    }
    var note = (DATA.phase_notes || {})[S.source];
    $('pbContentsInfo').innerHTML = '<div>' + esc(header) + '</div>' + (note ? '<div class="pb-note">' + esc(note) + '</div>' : '');
    renderTree(groups);
  }

  function nodeHtml(key, label, ids, kind, childrenHtml) {
    treeIds[key] = ids;
    var hasKids = !!childrenHtml;
    var open = hasKids && (key in treeOpen ? treeOpen[key] : kind === 'cat');
    return '<div class="pb-node pb-' + kind + (open ? ' open' : '') + '">' +
      '<div class="pb-node-row' + (key === treeSel ? ' sel' : '') + '" data-key="' + esc(key) + '">' +
      '<span class="pb-caret">' + (hasKids ? (open ? '\u25BE' : '\u25B8') : '') + '</span>' +
      '<span class="pb-node-label">' + esc(label) + '</span></div>' +
      (hasKids ? '<div class="pb-kids">' + childrenHtml + '</div>' : '') + '</div>';
  }

  function renderTree(groups) {
    treeIds = {};
    var html = groups.map(function (g) {
      var catKey = 'c|' + g.name;
      var types = g.types.map(function (t) {
        var typeKey = 't|' + g.name + '|' + t.name;
        var leaves = t.items.map(function (i) { return nodeHtml('i|' + i.id, i.label, [i.id], 'item', ''); }).join('');
        return nodeHtml(typeKey, t.name + ' (' + t.ids.length + ')', t.ids, 'type', leaves);
      }).join('');
      return nodeHtml(catKey, g.name + ' (' + g.ids.length + ')', g.ids, 'cat', types);
    }).join('');
    if (treeSel && !treeIds[treeSel]) { treeSel = null; }
    $('pbTree').innerHTML = html || '<div class="pb-tree-empty">Nothing to list.</div>';
  }

  function treeSelection() { return treeSel && treeIds[treeSel] ? treeIds[treeSel].slice() : listedIds.slice(); }

  // --- Revit ------------------------------------------------------------------------------
  function navigate(url) { root.location.href = url; }

  function sendToRevit(ids, show, emptyMessage) {
    if (!ids.length) { setStatus(emptyMessage); return; }
    var url = selectUrl(ids, show);
    if (url.length > MAX_URL_LENGTH) {
      setStatus('Too many elements (' + ids.length + ') to send in one request. Narrow the selection and try again.', true);
      return;
    }
    navigate(url);
    setStatus('Sent ' + ids.length + ' element(s) to Revit to ' + (show ? 'select and show' : 'select') +
      ". Revit acts on the active project; this list was read from '" + DATA.document + "'.");
  }

  function refreshModel() {
    if (!DATA.control_id) {
      setStatus('Run the Parameter Browser WebView ribbon button again to re-read the model.', true);
      return;
    }
    if (storage) {
      try {
        storage.setItem(HANDOFF_KEY, JSON.stringify({
          document: DATA.document, time: Date.now(), source: S.source, search: $('pbSearch').value,
          selected: selectedIds(), unassigned: unassigned
        }));
      } catch (e) { /* not essential */ }
    }
    $('pbBusy').hidden = false;
    navigate(refreshUrl(DATA.control_id));
    setTimeout(function () {
      $('pbBusy').hidden = true;
      setStatus('If the browser did not reload, click the Parameter Browser WebView ribbon button.', true);
    }, 20000);
  }

  function restoreHandoff() {
    var h = loadJson(HANDOFF_KEY);
    if (storage) { try { storage.removeItem(HANDOFF_KEY); } catch (e) { /* ignore */ } }
    if (!h || h.document !== DATA.document || !(Date.now() - h.time < HANDOFF_MAX_AGE_MS) || h.source !== S.source) { return false; }
    $('pbSearch').value = text(h.search);
    unassigned = !!h.unassigned;
    $('pbUnassigned').checked = unassigned;
    (h.selected || []).forEach(function (id) { selected[text(id)] = true; });
    return true;
  }

  // --- export -----------------------------------------------------------------------------
  function exportRows() { return view.rows.map(function (r) { return r.values; }); }

  function copyTable() {
    var data = delimited(view.fields, exportRows(), '\t');
    function done() { setStatus('Copied ' + view.rows.length + ' row(s) to the clipboard.'); }
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = data;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove();
      if (ok) { done(); } else { setStatus('Copy failed. Use Export CSV instead.', true); }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(data).then(done, fallback);
    } else {
      fallback();
    }
  }

  function exportCsv() {
    var csv = '\ufeff' + delimited(view.fields, exportRows(), ',');
    var name = (DATA.document + ' - ' + S.source).replace(/[\\/:*?"<>|]+/g, '_') + '.csv';
    var a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
    setStatus('Exported ' + view.rows.length + ' row(s) to ' + name + ' (Downloads folder).');
  }

  // --- dialogs ----------------------------------------------------------------------------
  function openModal(title, bodyHtml, onApply, extraButtons) {
    var overlay = document.createElement('div');
    overlay.className = 'pb-modal-back';
    overlay.innerHTML = '<div class="pb-modal" role="dialog"><div class="pb-modal-head">' + esc(title) + '</div>' +
      '<div class="pb-modal-body">' + bodyHtml + '</div><div class="pb-modal-foot">' +
      '<span class="pb-modal-msg"></span>' + (extraButtons || '') +
      '<button class="pb-btn" data-act="cancel">Cancel</button>' +
      '<button class="pb-btn pb-primary" data-act="apply">Apply</button></div></div>';
    function close() { overlay.remove(); document.removeEventListener('keydown', onKey, true); }
    function onKey(e) {
      if (e.key === 'Escape') { e.stopPropagation(); close(); }
    }
    overlay.addEventListener('mousedown', function (e) { if (e.target === overlay) { close(); } });
    overlay.querySelector('[data-act="cancel"]').onclick = close;
    overlay.querySelector('[data-act="apply"]').onclick = function () {
      var err = onApply(overlay);
      if (err) { overlay.querySelector('.pb-modal-msg').textContent = err; } else { close(); }
    };
    document.addEventListener('keydown', onKey, true);
    document.querySelector('.pb').appendChild(overlay);
    return overlay;
  }

  function openColumns() {
    var available = sourceFields();
    var chosen = currentFields().slice();
    var overlay = openModal('Choose columns - ' + S.source,
      '<div class="pb-cols"><div class="pb-cols-pane"><input type="search" class="pb-input" data-role="filter" placeholder="Filter parameters">' +
      '<div class="pb-cols-list" data-role="all"></div></div>' +
      '<div class="pb-cols-pane"><div class="pb-cols-caption">Shown columns, in order</div><div class="pb-cols-list" data-role="chosen"></div></div></div>',
      function () {
        if (!chosen.length) { return 'Select at least one parameter.'; }
        S.fields = chosen.slice();
        save();
        renderGrid();
        return null;
      },
      '<button class="pb-btn" data-act="defaults">Defaults</button>');
    var filter = overlay.querySelector('[data-role="filter"]');
    function draw() {
      var q = lower(filter.value);
      overlay.querySelector('[data-role="all"]').innerHTML = available.filter(function (f) {
        return !q || lower(f).indexOf(q) >= 0;
      }).map(function (f) {
        return '<label class="pb-check"><input type="checkbox" data-field="' + esc(f) + '"' +
          (chosen.indexOf(f) >= 0 ? ' checked' : '') + '> ' + esc(f) + '</label>';
      }).join('') || '<div class="pb-muted">No parameters match.</div>';
      overlay.querySelector('[data-role="chosen"]').innerHTML = chosen.map(function (f, i) {
        return '<div class="pb-chosen"><span title="' + esc(f) + '">' + esc(f) + '</span>' +
          '<button class="pb-icon" data-move="-1" data-i="' + i + '" title="Move up"' + (i ? '' : ' disabled') + '>\u25B2</button>' +
          '<button class="pb-icon" data-move="1" data-i="' + i + '" title="Move down"' + (i < chosen.length - 1 ? '' : ' disabled') + '>\u25BC</button>' +
          '<button class="pb-icon" data-remove="' + i + '" title="Remove">\u2715</button></div>';
      }).join('') || '<div class="pb-muted">No columns selected.</div>';
    }
    filter.oninput = draw;
    overlay.addEventListener('change', function (e) {
      var f = e.target.getAttribute('data-field');
      if (f === null) { return; }
      var i = chosen.indexOf(f);
      if (e.target.checked && i < 0) { chosen.push(f); }
      if (!e.target.checked && i >= 0) { chosen.splice(i, 1); }
      draw();
    });
    overlay.addEventListener('click', function (e) {
      var b = e.target.closest('button');
      if (!b) { return; }
      if (b.getAttribute('data-act') === 'defaults') { chosen = resolveFields([], available, D.fields); draw(); return; }
      var i = Number(b.getAttribute('data-i'));
      if (b.hasAttribute('data-move')) {
        var j = i + Number(b.getAttribute('data-move'));
        var tmp = chosen[i]; chosen[i] = chosen[j]; chosen[j] = tmp;
        draw();
      } else if (b.hasAttribute('data-remove')) {
        chosen.splice(Number(b.getAttribute('data-remove')), 1);
        draw();
      }
    });
    draw();
    filter.focus();
  }

  function openCategories() {
    var overlay = openModal('Categories listed in the panel',
      '<div class="pb-cat-list">' + D.categories.map(function (c) {
        return '<label class="pb-check"><input type="checkbox" value="' + esc(c) + '"' +
          (S.categories.indexOf(c) >= 0 ? ' checked' : '') + '> ' + esc(c) + '</label>';
      }).join('') + '</div>',
      function (o) {
        var boxes = o.querySelectorAll('.pb-cat-list input');
        S.categories = D.categories.filter(function (c, i) { return boxes[i].checked; });
        save();
        updateContents();
        return null;
      },
      '<button class="pb-btn" data-act="all">All</button><button class="pb-btn" data-act="none">None</button>');
    overlay.addEventListener('click', function (e) {
      var act = e.target.getAttribute && e.target.getAttribute('data-act');
      if (act === 'all' || act === 'none') {
        Array.prototype.forEach.call(overlay.querySelectorAll('.pb-cat-list input'), function (b) { b.checked = act === 'all'; });
      }
    });
  }

  // --- column resize / reorder ------------------------------------------------------------
  function startResize(e, col) {
    e.preventDefault();
    e.stopPropagation();
    var field = view.fields[col];
    var colEl = $('pbTable').querySelectorAll('col')[col];
    var startX = e.clientX;
    var startW = S.colWidths[field] || DEFAULT_COL_WIDTH;
    var table = $('pbTable');
    var baseTotal = parseInt(table.style.width, 10) - startW;
    function move(ev) {
      var w = Math.max(MIN_COL_WIDTH, startW + ev.clientX - startX);
      colEl.style.width = w + 'px';
      table.style.width = (baseTotal + w) + 'px';
      S.colWidths[field] = w;
    }
    function up() {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      suppressHeaderClick = true;
      setTimeout(function () { suppressHeaderClick = false; }, 0);
      save();
    }
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  }

  function moveColumn(fromField, toField) {
    if (!fromField || fromField === toField) { return; }
    var shown = view.fields;
    if (shown.some(function (f) { return S.fields.indexOf(f) < 0; })) { S.fields = shown.slice(); }
    S.fields.splice(S.fields.indexOf(fromField), 1);
    S.fields.splice(S.fields.indexOf(toField) + (shown.indexOf(fromField) < shown.indexOf(toField) ? 1 : 0), 0, fromField);
    save();
    renderGrid();
  }

  // --- wiring -----------------------------------------------------------------------------
  function bind() {
    fillSelect($('pbSource'), D.sources, S.source);
    fillSelect($('pbScope'), D.scopes, S.scope);
    $('pbMeta').textContent = DATA.document + ' \u00b7 view: ' + (DATA.view || '-') + ' \u00b7 read ' + DATA.generated;
    $('pbRefresh').disabled = !DATA.control_id;
    if (!DATA.control_id) { $('pbRefresh').title = 'Run the ribbon button again to refresh'; }
    $('pbMain').style.setProperty('--pb-panel', S.panelWidth + 'px');

    $('pbSource').onchange = function () {
      S.source = this.value;
      selected = {}; anchorId = focusId = null; treeSel = null;
      populateLevels(); save(); renderGrid();
    };
    $('pbScope').onchange = function () {
      S.scope = this.value;
      $('pbLevelWrap').hidden = S.scope !== 'Level';
      save(); renderGrid();
    };
    $('pbLevel').onchange = function () { S.level = this.value; save(); renderGrid(); };
    var searchTimer = null;
    $('pbSearch').oninput = function () { clearTimeout(searchTimer); searchTimer = setTimeout(renderGrid, 120); };
    $('pbClear').onclick = function () { $('pbSearch').value = ''; renderGrid(); $('pbSearch').focus(); };
    $('pbColumns').onclick = openColumns;
    $('pbCategories').onclick = openCategories;
    $('pbRefresh').onclick = refreshModel;
    $('pbCopy').onclick = copyTable;
    $('pbCsv').onclick = exportCsv;
    $('pbSelect').onclick = function () { sendToRevit(selectedIds(), false, 'Select one or more rows first.'); };
    $('pbShow').onclick = function () { sendToRevit(selectedIds(), true, 'Select one or more rows first.'); };
    $('pbTreeSelect').onclick = function () { sendToRevit(treeSelection(), false, 'No elements listed.'); };
    $('pbTreeShow').onclick = function () { sendToRevit(treeSelection(), true, 'No elements listed.'); };
    $('pbUnassigned').onchange = function () { unassigned = this.checked; treeSel = null; updateContents(); };

    var table = $('pbTable');
    table.addEventListener('mousedown', function (e) {
      var grip = e.target.closest('[data-grip]');
      if (grip) { startResize(e, Number(grip.getAttribute('data-grip'))); return; }
      if (e.shiftKey && e.target.closest('tbody')) { e.preventDefault(); }
    });
    table.addEventListener('click', function (e) {
      var th = e.target.closest('th');
      if (th) {
        if (suppressHeaderClick) { return; }
        var field = view.fields[Number(th.getAttribute('data-col'))];
        if (S.sortField === field) { S.sortDir = -S.sortDir; } else { S.sortField = field; S.sortDir = 1; }
        save(); renderGrid();
        return;
      }
      var tr = e.target.closest('tbody tr');
      if (tr) { clickRow(tr.getAttribute('data-id'), e); $('pbTableWrap').focus({ preventScroll: true }); }
    });
    table.addEventListener('dblclick', function (e) {
      var tr = e.target.closest('tbody tr');
      if (!tr) { return; }
      var id = tr.getAttribute('data-id');
      selected = {}; selected[id] = true; anchorId = focusId = id;
      paintSelection();
      sendToRevit([id], true, '');
    });
    table.addEventListener('dragstart', function (e) {
      var th = e.target.closest('th');
      if (!th) { return; }
      dragField = view.fields[Number(th.getAttribute('data-col'))];
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', dragField);
    });
    table.addEventListener('dragover', function (e) {
      if (dragField !== null && e.target.closest('th')) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
    });
    table.addEventListener('drop', function (e) {
      var th = e.target.closest('th');
      if (!th || dragField === null) { return; }
      e.preventDefault();
      var target = view.fields[Number(th.getAttribute('data-col'))];
      var from = dragField;
      dragField = null;
      moveColumn(from, target);
    });
    table.addEventListener('dragend', function () { dragField = null; });

    $('pbTableWrap').addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        moveCursor(e.key === 'ArrowDown' ? 1 : -1, e.shiftKey);
      } else if ((e.ctrlKey || e.metaKey) && (e.key === 'a' || e.key === 'A')) {
        e.preventDefault();
        selected = {};
        view.rows.forEach(function (r) { selected[r.rec.id] = true; });
        paintSelection();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        sendToRevit(selectedIds(), true, 'Select one or more rows first.');
      } else if (e.key === 'Escape') {
        selected = {}; anchorId = null;
        paintSelection();
      }
    });

    var tree = $('pbTree');
    // Update classes in place (no re-render) so a double-click still lands on the same element.
    tree.addEventListener('click', function (e) {
      var row = e.target.closest('.pb-node-row');
      var previous = tree.querySelector('.pb-node-row.sel');
      if (!row) {
        treeSel = null;
        if (previous) { previous.classList.remove('sel'); }
        return;
      }
      var key = row.getAttribute('data-key');
      var node = row.parentNode;
      if (e.target.classList.contains('pb-caret') && node.querySelector('.pb-kids')) {
        var open = !node.classList.contains('open');
        treeOpen[key] = open;
        node.classList.toggle('open', open);
        e.target.textContent = open ? '\u25BE' : '\u25B8';
        return;
      }
      if (previous) { previous.classList.remove('sel'); }
      row.classList.add('sel');
      treeSel = key;
    });
    tree.addEventListener('dblclick', function (e) {
      var row = e.target.closest('.pb-node-row');
      if (!row || e.target.classList.contains('pb-caret')) { return; }
      treeSel = row.getAttribute('data-key');
      sendToRevit(treeSelection(), true, 'No elements listed.');
    });

    $('pbSplitter').addEventListener('mousedown', function (e) {
      e.preventDefault();
      var main = $('pbMain');
      function move(ev) {
        var rect = main.getBoundingClientRect();
        S.panelWidth = Math.max(220, Math.min(rect.width - 320, rect.right - ev.clientX - 3));
        main.style.setProperty('--pb-panel', S.panelWidth + 'px');
      }
      function up() {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
        save();
      }
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    });
  }

  bind();
  populateLevels();
  var refreshed = restoreHandoff();
  renderGrid();
  if (refreshed) { setStatus('Model re-read. ' + scopeSummary()); }
})(typeof window !== 'undefined' ? window : globalThis);
