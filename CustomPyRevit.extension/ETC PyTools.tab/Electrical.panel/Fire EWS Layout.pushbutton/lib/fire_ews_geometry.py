"""Plan geometry in host millimetres. No Revit dependency."""
import math
from bisect import bisect_left, bisect_right


TOL = 2.0


def dot(a, b):
    return a[0]*b[0]+a[1]*b[1]


def sub(a, b):
    return a[0]-b[0], a[1]-b[1]


def cross(a, b):
    return a[0]*b[1]-a[1]*b[0]


def distance(a, b):
    return math.hypot(*sub(a, b))


def edges(loops):
    return [(a, b) for loop in loops for a, b in zip(loop, loop[1:]+loop[:1])
            if a != b]


def bounds(loops):
    pts = [p for loop in loops for p in loop]
    return (min(p[0] for p in pts), min(p[1] for p in pts),
            max(p[0] for p in pts), max(p[1] for p in pts))


def overlap(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def segment_box(a, b):
    return min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])


class SegmentIndex(object):
    def __init__(self, segments, step=2500):
        self.cells = {}
        self.step = step
        self.segments = segments
        self.boxes = [segment_box(a, b) for a, b in segments]
        for i, box in enumerate(self.boxes):
            for cell in self._cells(box):
                self.cells.setdefault(cell, []).append(i)

    def _cells(self, box):
        x0, y0, x1, y1 = [int(math.floor(v/self.step)) for v in box]
        for x in range(x0, x1+1):
            for y in range(y0, y1+1):
                yield x, y

    def search(self, box):
        ids = set()
        for cell in self._cells(box):
            ids.update(self.cells.get(cell, ()))
        return [self.segments[i] for i in ids if overlap(box, self.boxes[i])]


def area(loops):
    total = 0.0
    for loop in loops:
        sign = -1 if sum(inside(loop[0], edges([other])) for other in loops
                         if other is not loop) % 2 else 1
        ref = loop[0]
        total += sign * abs(sum(cross(sub(a, ref), sub(b, ref))
                                for a, b in edges([loop]))) / 2.0
    return total


def minimum_distance(p, segments):
    if not segments:
        return None
    best = float('inf')
    for a, b in segments:
        ab = sub(b, a)
        length2 = dot(ab, ab)
        q = min(1.0, max(0.0, dot(sub(p, a), ab)/length2)) if length2 else 0
        best = min(best, (p[0]-a[0]-ab[0]*q)**2+(p[1]-a[1]-ab[1]*q)**2)
    return math.sqrt(best)


def inside(p, segments):
    x, y = p
    parity = False
    for (ax, ay), (bx, by) in segments:
        if (ay > y) != (by > y) and x < (bx-ax)*(y-ay)/(by-ay)+ax:
            parity = not parity
    return parity


def interior(p, segments, clear=TOL):
    return bool(segments) and inside(p, segments) and minimum_distance(p, segments) >= clear


def contained_ray(start, end, boundary):
    """Reject paths through concavities or holes, even if both endpoints are inside."""
    move = sub(end, start)
    length2 = dot(move, move)
    if not length2:
        return True
    segments = boundary.search(segment_box(start, end)) if isinstance(boundary, SegmentIndex) else boundary
    for a, b in segments:
        edge = sub(b, a)
        den = cross(move, edge)
        delta = sub(a, start)
        if abs(den) < 1e-8:
            if abs(cross(delta, move)) < 1e-8:
                lo, hi = sorted((dot(delta, move)/length2, dot(sub(b, start), move)/length2))
                if lo < 1-1e-8 and hi > 1e-8:
                    return False
            continue
        t, u = cross(delta, edge)/den, cross(delta, move)/den
        if 1e-8 < t < 1-1e-8 and -1e-8 <= u <= 1+1e-8:
            return False
    return True


def section_triangle(vertices, z):
    """Intersect one mesh triangle with a horizontal plane."""
    hits = []
    for a, b in zip(vertices, vertices[1:]+vertices[:1]):
        if (a[2] <= z < b[2]) or (b[2] <= z < a[2]):
            t = (z-a[2])/(b[2]-a[2])
            hits.append((a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t))
    if len(hits) == 2 and distance(*hits) > 0.0001:
        return tuple(hits)


def regular_samples(box, boundary, pitch, limit=500):
    """Interior sample centres, coarsened for large rooms; not a compliance envelope."""
    x0, y0, x1, y1 = box
    while max(1, int(math.ceil((x1-x0)/pitch)))*max(1, int(math.ceil((y1-y0)/pitch))) > limit:
        pitch *= 1.5
    nx, ny = max(1, int(math.ceil((x1-x0)/pitch))), max(1, int(math.ceil((y1-y0)/pitch)))
    pts = [(x0+(x1-x0)*(i+.5)/nx, y0+(y1-y0)*(j+.5)/ny)
           for i in range(nx) for j in range(ny)]
    return [p for p in pts if inside(p, boundary)], pitch


def _axis_lines(projected):
    groups = []
    for normal, lo, hi in sorted(projected):
        if groups and abs(normal-groups[-1][0]) <= TOL:
            groups[-1][1].append((lo, hi))
        else:
            groups.append((normal, [(lo, hi)]))
    for _, spans in groups:
        spans.sort()
    return groups


def _brackets(groups, interval):
    coords = [n for n, _ in groups]
    lo = max(0, bisect_left(coords, interval[0])-1)
    hi = min(len(groups), bisect_right(coords, interval[1])+1)
    return list(zip(groups[lo:hi-1], groups[lo+1:hi])) if hi-lo >= 2 else []


def _covers(spans, lo, hi):
    cursor = lo
    for a, b in spans:
        if a > cursor+TOL:
            return False
        cursor = max(cursor, b)
        if cursor >= hi-TOL:
            return True
    return False


class TileGrid(object):
    """One regular orthogonal orientation, with finite native line support per tile."""

    def __init__(self, segments, dims):
        self.axes = None
        self.groups = ([], [])
        self.dims = tuple(sorted(dims))
        if not segments:
            return
        v = sub(*max(segments, key=lambda s: distance(*s)))
        theta = math.atan2(v[1], v[0]) % (math.pi/2)
        self.axes = ((math.cos(theta), math.sin(theta)), (-math.sin(theta), math.cos(theta)))
        probe = ([], [])
        for a, b in segments:
            a, b = self.local(a), self.local(b)
            for idx in (0, 1):
                j = 1-idx
                if abs(a[idx]-b[idx]) <= TOL and abs(a[j]-b[j]) > TOL:
                    probe[idx].append(((a[idx]+b[idx])/2, min(a[j], b[j]), max(a[j], b[j])))
        self.groups = tuple(_axis_lines(s) for s in probe)

    def local(self, p):
        return tuple(dot(p, a) for a in self.axes)

    def host(self, u, v):
        a, b = self.axes
        return u*a[0]+v*b[0], u*a[1]+v*b[1]

    def tiles(self, box, ceiling_boundary):
        if not self.axes:
            return []
        result = []
        x0, y0, x1, y1 = box
        corners = [self.local(p) for p in ((x0, y0), (x0, y1), (x1, y0), (x1, y1))]
        limits = [(min(p[i] for p in corners), max(p[i] for p in corners)) for i in (0, 1)]
        for (u0, us0), (u1, us1) in _brackets(self.groups[0], limits[0]):
            du = u1-u0
            if not any(abs(du-d) <= TOL for d in self.dims):
                continue
            for (v0, vs0), (v1, vs1) in _brackets(self.groups[1], limits[1]):
                dv = v1-v0
                if any(abs(a-b) > TOL for a, b in zip(sorted((du, dv)), self.dims)):
                    continue
                if not all((_covers(us0, v0, v1), _covers(us1, v0, v1),
                            _covers(vs0, u0, u1), _covers(vs1, u0, u1))):
                    continue
                local_corners = ((u0, v0), (u1, v0), (u1, v1), (u0, v1))
                tile = [self.host(*p) for p in local_corners]
                tile_edges = edges([tile])
                if any(not inside(p, ceiling_boundary) and minimum_distance(p, ceiling_boundary) > TOL
                       for p in tile):
                    continue
                if any(not contained_ray(a, b, ceiling_boundary) and
                       minimum_distance(((a[0]+b[0])/2, (a[1]+b[1])/2), ceiling_boundary) > TOL
                       for a, b in tile_edges):
                    continue
                if any(interior(a, tile_edges) for a, _ in ceiling_boundary):
                    continue
                u, v = (u0+u1)/2, (v0+v1)/2
                halves = (self.host(u-du/4, v), self.host(u+du/4, v)) if du > dv else (
                    self.host(u, v-dv/4), self.host(u, v+dv/4))
                if max(du, dv) < 1.9*min(du, dv):
                    halves = ()
                result.append((self.host(u, v), tile, halves))
        return result
