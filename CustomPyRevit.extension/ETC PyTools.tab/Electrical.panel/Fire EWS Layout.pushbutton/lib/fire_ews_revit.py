"""Extract plain host-coordinate geometry from one authoritative link."""
from pyrevit import DB
from pyrevit.revit import query

import fire_ews_geometry as geom
from fire_ews_review import HEIGHT_TOL

MM = 304.8


def key(e):
    eid = e.Id
    return int(eid.Value if hasattr(eid, 'Value') else eid.IntegerValue)


def xyz(p):
    return p.X*MM, p.Y*MM, p.Z*MM


def alive(e, phase):
    return e.GetPhaseStatus(phase) in (DB.ElementOnPhaseStatus.Existing, DB.ElementOnPhaseStatus.New)


def level_elements(doc, level, category):
    return list(DB.FilteredElementCollector(doc).OfCategory(category)
                .WherePasses(DB.ElementLevelFilter(level)).WhereElementIsNotElementType())


def available_rooms(doc, level, phase):
    return [r for r in level_elements(doc, level, DB.BuiltInCategory.OST_Rooms)
            if r.Area > 0 and r.DesignOption is None and
            r.get_Parameter(DB.BuiltInParameter.ROOM_PHASE).AsElementId() == phase]


def curve_points(curve, tf):
    return [xyz(tf.OfPoint(p)) for p in curve.Tessellate()]


def box_corners(bounds, tf):
    return [xyz(tf.OfPoint(bounds.Transform.OfPoint(DB.XYZ(x, y, z))))
            for x in (bounds.Min.X, bounds.Max.X) for y in (bounds.Min.Y, bounds.Max.Y)
            for z in (bounds.Min.Z, bounds.Max.Z)]


def room_data(room, link, opts):
    doc, tf = room.Document, link.GetTotalTransform() if link else DB.Transform.Identity
    loops, walls, spans, opens = [], [], [], []
    boundary = room.GetBoundarySegments(opts)
    bb = room.get_BoundingBox(None)
    if not boundary or not bb:
        raise ValueError('Room {} has no bounded model geometry.'.format(room.Number))
    for collection in boundary:
        points = []
        for segment in collection:
            pts = [p[:2] for p in curve_points(segment.GetCurve(), tf)]
            points.extend(pts[:-1])
            pieces = list(zip(pts, pts[1:]))
            e = doc.GetElement(segment.ElementId)
            etf = tf
            if isinstance(e, DB.RevitLinkInstance):
                inner = e.GetLinkDocument()
                etf = tf.Multiply(e.GetTotalTransform())
                e = inner.GetElement(segment.LinkElementId) if inner else None
            if isinstance(e, DB.Wall):
                walls.extend(pieces)
                wb = e.get_BoundingBox(None)
                if wb:
                    z = [p[2] for p in box_corners(wb, etf)]
                    spans.append((min(z), max(z)))
                else:
                    spans.append(None)
            else:
                opens.extend(pieces)
        if len(points) >= 3:
            loops.append(points)
    if not loops:
        raise ValueError('Room {} has no usable boundary loops.'.format(room.Number))
    zs = [p[2] for p in box_corners(bb, tf)]
    return {'id': key(room), 'number': room.Number, 'name': query.get_name(room),
            'loops': loops, 'boundary': geom.edges(loops), 'box': geom.bounds(loops),
            'walls': walls, 'wall_spans': spans, 'open_edges': opens, 'zmin': min(zs),
            'zmax': max(zs), 'area': room.Area*MM**2/1e6}


def linked_phase_id(host, link, host_phase):
    phase_map = host.GetElement(link.GetTypeId()).GetPhaseMap()
    if not phase_map.ContainsKey(host_phase):
        raise ValueError('The selected link does not map the active view phase.')
    return phase_map[host_phase]


def wall_meshes(geometry, transform):
    triangles = []
    for obj in geometry:
        if isinstance(obj, DB.GeometryInstance):
            triangles.extend(wall_meshes(obj.GetInstanceGeometry(), transform))
        elif isinstance(obj, DB.Solid) and obj.Volume > 0:
            shell = []
            for face in obj.Faces:
                mesh = face.Triangulate()
                for i in range(mesh.NumTriangles):
                    tri = mesh.get_Triangle(i)
                    shell.append([xyz(transform.OfPoint(tri.get_Vertex(j))) for j in range(3)])
            if shell:
                triangles.append(shell)
    return triangles


def ceiling_zone(c, footprint, host, link, host_phase, source_phase):
    """Use the ceiling as the envelope and section walls 10 mm below its underside."""
    box = footprint['box']
    underside = footprint['z']
    walls, solids, warnings = [], [], []
    tf = link.GetTotalTransform() if link else DB.Transform.Identity
    sources = [(host, DB.Transform.Identity, host_phase)]
    if link:
        sources.append((c.Document, tf, source_phase))
    if underside is not None:
        cut = underside-10
        x0, y0, x1, y1 = box
        for doc, transform, phase in sources:
            inv = transform.Inverse
            corners = [inv.OfPoint(DB.XYZ(x/MM, y/MM, z/MM))
                       for x in (x0-500, x1+500) for y in (y0-500, y1+500)
                       for z in (cut-1, cut+1)]
            lo = DB.XYZ(min(p.X for p in corners), min(p.Y for p in corners), min(p.Z for p in corners))
            hi = DB.XYZ(max(p.X for p in corners), max(p.Y for p in corners), max(p.Z for p in corners))
            collector = (DB.FilteredElementCollector(doc).OfClass(DB.Wall)
                         .WherePasses(DB.BoundingBoxIntersectsFilter(DB.Outline(lo, hi)))
                         .WhereElementIsNotElementType())
            for wall in collector:
                if not alive(wall, phase) or wall.DesignOption is not None:
                    continue
                geometry = wall.get_Geometry(DB.Options())
                shells = wall_meshes(geometry, transform) if geometry else []
                if not shells:
                    warnings.append('Wall {} has no supported solid geometry.'.format(key(wall)))
                for shell in shells:
                    segments = [geom.section_triangle(tri, cut) for tri in shell]
                    segments = [s for s in segments if s]
                    if segments:
                        solids.append(segments)
                        walls.extend(segments)
    zone = {'id': 'ceiling-{}'.format(footprint['id']), 'number': 'Ceiling {}'.format(footprint['id']),
            'name': footprint['name'], 'loops': footprint['loops'], 'boundary': footprint['boundary'],
            'box': box, 'zmin': underside if underside is not None else -float('inf'),
            'zmax': underside if underside is not None else float('inf'),
            'area': geom.area(footprint['loops'])/1e6, 'walls': walls, 'barriers': walls,
            'wall_solids': solids, 'open_edges': footprint['boundary'], 'ceiling_zone': True,
            'ceiling_patch': footprint['id'],
            'wall_heights_verified': bool(walls and not warnings), 'warnings': warnings}
    return zone


def ceiling_types(doc, level, phase):
    return [c for c in level_elements(doc, level, DB.BuiltInCategory.OST_Ceilings)
            if c.DesignOption is None and alive(c, phase)]


def in_region(e, tf, scope):
    bb = e.get_BoundingBox(None)
    return bool(bb and geom.overlap(geom.bounds([[p[:2] for p in box_corners(bb, tf)]]), scope))


def ceiling_data(c, tf, finish, dims, scope):
    bb = c.get_BoundingBox(None)
    if not bb:
        return []
    corners = [p[:2] for p in box_corners(bb, tf)]
    if not geom.overlap(geom.bounds([corners]), scope):
        return []
    name = query.get_name(c.Document.GetElement(c.GetTypeId()))
    base = {'element': key(c), 'name': name, 'finish': finish, 'grid': None, 'bars': []}
    boards = []
    if isinstance(c, DB.Ceiling):
        boards = [c.GetGeometryObjectFromReference(r) for r in DB.HostObjectUtils.GetBottomFaces(c)]
    if not boards:
        base.update(id=str(key(c)), box=geom.bounds([corners]), z=None, boundary=geom.edges([
            _box_polygon(geom.bounds([corners]))]), loops=[],
            warning='No supported underside; bounding box used to block ambiguous XY targets')
        return [base]
    lines, native_warning = [], None
    if finish == 'tile':
        if not hasattr(c, 'GetCeilingGridLines'):
            native_warning = 'Native grid API requires Revit 2025.3 or later'
        else:
            for curve in c.GetCeilingGridLines(False):
                if isinstance(curve, DB.Line) and curve.IsBound:
                    lines.append(tuple(curve_points(curve, tf)))
    output = []
    for index, face in enumerate(boards):
        loops = []
        for loop in face.GetEdgesAsCurveLoops():
            points = [p[:2] for curve in loop for p in curve_points(curve, tf)[:-1]]
            if len(points) >= 3:
                loops.append(points)
        if not loops or geom.area(loops) <= 1:
            continue
        z = None
        if isinstance(face, DB.PlanarFace):
            normal = tf.OfVector(face.FaceNormal)
            if abs(normal.Z+1.0) < 1e-8:
                z = xyz(tf.OfPoint(face.Origin))[2]
        bars = [(a[:2], b[:2]) for a, b in lines if z is not None and
                abs(a[2]-z) <= geom.TOL and abs(b[2]-z) <= geom.TOL]
        warning = ('Sloped or non-planar underside' if z is None else
                   ('Ceiling type not mapped; audit only' if finish == 'unknown' else
                    (native_warning or 'No usable native grid on underside' if finish == 'tile' and not bars else None)))
        patch = dict(base, id='{}:{}'.format(key(c), index+1), face_index=index, box=geom.bounds(loops),
                     z=z, loops=loops, boundary=geom.edges(loops), bars=bars, warning=warning)
        if bars:
            patch['grid'] = geom.TileGrid(bars, dims)
        output.append(patch)
    if not output:
        base.update(id=str(key(c)), box=geom.bounds([corners]), z=None, boundary=geom.edges([
            _box_polygon(geom.bounds([corners]))]), loops=[], warning='Underside cannot be projected; targets blocked')
        output.append(base)
    return output


def _box_polygon(box):
    x0, y0, x1, y1 = box
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def type_labels(doc, type_ids):
    labels = {}
    for tid, count in type_ids:
        symbol = doc.GetElement(tid)
        family = query.get_name(symbol.Family) + ' : ' if isinstance(symbol, DB.FamilySymbol) else ''
        labels[key(symbol)] = u'{}{} [id {}{}]'.format(family, query.get_name(symbol), key(symbol),
                                                       '; {}'.format(count) if count else '')
    return labels


def host_devices(doc, phase, rooms):
    """Whole room, not RCP crop or view filters. Non-device services are not surveyed."""
    categories = [DB.BuiltInCategory.OST_FireAlarmDevices, DB.BuiltInCategory.OST_CommunicationDevices,
                  DB.BuiltInCategory.OST_ElectricalFixtures, DB.BuiltInCategory.OST_SecurityDevices,
                  DB.BuiltInCategory.OST_LightingFixtures, DB.BuiltInCategory.OST_Sprinklers]
    box = geom.bounds([_box_polygon(r['box']) for r in rooms])
    lo = min(r.get('occupancy_zmin', r['zmin']) for r in rooms)-HEIGHT_TOL
    hi = max(r['zmax'] for r in rooms)+HEIGHT_TOL
    outline = DB.Outline(DB.XYZ(box[0]/MM, box[1]/MM, lo/MM), DB.XYZ(box[2]/MM, box[3]/MM, hi/MM))
    spatial = DB.BoundingBoxIntersectsFilter(outline)
    result, type_ids, counts = [], {}, {}
    for cat in categories:
        mappable = cat not in (DB.BuiltInCategory.OST_LightingFixtures, DB.BuiltInCategory.OST_Sprinklers)
        if mappable:
            for symbol in DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol).OfCategory(cat):
                type_ids[key(symbol)] = symbol.Id
        elements = (DB.FilteredElementCollector(doc).OfCategory(cat).WherePasses(spatial)
                    .WhereElementIsNotElementType())
        for e in elements:
            if (not isinstance(e, DB.FamilyInstance) or not isinstance(e.Location, DB.LocationPoint) or
                    e.SuperComponent is not None or not alive(e, phase) or e.DesignOption is not None):
                continue
            x, y, z = xyz(e.Location.Point)
            if not (lo <= z <= hi and box[0] <= x <= box[2] and box[1] <= y <= box[3]):
                continue
            tid = key(e.Symbol)
            if mappable:
                type_ids[tid] = e.Symbol.Id
                counts[tid] = counts.get(tid, 0)+1
            result.append({'id': key(e), 'type': tid, 'point': (x, y), 'z': z, 'kind': 'obstacle',
                           'fire_alarm': cat == DB.BuiltInCategory.OST_FireAlarmDevices,
                           'fixed': 'Pinned' if e.Pinned else ('Group member' if
                                     e.GroupId != DB.ElementId.InvalidElementId else None)})
    return result, type_labels(doc, [(eid, counts.get(tid, 0)) for tid, eid in type_ids.items()])


def existing_layout_devices(room, devices, mapped):
    return [d['id'] for d in devices if (d['fire_alarm'] or d['type'] in mapped)
            and room.get('occupancy_zmin', room['zmin'])-HEIGHT_TOL <= d['z'] <= room['zmax']+HEIGHT_TOL
            and geom.inside(d['point'], room['boundary'])]


def place_layout(host, level, phase, ceiling, patch, link, symbols, targets):
    """Create and verify the exact preview in the caller's open transaction."""
    tf = link.GetTotalTransform() if link else DB.Transform.Identity
    refs = DB.HostObjectUtils.GetBottomFaces(ceiling)
    if patch['face_index'] >= refs.Count:
        raise ValueError('The previewed ceiling face no longer exists.')
    local_ref = refs[patch['face_index']]
    face = ceiling.GetGeometryObjectFromReference(local_ref)
    ref = local_ref.CreateLinkReference(link) if link else local_ref
    for symbol in symbols.values():
        if not symbol.IsActive:
            symbol.Activate()
    host.Regenerate()
    created = []
    for target in targets:
        symbol = symbols[target['kind']]
        p = DB.XYZ(target['to'][0]/MM, target['to'][1]/MM, target['z']/MM)
        hit = face.Project(tf.Inverse.OfPoint(p))
        if hit is None or hit.Distance*MM > 2:
            raise ValueError('A preview target is not on the selected ceiling face.')
        method = symbol.Family.FamilyPlacementType
        if method == DB.FamilyPlacementType.WorkPlaneBased:
            e = host.Create.NewFamilyInstance(ref, p, DB.XYZ.BasisX, symbol)
        elif method == DB.FamilyPlacementType.OneLevelBased:
            e = host.Create.NewFamilyInstance(p, symbol, level, DB.Structure.StructuralType.NonStructural)
            host.Regenerate()
            if not isinstance(e.Location, DB.LocationPoint):
                raise ValueError('A new level-based device has no point location.')
            delta = p-e.Location.Point
            if delta.GetLength()*MM > 2:
                DB.ElementTransformUtils.MoveElement(host, e.Id, delta)
        else:
            raise ValueError('Unsupported placement method: {}.'.format(method))
        created.append((e, p, symbol, method))
    host.Regenerate()
    for e, p, symbol, method in created:
        if (e.GetTypeId() != symbol.Id or not isinstance(e.Location, DB.LocationPoint)
                or e.Location.Point.DistanceTo(p)*MM > 2 or e.CreatedPhaseId != phase):
            raise ValueError('A newly created device did not match the previewed type, location or phase.')
        if method == DB.FamilyPlacementType.WorkPlaneBased:
            actual = e.HostFace
            if not actual or (link and (actual.ElementId != link.Id or actual.LinkedElementId != ceiling.Id)):
                raise ValueError('A newly created device did not retain the intended ceiling host.')
            actual_local = actual.CreateReferenceInLink() if link else actual
            if actual_local.ConvertToStableRepresentation(ceiling.Document) != local_ref.ConvertToStableRepresentation(ceiling.Document):
                raise ValueError('A newly created device did not retain the intended ceiling face.')
        elif e.LevelId != level.Id:
            raise ValueError('A newly created device did not retain the intended level.')
    return [key(e) for e, _, _, _ in created]


def room_context(rooms, ceilings, devices):
    for room in rooms:
        room['ambiguous_devices'] = set()
        if room.get('ceiling_zone'):
            continue
        relevant = [c for c in ceilings if geom.overlap(room['box'], c['box']) and
                    c['z'] is not None and room['zmin'] <= c['z'] <= room['zmax']+HEIGHT_TOL]
        heights = set(c['z'] for c in relevant)
        spans = room['wall_spans']
        room['wall_heights_verified'] = bool(heights and spans and all(
            span and all(span[0] <= z <= span[1]+geom.TOL for z in heights) for span in spans))
    for d in devices:
        matches = [r for r in rooms if r['zmin']-HEIGHT_TOL <= d['z'] <= r['zmax']+HEIGHT_TOL and
                   geom.inside(d['point'], r['boundary'])]
        if len(matches) > 1:
            for r in matches:
                r['ambiguous_devices'].add(d['id'])
