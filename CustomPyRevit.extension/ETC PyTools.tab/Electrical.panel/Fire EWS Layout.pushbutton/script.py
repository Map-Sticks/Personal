"""Review smoke detector and EWS speaker geometry, spacing and ceiling coordination. Preview only."""
import hashlib
import json

from pyrevit import DB, forms, revit, script
from pyrevit.revit import query
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

import fire_ews_report as report
import fire_ews_review as planner
import fire_ews_revit as model
import fire_ews_new as new_layout
from fire_ews_geometry import bounds


TITLE = 'Fire & EWS Layout'
DETECTOR, SPEAKER = 'detector', 'speaker'
FIELDS = {'reference': 'Reference', 'wall': 'Wall', 'separation': 'Separation',
          'travel': 'Travel', 'detector_pitch': 'DetectorPitch', 'detector_radius': 'DetectorRadius',
          'detector_edge': 'DetectorEdge', 'speaker_pitch': 'SpeakerPitch'}


class Criteria(forms.WPFWindow):
    def __init__(self, saved, new_placement=False):
        forms.WPFWindow.__init__(self, script.get_bundle_file('settings.xaml'))
        self.settings = None
        self.saved = None
        self.new_placement = new_placement
        self.Travel.IsEnabled = not new_placement
        self.Candidates.IsEnabled = not new_placement
        if saved:
            for key, name in FIELDS.items():
                getattr(self, name).Text = saved['fields'].get(key, '')
            self.Tile.Text = saved['tile']
            self.Candidates.IsChecked = saved['candidates']

    def accept(self, sender, args):
        try:
            raw = {'fields': {k: getattr(self, v).Text for k, v in FIELDS.items()},
                   'tile': self.Tile.Text, 'candidates': bool(self.Candidates.IsChecked)}
            self.settings = parse_profile(raw, self.new_placement)
        except ValueError as exc:
            forms.alert(str(exc), title=TITLE)
            return
        self.saved = raw
        self.Close()


def parse_profile(raw, new_placement=False):
    dims = tuple(float(part.strip()) for part in raw['tile'].lower().split('x'))
    if len(dims) != 2 or not all(100 <= d <= 5000 for d in dims):
        raise ValueError('Enter two valid tile dimensions, e.g. 600 x 1200 mm (omit mm).')
    return planner.parse_rules(raw['fields'], new_placement), dims, bool(raw['candidates'])


def project_option(doc):
    if doc.IsModelInCloud:
        path = doc.GetCloudModelPath()
        identity = '{}:{}'.format(path.GetProjectGUID(), path.GetModelGUID())
    elif doc.IsWorkshared and doc.GetWorksharingCentralModelPath():
        identity = DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(doc.GetWorksharingCentralModelPath()).lower()
    else:
        identity = doc.PathName.lower()
    return 'criteria_'+hashlib.sha256(identity.encode('utf-8')).hexdigest() if identity else None


def criteria(doc, new_placement=False):
    option = project_option(doc)
    config = script.get_config()
    stored = config.get_option(option, '') if option else ''
    profile = None
    if stored:
        try:
            profile = json.loads(stored)
            settings = parse_profile(profile, new_placement)
        except (ValueError, KeyError, TypeError, AttributeError):
            forms.alert('The saved criteria could not be read. Enter them again to replace that profile.',
                        title=TITLE)
            profile = None
    if profile:
        actions = ['Use saved criteria', 'Edit criteria', 'Use saved criteria and reconfirm types']
        action = forms.CommandSwitchWindow.show(actions, message='Review criteria for this project')
        if not action:
            return None
        if action != actions[1]:
            return settings, action == actions[2]
    dialog = Criteria(profile, new_placement)
    dialog.show_dialog()
    if dialog.saved and option:
        setattr(config, option, json.dumps(dialog.saved, ensure_ascii=True))
        script.save_config()
    elif dialog.saved:
        forms.alert('Criteria cannot be remembered until this project has been saved.', title=TITLE)
    return (dialog.settings, False) if dialog.settings is not None else None


class TypeSetup(object):
    def __init__(self, doc):
        project = project_option(doc)
        self.option = project+'_types' if project else None
        self.config = script.get_config()
        self.data = self.config.get_option(self.option, {}) if self.option else {}

    def save(self):
        if self.option:
            setattr(self.config, self.option, self.data)
            script.save_config()


class PickFilter(ISelectionFilter):
    def __init__(self, doc, predicate, linked=False):
        self.doc, self.predicate, self.linked = doc, predicate, linked

    def AllowElement(self, element):
        return (isinstance(element, DB.RevitLinkInstance) and element.GetLinkDocument() is not None
                if self.linked else self.predicate(element))

    def AllowReference(self, ref, pos):
        if not self.linked:
            return False
        link = self.doc.GetElement(ref.ElementId)
        if not isinstance(link, DB.RevitLinkInstance):
            return False
        source = link.GetLinkDocument()
        return bool(source and self.predicate(source.GetElement(ref.LinkedElementId)))


def pick(doc, predicate, prompt, linked=False, use_preselection=False):
    sel = revit.uidoc.Selection
    if use_preselection and not linked:
        existing = [doc.GetElement(eid) for eid in sel.GetElementIds()]
        matching = [e for e in existing if predicate(e)]
        if matching:
            return [(e, None) for e in matching]
    refs = sel.PickObjects(ObjectType.LinkedElement if linked else ObjectType.Element,
                           PickFilter(doc, predicate, linked), prompt+'; then click Finish')
    result, seen = [], set()
    for ref in refs:
        identity = (ref.ElementId, ref.LinkedElementId)
        if identity in seen:
            continue
        seen.add(identity)
        element = doc.GetElement(ref.ElementId)
        if linked:
            result.append((element.GetLinkDocument().GetElement(ref.LinkedElementId), element))
        else:
            result.append((element, None))
    return result


def ceiling_element(e):
    return bool(e and isinstance(e, DB.Ceiling))


def space_element(e):
    return bool(e and isinstance(e, DB.SpatialElement) and e.Category and
                model.key(e.Category) in (int(DB.BuiltInCategory.OST_Rooms), int(DB.BuiltInCategory.OST_MEPSpaces)))


def device_element(e):
    return bool(e and isinstance(e, DB.FamilyInstance) and isinstance(e.Location, DB.LocationPoint)
                and e.SuperComponent is None and e.Category and
                model.key(e.Category) in (int(DB.BuiltInCategory.OST_FireAlarmDevices),
                                          int(DB.BuiltInCategory.OST_CommunicationDevices),
                                          int(DB.BuiltInCategory.OST_ElectricalFixtures),
                                          int(DB.BuiltInCategory.OST_SecurityDevices)))


def device_types(doc, kinds, saved, reconfirm):
    maps = {}
    updates = {}
    for kind in kinds:
        remembered = saved.get(kind, [])
        symbols = [doc.GetElement(uid) for uid in remembered]
        if not reconfirm and symbols and all(isinstance(s, DB.FamilySymbol) for s in symbols):
            ids = set(model.key(s) for s in symbols)
        else:
            options = ['Select {}'.format('smoke detectors' if kind == DETECTOR else 'EWS speakers'),
                       'Skip this system']
            action = forms.CommandSwitchWindow.show(
                options, message='One-time setup: all devices of the selected types will be reviewed.')
            if not action:
                return None
            if action == options[1]:
                maps[kind] = set()
                continue
            picked = pick(doc, device_element, options[0]+' in the model')
            ids = set(model.key(e.Symbol) for e, _ in picked)
            if ids:
                updates[kind] = sorted(set(e.Symbol.UniqueId for e, _ in picked))
        if any(ids & existing for existing in maps.values()):
            raise ValueError('The same device type was picked for both systems. Pick distinct types.')
        maps[kind] = ids
    saved.update(updates)
    return maps


def finish_mapping(source, elements, by_ceiling, saved, reconfirm):
    types = {c.GetTypeId(): source.GetElement(c.GetTypeId()) for c in elements}
    finishes = {}
    options = ['Lay-in tiles', 'Flush ceiling', 'Unverified (measure only)']
    maps = dict(zip(options, ('tile', 'flush', 'unknown')))
    for tid, symbol in sorted(types.items(), key=lambda pair: model.key(pair[1])):
        uid = symbol.UniqueId
        if uid in saved and not (by_ceiling and reconfirm):
            finishes[tid] = saved[uid]
        elif by_ceiling:
            action = forms.CommandSwitchWindow.show(options, message=u'Ceiling type: {}'.format(query.get_name(symbol)))
            if not action:
                return None
            finishes[tid] = saved[uid] = maps[action]
    return finishes


def labelled_choice(items, label, title, multi=False, optional=False):
    mapping = {label(item): item for item in items}
    if not mapping:
        return [] if multi and optional else None
    none = '(None: no types mapped)'
    choices = sorted(mapping)
    if optional:
        choices.insert(0, none)
    selected = forms.SelectFromList.show(choices, title=title, multiselect=multi, button_name='Continue')
    if not selected:
        return None
    if not multi:
        return mapping[selected]
    return [mapping[value] for value in selected if value != none]


def choose_bulk(doc, view):
    links = [e for e in query.get_elements_by_categories([DB.BuiltInCategory.OST_RvtLinks], doc=doc)
             if isinstance(e, DB.RevitLinkInstance) and e.GetLinkDocument()]
    link = labelled_choice(links, lambda e: '{} [{}]'.format(query.get_name(e), model.key(e)),
                           'Select the architectural link containing rooms and ceilings')
    if link is None:
        if not links:
            forms.alert('No loaded Revit links.', title=TITLE)
        return
    linked, tf = link.GetLinkDocument(), link.GetTotalTransform()
    host_elevation = view.GenLevel.ProjectElevation
    levels = list(DB.FilteredElementCollector(linked).OfClass(DB.Level))
    level = labelled_choice(levels, lambda l: '{} | offset {:+.2f} m [{}]'.format(query.get_name(l),
                            (tf.OfPoint(DB.XYZ(0, 0, l.ProjectElevation)).Z-host_elevation)*.3048, model.key(l)),
                            'Select linked level (offset versus active view level)')
    if level is None:
        return
    link_phase = labelled_choice(list(linked.Phases), lambda p: '{} [{}]'.format(query.get_name(p), model.key(p)),
                                 'Select linked room and ceiling phase')
    if link_phase is None:
        return
    available = model.available_rooms(linked, level.Id, link_phase.Id)
    if not available:
        forms.alert('No placed main-model rooms on that linked level and phase.', title=TITLE)
        return
    scopes = ['Selected rooms', 'Room number prefix (department)', 'Entire linked level']
    scope = forms.CommandSwitchWindow.show(scopes, message='Choose which rooms to review; rooms are not clipped to the RCP.')
    if scope == scopes[0]:
        chosen = labelled_choice(available, lambda r: '{} {} [{}]'.format(r.Number, query.get_name(r), model.key(r)),
                                 'Select rooms to review', multi=True)
    elif scope == scopes[1]:
        prefix = forms.ask_for_string(prompt='Room-number prefix, e.g. TRL.0', title=TITLE)
        if not prefix or not prefix.strip():
            return
        chosen = [r for r in available if r.Number.upper().startswith(prefix.strip().upper())]
        if not chosen:
            forms.alert('No rooms match that prefix on the chosen level and phase.', title=TITLE)
    elif scope == scopes[2]:
        chosen = available if forms.alert('Review all {} rooms on this linked level?'.format(len(available)),
                                          title=TITLE, yes=True, no=True) else None
    else:
        return
    if not chosen:
        return
    return chosen, link, level, link_phase


def run():
    doc, view = revit.doc, revit.active_view
    if not isinstance(view, DB.ViewPlan) or view.IsTemplate or view.GenLevel is None:
        forms.alert('Open an RCP or level plan first.', title=TITLE)
        return
    phase = view.get_Parameter(DB.BuiltInParameter.VIEW_PHASE).AsElementId()
    scopes = ['Pick linked ceilings', 'Pick linked rooms', 'Pick host rooms or spaces',
              'Pick host ceilings', 'Department / level']
    scope = forms.CommandSwitchWindow.show(scopes, message='Choose what defines the review area')
    if not scope:
        return
    by_ceiling = scope in (scopes[0], scopes[3])
    linked_pick = scope in (scopes[0], scopes[1])
    if scope == scopes[4]:
        bulk = choose_bulk(doc, view)
        if not bulk:
            return
        chosen, link, level, link_phase = bulk
    else:
        picked = pick(doc, ceiling_element if by_ceiling else space_element,
                      'Pick {}'.format(scope[5:].lower()), linked_pick, use_preselection=True)
        if not picked:
            return
        chosen = [e for e, _ in picked]
        link = picked[0][1]
        if len(set(model.key(l) for _, l in picked if l)) > 1:
            raise ValueError('Pick from one link instance per review.')
        source = chosen[0].Document
        level = source.GetElement(chosen[0].LevelId)
        if not level or any(e.LevelId != level.Id for e in chosen):
            raise ValueError('Pick bounded elements from one level per review.')
        link_phase = source.GetElement(model.linked_phase_id(doc, link, phase)) if link else doc.GetElement(phase)
        if not link_phase:
            raise ValueError('The model phase for the picked elements could not be resolved.')
        if not by_ceiling and any(not e.get_Parameter(DB.BuiltInParameter.ROOM_PHASE) or
                                  e.get_Parameter(DB.BuiltInParameter.ROOM_PHASE).AsElementId() != link_phase.Id
                                  for e in chosen):
            raise ValueError('A picked room or space is outside the review phase. Check the active view and link phase map.')
        if by_ceiling and any(not model.alive(e, link_phase.Id) for e in chosen):
            raise ValueError('A picked ceiling is not present in the review phase.')
        if any(e.DesignOption is not None for e in chosen):
            raise ValueError('Pick main-model elements; this review does not resolve design options.')
    linked = link.GetLinkDocument() if link else doc
    tf = link.GetTotalTransform() if link else DB.Transform.Identity
    modes = ['Detectors only', 'EWS speakers only', 'Both (independent)', 'Preview detector + speaker pairs',
             'Place new detectors', 'Place new detector + speaker pairs']
    mode = forms.CommandSwitchWindow.show(modes, message='Choose what to review or place.')
    if mode is None:
        return
    pair_preview = mode == modes[3]
    new_placement = mode in modes[4:]
    if new_placement and len(chosen) != 1:
        raise ValueError('Select one empty ceiling, room or MEP space for new placement.')
    kinds = ([DETECTOR, SPEAKER] if mode in (modes[2], modes[3], modes[5])
             else [SPEAKER if mode == modes[1] else DETECTOR])
    settings = criteria(doc, new_placement)
    if settings is None:
        return
    review_settings, reconfirm = settings
    rules, dims, add = review_settings
    if pair_preview and not rules['reference']:
        forms.alert('Pair preview needs approved criteria. Choose Edit criteria and enter your project '
                    'design reference, minimum wall clearance, minimum object clearance and maximum move.',
                    title=TITLE)
        return
    setup = TypeSetup(doc)
    type_maps = device_types(doc, kinds, setup.data.setdefault('devices', {}), reconfirm)
    if type_maps is None:
        return
    opts = DB.SpatialElementBoundaryOptions()
    opts.SpatialElementBoundaryLocation = DB.SpatialElementBoundaryLocation.Finish
    rooms, errors = [], []
    with forms.ProgressBar(title='Reading rooms {value}/{max_value}', cancellable=True) as progress:
        for i, room in enumerate([] if by_ceiling else chosen, 1):
            progress.update_progress(i, len(chosen))
            if progress.cancelled:
                return
            try:
                rooms.append(model.room_data(room, link, opts))
            except ValueError as exc:
                errors.append(str(exc))
    if not rooms and not by_ceiling:
        forms.alert('No rooms could be read.\n'+'\n'.join(errors[:8]), title=TITLE)
        return
    if by_ceiling:
        boxes = [[p[:2] for p in model.box_corners(c.get_BoundingBox(None), tf)]
                 for c in chosen if c.get_BoundingBox(None)]
        if not boxes:
            raise ValueError('No picked ceiling has bounded model geometry.')
        region, source_ceilings = bounds(boxes), chosen
    else:
        region = bounds([loop for room in rooms for loop in room['loops']])
        source_ceilings = [c for c in model.ceiling_types(linked, level.Id, link_phase.Id)
                           if model.in_region(c, tf, region)]
    source_key = project_option(linked)
    known_finishes = setup.data.setdefault('ceilings', {}).setdefault(source_key, {}) if source_key else {}
    finishes = finish_mapping(linked, source_ceilings, by_ceiling, known_finishes, reconfirm)
    if finishes is None:
        return
    setup.save()
    if not by_ceiling and any(c.GetTypeId() not in finishes for c in source_ceilings):
        errors.append('Unconfigured ceiling types are measurement-only. Pick a ceiling directly once to set its finish.')
    ceilings = []
    with forms.ProgressBar(title='Reading ceiling geometry {value}/{max_value}', cancellable=True) as progress:
        for i, c in enumerate(source_ceilings, 1):
            progress.update_progress(i, len(source_ceilings))
            if progress.cancelled:
                return
            finish = finishes.get(c.GetTypeId(), 'unknown')
            patches = model.ceiling_data(c, tf, finish, dims, region)
            ceilings.extend(patches)
            if by_ceiling:
                supported = [patch for patch in patches if patch['z'] is not None and patch['loops']]
                if not supported:
                    errors.append('Ceiling {}: unsupported underside; not reviewed.'.format(model.key(c)))
                for patch in supported:
                    rooms.append(model.ceiling_zone(c, patch, doc, link, phase, link_phase.Id))
    if not rooms:
        raise ValueError('No picked ceilings have a supported horizontal underside.')
    if new_placement and by_ceiling:
        if len(rooms) != 1:
            raise ValueError('Select a ceiling with one supported horizontal underside for new placement.')
        level_z = tf.OfPoint(DB.XYZ(0, 0, level.ProjectElevation)).Z*model.MM
        if level_z >= rooms[0]['zmin']:
            raise ValueError('The selected ceiling underside must be above its associated level.')
        rooms[0]['occupancy_zmin'] = level_z
    devices, _ = model.host_devices(doc, phase, rooms)
    for kind, ids in type_maps.items():
        for d in devices:
            if d['type'] in ids:
                d['kind'] = kind
        if not ids:
            rules.setdefault('unmapped', set()).add(kind)
    model.room_context(rooms, ceilings, devices)
    if new_placement:
        run_new_placement(doc, view, phase, link, rooms[0], ceilings, devices, type_maps, kinds, rules)
        return
    rows, reserved = [], []
    with forms.ProgressBar(title='Checking layout {value}/{max_value}', cancellable=True) as progress:
        for i, room in enumerate(sorted(rooms, key=lambda r: (r['number'], r['id'])), 1):
            progress.update_progress(i, len(rooms))
            if progress.cancelled:
                return
            row = planner.review(room, ceilings, devices, kinds, rules, add_candidate=add,
                                 stop=lambda: progress.cancelled, reserved=reserved,
                                 pair_preview=pair_preview)
            if row is None:
                return
            rows.append((room, row))
    show_report(link, level, link_phase, doc.GetElement(phase), rules, dims, rows, errors, pair_preview)


def run_new_placement(doc, view, phase, link, room, ceilings, devices, type_maps, kinds, rules):
    if not view.GenLevel or abs(view.GenLevel.ProjectElevation*model.MM -
                                room.get('occupancy_zmin', room['zmin'])) > 150:
        raise ValueError('Open a plan on the selected area level before placing new devices.')
    if any(len(type_maps[k]) != 1 for k in kinds):
        raise ValueError('Select exactly one family type for each system you want to place.')
    mapped = set().union(*type_maps.values())
    existing = model.existing_layout_devices(room, devices, mapped)
    if existing:
        raise ValueError('This area is not empty. Existing mapped or fire alarm devices: {}. Nothing was changed.'
                         .format(', '.join(str(i) for i in sorted(existing))))
    symbols = {kind: doc.GetElement(DB.ElementId(next(iter(type_maps[kind])))) for kind in kinds}
    for symbol in symbols.values():
        if symbol.Family.FamilyPlacementType not in (DB.FamilyPlacementType.OneLevelBased,
                                                     DB.FamilyPlacementType.WorkPlaneBased):
            raise ValueError('{} uses a placement method this tool does not support.'.format(query.get_name(symbol)))
    patch, proposals, samples = new_layout.plan(room, ceilings, devices, rules, len(kinds) == 2)
    source = link.GetLinkDocument() if link else doc
    ceiling = source.GetElement(DB.ElementId(patch['element']))
    output = script.get_output()
    output.set_title('Fire & EWS | new placement')
    names = u'; '.join(u'{}: {}'.format(k, query.get_name(symbols[k])) for k in kinds)
    output.print_md('## New device placement preview')
    from xml.sax.saxutils import escape
    output.print_html(u'<p>{}. All {} sampled reach points passed the entered limits. '
                      'This is not a compliance, acoustics, or trade clash check. No wiring or addresses '
                      'will be created.</p>'.format(escape(names), samples))
    notes = (['The selected ceiling footprint limits placement. Room separators are ignored; '
              'physical wall sections from the host and selected link block paths. Adjacent ceiling '
              'elements and walls in other links are not included.'] if room.get('ceiling_zone') else [])
    output.print_html(report.room_html(room, {'ceilings': [patch], 'devices': devices_in_room(room, devices),
                                             'proposals': proposals, 'metrics': [], 'notes': notes}))
    output.show()
    if not forms.alert(u'Place these {} new devices in {} {}?\n{}\n\n'
                       'Review the plotted points and confirm the area and linked models are clear. '
                       'No existing devices will be moved.'.format(len(proposals), room['number'],
                                                                     room['name'], names),
                       title=TITLE, yes=True, no=True, warn_icon=True):
        return
    refreshed, _ = model.host_devices(doc, phase, [room])
    if model.existing_layout_devices(room, refreshed, mapped):
        raise ValueError('A device is now present in the selected area. Rerun the preview.')
    transaction = DB.Transaction(doc, 'Place reviewed fire and EWS devices')
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError('Revit could not start the placement transaction.')
        ids = model.place_layout(doc, view.GenLevel, phase, ceiling, patch, link, symbols, proposals)
        if transaction.Commit() != DB.TransactionStatus.Committed:
            raise ValueError('Revit did not commit the placement.')
    finally:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()
        transaction.Dispose()
    output.print_md('**Placed {} new devices.** Revit IDs: {}'.format(len(ids), ', '.join(map(str, ids))))


def devices_in_room(room, devices):
    from fire_ews_geometry import inside
    return [d for d in devices if inside(d['point'], room['boundary'])]


def show_report(link, level, linked_phase, host_phase, rules, dims, rows, errors, pair_preview=False):
    from xml.sax.saxutils import escape
    output = script.get_output()
    output.set_title(TITLE)
    output.print_md('## Fire & EWS Layout | read-only')
    moves = sum(p['id'] is not None for _, row in rows for p in row['proposals'])
    additions = sum(p['id'] is None for _, row in rows for p in row['proposals'])
    if pair_preview:
        output.print_md('**Pair placement preview | {} pair(s) proposed.**'.format(moves//2))
    output.print_md('**{} proposed move(s), {} optional new position(s).** {}'.format(
        moves, additions, '' if moves else 'Expand an area to see why devices were kept or blocked.'))
    output.print_html(u'<p>{} | {} | source phase {} | host phase {} | {} review areas</p>'.format(
        *(escape(v) for v in (query.get_name(link) if link else 'Host model', query.get_name(level), query.get_name(linked_phase),
                              query.get_name(host_phase), str(len(rows))))))
    output.print_html(u'<p>Design reference: <b>{}</b> | Approved tile: {:g} x {:g} mm</p>'.format(
        escape(rules['reference'] or 'not supplied; measurement only'), *dims))
    labels = [('wall', 'Min wall clearance'), ('separation', 'Min object-point separation'),
              ('travel', 'Max XY move'), ('detector_pitch', 'Detector neighbour'),
              ('detector_radius', 'Detector sample reach'), ('detector_edge', 'Detector wall reach'),
              ('speaker_pitch', 'EWS neighbour')]
    output.print_html('<p>{}</p>'.format(' | '.join('{}: {}'.format(label, '{:g} mm'.format(rules[key])
                      if rules[key] is not None else 'not assessed') for key, label in labels)))
    output.print_html('<p>Red: smoke detector. Blue: EWS speaker. Grey: other host points and grid. '
                      'Green: ceiling edges. Dashed amber: non-wall review edge or unverified room boundary. Rings are proposals. '
                      'Wall distances use room finish segments in room/space mode and physical wall sections in ceiling mode. '
                      'Spacing is to the nearest direct same-system neighbour in the review area, not on the whole floor. '
                      'Sampled distances are a plan proxy; this is not compliance, acoustic analysis or clash sign-off. '
                      'Trade links, door constraints, device footprints and final hostability are not checked.</p>')
    for n in errors:
        output.print_html(u'<p>{}</p>'.format(escape(n)))
    output.print_html(''.join(report.room_html(room, row) for room, row in rows))


if __name__ == '__main__':
    try:
        run()
    except OperationCanceledException:
        pass
    except ValueError as exc:
        forms.alert(str(exc), title=TITLE)
