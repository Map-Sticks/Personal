"""Deterministic, read-only coordination and geometric screening."""
import math

import fire_ews_geometry as geom


HEIGHT_TOL = 150.0
ALREADY = 25.0
PAIR_GAP = 1500.0
HALF_SPAN = 300.0
CENTRAL_PAIR_MAX_AREA_M2 = 25.0
CENTRAL_PAIR_MAX_SPAN_MM = 6000.0


def parse_rules(raw, new_placement=False):
    rules = {'reference': raw.get('reference', '').strip()}
    for key in ('wall', 'separation', 'travel', 'detector_pitch', 'detector_radius',
                'detector_edge', 'speaker_pitch'):
        text = raw.get(key, '').strip()
        if not text:
            rules[key] = None
            continue
        try:
            value = float(text)
        except ValueError:
            raise ValueError('{} must be a positive number in mm, or blank.'.format(key))
        if not 0 < value <= 100000:
            raise ValueError('{} must be greater than zero and at most 100,000 mm.'.format(key))
        rules[key] = value
    if not new_placement and rules['reference'] and any(
            rules[key] is None for key in ('wall', 'separation', 'travel')):
        raise ValueError('Enter minimum wall clearance, object separation and maximum move to enable proposals.')
    return rules


def ceiling_at(point, ceilings, z=None):
    possible = [c for c in ceilings if geom.inside(point, c['boundary']) and
                (z is None or c['z'] is None or abs(z-c['z']) <= HEIGHT_TOL)]
    return possible[0] if len(possible) == 1 and possible[0]['z'] is not None else None


def _finite_max(values):
    return max(values) if values and all(v is not None for v in values) else None


def _nearest_visible(p, points, boundary):
    ranked = sorted((geom.distance(p, q), q) for q in points)
    for distance, q in ranked:
        if geom.contained_ray(p, q, boundary):
            return distance
    return None


def _misses(values, radius):
    return sum(v is None or v > radius for v in values) if radius is not None else None


def _wall_samples(walls):
    for a, b in walls:
        n = min(50, max(1, int(math.ceil(geom.distance(a, b)/500))))
        for i in range(n):
            t = (i+.5)/n
            yield (a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t)


def review(room, ceilings, devices, kinds, rules, add_candidate=False, stop=None, reserved=None,
           pair_preview=False):
    """Review one room. Coordinates are XY mm, Z is host mm and outputs are proposals only."""
    boundary, box = room['boundary'], room['box']
    barriers = geom.SegmentIndex(boundary+room.get('barriers', []))
    actual_barriers = geom.SegmentIndex(room.get('barriers', []))
    wall_index = geom.SegmentIndex(room['walls'])
    solids = room.get('wall_solids', [])
    relevant = [c for c in ceilings if geom.overlap(c['box'], box) and
                (c['z'] is None or room['zmin'] <= c['z'] <= room['zmax']+HEIGHT_TOL)]
    scoped = [d for d in devices if room['zmin']-HEIGHT_TOL <= d['z'] <= room['zmax']+HEIGHT_TOL
              and d['id'] not in room.get('ambiguous_devices', set())
              and geom.inside(d['point'], boundary)
              and not any(geom.inside(d['point'], shell) for shell in solids)]
    proposals, metrics, notes = [], [], []
    samples, sample_pitch = geom.regular_samples(box, boundary, 500)
    if solids:
        samples = [p for p in samples if not any(geom.inside(p, shell) for shell in solids)]
    walls = room['walls']
    wall_samples = [p for p in _wall_samples(walls) if geom.inside(p, boundary)] if solids else list(
        _wall_samples(walls))
    can_propose = bool(rules['reference'] and walls and room.get('wall_heights_verified')
                       and not room.get('ambiguous_devices'))
    if not rules['reference']:
        notes.append('Measurement only: enter your design reference and the three move limits to preview moves.')
    physical = [d for d in devices if d['z'] >= room['zmin']-HEIGHT_TOL and
                d['z'] <= room['zmax']+HEIGHT_TOL]
    reserved = [] if reserved is None else reserved
    if not walls:
        notes.append('No confirmed physical wall faces.')
    if room.get('ceiling_zone'):
        notes.append('Ceiling footprint is the review limit, not a wall. Room separators are ignored. '
                     'Walls are sectioned 10 mm below the underside (host and selected link only). '
                     'No new-device gap candidates in this mode.')
        notes.extend(room.get('warnings', []))
    elif room.get('open_edges'):
        notes.append('Open or unverified room boundaries: neighbouring spaces were not merged; no gap candidates.')
    if walls and not room.get('wall_heights_verified'):
        notes.append('Not all physical walls are confirmed at ceiling height; positions are audit-only.')
    if room.get('ambiguous_devices'):
        notes.append('Overlapping room assignments: affected devices excluded and proposals withheld.')
    if not relevant:
        notes.append('No ceiling footprints in this room envelope.')
    for c in relevant:
        if c.get('warning'):
            notes.append('Ceiling {}: {}'.format(c['id'], c['warning']))
    panels = []
    preserved = set()
    if can_propose:
        for c in relevant:
            if c.get('grid') is not None:
                for centre, tile, halves in c['grid'].tiles(box, c['boundary']):
                    if geom.inside(centre, boundary):
                        panels.append((centre, c, tile, halves))
                        if halves and len(kinds) > 1:
                            matches = [[d for d in scoped if d['kind'] in kinds and
                                        abs(d['z']-c['z']) <= HEIGHT_TOL and
                                        geom.distance(d['point'], p) <= ALREADY] for p in halves]
                            if all(len(group) == 1 for group in matches) and \
                                    matches[0][0]['kind'] != matches[1][0]['kind']:
                                preserved.update(group[0]['id'] for group in matches)
            elif c.get('finish') == 'flush' and c['z'] is not None:
                if not pair_preview:
                    panels.extend((p, c, None, ()) for p in samples if geom.inside(p, c['boundary']))
    valid = []
    pair_tiles = {}
    for p, c, tile, halves in panels:
        if stop is not None and stop():
            return None
        clearance = rules['wall']
        x, y = p
        near_walls = wall_index.search((x-clearance, y-clearance, x+clearance, y+clearance))
        w = geom.minimum_distance(p, near_walls)
        tile_barriers = actual_barriers if room.get('ceiling_patch') == c['id'] else barriers
        tile_edges = geom.edges([tile]) if tile else []
        if (((pair_preview and bool(halves)) or w is None or w >= clearance) and geom.interior(p, boundary) and
                not any(geom.inside(p, shell) for shell in solids) and ceiling_at(p, relevant) is c and
                (not tile or (tile_barriers is actual_barriers or all(geom.interior(q, boundary) for q in tile))
                 and not any(not geom.contained_ray(a, b, tile_barriers) for a, b in tile_edges)
                 and not any(geom.interior(a, tile_edges)
                             for a, _ in tile_barriers.search(geom.bounds([tile]))))):
            valid.append((p, c))
            if pair_preview and halves and abs(geom.distance(*halves)-2*HALF_SPAN) <= geom.TOL and (
                    abs(geom.minimum_distance(halves[0], tile_edges)-HALF_SPAN) <= geom.TOL):
                pair_tiles.setdefault(c['id'], []).append(halves)
    if can_propose:
        if pair_preview:
            notes.append('{} complete 1200 x 600 panels passed the geometry checks. Wall clearance is '
                         'checked at each device target, not at the tile centre.'.format(
                             sum(len(tiles) for tiles in pair_tiles.values())))
        else:
            notes.append('{} full tile centres found; {} pass review boundary and wall-clearance checks '
                         'before device-specific checks.'.format(sum(tile is not None for _, _, tile, _ in panels),
                         sum(c.get('grid') is not None for _, c in valid)))

    def available(point, c, ignored=()):
        separation = rules['separation']
        return (all(d['id'] in ignored or abs(d['z']-c['z']) > HEIGHT_TOL or
                    geom.distance(point, d['point']) >= separation for d in physical)
                and all(abs(z-c['z']) > HEIGHT_TOL or geom.distance(point, q) >= separation
                        for q, z in reserved))

    for kind in kinds:
        if kind in rules.get('unmapped', set()):
            notes.append('{}: type mapping missing; not assessed.'.format(kind.capitalize()))
            continue
        current = sorted([d for d in scoped if d['kind'] == kind], key=lambda d: d['id'])
        baseline = {d['id']: d['point'] for d in current}
        planned = dict(baseline)
        pitch = rules[kind+'_pitch']
        radius = rules['detector_radius'] if kind == 'detector' else None
        edge_limit = rules['detector_edge'] if kind == 'detector' else None
        sampling = can_propose and kind == 'detector' and (room.get('ceiling_zone') or not room.get('open_edges'))
        room_values = ([_nearest_visible(p, list(baseline.values()), barriers) for p in samples]
                       if kind == 'detector' else [])
        wall_values = ([_nearest_visible(p, list(baseline.values()), barriers) for p in wall_samples]
                       if kind == 'detector' else [])
        for d in current:
            if stop is not None and stop():
                return None
            p, eid = d['point'], d['id']
            peers = [(geom.distance(p, q['point']), q['id']) for q in current
                     if q['id'] != eid and geom.contained_ray(p, q['point'], barriers)]
            nearest, peer = min(peers) if peers else (None, None)
            wall_distance = geom.minimum_distance(p, walls)
            c = ceiling_at(p, relevant, d['z'])
            grid_clear = geom.minimum_distance(p, c['bars']) if c and c.get('grid') else None
            issues = []
            if wall_distance is not None and rules['wall'] is not None and wall_distance < rules['wall']:
                issues.append('Below wall clearance')
            if nearest is not None and pitch is not None and nearest > pitch:
                issues.append('Nearest same-kind neighbour exceeds entered spacing')
            if not c:
                issues.append('Unresolved ceiling at insertion elevation')
            if d.get('fixed'):
                issues.append(d['fixed'])
            if eid in preserved:
                issues.append('Existing pair already aligned on measured tile halves')
            metric = {'id': eid, 'kind': kind, 'wall': wall_distance, 'peer': peer,
                            'pitch': nearest, 'grid_clear': grid_clear, 'issues': issues,
                            'ceiling': c['id'] if c else None}
            metrics.append(metric)
            if pair_preview:
                continue
            if d.get('fixed'):
                metric['decision'] = 'Keep: {}'.format(d['fixed'])
            elif eid in preserved:
                metric['decision'] = 'Keep: aligned pair'
            elif not can_propose:
                metric['decision'] = 'Audit only: see area notes'
            elif not c:
                metric['decision'] = 'Blocked: ceiling at device elevation unresolved'
            elif not c.get('grid'):
                metric['decision'] = 'Audit only: no confirmed tile grid on this ceiling'
            if 'decision' in metric:
                continue
            candidates = [(geom.distance(p, q), q) for q, patch in valid if c is patch]
            remaining = set()
            old_room_miss = _misses([_nearest_visible(q, list(planned.values()), barriers)
                                    for q in samples], radius) if sampling and radius else None
            old_edge_miss = _misses([_nearest_visible(q, list(planned.values()), barriers)
                                    for q in wall_samples], edge_limit) if sampling and edge_limit else None
            for travel, q in sorted(candidates):
                if travel > rules['travel']:
                    remaining.add('move limit')
                    break
                if not geom.contained_ray(p, q, barriers):
                    remaining.add('wall or review edge in move path')
                    continue
                if not available(q, c, (eid,)):
                    remaining.add('object clearance')
                    continue
                if travel <= ALREADY and wall_distance >= rules['wall'] and (pitch is None or
                                                                             nearest is None or nearest <= pitch):
                    metric['decision'] = 'Keep: already within 25 mm of a measured tile centre'
                    break
                if pitch is not None and len(planned) > 1:
                    others = [r for key, r in planned.items() if key != eid]
                    old = _nearest_visible(p, others, barriers)
                    new = _nearest_visible(q, others, barriers)
                    if (old is not None and (new is None or new > max(pitch, old)
                                            or (old > pitch and new >= old))):
                        remaining.add('same-system spacing')
                        continue
                trial = list(v for key, v in planned.items() if key != eid)+[q]
                if any(old is not None and _misses([_nearest_visible(s, trial, barriers)
                                                    for s in pts], limit) > old
                       for pts, limit, old in ((samples, radius, old_room_miss),
                                               (wall_samples, edge_limit, old_edge_miss))):
                    remaining.add('sampled detector reach worsens')
                    continue
                if (travel <= ALREADY and all(geom.minimum_distance(p, walls) >= rules['wall']
                                              and available(p, c, (eid, bid)) for p in old)
                        and geom.distance(*old)+1e-6 >= rules['separation']):
                    metric['decision'] = 'Keep: already within 25 mm of a measured tile centre'
                    break
                proposals.append({'id': eid, 'kind': kind, 'from': p, 'to': q, 'z': d['z'],
                                  'ceiling': c['id'], 'reason': 'Measured full tile centre',
                                  'wall': geom.minimum_distance(q, walls),
                                  'bar': geom.minimum_distance(q, c['bars'])})
                metric['decision'] = 'Proposed: see move list'
                reserved.append((q, c['z']))
                planned[eid] = q
                break
            if 'decision' not in metric:
                metric['decision'] = 'Blocked: {}'.format(', '.join(sorted(remaining)) or
                                                         'no full tile centre passed the geometry checks')

        if (add_candidate and not pair_preview and can_propose and not room.get('open_edges')
                and not room.get('ceiling_zone')
                and (not current or (kind == 'detector' and radius is not None))):
            pts = list(planned.values())
            gaps = []
            if radius is not None:
                for sample in samples:
                    value = _nearest_visible(sample, pts, boundary)
                    if value is None or value > radius:
                        gaps.append(sample)
            centre = ((box[0]+box[2])/2, (box[1]+box[3])/2)
            options = []
            for p, c in valid:
                if stop is not None and stop():
                    return None
                if not available(p, c) or (current and not gaps):
                    continue
                covered = sum(geom.distance(p, q) <= radius and geom.contained_ray(p, q, boundary)
                              for q in gaps)
                if current and not covered:
                    continue
                options.append((-covered, geom.distance(centre, p), c['id'], p, c['z']))
            if options:
                _, _, cid, p, z = min(options)
                proposals.append({'id': None, 'kind': kind, 'from': None, 'to': p, 'z': z,
                                  'ceiling': cid, 'reason': 'Option only: no in-scope devices' if not pts
                                  else 'Option only: improves sampled detector reach'})
                reserved.append((p, z))
            elif not current or gaps:
                notes.append('No coordinated {} candidate found.'.format(kind))
        metrics.append({'kind': kind, 'count': len(current),
                        'sample_count': len(samples) if kind == 'detector' else None,
                        'sample_pitch': sample_pitch, 'max_reach': _finite_max(room_values),
                        'unseen': sum(v is None for v in room_values) if kind == 'detector' else None,
                        'missed': _misses(room_values, radius),
                        'edge_count': len(wall_samples) if kind == 'detector' else None,
                        'edge_missed': _misses(wall_values, edge_limit),
                        'edge_reach': _finite_max(wall_values)})
    if pair_preview:
        notes.append('Pair preview considers only one-to-one matches on the same ceiling within 1,500 mm. '
                     'It keeps device counts and does not place new devices.')
        if can_propose:
            _preview_pairs(scoped, metrics, relevant, rules, pair_tiles, boundary, barriers, solids, walls,
                           box, room['area'], samples, wall_samples, available, reserved, proposals, stop)
            if stop is not None and stop():
                return None
        else:
            for m in metrics:
                if 'id' in m:
                    m['decision'] = 'Audit only: see area notes'
    return {'metrics': metrics, 'proposals': proposals, 'notes': notes, 'ceilings': relevant,
            'devices': scoped}


def _flush_pair_slots(a, b, box, samples):
    x0, y0, x1, y1 = box
    axis = 0 if x1-x0 >= y1-y0 else 1
    across = (y0+y1)/2 if axis == 0 else (x0+x1)/2
    positions = set(p[axis] for p in samples)
    positions.add((a[axis]+b[axis])/2)
    for t in sorted(positions):
        if axis == 0:
            yield (t-HALF_SPAN, across), (t+HALF_SPAN, across)
        else:
            yield (across, t-HALF_SPAN), (across, t+HALF_SPAN)


def _preview_pairs(scoped, metrics, ceilings, rules, pair_tiles, boundary, barriers, solids, walls,
                   box, area, samples, wall_samples, available, reserved, proposals, stop):
    rows = {m['id']: m for m in metrics if 'id' in m}
    devices = [d for d in scoped if d['id'] in rows]
    centre = None
    if (len(devices) == 2 and samples and 0 < area <= CENTRAL_PAIR_MAX_AREA_M2 and
            max(box[2]-box[0], box[3]-box[1]) <= CENTRAL_PAIR_MAX_SPAN_MM):
        centre = tuple(sum(p[i] for p in samples)/len(samples) for i in (0, 1))
    points = {d['kind']: {} for d in devices}
    patches = {}
    for d in devices:
        points[d['kind']][d['id']] = d['point']
        patches[d['id']] = ceiling_at(d['point'], ceilings, d['z'])
    partners = {d['id']: [] for d in devices}
    speakers = [d for d in devices if d['kind'] == 'speaker']
    for a in devices:
        if a['kind'] != 'detector' or not patches[a['id']]:
            continue
        for b in speakers:
            if (patches[b['id']] is patches[a['id']] and geom.distance(a['point'], b['point']) <= PAIR_GAP
                    and geom.contained_ray(a['point'], b['point'], barriers)):
                partners[a['id']].append(b)
                partners[b['id']].append(a)
    for d in sorted(devices, key=lambda d: d['id']):
        if stop is not None and stop():
            return
        eid = d['id']
        choices = partners[eid]
        if not patches[eid]:
            rows[eid]['decision'] = 'Blocked: no unambiguous ceiling at the device elevation'
        elif not choices:
            rows[eid]['decision'] = 'Keep: no partner on this ceiling within 1,500 mm, without crossing a barrier'
        elif len(choices) > 1 or len(partners[choices[0]['id']]) != 1:
            rows[eid]['decision'] = 'Blocked: more than one possible partner'
        elif d['kind'] == 'detector':
            b = choices[0]
            bid = b['id']
            c = patches[eid]
            old = (d['point'], b['point'])
            reason = None
            if d.get('fixed') or b.get('fixed'):
                reason = 'Keep pair: {}'.format(d.get('fixed') or b['fixed'])
            elif c.get('grid'):
                slots = pair_tiles.get(c['id'], ())
                placement = 'Opposite half-centres of one measured 1200 x 600 tile'
            elif c.get('finish') == 'flush':
                slots = _flush_pair_slots(old[0], old[1], box, samples)
                placement = 'Aligned along the review area centreline (flush ceiling)'
            else:
                reason = 'Blocked: ceiling finish or grid is unverified'
            if reason:
                rows[eid]['decision'] = rows[bid]['decision'] = reason
                continue
            prefer_centre = centre is not None and c.get('grid') is not None
            if prefer_centre:
                placement += '; favours the centre of this small, single-pair area'
            ranked = []
            for first, second in slots:
                # Preserve side order along the candidate tile or centreline.
                if geom.dot(geom.sub(old[1], old[0]), geom.sub(second, first)) < 0:
                    first, second = second, first
                travel = (geom.distance(old[0], first), geom.distance(old[1], second))
                midpoint = ((first[0]+second[0])/2, (first[1]+second[1])/2)
                central_distance = round(geom.distance(midpoint, centre), 3) if prefer_centre else 0
                ranked.append((central_distance, sum(travel), max(travel), first, second))
            rejected = set()
            for _, _, travel, first, second in sorted(ranked):
                if stop is not None and stop():
                    return
                targets = (first, second)
                if travel > rules['travel']:
                    rejected.add('move limit')
                    continue
                if geom.distance(first, second)+1e-6 < rules['separation']:
                    rejected.add('pair spacing below entered object clearance')
                    continue
                if (not geom.contained_ray(first, second, barriers)
                        or not geom.contained_ray(first, second, c['boundary']) or any(
                        not geom.interior(q, boundary) or ceiling_at(q, ceilings) is not c
                        or any(geom.inside(q, shell) for shell in solids)
                        or not geom.contained_ray(p, q, barriers)
                        or not geom.contained_ray(p, q, c['boundary']) for p, q in zip(old, targets))):
                    rejected.add('ceiling edge or wall in path')
                    continue
                wall_distances = [geom.minimum_distance(q, walls) for q in targets]
                if min(wall_distances) < rules['wall']:
                    rejected.add('wall clearance')
                    continue
                if not all(available(q, c, (eid, bid)) for q in targets):
                    rejected.add('object clearance')
                    continue
                if (_worsens_spacing(points['detector'], eid, first, rules['detector_pitch'], barriers) or
                        _worsens_spacing(points['speaker'], bid, second, rules['speaker_pitch'], barriers)):
                    rejected.add('same-system spacing')
                    continue
                if any(_worsens_reach(points['detector'], eid, first, pts, limit, barriers)
                       for pts, limit in ((samples, rules['detector_radius']),
                                          (wall_samples, rules['detector_edge'])) if limit is not None):
                    rejected.add('sampled detector reach')
                    continue
                if travel <= ALREADY:
                    reason = 'Keep pair: both already within 25 mm of aligned targets'
                    break
                for member, other, q, w in zip((d, b), (b, d), targets, wall_distances):
                    points[member['kind']][member['id']] = q
                    reserved.append((q, c['z']))
                    proposals.append({'id': member['id'], 'kind': member['kind'], 'from': member['point'],
                                      'to': q, 'z': member['z'], 'ceiling': c['id'], 'wall': w,
                                      'bar': geom.minimum_distance(q, c['bars']) if c.get('grid') else None,
                                      'reason': '{}; with {}'.format(placement, other['id'])})
                reason = 'Proposed pair: see move list'
                break
            if not reason:
                reason = 'Blocked pair: {}'.format(', '.join(sorted(rejected)) or 'no usable full tile or centreline')
            rows[eid]['decision'] = rows[bid]['decision'] = reason


def _worsens_spacing(points, eid, new, limit, barriers):
    if limit is None:
        return False
    for key, p in points.items():
        others = [q for i, q in points.items() if i != key and i != eid]
        old = _nearest_visible(p, others if key == eid else others+[points[eid]], barriers)
        revised = _nearest_visible(new if key == eid else p, others if key == eid else others+[new], barriers)
        if old is not None and (revised is None or revised > max(limit, old)+1e-6):
            return True
        if key == eid and old is not None and old > limit and revised >= old-1e-6:
            return True
    return False


def _worsens_reach(points, eid, new, samples, limit, barriers):
    others = [q for key, q in points.items() if key != eid]
    old = points[eid]
    return any(geom.distance(s, old) <= limit and geom.contained_ray(s, old, barriers)
               and not (geom.distance(s, new) <= limit and geom.contained_ray(s, new, barriers))
               and not any(geom.distance(s, q) <= limit and geom.contained_ray(s, q, barriers)
                           for q in others) for s in samples)
