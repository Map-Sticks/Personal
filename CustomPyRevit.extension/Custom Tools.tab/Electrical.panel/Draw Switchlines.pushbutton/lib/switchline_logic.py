# -*- coding: utf-8 -*-
"""Pure switchline layout logic for Draw Switchlines (no Revit API).

All coordinates are view-local 2D tuples (u, v) in feet: u = view right, v = view up.
Every generated segment is axis-aligned (orthogonal drafting).

A drawing session holds one or more switch groups. Each group is a set of switches/sensors and the
fittings they switch, either all together or every second fitting (outputs A/B). All groups are
planned together so wall-switch banks get non-crossing lanes, routes avoid existing and planned
switchlines, and crossings get hops.
"""
import heapq
from itertools import permutations

TOL = 1e-4
MM_PER_FOOT = 304.8

# Paper millimetres; multiplied by the view scale. Defaults match the 1:50 bedroom drafting
# (150 mm fitting gap, 100 mm lanes and hop radius at 1:50).
DEFAULTS = {
    'fitting_gap_mm': 3.0,
    'switch_gap_mm': 1.6,
    'lane_mm': 2.0,
    'hop_radius_mm': 2.0,
    'trunk_offset_mm': 6.0,
}

MODE_ALL = 'all'
MODE_ALTERNATE = 'alternate'
STYLE_CHAIN = 'chain'
STYLE_TRUNK = 'trunk'

OBSTACLE_PENALTY = 1000.0
OVERLAP_PENALTY = 500.0

# Lighting Devices whose family/type name contains one of these are ceiling devices (connected like a
# fitting); every other Lighting Device is a wall switch that leads out along its facing direction.
CEILING_DEVICE_KEYWORDS = ('sensor', 'detector', 'pir', 'occupancy', 'motion', 'presence')


def is_ceiling_device(name):
    text = (name or '').lower()
    return any(k in text for k in CEILING_DEVICE_KEYWORDS)


def paper_to_model(mm, scale):
    return float(mm) * float(scale) / MM_PER_FOOT


class Node(object):
    """A switch/sensor or fitting. ``facing`` is a unit (u, v) used only for wall devices."""

    def __init__(self, key, kind, u, v, facing=None, wall=False, label=''):
        self.key = key
        self.kind = kind
        self.u = float(u)
        self.v = float(v)
        self.facing = facing
        self.wall = bool(wall and facing)
        self.label = label

    @property
    def pt(self):
        return (self.u, self.v)


def make_group(switches, fittings, mode=MODE_ALL, style=None, horizontal_first=True, side=1):
    if style is None:
        style = STYLE_TRUNK if mode == MODE_ALTERNATE else STYLE_CHAIN
    return {'switches': list(switches), 'fittings': list(fittings), 'mode': mode, 'style': style,
            'horizontal_first': horizontal_first, 'side': side}


def _same(a, b):
    return abs(a[0] - b[0]) < TOL and abs(a[1] - b[1]) < TOL


def manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def is_horizontal(seg):
    return abs(seg[0][1] - seg[1][1]) < TOL


def seg_length(seg):
    return manhattan(seg[0], seg[1])


def mst_edges(points):
    """Prim's minimum spanning tree on Manhattan distance. Returns [(i, j)]."""
    n = len(points)
    if n < 2:
        return []
    inside = set([0])
    edges = []
    while len(inside) < n:
        best = None
        for i in sorted(inside):
            for j in range(n):
                if j in inside:
                    continue
                d = manhattan(points[i], points[j])
                if best is None or d < best[0] - TOL:
                    best = (d, i, j)
        edges.append((best[1], best[2]))
        inside.add(best[2])
    return edges


def l_path(a, b, horizontal_first=True):
    if abs(a[0] - b[0]) < TOL or abs(a[1] - b[1]) < TOL:
        return [a, b]
    corner = (b[0], a[1]) if horizontal_first else (a[0], b[1])
    return [a, corner, b]


def _move_towards(a, b, d):
    length = manhattan(a, b)
    if length < TOL:
        return a
    t = min(d, length) / length
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def trim_path(path, start_gap=0.0, end_gap=0.0):
    """Shorten the first/last legs of a polyline. Legs shorter than the gap collapse to 0 length."""
    path = list(path)
    if len(path) < 2:
        return path
    if start_gap > 0:
        path[0] = _move_towards(path[0], path[1], start_gap)
    if end_gap > 0:
        path[-1] = _move_towards(path[-1], path[-2], end_gap)
    return path


def path_segments(path):
    segs = []
    for i in range(len(path) - 1):
        if manhattan(path[i], path[i + 1]) > TOL:
            segs.append((path[i], path[i + 1]))
    return segs


def row_axis(points):
    """'u' when the points spread more horizontally than vertically."""
    us = [p[0] for p in points]
    vs = [p[1] for p in points]
    return 'u' if (max(us) - min(us)) >= (max(vs) - min(vs)) else 'v'


def alternate_split(fittings):
    """Sort fittings along their row and return (1st, 3rd, 5th...) and (2nd, 4th...)."""
    axis = row_axis([f.pt for f in fittings])
    idx = 0 if axis == 'u' else 1
    ordered = sorted(fittings, key=lambda f: (f.pt[idx], f.pt[1 - idx]))
    return ordered[0::2], ordered[1::2]


def _interior(p, seg, margin=TOL):
    (u1, v1), (u2, v2) = seg
    if is_horizontal(seg):
        return abs(p[1] - v1) < TOL and min(u1, u2) + margin < p[0] < max(u1, u2) - margin
    return abs(p[0] - u1) < TOL and min(v1, v2) + margin < p[1] < max(v1, v2) - margin


def crossing(s1, s2):
    """Interior crossing point of a horizontal and a vertical segment, else None."""
    if seg_length(s1) < TOL or seg_length(s2) < TOL or is_horizontal(s1) == is_horizontal(s2):
        return None
    h, v = (s1, s2) if is_horizontal(s1) else (s2, s1)
    p = (v[0][0], h[0][1])
    if _interior(p, h) and _interior(p, v):
        return p
    return None


def parallel_overlap(s1, s2, clearance):
    """Length over which two parallel segments run closer than ``clearance`` to each other."""
    if seg_length(s1) < TOL or seg_length(s2) < TOL or is_horizontal(s1) != is_horizontal(s2):
        return 0.0
    k, c = (0, 1) if is_horizontal(s1) else (1, 0)
    if abs(s1[0][c] - s2[0][c]) >= clearance:
        return 0.0
    lo = max(min(s1[0][k], s1[1][k]), min(s2[0][k], s2[1][k]))
    hi = min(max(s1[0][k], s1[1][k]), max(s2[0][k], s2[1][k]))
    return max(0.0, hi - lo)


def conflict_cost(segs, avoid, clearance, cross_cost):
    """Penalty for crossing (``cross_cost`` each) or running alongside (``avoid``) segments."""
    cost = 0.0
    for s in segs:
        for o in avoid:
            if crossing(s, o) is not None:
                cost += cross_cost
            elif parallel_overlap(s, o, clearance) > TOL:
                cost += OVERLAP_PENALTY
    return cost


def path_hits(path, obstacles, clearance=None):
    """Number of obstacles a polyline passes through (excluding its ends).

    ``obstacles`` are ((u, v), radius) pairs, or plain points using ``clearance`` as the radius.
    """
    hits = 0
    for item in obstacles:
        if isinstance(item[0], (tuple, list)):
            p, r = item
        else:
            p, r = item, clearance
        if manhattan(p, path[0]) < r or manhattan(p, path[-1]) < r:
            continue
        for a, b in path_segments(path):
            if _point_seg_distance(p, (a, b)) < r - TOL:
                hits += 1
                break
    return hits


def _point_seg_distance(p, seg):
    q = closest_on_segment(p, seg)
    return ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5


DIRS = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))
TOUCH_PENALTY = 300.0


def dir_index(vec):
    """Index into DIRS of the axis direction closest to ``vec``."""
    if abs(vec[0]) >= abs(vec[1]):
        return 0 if vec[0] >= 0 else 2
    return 1 if vec[1] >= 0 else 3


def gap_points(node, gap):
    """[(point, outward direction index)] where a switchline may end at (or leave) a fitting or a
    ceiling device. Wall-mounted fittings (``facing`` set) are only reached from the front."""
    if node.kind == 'fitting' and node.facing:
        dirs = [dir_index(node.facing)]
    else:
        dirs = [0, 1, 2, 3]
    return [((node.u + DIRS[d][0] * gap, node.v + DIRS[d][1] * gap), d) for d in dirs]


def _params_from_gap(gap):
    lane = gap * 2.0 / 3.0
    return {'fitting_gap': gap, 'switch_gap': gap * 1.6 / 3.0, 'lane': lane, 'hop_radius': lane,
            'trunk_offset': gap * 2.0}


def obstacle_list(items, params):
    """Normalise obstacles to sorted [((u, v), radius)].

    Accepts Nodes, plain (u, v) points (fitting sized) or ((u, v), radius) pairs. Fittings and
    ceiling devices keep (most of) the fitting gap clear; wall switches their small symbol plus a
    keep-out strip in front of it (where its own line leaves) so other lines cannot run along the
    face of a switch bank.
    """
    big = params['fitting_gap'] * 0.9
    small = params['switch_gap'] * 0.9
    half = params['lane'] * 0.5
    found = {}

    def add(pt, r):
        key = (round(pt[0], 3), round(pt[1], 3))
        if key not in found or found[key][1] < r:
            found[key] = (pt, r)

    for item in items or []:
        if isinstance(item, Node):
            if item.kind == 'switch' and item.wall:
                add(item.pt, small)
                f = item.facing
                for dist in (params['switch_gap'], params['switch_gap'] + half):
                    add((item.u + f[0] * dist, item.v + f[1] * dist), half)
            else:
                add(item.pt, big)
        elif isinstance(item[0], (tuple, list)):
            add((float(item[0][0]), float(item[0][1])), float(item[1]))
        else:
            add((float(item[0]), float(item[1])), big)
    return sorted(found.values())


def _merge_coords(values, lo, hi):
    out = []
    for v in sorted(values):
        if v < lo - TOL or v > hi + TOL:
            continue
        if not out or v - out[-1] > TOL * 5:
            out.append(v)
    return out


def _nearest(values, v):
    best = 0
    for i, x in enumerate(values):
        if abs(x - v) < abs(values[best] - v):
            best = i
    return best


def _simplify(path):
    pts = []
    for p in path:
        if pts and manhattan(p, pts[-1]) < TOL:
            continue
        if len(pts) >= 2:
            a, b = pts[-2], pts[-1]
            if (abs(a[0] - b[0]) < TOL and abs(b[0] - p[0]) < TOL) or \
                    (abs(a[1] - b[1]) < TOL and abs(b[1] - p[1]) < TOL):
                pts[-1] = p
                continue
        pts.append(p)
    return pts


def grid_route(starts, goals, tees, obstacles, avoid, params):
    """Least-cost orthogonal polyline on a grid of the coordinates that matter.

    starts: [(point, direction index, lead points, may turn at once, start cost)]. Starts that may
        not turn at once run straight for at least one lane first (a line leaving a fitting/sensor).
    goals: [(point, arrival direction index)]: fitting gap points, reached heading into the fitting.
    tees: [(horizontal, line coordinate, lo, hi)]: tee perpendicular into a line between lo and hi.
    obstacles: [((u, v), radius)] never to pass through.
    avoid: segments not to run along or touch, and to cross only where a hop fits.
    Returns (points, tag, cost); tag is ('gap', goal index) or ('tee', point). (None, None, None)
    if there is no route.
    """
    if not starts or not (goals or tees):
        return None, None, None
    lane = params['lane']
    gap = params['fitting_gap']
    bend_cost = lane * 3.0
    cross_cost = lane * 4.0
    near_hop_cost = lane * 6.0
    near_parallel_cost = lane * 4.0
    hop_room = params['hop_radius'] + lane * 0.5
    min_leg = lane
    walls = params.get('walls') or []
    wall_depth = lane * 2.0
    pad = gap * 3.0 + lane * 4.0

    key_pts = [s[0] for s in starts] + [g[0] for g in goals]
    for horiz, c, lo, hi in tees:
        key_pts.append((lo, c) if horiz else (c, lo))
        key_pts.append((hi, c) if horiz else (c, hi))
    u0 = min(p[0] for p in key_pts) - pad
    u1 = max(p[0] for p in key_pts) + pad
    v0 = min(p[1] for p in key_pts) - pad
    v1 = max(p[1] for p in key_pts) + pad

    obstacles = [(p, r) for p, r in obstacles
                 if u0 - r <= p[0] <= u1 + r and v0 - r <= p[1] <= v1 + r]
    hsegs, vsegs = [], []
    for s in avoid:
        if seg_length(s) < TOL:
            continue
        if is_horizontal(s):
            lo, hi = sorted((s[0][0], s[1][0]))
            if v0 - lane <= s[0][1] <= v1 + lane and hi >= u0 and lo <= u1:
                hsegs.append((s[0][1], lo, hi))
        else:
            lo, hi = sorted((s[0][1], s[1][1]))
            if u0 - lane <= s[0][0] <= u1 + lane and hi >= v0 and lo <= v1:
                vsegs.append((s[0][0], lo, hi))

    xs = set([u0, u1])
    ys = set([v0, v1])
    for p in key_pts:
        xs.add(p[0])
        ys.add(p[1])
    for p, r in obstacles:
        side = r / 0.9
        xs.update((p[0], p[0] - side, p[0] + side))
        ys.update((p[1], p[1] - side, p[1] + side))
    for c, lo, hi in hsegs:
        ys.update((c, c - lane, c + lane))
    for c, lo, hi in vsegs:
        xs.update((c, c - lane, c + lane))
    for horiz, c, lo, hi in tees:
        (ys if horiz else xs).add(c)
        (xs if horiz else ys).update((lo, hi, (lo + hi) * 0.5))
    xs = _merge_coords(xs, u0, u1)
    ys = _merge_coords(ys, v0, v1)
    nx, ny = len(xs), len(ys)

    def step(i, j, k):
        if k == 0:
            return i + 1, j
        if k == 1:
            return i, j + 1
        if k == 2:
            return i - 1, j
        return i, j - 1

    edge_cache = {}

    def edge_cost(i, j, k):
        key = (i, j, k)
        if key in edge_cache:
            return edge_cache[key]
        ni, nj = step(i, j, k)
        a, b = (xs[i], ys[j]), (xs[ni], ys[nj])
        cost = manhattan(a, b)
        for p, r in obstacles:
            if _point_seg_distance(p, (a, b)) < r - TOL:
                cost += OBSTACLE_PENALTY
        for n, wc, back, wlo, whi in walls:
            d1, d2 = sorted(((a[n] - wc) * back, (b[n] - wc) * back))
            lo_a, hi_a = min(a[1 - n], b[1 - n]), max(a[1 - n], b[1 - n])
            if d2 >= -TOL and d1 <= wall_depth and hi_a >= wlo - TOL and lo_a <= whi + TOL:
                cost += OBSTACLE_PENALTY
        if k in (0, 2):
            c, lo, hi = a[1], min(a[0], b[0]), max(a[0], b[0])
            parallel = hsegs
        else:
            c, lo, hi = a[0], min(a[1], b[1]), max(a[1], b[1])
            parallel = vsegs
        for sc, slo, shi in parallel:
            d = abs(sc - c)
            if d < lane - TOL and min(hi, shi) - max(lo, slo) > TOL:
                cost += OVERLAP_PENALTY if d < lane * 0.5 else near_parallel_cost
        edge_cache[key] = cost
        return cost

    flag_cache = {}

    def flags(i, j):
        """(on an avoid segment end, inside a horizontal one, inside a vertical one, least room
        either side of the crossing point on those lines)."""
        key = (i, j)
        if key in flag_cache:
            return flag_cache[key]
        x, y = xs[i], ys[j]
        ends = h_in = v_in = False
        room = 1e18
        for c, lo, hi in hsegs:
            if abs(c - y) < TOL and lo - TOL <= x <= hi + TOL:
                if abs(x - lo) < TOL or abs(x - hi) < TOL:
                    ends = True
                else:
                    h_in = True
                    room = min(room, x - lo, hi - x)
        for c, lo, hi in vsegs:
            if abs(c - x) < TOL and lo - TOL <= y <= hi + TOL:
                if abs(y - lo) < TOL or abs(y - hi) < TOL:
                    ends = True
                else:
                    v_in = True
                    room = min(room, y - lo, hi - y)
        flag_cache[key] = (ends, h_in, v_in, room)
        return flag_cache[key]

    def crosses_soon(i, j, k):
        """True if heading ``k`` from node (i, j) crosses a line too soon for a hop to fit."""
        x, y = xs[i], ys[j]
        dx, dy = DIRS[k]
        if dy == 0:
            for c, lo, hi in vsegs:
                if TOL < (c - x) * dx <= hop_room and lo + TOL < y < hi - TOL:
                    return True
        else:
            for c, lo, hi in hsegs:
                if TOL < (c - y) * dy <= hop_room and lo + TOL < x < hi - TOL:
                    return True
        return False

    goal_map = {}
    for gi, (p, arrive) in enumerate(goals):
        node = (_nearest(xs, p[0]), _nearest(ys, p[1]))
        goal_map.setdefault(node, []).append((arrive, ('gap', gi)))
    for horiz, c, lo, hi in tees:
        if horiz:
            j = _nearest(ys, c)
            for i, x in enumerate(xs):
                if lo - TOL <= x <= hi + TOL:
                    for arrive in (1, 3):
                        goal_map.setdefault((i, j), []).append((arrive, ('tee', (x, ys[j]))))
        else:
            i = _nearest(xs, c)
            for j, y in enumerate(ys):
                if lo - TOL <= y <= hi + TOL:
                    for arrive in (0, 2):
                        goal_map.setdefault((i, j), []).append((arrive, ('tee', (xs[i], y))))

    def goal_extra(i, j, tag, arrive):
        ends, h_in, v_in, _ = flags(i, j)
        extra = 0.0
        if ends:
            extra += TOUCH_PENALTY
        elif tag[0] == 'gap' and (h_in or v_in):
            extra += TOUCH_PENALTY
        if crosses_soon(i, j, (arrive + 2) % 4):
            extra += near_hop_cost
        return extra

    heap = []
    best = {}
    prev = {}
    counter = [0]

    def push(cost, key):
        counter[0] += 1
        heapq.heappush(heap, (cost, counter[0], key))

    for si, (p, d, lead, may_turn, c0) in enumerate(starts):
        i, j = _nearest(xs, p[0]), _nearest(ys, p[1])
        ends, h_in, v_in, _ = flags(i, j)
        cost = c0 + (TOUCH_PENALTY if (ends or h_in or v_in) else 0.0)
        key = (i, j, d, 0 if may_turn else si + 1)
        if cost < best.get(key, 1e18):
            best[key] = cost
            prev[key] = ('start', si)
            push(cost, key)

    result = None
    while heap:
        cost, _, key = heapq.heappop(heap)
        if key[0] == 'goal':
            result, result_cost = key, cost
            break
        if cost > best.get(key, 1e18) + TOL:
            continue
        i, j, d, fresh = key
        at_start = prev[key][0] == 'start'
        ends, h_in, v_in, room = flags(i, j)
        for k in range(4):
            if k == (d + 2) % 4 or (fresh and k != d):
                continue
            ni, nj = step(i, j, k)
            if not (0 <= ni < nx and 0 <= nj < ny):
                continue
            add = edge_cost(i, j, k)
            if k != d:
                add += bend_cost
                if ends or h_in or v_in:
                    add += TOUCH_PENALTY
                if crosses_soon(i, j, (d + 2) % 4) or crosses_soon(i, j, k):
                    add += near_hop_cost
            elif not at_start:
                if ends:
                    add += TOUCH_PENALTY
                elif (v_in if k in (0, 2) else h_in):
                    # A crossing needs room for a hop on the line being crossed.
                    add += cross_cost if room >= hop_room else near_hop_cost
            elif fresh == 0 and crosses_soon(i, j, k):
                add += near_hop_cost
            nfresh = 0
            if fresh:
                origin = starts[fresh - 1][0]
                if manhattan(origin, (xs[ni], ys[nj])) < min_leg - TOL:
                    nfresh = fresh
            nkey = (ni, nj, k, nfresh)
            ncost = cost + add
            if ncost < best.get(nkey, 1e18) - TOL:
                best[nkey] = ncost
                prev[nkey] = key
                push(ncost, nkey)
                short_turn = k != d and manhattan((xs[i], ys[j]), (xs[ni], ys[nj])) < min_leg - TOL
                for arrive, tag in goal_map.get((ni, nj), ()):
                    if arrive != k or (short_turn and tag[0] == 'gap'):
                        continue
                    push(ncost + goal_extra(ni, nj, tag, arrive), ('goal', tag, nkey))

    if result is None:
        return None, None, None
    route_cost = result_cost
    _, tag, key = result
    nodes = []
    while True:
        nodes.append((xs[key[0]], ys[key[1]]))
        back = prev[key]
        if back[0] == 'start':
            si = back[1]
            break
        key = back
    nodes.reverse()
    return _simplify(list(starts[si][2]) + nodes), tag, route_cost


def _mark(used, node, point):
    used.setdefault(node.key, set()).add(dir_index((point[0] - node.u, point[1] - node.v)))


def _free_gaps(node, gap, used, fallback=True):
    pts = gap_points(node, gap)
    free = [g for g in pts if g[1] not in (used or {}).get(node.key, ())]
    return free or (pts if fallback else [])


def _connect_fittings(a, b, params, obstacles, avoid, used, horizontal_first):
    gap = params['fitting_gap']
    starts = []
    for p, d in _free_gaps(a, gap, used):
        pref = 0.0 if (d in (0, 2)) == horizontal_first else TOL * 100
        starts.append((p, d, [], False, pref))
    goals = [(p, (d + 2) % 4) for p, d in _free_gaps(b, gap, used)]
    path, _, cost = grid_route(starts, goals, [], obstacles, avoid, params)
    if path is None:
        return trim_path(l_path(a.pt, b.pt, horizontal_first), gap, gap), None
    if used is not None:
        _mark(used, a, path[0])
        _mark(used, b, path[-1])
    return path, cost


def chain_network(fittings, gap, horizontal_first=True, obstacles=None, avoid=None, clearance=None,
                  params=None, used=None):
    """Spanning tree of orthogonal routes between fitting gap points. Routes go around other
    fittings, avoid running along or touching ``avoid`` and prefer few bends and crossings."""
    params = params or _params_from_gap(gap)
    pts = [f.pt for f in fittings]
    obs = obstacle_list(obstacles if obstacles is not None else fittings, params)
    avoid = list(avoid or [])
    used = used if used is not None else {}
    segs = []
    for i, j in _route_tree(fittings, params, obs, avoid, horizontal_first):
        path, _ = _connect_fittings(fittings[i], fittings[j], params, obs, avoid + segs, used,
                                    horizontal_first)
        segs.extend(path_segments(path))
    return segs


def _route_tree(fittings, params, obstacles, avoid, horizontal_first, neighbours=4):
    """Spanning tree of the fittings weighted by real route cost, so a fitting is chained to the
    neighbour it can reach cleanly (not through another group's fitting). Edges in build order."""
    n = len(fittings)
    if n < 2:
        return []
    pts = [f.pt for f in fittings]
    cost = {}
    for i in range(n):
        near = sorted((manhattan(pts[i], pts[j]), j) for j in range(n) if j != i)
        for _, j in near[:neighbours]:
            key = (min(i, j), max(i, j))
            if key not in cost:
                _, c = _connect_fittings(fittings[key[0]], fittings[key[1]], params, obstacles,
                                         avoid, None, horizontal_first)
                cost[key] = c if c is not None else manhattan(pts[i], pts[j]) + OBSTACLE_PENALTY
    inside = set([0])
    edges = []
    while len(inside) < n:
        best = None
        for (i, j), c in cost.items():
            if (i in inside) == (j in inside):
                continue
            if best is None or c < best[0] - TOL or (abs(c - best[0]) <= TOL and (i, j) < best[1]):
                best = (c, (i, j))
        if best is None:
            # Disconnected by the neighbour limit: fall back to the nearest outside fitting.
            best = min((manhattan(pts[i], pts[j]), (i, j)) for i in inside for j in range(n)
                       if j not in inside)
        i, j = best[1]
        edges.append((i, j) if i in inside else (j, i))
        inside.update((i, j))
    return edges


def trunk_network(fittings, gap, offset, side=1, axis=None):
    """A trunk parallel to the fitting row with a perpendicular stub to every fitting."""
    pts = [f.pt for f in fittings]
    axis = axis or row_axis(pts)
    along, across = (0, 1) if axis == 'u' else (1, 0)
    acr = [p[across] for p in pts]
    trunk_c = (max(acr) + offset) if side > 0 else (min(acr) - offset)
    segs = []
    alongs = []
    for p in pts:
        end = [0.0, 0.0]
        end[along] = p[along]
        end[across] = trunk_c
        segs.extend(path_segments(trim_path([p, tuple(end)], gap, 0.0)))
        alongs.append(p[along])
    if max(alongs) - min(alongs) > TOL:
        a = [0.0, 0.0]
        b = [0.0, 0.0]
        a[along], b[along] = min(alongs), max(alongs)
        a[across] = b[across] = trunk_c
        segs.append((tuple(a), tuple(b)))
    return segs


def closest_on_segment(p, seg):
    (u1, v1), (u2, v2) = seg
    return (min(max(p[0], min(u1, u2)), max(u1, u2)),
            min(max(p[1], min(v1, v2)), max(v1, v2)))


def tee_ranges(segs, tee_points, margin):
    """Where a new line may tee into ``segs``: [(horizontal, line coordinate, lo, hi)], keeping
    ``margin`` clear of segment ends, corners and other tees so every joint reads cleanly."""
    ranges = []
    for s in segs:
        if seg_length(s) < 2 * margin + TOL:
            continue
        horiz = is_horizontal(s)
        k = 0 if horiz else 1
        c = s[0][1 - k]
        lo, hi = sorted((s[0][k], s[1][k]))
        cuts = [t[k] for t in tee_points if abs(t[1 - k] - c) < TOL]
        for o in segs:
            for p in o:
                if abs(p[1 - k] - c) < TOL:
                    cuts.append(p[k])
        cuts = sorted(x for x in cuts if lo + TOL < x < hi - TOL)
        start = lo + margin
        for x in cuts + [hi + margin]:
            end = x - margin
            if end - start > TOL:
                ranges.append((horiz, c, start, end))
            start = max(start, x + margin)
    return ranges


def lane_reach(params, lane_index):
    """Distance from a wall switch to where its line may turn (lane 0 is one lane off the wall)."""
    return params['switch_gap'] + (lane_index + 1) * params['lane']


def switch_route(switch, out, params, lane_index=0, obstacles=None, avoid=None, used=None):
    """Route one switch/sensor to its output.

    Ends at a free fitting gap (heading into the fitting) or tees perpendicular into the middle of
    the output's lines. Wall switches leave from their front face out to their lane.
    Returns dict(segs, marks=[(node, point)] gap points used, tee=point or None, cost).
    """
    gap = params['fitting_gap']
    obstacles = obstacles or []
    used = used if used is not None else {}
    starts = []
    if switch.wall:
        k = dir_index(switch.facing)
        f = DIRS[k]
        lead = (switch.u + f[0] * params['switch_gap'], switch.v + f[1] * params['switch_gap'])
        reach = lane_reach(params, lane_index)
        starts.append(((switch.u + f[0] * reach, switch.v + f[1] * reach), k, [lead], True, 0.0))
    else:
        for p, d in _free_gaps(switch, gap, used):
            starts.append((p, d, [], False, 0.0))
    goals = []
    goal_nodes = []
    for fitting in out['fittings']:
        for p, d in _free_gaps(fitting, gap, used, fallback=False):
            goals.append((p, (d + 2) % 4))
            goal_nodes.append(fitting)
    tees = tee_ranges(out['network'] + out['routes'], out['tees'], params['lane'] * 1.5)
    if not goals and not tees:
        for fitting in out['fittings']:
            for p, d in gap_points(fitting, gap):
                goals.append((p, (d + 2) % 4))
                goal_nodes.append(fitting)
    path, tag, cost = grid_route(starts, goals, tees, obstacles, avoid or [], params)
    if path is None:
        segs = _estimate_route(switch, out['network'], [f.pt for f in out['fittings']], params,
                               lane_index, obstacles, avoid)
        return {'segs': segs, 'marks': [], 'tee': None, 'cost': OBSTACLE_PENALTY}
    marks = []
    if not switch.wall:
        marks.append((switch, path[0]))
    tee = None
    if tag[0] == 'gap':
        marks.append((goal_nodes[tag[1]], path[-1]))
    else:
        tee = tag[1]
    return {'segs': path_segments(path), 'marks': marks, 'tee': tee, 'cost': cost}


def _estimate_route(switch, network, fitting_targets, params, lane_index=0, obstacles=None,
                    avoid=None):
    """Quick L-shaped route estimate, used to compare lane orderings of a switch bank."""
    gap = params['fitting_gap']
    obstacles = obstacles or []
    avoid = avoid or []
    cross_cost = params['lane'] * 2.0
    if switch.wall:
        f = DIRS[dir_index(switch.facing)]
        start = (switch.u + f[0] * params['switch_gap'], switch.v + f[1] * params['switch_gap'])
        reach = lane_reach(params, lane_index)
        lane_pt = (switch.u + f[0] * reach, switch.v + f[1] * reach)
        lead = [start]
        options = [abs(f[0]) <= abs(f[1]), abs(f[0]) > abs(f[1])]
    else:
        lane_pt, lead, options, f = switch.pt, [], [True, False], None
    targets = [(seg, False) for seg in network]
    if not network:
        targets = [((t, t), True) for t in fitting_targets]
    best = None
    for seg, is_fitting in targets:
        p = closest_on_segment(lane_pt, seg)
        for rank, hf in enumerate(options):
            path = lead + l_path(lane_pt, p, hf)
            segs = path_segments(trim_path(path, 0.0 if switch.wall else gap,
                                           gap if is_fitting else 0.0))
            score = manhattan(lane_pt, p) + rank * TOL * 100
            if f is not None:
                ahead = (p[0] - lane_pt[0]) * f[0] + (p[1] - lane_pt[1]) * f[1]
                if ahead < -TOL:
                    score += 2.0 * abs(ahead) + 1.0
            score += OBSTACLE_PENALTY * path_hits(path, obstacles)
            score += conflict_cost(segs, avoid, params['lane'] * 0.5, cross_cost)
            if best is None or score < best[0] - TOL:
                best = (score, segs)
    return best[1] if best else []


def merge_collinear(segs):
    """Merge overlapping or touching collinear axis-aligned segments."""
    buckets = {}
    for a, b in segs:
        if is_horizontal((a, b)):
            key = ('h', round(a[1], 4))
            lo, hi = sorted([a[0], b[0]])
            coord = a[1]
        else:
            key = ('v', round(a[0], 4))
            lo, hi = sorted([a[1], b[1]])
            coord = a[0]
        buckets.setdefault(key, []).append((lo, hi, coord))
    merged = []
    for key in sorted(buckets):
        spans = sorted(buckets[key])
        coord = spans[0][2]
        cur = [spans[0][0], spans[0][1]]
        out = []
        for lo, hi, _ in spans[1:]:
            if lo <= cur[1] + TOL:
                cur[1] = max(cur[1], hi)
            else:
                out.append(tuple(cur))
                cur = [lo, hi]
        out.append(tuple(cur))
        for lo, hi in out:
            if hi - lo < TOL:
                continue
            if key[0] == 'h':
                merged.append(((lo, coord), (hi, coord)))
            else:
                merged.append(((coord, lo), (coord, hi)))
    return merged


def junctions(segs):
    """T-junctions (an endpoint on another segment's interior) and 3+-way endpoint meetings."""
    pts = []
    counts = {}
    for s in segs:
        for p in s:
            key = (round(p[0], 4), round(p[1], 4))
            counts[key] = counts.get(key, 0) + 1
            for other in segs:
                if other is not s and _interior(p, other):
                    pts.append(p)
                    break
    for key, n in counts.items():
        if n >= 3:
            pts.append(key)
    unique = []
    for p in pts:
        if not any(_same(p, q) for q in unique):
            unique.append(p)
    return unique


def _hop_room(seg, p):
    k = 0 if is_horizontal(seg) else 1
    return min(abs(p[k] - seg[0][k]), abs(seg[1][k] - p[k]))


def _hop_points(new_segs, existing_segs, radius=None):
    """Crossing points per new segment as (point, hop radius).

    Between two new segments the horizontal one hops unless only the vertical has room for a full
    hop. Against existing drafting the new one hops. When room is short the hop is made smaller
    instead of being dropped, so every crossing gets a hop.
    """
    hops = dict((i, []) for i in range(len(new_segs)))

    def size(seg, p):
        if radius is None:
            return None
        return min(radius, _hop_room(seg, p) * 0.9)

    for i, s in enumerate(new_segs):
        for j in range(i + 1, len(new_segs)):
            p = crossing(s, new_segs[j])
            if p is None:
                continue
            h, v = (i, j) if is_horizontal(s) else (j, i)
            host = h
            if radius is not None:
                room_h = _hop_room(new_segs[h], p)
                room_v = _hop_room(new_segs[v], p)
                if room_h < radius + TOL and room_v > room_h + TOL:
                    host = v
            hops[host].append((p, size(new_segs[host], p)))
        for e in existing_segs:
            p = crossing(s, e)
            if p is not None:
                hops[i].append((p, size(s, p)))
    return hops


def apply_hops_indexed(new_segs, existing_segs, radius):
    """Like apply_hops, also returning the source segment index of every line and arc.

    Crossings closer together than their hops are merged into a single wider hop.
    """
    hops = _hop_points(new_segs, existing_segs, radius)
    lines, arcs, line_src, arc_src = [], [], [], []
    for i, s in enumerate(new_segs):
        a, b = s
        horiz = is_horizontal(s)
        k = 0 if horiz else 1
        sign = 1.0 if b[k] >= a[k] else -1.0
        pts = [(p, r) for p, r in hops[i] if r > TOL]
        pts.sort(key=lambda q: sign * q[0][k])
        clusters = []
        for p, r in pts:
            if clusters:
                last_p, last_r = clusters[-1][-1]
                if sign * (p[k] - last_p[k]) < last_r + r + TOL:
                    clusters[-1].append((p, r))
                    continue
            clusters.append([(p, r)])
        cursor = a
        for cluster in clusters:
            (first, r_first), (last, r_last) = cluster[0], cluster[-1]
            before = list(first)
            after = list(last)
            before[k] -= sign * r_first
            after[k] += sign * r_last
            # Keep the hop inside the segment and clear of the previous hop.
            if sign * (before[k] - cursor[k]) < 0:
                before[k] = cursor[k]
            if sign * (b[k] - after[k]) < 0:
                after[k] = b[k]
            if sign * (after[k] - before[k]) < TOL:
                continue
            bulge = min(r for _, r in cluster)
            centre = list(first)
            centre[k] = (before[k] + after[k]) / 2.0
            mid = (centre[0], centre[1] + bulge) if horiz else (centre[0] - bulge, centre[1])
            if manhattan(cursor, tuple(before)) > TOL:
                lines.append((cursor, tuple(before)))
                line_src.append(i)
            arcs.append((tuple(before), tuple(after), mid))
            arc_src.append(i)
            cursor = tuple(after)
        if manhattan(cursor, b) > TOL:
            lines.append((cursor, b))
            line_src.append(i)
    return lines, arcs, line_src, arc_src

def apply_hops(new_segs, existing_segs, radius):
    """Split new segments at crossings and return (lines, arcs).

    Between two new segments the horizontal one hops; against existing drafting the new one hops.
    Arcs are (start, end, mid) bulging +v (horizontal) or -u (vertical).
    """
    lines, arcs, _, _ = apply_hops_indexed(new_segs, existing_segs, radius)
    return lines, arcs


def resolve_params(scale, settings=None):
    s = dict(DEFAULTS)
    s.update(settings or {})
    return {
        'fitting_gap': paper_to_model(s['fitting_gap_mm'], scale),
        'switch_gap': paper_to_model(s['switch_gap_mm'], scale),
        'lane': paper_to_model(s['lane_mm'], scale),
        'hop_radius': paper_to_model(s['hop_radius_mm'], scale),
        'trunk_offset': paper_to_model(s['trunk_offset_mm'], scale),
    }


def _assign_switches(outputs, switches):
    walls = [s for s in switches if s.wall]
    for out in outputs:
        out['switches'] = [s for s in switches if not s.wall]
    if len(outputs) == 2 and len(walls) == 2:
        def cost(pair):
            return sum(manhattan(sw.pt, o['fittings'][0].pt) for sw, o in zip(pair, outputs))
        rev = list(reversed(walls))
        pair = walls if cost(walls) <= cost(rev) else rev
        for sw, out in zip(pair, outputs):
            out['switches'].append(sw)
    else:
        for out in outputs:
            out['switches'].extend(walls)


def _normal_axis(facing):
    return 0 if abs(facing[0]) > 0.5 else 1


def wall_banks(switches, params):
    """Cluster wall switches that face the same way and sit side by side on one wall."""
    walls = []
    seen = set()
    for s in switches:
        if s.wall and s.key not in seen:
            seen.add(s.key)
            walls.append(s)
    parent = list(range(len(walls)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(walls)):
        for j in range(i + 1, len(walls)):
            a, b = walls[i], walls[j]
            if tuple(a.facing) != tuple(b.facing):
                continue
            n = _normal_axis(a.facing)
            if abs(a.pt[n] - b.pt[n]) < params['lane'] and \
                    abs(a.pt[1 - n] - b.pt[1 - n]) <= params['lane'] * 4:
                parent[find(i)] = find(j)
    banks = {}
    order = []
    for i, s in enumerate(walls):
        root = find(i)
        if root not in banks:
            banks[root] = []
            order.append(root)
        banks[root].append(s)
    return [banks[r] for r in order]


def wall_lines(nodes, params):
    """Band behind each bank of wall switches (the wall itself, two lanes deep), as long as the
    bank plus half a lane: [(normal axis, wall coordinate, back sign, lo, hi)].
    Switchlines may pass the end of a bank (to lights beyond the wall) but never run behind it."""
    switches = [n for n in nodes if isinstance(n, Node) and n.kind == 'switch' and n.wall]
    ext = params['lane'] * 0.5
    out = []
    for bank in wall_banks(switches, params):
        n = _normal_axis(bank[0].facing)
        a = 1 - n
        c = sum(s.pt[n] for s in bank) / float(len(bank))
        back = -1.0 if bank[0].facing[n] > 0 else 1.0
        lo = min(s.pt[a] for s in bank) - ext
        hi = max(s.pt[a] for s in bank) + ext
        out.append((n, c, back, lo, hi))
    return out


def occupied_lanes(bank, params, segs, max_lane=60):
    """Lane indices in front of a switch bank already taken by switchlines parallel to the wall."""
    facing = bank[0].facing
    n = _normal_axis(facing)
    a = 1 - n
    sign = 1.0 if facing[n] > 0 else -1.0
    base = sum(s.pt[n] for s in bank) / float(len(bank))
    lo = min(s.pt[a] for s in bank) - params['lane']
    hi = max(s.pt[a] for s in bank) + params['lane']
    taken = set()
    for seg in segs:
        if abs(seg[0][n] - seg[1][n]) > TOL:
            continue
        slo, shi = sorted([seg[0][a], seg[1][a]])
        if shi < lo or slo > hi:
            continue
        k = (seg[0][n] - base) * sign - params['switch_gap'] - params['lane']
        idx = int(round(k / params['lane']))
        if 0 <= idx <= max_lane and abs(k - idx * params['lane']) < params['lane'] * 0.4:
            taken.add(idx)
    return taken


def _route_target(out, s):
    segs = out['network'] or [(f.pt, f.pt) for f in out['fittings']]
    best = None
    for seg in segs:
        p = closest_on_segment(s.pt, seg)
        d = manhattan(s.pt, p)
        if best is None or d < best[0] - TOL:
            best = (d, p)
    return best[1]


def bank_lane_orders(conns, params, taken):
    """Candidate lane assignments [(out, switch, lane)] for the connections of one wall bank:
    every order of the up / down / level sets, each sorted by switch or by target position."""
    facing = conns[0][1].facing
    a = 1 - _normal_axis(facing)
    sets = {'up': [], 'down': [], 'level': []}
    for out, s in conns:
        t = _route_target(out, s)
        d = t[a] - s.pt[a]
        name = 'level' if abs(d) < params['lane'] else ('up' if d > 0 else 'down')
        sets[name].append((out, s, t))
    free = []
    i = 0
    while len(free) < len(conns):
        if i not in taken:
            free.append(i)
        i += 1
    seen = set()
    orders = []
    for order in permutations(('level', 'up', 'down')):
        for by_target in (False, True):
            for reverse_up in (False, True):
                seq = []
                for name in order:
                    items = list(sets[name])
                    key = (lambda it: it[2][a]) if by_target else (lambda it: it[1].pt[a])
                    items.sort(key=key, reverse=(name == 'up') != reverse_up)
                    seq.extend(items)
                sig = tuple((id(o), s.key) for o, s, _ in seq)
                if sig not in seen:
                    seen.add(sig)
                    orders.append([(o, s, lane) for lane, (o, s, _) in zip(free, seq)])
    return orders


def plan_bank_lanes(conns, params, obstacles, avoid, taken, new_trial=None):
    """Pick the lane assignment of one wall bank whose routes cost least (crossings, hops that do
    not fit, bends, length). ``new_trial()`` returns a fresh ``route(out, switch, lane, avoid)``
    giving (segments, cost); the default is a quick L-shaped estimate.
    Returns [(out, switch, lane)]."""
    if new_trial is None:
        def estimate(out, s, lane, avoid_now):
            segs = _estimate_route(s, out['network'], [f.pt for f in out['fittings']], params,
                                   lane, obstacles, avoid_now)
            return segs, (sum(seg_length(x) for x in segs) * 0.01
                          + conflict_cost(segs, avoid_now, params['lane'] * 0.5, 1.0))
        new_trial = lambda: estimate
    best = None
    for plan in bank_lane_orders(conns, params, taken):
        route = new_trial()
        routes = []
        cost = 0.0
        for out, s, lane in plan:
            segs, c = route(out, s, lane, avoid + routes)
            cost += c
            routes.extend(segs)
            if best is not None and cost >= best[0]:
                break
        if best is None or cost < best[0] - TOL:
            best = (cost, plan)
    return best[1]


def _group_outputs(gi, group, params, obstacles, planned, existing, used):
    switches, fittings = group['switches'], group['fittings']
    if not fittings:
        raise ValueError('Select at least one light fitting.')
    if not switches:
        raise ValueError('Select at least one switch or sensor.')
    axis = row_axis([f.pt for f in fittings])
    side = group.get('side', 1)
    if group.get('mode') == MODE_ALTERNATE and len(fittings) >= 2:
        group_a, group_b = alternate_split(fittings)
        outputs = [{'name': 'A', 'fittings': group_a, 'side': side},
                   {'name': 'B', 'fittings': group_b, 'side': -side}]
    else:
        outputs = [{'name': 'A', 'fittings': list(fittings), 'side': side}]
    for out in outputs:
        out['group'] = gi
        out['routes'] = []
        out['tees'] = []
        if group.get('style', STYLE_CHAIN) == STYLE_TRUNK:
            out['network'] = trunk_network(out['fittings'], params['fitting_gap'],
                                           params['trunk_offset'], out['side'], axis)
        else:
            out['network'] = chain_network(out['fittings'], params['fitting_gap'],
                                           group.get('horizontal_first', True), obstacles,
                                           existing + planned, params=params, used=used)
        planned.extend(out['network'])
    _assign_switches(outputs, switches)
    return outputs


def build_session(groups, scale=50, existing_segments=None, settings=None, extra_obstacles=None):
    """Plan switchlines for every switch group of one drawing session.

    Each group: dict(switches, fittings, mode, style, horizontal_first, side) (see ``make_group``).
    MODE_ALL: one output with every fitting; every switch connects to it (2-way, several sensors).
    MODE_ALTERNATE: fittings alternate between outputs A and B along their row. Ceiling sensors feed
    both outputs (dual-output sensor); exactly two wall switches take one output each.
    ``extra_obstacles``: other fittings/devices in the view (Nodes or points) to keep clear of.
    Returns dict(lines, arcs, joints, line_groups, arc_groups, joint_groups, outputs).
    """
    if not groups:
        raise ValueError('Add at least one switch group.')
    params = resolve_params(scale, settings)
    existing = list(existing_segments or [])
    nodes = []
    for g in groups:
        nodes.extend(g['fittings'])
        nodes.extend(g['switches'])
    everything = nodes + list(extra_obstacles or [])
    obstacles = obstacle_list(everything, params)
    params = dict(params, walls=wall_lines(everything, params))

    planned = []
    outputs = []
    used = {}
    for gi, group in enumerate(groups):
        outputs.extend(_group_outputs(gi, group, params, obstacles, planned, existing, used))

    ceiling_conns = []
    wall_conns = []
    for out in outputs:
        for s in out['switches']:
            (wall_conns if s.wall else ceiling_conns).append((out, s))

    def route(out, s, lane):
        res = switch_route(s, out, params, lane, obstacles, existing + planned, used)
        for node, point in res['marks']:
            _mark(used, node, point)
        if res['tee'] is not None:
            out['tees'].append(res['tee'])
        out['routes'].extend(res['segs'])
        planned.extend(res['segs'])

    def trial(conns):
        """Returns a factory of routers that score a lane plan with the real router on a
        scratch copy of the session state."""
        def new_trial():
            state = {'used': dict((k, set(v)) for k, v in used.items()),
                     'outs': dict((id(o), (list(o['routes']), list(o['tees'])))
                                  for o, _ in conns)}
            return lambda out, s, lane, avoid_now: run(state, out, s, lane, avoid_now)
        return new_trial

    def run(state, out, s, lane, avoid_now):
        routes, tees = state['outs'][id(out)]
        scratch = dict(out, routes=routes, tees=tees)
        res = switch_route(s, scratch, params, lane, obstacles, avoid_now, state['used'])
        for node, point in res['marks']:
            _mark(state['used'], node, point)
        if res['tee'] is not None:
            tees.append(res['tee'])
        routes.extend(res['segs'])
        return res['segs'], res['cost']

    for out, s in ceiling_conns:
        route(out, s, 0)
    for bank in wall_banks([s for _, s in wall_conns], params):
        keys = set(s.key for s in bank)
        conns = [(o, s) for o, s in wall_conns if s.key in keys]
        taken = occupied_lanes(bank, params, existing)
        plan = plan_bank_lanes(conns, params, obstacles, existing + planned, taken, trial(conns))
        for out, s, lane in plan:
            route(out, s, lane)

    all_segs = []
    seg_groups = []
    joints = []
    joint_groups = []
    for out in outputs:
        out['segments'] = merge_collinear(out['network'] + out['routes'])
        all_segs.extend(out['segments'])
        seg_groups.extend([out['group']] * len(out['segments']))
        for p in junctions(out['segments']):
            if not any(_same(p, q) for q in joints):
                joints.append(p)
                joint_groups.append(out['group'])
    lines, arcs, line_src, arc_src = apply_hops_indexed(all_segs, existing, params['hop_radius'])
    return {
        'lines': lines,
        'arcs': arcs,
        'joints': joints,
        'line_groups': [seg_groups[i] for i in line_src],
        'arc_groups': [seg_groups[i] for i in arc_src],
        'joint_groups': joint_groups,
        'outputs': [{'name': o['name'], 'group': o['group'],
                     'fittings': [f.key for f in o['fittings']],
                     'switches': [s.key for s in o['switches']]} for o in outputs],
    }


def build_layout(switches, fittings, mode=MODE_ALL, style=STYLE_CHAIN, scale=50,
                 existing_segments=None, horizontal_first=True, side=1, settings=None,
                 extra_obstacles=None):
    """Single-group convenience wrapper around ``build_session``."""
    group = make_group(switches, fittings, mode, style, horizontal_first, side)
    return build_session([group], scale, existing_segments, settings, extra_obstacles)


def describe_group(group):
    mode = 'every second fitting (A/B)' if group['mode'] == MODE_ALTERNATE else 'all together'
    return '{0} switch/sensor(s), {1} fitting(s), {2}, {3}'.format(
        len(group['switches']), len(group['fittings']), mode, group['style'])


def describe(layout):
    parts = []
    for o in layout['outputs']:
        parts.append('Group {0} output {1}: {2} fitting(s), {3} switch/sensor link(s)'.format(
            o['group'] + 1, o['name'], len(o['fittings']), len(o['switches'])))
    parts.append('{0} line(s), {1} hop(s), {2} joint(s)'.format(
        len(layout['lines']), len(layout['arcs']), len(layout['joints'])))
    return '\n'.join(parts)
