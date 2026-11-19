"""Plan new devices in one empty room; never modify the Revit model."""
from fire_ews_geometry import (TOL, SegmentIndex, bounds, contained_ray, distance, edges, inside,
                               interior, minimum_distance, regular_samples)
from fire_ews_review import _wall_samples, ceiling_at, HALF_SPAN


def _exposed_wall_samples(walls, boundary, solids, rays):
    """Keep only wall faces with an air side inside the selected footprint."""
    for a, b in walls:
        length = distance(a, b)
        if length <= TOL:
            continue
        nx, ny = (b[1]-a[1])/length, (a[0]-b[0])/length
        for p in _wall_samples([(a, b)]):
            for offset in (-5.0, 5.0):
                q = p[0]+nx*offset, p[1]+ny*offset
                if (inside(q, boundary) and not any(inside(q, shell) for shell in solids)
                        and contained_ray(q, p, rays)):
                    yield p, q


def plan(room, ceilings, obstacles, rules, paired):
    if not rules['reference'] or any(rules[k] is None for k in
                                     ('wall', 'separation', 'detector_radius', 'detector_edge')):
        raise ValueError('New placement needs a design reference, wall/fixture clearance, and both detector reach limits.')
    ceiling_scope = room.get('ceiling_zone', False)
    if not room['walls'] or not room['wall_heights_verified']:
        raise ValueError('Could not verify physical walls at ceiling height. Check the architectural wall model.')
    if not ceiling_scope and room['open_edges']:
        raise ValueError('This is not a closed room. Pick the ceiling instead to ignore room separators.')
    boundary, box = room['boundary'], room['box']
    if max(box[2]-box[0], box[3]-box[1]) > 30000:
        raise ValueError('New placement currently supports areas up to 30 m across.')
    physical_barriers = room.get('barriers', [])
    rays = SegmentIndex(boundary + physical_barriers)
    solids = room.get('wall_solids', [])
    interior_points, pitch = regular_samples(box, boundary, 500, limit=4000)
    if ceiling_scope:
        interior_points = [p for p in interior_points if not any(inside(p, shell) for shell in solids)]
        walls = list(_exposed_wall_samples(room['walls'], boundary, solids, rays))
    else:
        walls = [(p, p) for p in _wall_samples(room['walls'])]
    demand = [(p, p, rules['detector_radius']) for p in interior_points]
    demand.extend((p, air, rules['detector_edge']) for p, air in walls)
    if not interior_points or not walls:
        raise ValueError('The selected area has insufficient open area or physical wall samples within its footprint.')
    c = ceiling_at(interior_points[0], ceilings)
    if c is None or any(ceiling_at(p, ceilings) is not c for p in interior_points):
        raise ValueError('The selected area must be covered by one unambiguous horizontal ceiling patch.')
    if c['finish'] == 'tile' and c.get('grid'):
        stations = []
        tile_barriers = SegmentIndex(physical_barriers) if ceiling_scope else rays
        for centre, tile, halves in c['grid'].tiles(box, c['boundary']):
            if not ceiling_scope and not all(interior(p, boundary) for p in tile):
                continue
            tile_edges = edges([tile])
            if any(not contained_ray(a, b, tile_barriers) for a, b in tile_edges) or any(
                    interior(a, tile_edges) for a, b in tile_barriers.search(bounds([tile]))):
                continue
            if not paired:
                stations.append((centre,))
            elif halves and abs(distance(*halves)-2*HALF_SPAN) <= 2 and abs(
                    minimum_distance(halves[0], tile_edges)-HALF_SPAN) <= 2:
                stations.extend((halves, halves[::-1]))
    elif c['finish'] == 'flush':
        cx, cy = ((box[0]+box[2])/2, (box[1]+box[3])/2)
        axis = 0 if box[2]-box[0] >= box[3]-box[1] else 1
        centres = (interior_points[:] if ceiling_scope else
                   [p for p in interior_points if abs(p[1-axis]-(cy if axis == 0 else cx)) <= pitch/2])
        centres.append((cx, cy))
        stations = []
        for x, y in centres:
            if not paired:
                stations.append(((x, y),))
                continue
            y0, x0 = (y, x) if ceiling_scope else (cy, cx)
            targets = ((x-HALF_SPAN, y0), (x+HALF_SPAN, y0)) if axis == 0 else (
                (x0, y-HALF_SPAN), (x0, y+HALF_SPAN))
            stations.extend((targets, targets[::-1]))
    else:
        raise ValueError('Confirm this ceiling finish and a usable native grid for lay-in tiles before placing.')
    separation = rules['separation']
    centre = tuple(sum(p[i] for p in interior_points)/len(interior_points) for i in (0, 1))
    candidates = []
    for station in stations:
        if paired and (distance(*station)+1e-6 < separation or
                       not contained_ray(station[0], station[1], rays) or
                       not contained_ray(station[0], station[1], c['boundary'])):
            continue
        if not all(ceiling_at(q, ceilings) is c and interior(q, boundary)
                   and not any(inside(q, shell) for shell in solids)
                   and minimum_distance(q, room['walls']) >= rules['wall']
                   and all(abs(d['z']-c['z']) > 150 or distance(q, d['point'])+1e-6 >= separation
                           for d in obstacles) for q in station):
            continue
        served = set(i for i, (p, air, limit) in enumerate(demand)
                     if distance(station[0], p) <= limit and contained_ray(station[0], air, rays))
        if served:
            midpoint = tuple(sum(q[i] for q in station)/len(station) for i in (0, 1))
            candidates.append((station, served, distance(midpoint, centre)))
    left = set(range(len(demand)))
    selected = []
    while left:
        options = [(-len(left & served), central, station, served) for station, served, central in candidates
                   if all(distance(a, b)+1e-6 >= separation for a in station for prior in selected for b in prior)]
        if not options:
            break
        count, _, station, served = min(options, key=lambda x: x[:3])
        if count == 0:
            break
        selected.append(station)
        left.difference_update(served)
    if left:
        raise ValueError('No complete proposal found: {} of {} reach samples remain outside your limits. '
                         'The tool will not offer partial placement.'.format(len(left), len(demand)))
    for index, kind in enumerate(('detector', 'speaker') if paired else ('detector',)):
        limit = rules[kind+'_pitch']
        if limit is not None and len(selected) > 1 and any(not any(
                other is not station and distance(station[index], other[index]) <= limit
                and contained_ray(station[index], other[index], rays) for other in selected)
                for station in selected):
            raise ValueError('The proposed {} stations fail the entered nearest-neighbour limit. Nothing was placed.'.format(kind))
    result = []
    for station in selected:
        for kind, q in zip(('detector', 'speaker'), station):
            result.append({'id': None, 'kind': kind, 'from': None, 'to': q, 'z': c['z'],
                           'ceiling': c['id'], 'wall': minimum_distance(q, room['walls']),
                           'bar': minimum_distance(q, c['bars']) if c.get('grid') else None,
                           'reason': 'New placement preview'})
    return c, result, len(demand)
