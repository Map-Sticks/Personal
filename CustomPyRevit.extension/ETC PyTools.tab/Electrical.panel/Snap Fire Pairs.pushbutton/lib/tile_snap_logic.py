# -*- coding: utf-8 -*-
"""Pure geometry for Snap Fire Pairs (no Revit imports; IronPython 2.7 and CPython).

All values are millimetres in project plan coordinates. Grids are axis-aligned.
House rule: a detector + speaker pair shares one ceiling tile, each device at the
centre of its half of the tile (1200 x 600 tile -> tile centre +/- 300 along the
long side). On square tiles the pair uses two adjacent tiles.
"""
import math

DETECTOR = "detector"
SPEAKER = "speaker"


class Grid(object):
    """Axis-aligned ceiling grid: a tile corner (ox, oy) and tile size (sx, sy)."""

    def __init__(self, ox, oy, sx, sy):
        self.ox = float(ox)
        self.oy = float(oy)
        self.sx = float(sx)
        self.sy = float(sy)

    def tile_centre(self, x, y):
        """Centre of the tile containing (x, y)."""
        i = math.floor((x - self.ox) / self.sx)
        j = math.floor((y - self.oy) / self.sy)
        return (self.ox + (i + 0.5) * self.sx, self.oy + (j + 0.5) * self.sy)

    def distance_to_line(self, x, y):
        """Distance from (x, y) to the nearest grid line (T-bar)."""
        fx = (x - self.ox) % self.sx
        fy = (y - self.oy) % self.sy
        return min(fx, self.sx - fx, fy, self.sy - fy)


class Device(object):
    def __init__(self, element_id, kind, x, y):
        self.id = element_id
        self.kind = kind
        self.x = float(x)
        self.y = float(y)


class Move(object):
    def __init__(self, device, x, y, notes=None):
        self.device = device
        self.x = x
        self.y = y
        self.notes = list(notes or [])

    @property
    def distance(self):
        return math.hypot(self.x - self.device.x, self.y - self.device.y)


def _round_module(value, module):
    return max(module, int(round(abs(value) / float(module))) * module)


def grid_from_corners(p1, p2, module=300.0, max_tile=2400.0):
    """Build a Grid from two picked, diagonally opposite corners of one tile.

    Tile sizes are rounded to the ceiling module (300 mm) so a slightly
    inaccurate click still gives 600 / 1200. The origin stays at the clicked
    corner. Raises ValueError for picks that cannot be one tile.
    """
    dx = abs(p2[0] - p1[0])
    dy = abs(p2[1] - p1[1])
    if dx < module * 0.5 or dy < module * 0.5:
        raise ValueError("The two points must be opposite corners of one tile, not on one line.")
    sx = _round_module(dx, module)
    sy = _round_module(dy, module)
    if sx > max_tile or sy > max_tile:
        raise ValueError("Picked tile is {0:.0f} x {1:.0f} mm; pick the corners of ONE tile.".format(sx, sy))
    return Grid(min(p1[0], p2[0]), min(p1[1], p2[1]), sx, sy)


def pair_devices(devices, max_gap=1500.0):
    """Greedy nearest pairing of detectors with speakers.

    Returns (pairs, singles); pairs are (detector, speaker) tuples. Devices further
    than max_gap from any partner stay single.
    """
    detectors = [d for d in devices if d.kind == DETECTOR]
    speakers = [d for d in devices if d.kind == SPEAKER]
    candidates = []
    for d in detectors:
        for s in speakers:
            gap = math.hypot(d.x - s.x, d.y - s.y)
            if gap <= max_gap:
                candidates.append((gap, d.id, s.id, d, s))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    used = set()
    pairs = []
    for _gap, did, sid, d, s in candidates:
        if did in used or sid in used:
            continue
        used.add(did)
        used.add(sid)
        pairs.append((d, s))
    singles = [d for d in devices if d.id not in used]
    return pairs, singles


def _pair_targets(grid, det, spk):
    mx = (det.x + spk.x) / 2.0
    my = (det.y + spk.y) / 2.0
    if max(grid.sx, grid.sy) >= 2 * min(grid.sx, grid.sy):
        # Long tile: both devices in one tile, along its long side.
        cx, cy = grid.tile_centre(mx, my)
        along_x = grid.sx >= grid.sy
        half = (grid.sx if along_x else grid.sy) / 4.0
    else:
        # Square-ish tile: two adjacent tiles, split at the joint nearest the midpoint,
        # along the axis the pair already runs.
        along_x = abs(det.x - spk.x) >= abs(det.y - spk.y)
        if along_x:
            cx = grid.ox + round((mx - grid.ox) / grid.sx) * grid.sx
            cy = grid.tile_centre(mx, my)[1]
            half = grid.sx / 2.0
        else:
            cx = grid.tile_centre(mx, my)[0]
            cy = grid.oy + round((my - grid.oy) / grid.sy) * grid.sy
            half = grid.sy / 2.0
    # Keep the user's side order: whichever device is lower along the axis stays lower.
    det_low = (det.x <= spk.x) if along_x else (det.y <= spk.y)
    sign = -1.0 if det_low else 1.0
    if along_x:
        return (cx + sign * half, cy), (cx - sign * half, cy)
    return (cx, cy + sign * half), (cx, cy - sign * half)


def plan_moves(grid, devices, max_gap=1500.0, big_move=1500.0, clash=300.0):
    """Plan XY moves that snap pairs (and single devices) into tiles.

    Returns (moves, warnings). Nothing is dropped silently: big moves, devices
    already in place and clashing targets are noted for the user to review.
    """
    pairs, singles = pair_devices(devices, max_gap)
    moves = []
    for det, spk in pairs:
        dt, st = _pair_targets(grid, det, spk)
        moves.append(Move(det, dt[0], dt[1]))
        moves.append(Move(spk, st[0], st[1]))
    for dev in singles:
        cx, cy = grid.tile_centre(dev.x, dev.y)
        moves.append(Move(dev, cx, cy, ["no partner within {0:.0f} mm".format(max_gap)]))

    warnings = []
    for m in moves:
        if m.distance > big_move:
            m.notes.append("big move ({0:.0f} mm) - check".format(m.distance))
        if m.distance < 1.0:
            m.notes.append("already in place")
    for i, a in enumerate(moves):
        for b in moves[i + 1:]:
            if math.hypot(a.x - b.x, a.y - b.y) < clash:
                text = "targets clash: {0} and {1}".format(a.device.id, b.device.id)
                a.notes.append(text)
                b.notes.append(text)
                warnings.append(text)
    return moves, warnings


def classify(type_name, family_name="", speaker_words=("speaker", "sounder"),
             detector_words=("detector",),
             exclude_words=("conceal", "indicator", "heat", "aspirat", "beam", "flame")):
    """Classify a device as DETECTOR, SPEAKER or None (ignored).

    Exclusions test the type name only: shared families such as
    "HEAT AND SMOKE DETECTORS" hold both wanted and unwanted types.
    """
    low_type = (type_name or "").lower()
    if any(w in low_type for w in exclude_words):
        return None
    for low in (low_type, (family_name or "").lower()):
        if any(w in low for w in speaker_words):
            return SPEAKER
        if any(w in low for w in detector_words):
            return DETECTOR
    return None
