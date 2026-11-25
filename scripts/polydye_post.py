#!/usr/bin/env python3
"""PolyDye post-processor for OrcaSlicer: turns a sliced model's colours into ink.

Slice a colour model in OrcaSlicer with the PolyDye printer profile (README, "Colour prints from OrcaSlicer"): one
filament per colour, all of them the one white or natural spool loaded, their colours saying what to ink. Orca paints
the model (colour painting, or a textured model's colours) as a thin shell of those filaments. As Orca's
post-processing script, this then rewrites the G-code:
- every filament prints with the spool loaded: the tool changes (T0, T1, ...) go;
- per layer, where each coloured filament's lines are becomes cyan, magenta and yellow dots: a solid colour fires every
  dot of its inks (--ink-limit, 100 %), a partial one an ordered (Bayer) dither of them, decided per pair of columns
  (every nozzle fires at least two columns running: one fire on its own doesn't eject); printed as sweeps along Y
  over each object's whole footprint, every colour in each, about 4.2 mm apart (positions 28-82), neighbours
  overlapping by --feather positions (6) whose dots go to one or the other by a fixed random ramp, so the joins show no
  line; the grid shifted every layer so the seams don't stack. The add-on keeps at most 4 heaters on each fire pulse
  and 5 on a packet's two together when it sends them, splitting a sweep that needs more into one board pass per fire
  line;
- each layer's ink moves gently (POLYDYE_INK_MOTION: --ink-accel 1000, --ink-scv 1), the print's limits put back
  after it;
- after each layer's last extrusion, POLYDYE_JOB_PASS prints those passes --gap mm above it, so the next layer seals the
  ink in, in --wall-coats and --top-coats coats (1);
- the head is hot and its nozzles primed when the ink comes: POLYDYE_PREWARM about --prewarm s (6) of moves before each
  ink layer's end warms it during the plastic, and POLYDYE_PRIME spits at the wipe sponge before the first ink pass and
  any pass using a colour that hasn't ejected for --prime-idle s (90), by the print time estimated here (Klipper's
  planning of the moves);
- the passes' nozzle data goes into the G-code too, as comments after the print; POLYDYE_JOB at the start has the
  PolyDye Klipper add-on stream it to the board while the print runs.

No ink on the glass bed: a dot is only inked where a drop landing up to --edge-margin mm off still lands on plastic
printed so far (with a brim, the colour reaches the model's edge). Nor beyond the model's surface: a dot is only inked
where the layer's own part reaches at least --inset mm further out (ink outside the wall fell on the base below and
smeared at its foot, 2026-11-25). With --depth, only near it: a dot is inked only where it lies at most --depth mm
under the model's surface, beside it or above or below it (a top or bottom face), however deep Orca's colour goes (ink
2.3 mm into natural PLA read blurred and darker, 2026-11-25).

The add-on applies the colour registration and direction trim from polydye.cfg, so recalibrating needs no re-slice;
dpi and nozzle_dpi must be the ones in polydye.cfg (POLYDYE_JOB checks).

Usage: polydye_post.py [options] FILE.gcode   (rewrites FILE.gcode; Orca adds the file name)
In Orca: Others > Post-processing Scripts:  "C:\\path\\to\\python.exe" "C:\\path\\to\\polydye_post.py";
Needs Python 3 and numpy (pip install numpy).

This file is part of polydye-klipper (GPLv3).
"""
import argparse
import base64
import math
import os
import re
import sys
import zlib

try:
    import numpy as np
except ImportError:  # reported by main(), so the module still imports for its tests' skip
    np = None

# The job format, read by the add-on (klipper/polydye.py, "Ink jobs"). After the print:
#   ; POLYDYE_JOB_BEGIN
#   ;PDJ version=1 dpi=847 nozzle_dpi=300 pass_axis=y positions=16-83 passes=N layers=M prewarms=B primes=P
#        ink_accel=A ink_z=Z prewarm_s=S ...   (the lowest acceleration in force at an ink block, 0 if the G-code
#        sets none, the highest Z an ink block goes to, and each burst's SECONDS, so POLYDYE_JOB can check them)
#   ;PDP n=0 layer=3 pass=0 coat=1 object=NAME dir=1 x=112.3456 y=95.12345 columns=812 colours=cm
#   ;PDD <base64>   (one or more: the pass's data, zlib-compressed)
#   ...
#   ; POLYDYE_JOB_END
# A pass's data is a plane per colour in `colours` (c, m, y), each `columns` x 14 bytes: column i (along +Y from y),
# byte j holding nozzle positions p with p % 14 == j, as bit p // 14. Position p lands at X = x + (50 - p) * 25.4 /
# nozzle_dpi (positions go towards -X; POLYDYE_PASS's X names where position 50 lands), column i at Y = y + i * 25.4 /
# dpi before colour registration. object is the EXCLUDE_OBJECT name the ink belongs to ("-": none).
JOB_VERSION = 2  # 2: POLYDYE_INK_MOTION in the ink blocks (an older add-on refuses the job before the print starts)
COLOR_BYTES = 14
# The nozzle positions the stock firmware fires (its random pattern's, 14-85); the job draws on DEFAULT_POSITIONS,
# leaving 2 positions each way for the colour registration across the pass.
NOZZLE_FIRST, NOZZLE_LAST = 14, 85
DEFAULT_POSITIONS = (28, 82)
REF_POSITION = 50
DATA_LINE = 1024  # base64 characters per ;PDD line
MAX_LAYERS = 65000  # the board's pass keys: layer numbers from 65000 on are the add-on's own (calibration)
CHANNELS = 'cmy'
# Line types (;TYPE:) that are not the model's surface: never inked, even when a coloured filament prints them.
NO_INK_TYPES = ('skirt', 'brim', 'support', 'ironing', 'prime tower', 'wipe tower', 'custom')
TOP_TYPE = 'top surface'
# Line kinds: OTHER (never inked), the model's walls and others (MODEL), its top surface (TOP) and its other infill
# (INFILL: sparse, solid, bottom surface, bridges, gap fill), which --depth tells from walls.
MODEL, TOP, OTHER, INFILL = 1, 2, 0, 3
# --depth: an enclosed empty region of a layer that touches no infill and is at least HOLE_MIN_MM2 is a hole in the
# model (its walls are a surface); otherwise it's a gap in the infill, inside the model.
HOLE_MIN_MM2 = 0.1
# Heights and Z within Z_TOLERANCE mm are the same (Orca writes ;HEIGHT:0.200001): a layer exactly --depth under the
# top is inked. A step over Z_GAP mm between an object's layers is a gap (no plastic of it there).
Z_TOLERANCE, Z_GAP = 1e-3, 0.01
# The ordered dither: an 8 x 8 Bayer matrix over the plate's cells (nozzle positions across, column pairs along), a cell
# firing where its colour's level is over the threshold. Each colour reads it shifted (cyan, magenta, yellow), so at up
# to 25 % each they fire on different cells, and coat k by k - 1 cells both ways, between coat 1's.


def bayer(size):
    """The size x size Bayer matrix (size a power of 2): thresholds 0 .. size^2 - 1, the finest 2 x 2 pattern the most
    significant."""
    m, n = [[0]], 1
    while n < size:
        m = [[4 * m[i % n][j % n] + (0, 2, 3, 1)[2 * (i // n) + j // n] for j in range(2 * n)] for i in range(2 * n)]
        n *= 2
    return m


BAYER = bayer(8)
CHANNEL_SHIFT = ((0, 0), (1, 0), (0, 1))  # (rows, positions) per colour
# Estimates for the ink between layers (s): its moves up and back, the trip to the sponge and back for a prime, the
# time from a layer's last extrusion to its first pass's first column (when no prime comes first), and a pass's run-ups.
INK_MOVES_S, PRIME_TRIP_S, PREWARM_LEAD_S, RUN_UP_MM = 1., 3., 1., 3.
PREWARM_MAX_S = 16.


class PostError(Exception):
    pass


class Layer:
    def __init__(self, index):
        self.index = index
        self.start_line = None  # its ;LAYER_CHANGE line
        self.z = None
        self.segs = []  # (tool, kind, object, x0, y0, x1, y1, width)
        self.seg_heights = []  # each one's ;HEIGHT: (None where the G-code doesn't say)
        self.last_line = None  # the line of its last extrusion
        self.end = None  # (x, y, z) after it
        self.end_object = None  # the EXCLUDE_OBJECT name it was in, or None
        self.accel = None  # the acceleration the G-code set (if any) at its last extrusion: its ink block's


def parse(lines):
    """Reads Orca G-code: returns (layers, config, tool_lines, markers, objects, timing). config is the ; key = value
    config block; tool_lines the line numbers of tool changes; markers the line numbers of '; EXECUTABLE_BLOCK_START',
    '...END' and '; CONFIG_BLOCK_START' (None where missing); objects the EXCLUDE_OBJECT names by index; timing what
    line_times() needs: the moves (line, dx, dy, dz, de, speed, accel, square corner velocity, flush: Klipper stops
    before it) and the dwells (line, seconds)."""
    config, layers, tool_lines = {}, [], []
    markers = {'exec_start': None, 'exec_end': None, 'config_start': None}
    in_config = False
    tool, x, y, z, e = 0, 0., 0., 0., 0.
    absolute, e_relative = True, False
    width, kind, obj, cur, height = 0.45, MODEL, None, None, None
    objects = {}
    moves, dwells = [], []
    speed, accel, scv, flush = 25., None, None, True
    for n, line in enumerate(lines):
        if line.startswith(';'):
            s = line.rstrip()
            if s.startswith(';LAYER_CHANGE'):
                cur = Layer(len(layers))
                cur.start_line = n
                layers.append(cur)
            elif s.startswith(';Z:'):
                if cur is not None:
                    cur.z = float(s[3:])
            elif s.startswith(';WIDTH:'):
                width = float(s[7:])
            elif s.startswith(';HEIGHT:'):
                try:
                    height = float(s[8:])
                except ValueError:
                    height = None
            elif s.startswith(';TYPE:'):
                t = s[6:].strip().lower()
                kind = (OTHER if t.startswith(NO_INK_TYPES) else TOP if t == TOP_TYPE else
                        INFILL if 'infill' in t or 'surface' in t or 'bridge' in t or t == 'gap fill' else MODEL)
            elif s == '; EXECUTABLE_BLOCK_START':
                markers['exec_start'] = n
            elif s == '; EXECUTABLE_BLOCK_END':
                markers['exec_end'] = n
            elif s == '; CONFIG_BLOCK_START':
                markers['config_start'], in_config = n, True
            elif s == '; CONFIG_BLOCK_END':
                in_config = False
            elif s.startswith('; POLYDYE_JOB_BEGIN'):
                raise PostError("this G-code already has a PolyDye ink job (post-processed twice?)")
            elif in_config and ' = ' in s:
                key, value = s[1:].split(' = ', 1)
                config[key.strip()] = value.strip()
            continue
        words = line.split(';', 1)[0].split()
        if not words:
            continue
        cmd = words[0].upper()
        if re.match(r'^T\d+$', cmd):
            tool = int(cmd[1:])
            tool_lines.append(n)
        elif cmd in ('G0', 'G1', 'G92'):
            p = {}
            for w in words[1:]:
                try:
                    p[w[0].upper()] = float(w[1:])
                except (ValueError, IndexError):
                    pass
            if cmd == 'G92':
                x, y, z, e = p.get('X', x), p.get('Y', y), p.get('Z', z), p.get('E', e)
                continue
            if p.get('F', 0.) > 0.:
                speed = p['F'] / 60.
            if absolute:
                nx, ny, nz = p.get('X', x), p.get('Y', y), p.get('Z', z)
            else:
                nx, ny, nz = x + p.get('X', 0.), y + p.get('Y', 0.), z + p.get('Z', 0.)
            de = 0.
            if 'E' in p:
                rel = e_relative or not absolute  # G91 makes E relative too, whatever M82 says (as Klipper)
                de = p['E'] if rel else p['E'] - e
                e = e + de if rel else p['E']
            if de > 0 and (nx != x or ny != y) and cur is not None:
                index = objects.setdefault(obj, len(objects))
                cur.segs.append((tool, kind, index, x, y, nx, ny, width))
                cur.seg_heights.append(height)
                cur.last_line, cur.end, cur.end_object, cur.accel = n, (nx, ny, nz), obj, accel
            if de or nx != x or ny != y or nz != z:
                moves.append((n, nx - x, ny - y, nz - z, de, speed, accel, scv, flush))
                flush = False
            x, y, z = nx, ny, nz
        elif cmd == 'G4':
            p = dict((w[0].upper(), w[1:]) for w in words[1:] if w)
            try:
                dwells.append((n, float(p['P']) / 1000. if 'P' in p else float(p.get('S', 0.))))
            except ValueError:
                pass
            flush = True
        elif cmd in ('M400', 'M109', 'M190', 'TEMPERATURE_WAIT', 'BED_MESH_CALIBRATE', 'G28', 'G29'):
            flush = True
        elif cmd == 'M204':
            p = dict((w[0].upper(), w[1:]) for w in words[1:] if w)
            try:
                v = float(p['S']) if 'S' in p else min(float(p[k]) for k in 'PT' if k in p)
                accel = v if v > 0 else accel  # 0: the G-code's own limit off
            except ValueError:
                pass
        elif cmd == 'SET_VELOCITY_LIMIT':
            p = dict(w.upper().split('=', 1) for w in words[1:] if '=' in w)
            try:
                accel = float(p['ACCEL']) if float(p.get('ACCEL', 0)) > 0 else accel
                scv = float(p['SQUARE_CORNER_VELOCITY']) if 'SQUARE_CORNER_VELOCITY' in p else scv
            except ValueError:
                pass
        elif cmd in ('G2', 'G3'):
            raise PostError("line %d: arc moves (G2/G3): turn off Arc fitting in the PolyDye process profile "
                            "(Quality > Precision)" % (n + 1,))
        elif cmd == 'G90':
            absolute = True
        elif cmd == 'G91':
            absolute = False
        elif cmd == 'M82':
            e_relative = False
        elif cmd == 'M83':
            e_relative = True
        elif cmd == 'EXCLUDE_OBJECT_START':
            m = re.search(r'NAME=(\S+)', line.split(';', 1)[0], re.I)
            obj = m.group(1) if m else None
        elif cmd == 'EXCLUDE_OBJECT_END':
            obj = None
    names = [None] * len(objects)
    for name, index in objects.items():
        names[index] = name
    return layers, config, tool_lines, markers, names, {'moves': moves, 'dwells': dwells}


def line_times(count, timing, accel, scv, max_velocity):
    """Seconds each of `count` lines takes to print, as Klipper plans the moves (toolhead.py's lookahead: junction
    speeds from the square corner velocity, trapezoids at each move's accel; without minimum_cruise_ratio or the
    extruder's own limits). `accel`, `scv` are the defaults until the G-code sets them; `max_velocity` caps every move.
    Waits for temperatures and homing count nothing."""
    out = [0.] * count
    for n, t in timing['dwells']:
        out[n] += t
    moves = timing['moves']
    k = len(moves)
    jd_base = math.sqrt(2.) - 1.
    dist, acc, cruise, delta, max_start, linked = [0.] * k, [0.] * k, [0.] * k, [0.] * k, [0.] * k, [False] * k
    prev = None
    for i, (n, dx, dy, dz, de, v, a, sc, flush) in enumerate(moves):
        a, sc, v = a or accel, scv if sc is None else sc, max(1e-3, min(v, max_velocity))
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        if d < 1e-9:
            out[n] += abs(de) / v  # extrude only: a move of its own, from and to a stop
            prev = None
            continue
        u = (dx / d, dy / d, dz / d)
        dist[i], acc[i], cruise[i], delta[i] = d, a, v * v, 2. * a * d
        jd = sc * sc * jd_base  # junction_deviation * accel
        if prev is not None and not flush:
            pu, pcruise, pdelta, pjd, pstart = prev
            ms = min(cruise[i], pcruise, pstart + pdelta)
            cos_theta = -(u[0] * pu[0] + u[1] * pu[1] + u[2] * pu[2])
            sin_d2 = math.sqrt(max(0.5 * (1. - cos_theta), 0.))
            cos_d2 = math.sqrt(max(0.5 * (1. + cos_theta), 0.))
            if 1. - sin_d2 > 0. and cos_d2 > 0.:
                r_jd = sin_d2 / (1. - sin_d2)
                tan_q = .25 * sin_d2 / cos_d2
                ms = min(ms, r_jd * jd, r_jd * pjd, delta[i] * tan_q, pdelta * tan_q)
            max_start[i], linked[i] = ms, True
        prev = (u, cruise[i], delta[i], jd, max_start[i])
    start = [0.] * k
    nxt = 0.
    for i in range(k - 1, -1, -1):
        if not dist[i]:
            nxt = 0.
            continue
        end = nxt if i + 1 < k and linked[i + 1] else 0.
        start[i] = nxt = min(max_start[i], end + delta[i])
    for i in range(1, k):
        if linked[i]:
            start[i] = min(start[i], start[i - 1] + delta[i - 1])
    for i in range(k):
        if not dist[i]:
            continue
        v0 = start[i]
        v1 = start[i + 1] if i + 1 < k and linked[i + 1] else 0.
        peak = min(cruise[i], (delta[i] + v0 + v1) * .5)
        vp, a = math.sqrt(peak), acc[i]
        cruise_d = max(0., dist[i] - (2. * peak - v0 - v1) / (2. * a))
        out[moves[i][0]] += (2. * vp - math.sqrt(v0) - math.sqrt(v1)) / a + cruise_d / vp
    return out


def check_config(config):
    """Refuses settings the ink can't work with; returns the filament colours."""
    if 'filament_colour' not in config:
        raise PostError("no filament_colour in the G-code's config block: polydye_post.py needs OrcaSlicer G-code "
                        "(with its config block: Others > G-code output > don't turn off the config)")
    if config.get('enable_prime_tower', '0').strip() == '1':
        raise PostError("turn off the prime tower (Others > Prime tower) in the PolyDye profile: every filament is "
                        "the same spool, so there's nothing to purge")
    change = [ln for ln in config.get('change_filament_gcode', '').replace('\\n', '\n').split('\n')
              if ln.split(';', 1)[0].strip()]
    if change:
        raise PostError("clear Change filament G-code (Printer settings > Machine G-code) in the PolyDye printer "
                        "profile: it has %r, but the filaments are all the spool loaded" % ('; '.join(change),))
    if config.get('spiral_mode', '0').strip() == '1':
        raise PostError("spiral vase mode has no layers to ink after: turn it off")
    if config.get('print_sequence', 'by layer').strip().lower().replace('_', ' ') == 'by object':
        raise PostError("set Print sequence to By layer (Others): the ink passes cross the whole plate every layer")
    for key in ('nozzle_temperature', 'nozzle_temperature_initial_layer', 'filament_diameter'):
        values = [v.strip() for v in re.split('[,;]', config.get(key, '')) if v.strip()]
        if len(set(values)) > 1:
            raise PostError("the filaments' %s differ (%s): they all print as the one spool loaded, so give them the "
                            "same" % (key, ", ".join(values)))
    return [c.strip() for c in config['filament_colour'].split(';')]


def coverage(colour, limit):
    """Fraction of dots of cyan, magenta and yellow for an sRGB '#RRGGBB' filament colour: 1 - R, 1 - G, 1 - B, times
    the ink limit. White is no ink."""
    m = re.match(r'^#?([0-9a-fA-F]{6})', colour or '')
    if not m:
        raise PostError("filament colour %r isn't #RRGGBB" % (colour,))
    rgb = [int(m.group(1)[i:i + 2], 16) / 255. for i in (0, 2, 4)]
    cov = tuple(round((1. - v) * limit, 4) for v in rgb)
    return cov if max(cov) > 0.01 * limit else (0., 0., 0.)


class Grid:
    """Cells over the plate: columns along X one nozzle pitch apart, rows along Y two bus columns (a dither pair) apart.
    Cell (r, g) is centred on X = x0 + g * sx, Y = y0 + r * sy."""

    def __init__(self, x0, y0, nx, ny, sx, sy):
        self.x0, self.y0, self.nx, self.ny, self.sx, self.sy = x0, y0, nx, ny, sx, sy


def stamp(mask, r0, g0, grid, segs):
    """Marks the cells of `mask` (rows r0.., columns g0.. of `grid`) whose centres are within half its width of a
    segment (rows x0, y0, x1, y1, width of `segs`): sampled every half cell and snapped to cells, so within about half
    a cell diagonal."""
    if not len(segs):
        return
    step = min(grid.sx, grid.sy) / 2.
    widths = np.round(segs[:, 4], 2)
    for w in np.unique(widths):
        s = segs[widths == w]
        r = w / 2.
        if r <= 0:
            continue
        length = np.hypot(s[:, 2] - s[:, 0], s[:, 3] - s[:, 1])
        k = np.maximum(1, np.ceil(length / step).astype(np.int64)) + 1
        idx = np.repeat(np.arange(len(s)), k)
        t = (np.arange(k.sum()) - np.repeat(np.cumsum(k) - k, k)) / np.repeat(k - 1, k)
        px = s[idx, 0] + t * (s[idx, 2] - s[idx, 0])
        py = s[idx, 1] + t * (s[idx, 3] - s[idx, 1])
        cg = np.rint((px - grid.x0) / grid.sx).astype(np.int64) + 64
        cr = np.rint((py - grid.y0) / grid.sy).astype(np.int64) + 64
        wide = grid.nx + 256
        cells = np.unique(cr * wide + cg)
        cr, cg = cells // wide - 64, cells % wide - 64
        hx, hy = int(r / grid.sx) + 1, int(r / grid.sy) + 1
        for dg in range(-hx, hx + 1):
            for dr in range(-hy, hy + 1):
                if (dg * grid.sx) ** 2 + (dr * grid.sy) ** 2 > r * r:
                    continue
                rr, gg = cr + dr - r0, cg + dg - g0
                ok = (rr >= 0) & (rr < mask.shape[0]) & (gg >= 0) & (gg < mask.shape[1])
                mask[rr[ok], gg[ok]] = True


def erode(a, hy, hx):
    """Cells of `a` with every cell within hy rows and hx columns also set (outside `a` counts as unset)."""
    return _box(a, hy, hx, True)


def dilate(a, hy, hx):
    """Cells of `a` with any cell within hy rows and hx columns set (outside `a` counts as unset)."""
    return _box(a, hy, hx, False)


def close(a):
    """`a` with gaps of a cell filled: neighbouring lines that only miss each other by a cell's snapping are one."""
    return erode(dilate(a, 1, 1), 1, 1)


def _box(a, hy, hx, every):
    for h, axis in ((hy, 0), (hx, 1)):
        if h <= 0:
            continue
        n = a.shape[axis]
        pad = [(0, 0), (0, 0)]
        pad[axis] = (h + 1, h)
        count = np.cumsum(np.pad(~a if every else a, pad, constant_values=every).astype(np.int32), axis=axis)
        hits = np.take(count, np.arange(2 * h + 1, 2 * h + 1 + n), axis=axis) - np.take(count, np.arange(0, n),
                                                                                      axis=axis)
        a = hits == 0 if every else hits > 0
    return a


GOLDEN = (5 ** 0.5 - 1) / 2


def cell_hash(q, r):
    """A fixed pseudo-random 0-1 per plate cell (rows q, grid columns r: integer arrays), the same for every sweep."""
    h = (q.astype(np.int64) * 73856093) ^ (r.astype(np.int64) * 19349663)
    h = (h ^ (h >> 13)) * 1274126177
    return ((h ^ (h >> 16)) & 0xFFFF) / 65536.


def swaths(lo, hi, width, overlap, phase):
    """The first grid columns of the fewest runs of `width` columns, each overlapping the next by `overlap`, that
    cover columns lo..hi: the sweeps over an object's whole footprint. Their spare columns go `phase` (0-1) of the way
    to the low side, so layers with different phases don't line up their seams."""
    span, step = hi - lo + 1, width - overlap
    n = 1 if span <= width else 1 + int(math.ceil((span - width) / float(step)))
    spare = (n - 1) * step + width - span
    start = lo - int(math.floor(phase * (spare + 1)))
    return [start + j * step for j in range(n)]


class InkPass:
    def __init__(self, layer, coat, obj, x, y, columns, colours, data):
        self.layer, self.coat, self.obj, self.x, self.y = layer, coat, obj, x, y
        self.columns, self.colours, self.data = columns, colours, data
        self.n = self.number = self.dir = None


class Support:
    """The plastic printed so far (all layers up to the one being inked), per object: its own lines and those outside
    any object, which no EXCLUDE_OBJECT can take away. An object's ink never counts on another object's plastic, which
    could be excluded before it prints. Each object's cells are kept over its lines' extent, `pad` mm around."""

    def __init__(self, grid, layers, objects, pad):
        self.grid = grid
        self.shared = objects.index(None) if None in objects else None
        self.windows = {}
        for k in range(len(objects)):
            pts = np.array([s[3:7] for layer in layers for s in layer.segs if s[2] == k], float).reshape(-1, 4)
            if not len(pts):
                continue
            r0 = max(0, int((pts[:, [1, 3]].min() - pad - grid.y0) / grid.sy))
            r1 = min(grid.ny, int((pts[:, [1, 3]].max() + pad - grid.y0) / grid.sy) + 2)
            g0 = max(0, int((pts[:, [0, 2]].min() - pad - grid.x0) / grid.sx))
            g1 = min(grid.nx, int((pts[:, [0, 2]].max() + pad - grid.x0) / grid.sx) + 2)
            self.windows[k] = (r0, g0, np.zeros((r1 - r0, g1 - g0), bool))

    def add(self, segs, meta):
        """Adds a layer's lines (segs: x0, y0, x1, y1, width; meta: tool, kind, object)."""
        for k, (r0, g0, cells) in self.windows.items():
            stamp(cells, r0, g0, self.grid, segs[(meta[:, 2] == k) | (meta[:, 2] == self.shared)])

    def cells(self, k, ra, rb, ga, gb):
        """Object k's support over rows ra..rb - 1 and columns ga..gb - 1 of the grid (none outside its extent)."""
        out = np.zeros((rb - ra, gb - ga), bool)
        r0, g0, cells = self.windows[k]
        a, b = max(ra, r0), min(rb, r0 + cells.shape[0])
        c, d = max(ga, g0), min(gb, g0 + cells.shape[1])
        if a < b and c < d:
            out[a - ra:b - ra, c - ga:d - ga] = cells[a - r0:b - r0, c - g0:d - g0]
        return out


def fill_body(lines, infill, min_cells):
    """A layer's plastic as a solid: `lines` (its lines' cells) with the enclosed empty regions that are gaps in the
    infill filled in: those touching `infill` (cells next to its infill lines), or smaller than `min_cells`. An empty
    region that reaches the window's edge is outside; one enclosed by walls alone is a hole in the model."""
    free = ~lines
    h, w = free.shape
    edge = np.diff(np.pad(free, ((0, 0), (1, 1))).astype(np.int8), axis=1)
    rs, cs = np.nonzero(edge == 1)  # runs of empty cells along each row: row, first column, column after the last
    _, ce = np.nonzero(edge == -1)
    n = len(rs)
    if not n:
        return lines
    # Runs on neighbouring rows that overlap are one region: label them (hooking, then shortcutting to the roots).
    wide = w + 1
    key_start, key_end = rs * wide + cs, rs * wide + ce
    up = np.nonzero(rs > 0)[0]
    lo = np.searchsorted(key_end, (rs[up] - 1) * wide + cs[up], 'right')
    hi = np.searchsorted(key_start, (rs[up] - 1) * wide + ce[up], 'left') - 1
    count = np.maximum(hi - lo + 1, 0)
    ea = np.repeat(up, count)
    eb = np.arange(count.sum()) - np.repeat(np.cumsum(count) - count, count) + np.repeat(lo, count)
    root = np.arange(n)
    while len(ea):
        pa, pb = root[ea], root[eb]
        keep = pa != pb
        if not keep.any():
            break
        ea, eb, pa, pb = ea[keep], eb[keep], pa[keep], pb[keep]
        np.minimum.at(root, np.maximum(pa, pb), np.minimum(pa, pb))
        while True:
            nxt = root[root]
            if (nxt == root).all():
                break
            root = nxt
    flat = np.concatenate(([0], np.cumsum(infill.ravel() & free.ravel())))
    touches = flat[rs * w + ce] > flat[rs * w + cs]
    border = (rs == 0) | (rs == h - 1) | (cs == 0) | (ce == w)
    area = np.bincount(root, weights=ce - cs, minlength=n)
    inside = ~(np.bincount(root, weights=border, minlength=n) > 0) & (
        (np.bincount(root, weights=touches, minlength=n) > 0) | (area < min_cells))
    fill = inside[root]
    if not fill.any():
        return lines
    start, length = (rs * w + cs)[fill], (ce - cs)[fill]
    idx = np.arange(length.sum()) - np.repeat(np.cumsum(length) - length, length) + np.repeat(start, length)
    body = lines.copy()
    body.ravel()[idx] = True
    return body


def row_distance(body, sx, cap):
    """Per cell, the distance (mm) along its row from its centre to the nearest cell outside `body` (to that cell's
    edge: half a cell less than to its centre; 0 outside), at most `cap`."""
    w = body.shape[1]
    col = np.arange(w, dtype=np.int32)
    left = np.maximum.accumulate(np.where(body, np.int32(-10 ** 6), col), axis=1)
    right = np.minimum.accumulate(np.where(body, np.int32(10 ** 7), col)[:, ::-1], axis=1)[:, ::-1]
    k = np.minimum(col - left, right - col)
    return np.minimum(np.maximum(k - .5, 0.) * sx, cap).astype(np.float32)


class Shell:
    """--depth: how deep under the model's surface each cell of a layer's ink is, from the object's plastic in that
    layer and the ones above and below: the distance from the cell's centre, half-way up the layer, to the nearest
    place outside the object (beside it in its own or another layer's plastic, above its top, below its bottom). Each
    layer's plastic is its lines' cells, gaps in the infill filled in but holes in the model kept (fill_body()), as a
    slab its layer height thick; per object, from its own layers' lines (an object's layer heights can differ). The
    layer height is its thinnest line's ;HEIGHT:; a thicker line (combined infill, several layers' high) is plastic in
    the layers below it too, down as far as it reaches."""

    def __init__(self, grid, layers, support, depth):
        self.grid, self.layers, self.depth = grid, layers, depth
        self.windows = dict((k, (r0, g0, cells.shape)) for k, (r0, g0, cells) in support.windows.items())
        self.own, self.at, self.cache, self.thickest = {}, {}, {}, {}
        last = {}
        for layer in layers:
            mine = {}
            for s, h in zip(layer.segs, layer.seg_heights):
                if s[1] != OTHER:
                    mine.setdefault(s[2], []).append(h)
            top = layer.z if layer.z is not None else layer.end[2] if layer.end else None
            for k in sorted(mine):
                if top is None or (k in last and top <= last[k] + 1e-6):
                    raise PostError("--depth needs each object's layers going up in Z (not by-object printing)")
                known = [h for h in mine[k] if h]
                height = round(min(known) if known else top - last.get(k, 0.), 4)
                thick = max(height, round(max(known), 4) if known else 0.)
                self.at[(k, layer.index)] = len(self.own.setdefault(k, []))
                self.own[k].append((layer.index, round(top, 4), height, thick))
                self.thickest[k] = max(self.thickest.get(k, 0.), thick)
                last[k] = top

    def distances(self, k, j):
        """row_distance() of object k's j-th layer's plastic, over its window."""
        key = (k, j)
        if key not in self.cache:
            r0, g0, shape = self.windows[k]
            grid, own = self.grid, self.own[k]
            mid = own[j][1] - own[j][2] / 2.
            lines = list(self.layers[own[j][0]].segs)
            # Lines of the layers above that reach down past this one's middle (combined infill).
            for m in range(j + 1, len(own)):
                if own[m][1] - self.thickest[k] >= mid:
                    break
                if own[m][3] > own[m][2] + Z_GAP:
                    above = self.layers[own[m][0]]
                    lines += [s for s, h in zip(above.segs, above.seg_heights)
                              if s[2] == k and s[1] != OTHER and h and own[m][1] - h < mid]
            segs = np.array([s[3:] for s in lines], float).reshape(-1, 5)
            meta = np.array([s[:3] for s in lines], int).reshape(-1, 3)
            mine = (meta[:, 2] == k) & (meta[:, 1] != OTHER)
            lines, infill = np.zeros(shape, bool), np.zeros(shape, bool)
            stamp(lines, r0, g0, grid, segs[mine])
            stamp(infill, r0, g0, grid, segs[mine & np.isin(meta[:, 1], (TOP, INFILL))])
            body = fill_body(close(lines), dilate(infill, 1, 1), HOLE_MIN_MM2 / (grid.sx * grid.sy))
            self.cache[key] = row_distance(body, grid.sx, self.depth + grid.sx)
        return self.cache[key]

    def near(self, layer, k, ra, ga, need):
        """Of the cells `need` (bool, rows ra.., columns ga.. of the grid), those at most --depth under object k's
        surface in `layer`."""
        grid, depth = self.grid, self.depth
        own = self.own[k]
        j = self.at[(k, layer.index)]
        top, height = own[j][1], own[j][2]
        mid = top - height / 2.
        for key in [key for key in self.cache if key[0] == k and own[key[1]][1] < mid - depth - Z_GAP]:
            del self.cache[key]  # below everything still to come
        # The object's layers within --depth above and below, each as (its index, how far above or below its slab
        # is); where its plastic stops (its top, its bottom, a gap between its layers), everything is outside.
        slabs, free = [(j, 0.)], []
        edge, n = top, j + 1
        while True:
            if n == len(own) or own[n][1] - own[n][2] > edge + Z_GAP:
                free.append(edge - mid)
                break
            dz = own[n][1] - own[n][2] - mid
            if dz > depth + Z_TOLERANCE:
                break
            slabs.append((n, dz))
            edge, n = own[n][1], n + 1
        edge, n = top - height, j - 1
        while True:
            if n < 0 or own[n][1] < edge - Z_GAP:
                free.append(mid - edge)
                break
            dz = mid - own[n][1]
            if dz > depth + Z_TOLERANCE:
                break
            slabs.append((n, dz))
            edge, n = own[n][1] - own[n][2], n - 1
        limit = depth + Z_TOLERANCE
        if free and min(free) <= limit:
            return need.copy()
        r0, g0, shape = self.windows[k]
        rr, gg = np.nonzero(need)
        wr, wg = rr + ra - r0, gg + ga - g0
        found = np.zeros(len(rr), bool)
        todo = np.arange(len(rr))
        for n, dz in sorted(slabs, key=lambda s: s[1]):
            if not len(todo):
                break
            fx = self.distances(k, n)
            reach = math.sqrt(max(0., limit * limit - dz * dz))
            best = np.full(len(todo), np.inf, np.float32)
            for dr in range(-int(reach / grid.sy + 1.5), int(reach / grid.sy + 1.5) + 1):
                ay = max(0., abs(dr) * grid.sy - grid.sy / 2.)
                if ay > reach:
                    continue
                v = fx[np.clip(wr[todo] + dr, 0, shape[0] - 1), wg[todo]]
                np.minimum(best, v * v + ay * ay, out=best)
            hit = best + dz * dz <= limit * limit
            found[todo[hit]] = True
            todo = todo[~hit]
        out = np.zeros(need.shape, bool)
        out[rr[found], gg[found]] = True
        return out


def layer_passes(layer, grid, support, tools, objects, opts, pitch, shell=None):
    """The ink passes for `layer`, in printing order, with their directions; `support` (a Support: plastic so far, all
    layers up to this one) is updated with the layer; `shell` (a Shell, with --depth) keeps the ink near the
    surface."""
    segs = np.array([s[3:] for s in layer.segs], float).reshape(-1, 5)
    meta = np.array([s[:3] for s in layer.segs], int).reshape(-1, 3)
    support.add(segs, meta)
    # Snapped to cells, the support can reach half a cell diagonal past the lines: the margin allows for that.
    margin = opts.edge_margin + math.hypot(grid.sx, grid.sy) / 2. if opts.edge_margin else 0.
    hy, hx = int(math.ceil(margin / grid.sy)), int(math.ceil(margin / grid.sx))
    # The model's surface: its cells (snapped like the ink's) eroded by --inset, at least a cell where it's set.
    iy, ix = ((int(math.ceil(opts.inset / grid.sy)), int(math.ceil(opts.inset / grid.sx))) if opts.inset else (0, 0))
    p_lo, p_hi = opts.positions
    width = p_hi - p_lo + 1
    bayer = (np.array(BAYER, np.float32) + .5) / 64.
    out = []
    for obj in sorted(set(meta[:, 2].tolist())):
        inked = [t for t in sorted(set(meta[meta[:, 2] == obj, 0].tolist())) if t < len(tools) and any(tools[t])]
        sel = (meta[:, 2] == obj) & (meta[:, 1] != OTHER) & np.isin(meta[:, 0], inked)
        if not sel.any():
            continue
        s = segs[sel]
        reach = s[:, 4].max() / 2. + max(grid.sx, grid.sy)
        g0 = max(0, int((s[:, [0, 2]].min() - reach - grid.x0) / grid.sx))
        g1 = min(grid.nx, int((s[:, [0, 2]].max() + reach - grid.x0) / grid.sx) + 2)
        r0 = max(0, int((s[:, [1, 3]].min() - reach - grid.y0) / grid.sy))
        r1 = min(grid.ny, int((s[:, [1, 3]].max() + reach - grid.y0) / grid.sy) + 2)
        wr0, wg0 = max(0, r0 - hy - 1), max(0, g0 - hx - 1)
        allowed = erode(close(support.cells(obj, wr0, min(grid.ny, r1 + hy + 1), wg0, min(grid.nx, g1 + hx + 1))),
                        hy, hx)
        allowed = allowed[r0 - wr0:, g0 - wg0:][:r1 - r0, :g1 - g0]
        shape = (r1 - r0, g1 - g0)
        if iy or ix:
            # In a window wider than the ink's, so its edge isn't taken for the surface.
            br0, bg0 = max(0, r0 - iy - 2), max(0, g0 - ix - 2)
            br1, bg1 = min(grid.ny, r1 + iy + 2), min(grid.nx, g1 + ix + 2)
            body = np.zeros((br1 - br0, bg1 - bg0), bool)
            stamp(body, br0, bg0, grid, segs[(meta[:, 2] == obj) & (meta[:, 1] != OTHER)])
            body = erode(close(body), iy, ix)
            allowed &= body[r0 - br0:r0 - br0 + shape[0], g0 - bg0:g0 - bg0 + shape[1]]
        coats = []  # per tool: (coverage, cells and how many coats each)
        for t in inked:
            here = (meta[:, 2] == obj) & (meta[:, 0] == t)
            cells = np.zeros(shape, bool)
            stamp(cells, r0, g0, grid, segs[here & (meta[:, 1] != OTHER)])
            top = np.zeros(shape, bool)
            stamp(top, r0, g0, grid, segs[here & (meta[:, 1] == TOP)])
            cells, top = close(cells) & allowed, close(top)
            n = np.where(top & cells, opts.top_coats, np.where(cells, opts.wall_coats, 0))
            coats.append((tools[t], n))
        if shell is not None and coats:
            near = shell.near(layer, obj, r0, g0, np.logical_or.reduce([n > 0 for _, n in coats]))
            coats = [(cov, np.where(near, n, 0)) for cov, n in coats]
        name = objects[obj]
        for coat in range(1, max(opts.top_coats, opts.wall_coats) + 1):
            level = np.zeros((3,) + shape, np.float32)
            for cov, n in coats:
                for ch in range(3):
                    if cov[ch]:
                        level[ch] = np.maximum(level[ch], np.where(n >= coat, cov[ch], 0.))
            if not level.any():
                continue
            # One decision per cell, a pair of bus columns: every fire is at least two columns long. The thresholds
            # are fixed to the plate's cells, so neighbouring passes and layers dither alike.
            fires = np.zeros(level.shape, bool)
            for ch in range(3):
                dr, dg = CHANNEL_SHIFT[ch][0] + coat - 1, CHANNEL_SHIFT[ch][1] + coat - 1
                rows = (np.arange(r0, r0 + shape[0]) + dr) % 8
                cols = (np.arange(g0, g0 + shape[1]) + dg) % 8
                fires[ch] = level[ch] > bayer[np.ix_(rows, cols)]
            occupied = np.nonzero(fires.any(axis=(0, 1)))[0]
            if not len(occupied):  # a tone so light the dither fires nothing (--ink-limit 0.5, say)
                continue
            # Sweeps over the whole footprint along Y, every colour in each, the grid shifted per layer (and coat).
            # Neighbours overlap by --feather positions; there each cell (two rows, so four bus columns: pairs stay
            # together; one position) goes to one of them, the right one where its fixed hash is under a ramp that
            # rises across the band: every dot prints once, and the join has no line (polydye_picture4, 2026-11-25).
            phase = (layer.index * GOLDEN + (coat - 1) / 2.) % 1.
            padded = np.pad(fires, ((0, 0), (0, 0), (width, width)))
            starts = swaths(int(occupied[0]), int(occupied[-1]), width, opts.feather, phase)
            if opts.feather:
                band = np.arange(opts.feather)
                ramp = (band + .5) / opts.feather
                cells = (np.arange(r0, r0 + shape[0]) // 2)[:, None]
            for j, gs in enumerate(starts):
                sub = padded[:, :, gs + width:gs + 2 * width]
                if opts.feather and len(starts) > 1:
                    sub = sub.copy()
                    if j > 0:  # the band shared with the sweep before: keep the cells the ramp gives this one
                        sub[:, :, :opts.feather] &= cell_hash(cells, g0 + gs + band[None, :]) < ramp
                    if j < len(starts) - 1:
                        edge = width - opts.feather
                        sub[:, :, edge:] &= cell_hash(cells, g0 + gs + edge + band[None, :]) >= ramp
                rows = np.nonzero(sub.any(axis=(0, 2)))[0]
                if not len(rows):
                    continue
                ri0, ri1 = rows[0], rows[-1]
                colours, planes = '', []
                for ch in range(3):
                    f = sub[ch, ri0:ri1 + 1]
                    if not f.any():
                        continue
                    plane = np.zeros((2 * (ri1 - ri0 + 1), COLOR_BYTES), np.uint8)
                    for k in range(f.shape[1]):
                        p = p_hi - k
                        if f[:, k].any():
                            plane[:, p % COLOR_BYTES] |= np.repeat(f[:, k], 2).astype(np.uint8) << (p // COLOR_BYTES)
                    colours += CHANNELS[ch]
                    planes.append(plane.tobytes())
                x = grid.x0 + (g0 + gs) * grid.sx + (p_hi - REF_POSITION) * grid.sx
                # Row r's cell centre is between its two bus columns: the first is half a column before.
                y = grid.y0 + (r0 + ri0) * grid.sy - pitch / 2.
                out.append(InkPass(layer.index, coat, name, x, y, 2 * (ri1 - ri0 + 1), colours,
                                   zlib.compress(b''.join(planes), 9)))
    for k, p in enumerate(out):
        p.number = k
        p.dir = -1 if opts.bidirectional and k % 2 else 1
    return out


def ink_block(layer, passes, opts, inside, primes=()):
    """G-code that inks `passes` after `layer`'s last extrusion and comes back: out of the object it's in (`inside`),
    retracted, up by --gap, the passes with live ink (a prime at the sponge before each pass whose index is in
    `primes`), a pause for the ink to dry, back and down."""
    x, y, z = layer.end
    g = ["; PolyDye ink: layer %d (Z %.3f), %d pass%s" % (layer.index, z, len(passes), "" if len(passes) == 1 else
                                                         "es")]
    if inside:
        g.append("EXCLUDE_OBJECT_END NAME=%s" % (inside,))
    g += ["SAVE_GCODE_STATE NAME=polydye_ink", "G90", "M83"]
    if opts.retract:
        g.append("G1 E-%.2f F2700" % (opts.retract,))
    g.append("G1 Z%.3f F600" % (z + opts.gap,))
    if opts.ink_accel:
        g.append("POLYDYE_INK_MOTION ACCEL=%g SCV=%g" % (opts.ink_accel, opts.ink_scv))
    g.append("POLYDYE_ENABLE LIVE=%d" % (0 if opts.dry_run else 1))
    for k, p in enumerate(passes):
        if k in primes:
            g.append("POLYDYE_PRIME ; colours idle %g s or more (or the first ink): primed at the sponge"
                     % (opts.prime_idle,))
        g.append("POLYDYE_JOB_PASS N=%d ; layer %d pass %d, coat %d, %s, X=%.3f Y=%.3f DIR=%d" % (
            p.n, p.layer, p.number, p.coat, p.colours, p.x, p.y, p.dir))
    g += ["M400", "POLYDYE_ENABLE LIVE=0"]
    if opts.ink_accel:
        g.append("POLYDYE_INK_MOTION RESTORE=1")
    if opts.dry > 0:
        g.append("G4 P%d" % (int(round(opts.dry * 1000)),))
    g += ["G1 X%.3f Y%.3f F%d" % (x, y, opts.travel * 60), "G1 Z%.3f F600" % (z,)]
    if opts.retract:
        g.append("G1 E%.2f F2400" % (opts.retract,))
    g.append("RESTORE_GCODE_STATE NAME=polydye_ink")
    if inside:
        g.append("EXCLUDE_OBJECT_START NAME=%s" % (inside,))
    return g


def job_block(passes, opts, layers, prewarms=0, primes=0, ink_accel=0., ink_z=0., prewarm_s=0.):
    """The job's lines: its index and the passes' data. `prewarms`, `primes` count the G-code's POLYDYE_PREWARM and
    POLYDYE_PRIME, so POLYDYE_JOB can refuse a board that can't run them before the print starts (`prewarm_s`: each
    burst's SECONDS); `ink_accel` (0: none set) and `ink_z` let it check the passes' run-ups and the ink blocks' height
    against the axes."""
    out = ["; POLYDYE_JOB_BEGIN",
           ";PDJ version=%d dpi=%g nozzle_dpi=%g pass_axis=y positions=%d-%d passes=%d layers=%d prewarms=%d primes=%d "
           "ink_accel=%g ink_z=%.3f prewarm_s=%g generator=%s" % (
               JOB_VERSION, opts.dpi, opts.nozzle_dpi, opts.positions[0], opts.positions[1], len(passes), layers,
               prewarms, primes, ink_accel, ink_z, prewarm_s, os.path.basename(__file__))]
    for p in passes:
        out.append(";PDP n=%d layer=%d pass=%d coat=%d object=%s dir=%d x=%.4f y=%.5f columns=%d colours=%s" % (
            p.n, p.layer, p.number, p.coat, p.obj or '-', p.dir, p.x, p.y, p.columns, p.colours))
        text = base64.b64encode(p.data).decode()
        out += [";PDD " + text[i:i + DATA_LINE] for i in range(0, len(text), DATA_LINE)]
    out.append("; POLYDYE_JOB_END")
    return out


def process(lines, opts):
    """The post-processed G-code lines and a summary, or (None, why) when there's nothing to ink."""
    if np is None:
        raise PostError("needs numpy: pip install numpy")
    layers, config, tool_lines, markers, objects, timing = parse(lines)
    filaments = check_config(config)
    tools = [coverage(c, opts.ink_limit) for c in filaments]
    if len(layers) >= MAX_LAYERS:
        raise PostError("%d layers: PolyDye jobs take at most %d" % (len(layers), MAX_LAYERS - 1))
    used = set(s[0] for layer in layers for s in layer.segs)
    if not any(any(tools[t]) for t in used if t < len(tools)):
        return None, "no coloured filament prints anything: nothing to ink, the G-code is unchanged"
    pitch = 25.4 / opts.dpi
    sx, sy = 25.4 / opts.nozzle_dpi, 2. * pitch
    pts = np.array([s[3:7] for layer in layers for s in layer.segs], float)
    pad = 2. + opts.edge_margin + max(s[7] for layer in layers for s in layer.segs)
    x0, y0 = pts[:, [0, 2]].min() - pad, pts[:, [1, 3]].min() - pad
    grid = Grid(x0, y0, int((pts[:, [0, 2]].max() + pad - x0) / sx) + 1, int((pts[:, [1, 3]].max() + pad - y0) / sy)
                + 1, sx, sy)
    support = Support(grid, layers, objects, pad)
    shell = Shell(grid, layers, support, opts.depth) if opts.depth else None
    plan = []  # (layer, passes)
    for layer in layers:
        if not layer.segs:
            continue
        passes = layer_passes(layer, grid, support, tools, objects, opts, pitch, shell)
        if passes:
            plan.append((layer, passes))
    passes = [p for _, ps in plan for p in ps]
    if not passes:
        return None, "no ink lands on plastic (--edge-margin %g mm): the G-code is unchanged" % (opts.edge_margin,)
    for n, p in enumerate(passes):
        p.n = n
    before, primes = warm_and_prime(lines, layers, plan, config, timing, opts)
    # The lowest acceleration an ink block can run at: the G-code's, or POLYDYE_INK_MOTION's if lower (0: unknown).
    ink_accel = min([layer.accel for layer, _ in plan if layer.accel] + ([opts.ink_accel] if opts.ink_accel else [])
                    or [0.])
    ink_z = max(layer.end[2] + opts.gap for layer, _ in plan)
    count = sum(len(v) for v in primes.values())
    job = job_block(passes, opts, len(plan), len(before), count, ink_accel, ink_z,
                    opts.prewarm if before else 0.)
    size = sum(len(p.data) for p in passes)
    summary = ("PolyDye ink job: %d passes on %d layers, %.2f MB; dpi %g, nozzle_dpi %g, ink limit %g %%%s; %d "
               "prewarm bursts, %d primes" % (len(passes), len(plan), size / 1e6, opts.dpi, opts.nozzle_dpi,
                                              opts.ink_limit * 100, ", depth %g mm" % (opts.depth,) if opts.depth
                                              else "", len(before), count))
    insert = {}
    for layer, ps in plan:
        insert.setdefault(layer.last_line, []).extend(ink_block(layer, ps, opts, layer.end_object,
                                                                primes.get(layer.index, ())))
    insert[plan[-1][0].last_line].append("POLYDYE_JOB_END")
    drop = set(tool_lines)
    out = []
    # POLYDYE_JOB goes first, before the start G-code heats anything: a job the add-on refuses stops the print there.
    if markers['exec_start'] is not None:
        job_cmd_at = markers['exec_start'] + 1
    else:
        job_cmd_at = next(n for n, ln in enumerate(lines) if ln.split(';', 1)[0].strip())
    # The job after the print, before the config block: Moonraker reads the slicer's details from the file's end.
    job_at = markers['config_start'] if markers['config_start'] is not None else len(lines)
    for n, line in enumerate(lines):
        if n == job_at:
            out += [ln + "\n" for ln in job] + ["\n"]
        if n == job_cmd_at:
            out += ["; %s (polydye_post.py)\n" % (summary,), "POLYDYE_JOB\n"]
        if n in before:
            out.append(before[n] + "\n")
        if n not in drop:
            out.append(line)
        if n in insert:
            out += [ln + "\n" for ln in insert[n]]
    if job_at == len(lines):
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out += [ln + "\n" for ln in job]
    return out, summary


def warm_and_prime(lines, layers, plan, config, timing, opts):
    """Where the ink layers' prewarm bursts go and where the primes go, by the print time estimated (line_times()):
    ({line: the POLYDYE_PREWARM line to put before it}, {layer index: indices of its passes that prime first}).
    - A pass primes first (POLYDYE_PRIME, at the sponge) if it's the print's first ink pass or uses a colour that
      hasn't ejected for --prime-idle s, counting the passes before it in a long ink block; a prime ejects every
      colour.
    - Its burst (--prewarm s) starts where the moves before it end, so it goes before the line from which the layer's
      moves take that long to its end, less the lead to the first pass (the prime trip instead: the spit would cut a
      burst still firing); no earlier than the previous ink (or the first layer), so a short layer warms through."""
    before, primes = {}, {}
    if opts.dry_run:
        return before, primes
    # The profile's limits (0 there: off), else Klipper-like defaults.
    accel = opts.accel or first_value(config.get('default_acceleration'), 0.) or 3000.
    vmax = opts.max_velocity or first_value(config.get('machine_max_speed_x'), 0.) or 500.
    durations = line_times(len(lines), timing, accel, opts.scv, vmax)
    at = np.concatenate([[0.], np.cumsum(durations)])  # at[n]: when line n starts
    pitch = 25.4 / opts.dpi
    last_eject, ink_time = {}, 0.  # colour: when it last ejected; the ink so far (not in the lines)
    bound = layers[0].start_line
    for k, (layer, passes) in enumerate(plan):
        e = layer.last_line
        ink_start = at[e + 1] + ink_time
        # The passes in order after the ink's moves up and over: each primes first if one of its colours has been
        # idle too long by then (the print's first pass always); each colour ejects last at the end of its last pass.
        now = ink_start + INK_MOVES_S / 2.
        for j, p in enumerate(passes):
            if opts.prime_idle > 0 and ((k == 0 and j == 0) or any(
                    c not in last_eject or now - last_eject[c] >= opts.prime_idle for c in p.colours)):
                primes.setdefault(layer.index, []).append(j)
                now += PRIME_TRIP_S
                last_eject = dict.fromkeys(CHANNELS, now)
            # Two inks or more may go as two board passes, one per fire line (the add-on decides).
            sweeps = 2 if len(p.colours) > 1 else 1
            now += sweeps * ((p.columns * pitch + 2 * RUN_UP_MM) / opts.pass_speed + 60. / opts.travel)
            last_eject.update(dict.fromkeys(p.colours, now))
        prime = 0 in primes.get(layer.index, ())
        if opts.prewarm > 0:
            target = at[e + 1] + (0. if prime else PREWARM_LEAD_S) - opts.prewarm
            n = int(np.searchsorted(at, target, side='right')) - 1
            n = min(e, max(bound, n))
            before[n] = ("POLYDYE_PREWARM SECONDS=%g ; warms the head for layer %d's ink: %.1f s of moves to go"
                         % (opts.prewarm, layer.index, at[e + 1] - at[n]))
        bound = e + 1
        # The ink dries after its last pass, then the moves back.
        ink_time = now + opts.dry + INK_MOVES_S / 2. - at[e + 1]
    return before, primes


def first_value(text, default):
    """The first number of an Orca config value like '300,500', or `default`."""
    try:
        return float(re.split('[,;]', text)[0])
    except (TypeError, ValueError):
        return default


def positions(text):
    m = re.match(r'^(\d+)-(\d+)$', text.strip())
    if not m:
        raise argparse.ArgumentTypeError("positions are FIRST-LAST, e.g. 16-83")
    lo, hi = int(m.group(1)), int(m.group(2))
    if not NOZZLE_FIRST <= lo < hi <= NOZZLE_LAST:
        raise argparse.ArgumentTypeError("positions must be within %d-%d, the nozzles the stock firmware fires"
                                         % (NOZZLE_FIRST, NOZZLE_LAST))
    return lo, hi


def options(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("gcode", help="the G-code file, rewritten in place (Orca adds it)")
    ap.add_argument("--dpi", type=float, default=847., help="bus columns per inch along the pass: polydye.cfg's dpi "
                    "(default 847)")
    ap.add_argument("--nozzle-dpi", type=float, default=300., help="nozzle positions per inch across the pass: "
                    "polydye.cfg's nozzle_dpi (default 300)")
    ap.add_argument("--ink-limit", type=float, default=100., help="%% of the dots of each ink colour a fully coloured "
                    "filament fires, per coat (default 100: solid colours printed cleanest in one coat at 100 %%)")
    ap.add_argument("--wall-coats", type=int, default=1, help="coats on the model's sides and inside (default 1)")
    ap.add_argument("--top-coats", type=int, default=1, help="coats on top surfaces (default 1)")
    ap.add_argument("--gap", type=float, default=1., help="mm above the layer the nozzle goes for the ink (default 1)")
    ap.add_argument("--dry", type=float, default=5., help="seconds the ink dries before the next layer (default 5)")
    ap.add_argument("--retract", type=float, default=1.5, help="mm of filament retracted while inking (default 1.5)")
    ap.add_argument("--travel", type=float, default=150., help="mm/s back to the layer after the ink (default 150)")
    ap.add_argument("--edge-margin", type=float, default=0.5, help="mm a drop may land off and still be on plastic "
                    "printed so far; ink nearer the edge than that is left out, so none reaches the bed (default 0.5)")
    ap.add_argument("--inset", type=float, default=0.05, help="mm inside the model's surface the ink stops, so none "
                    "lands beyond it on what's below (default 0.05)")
    ap.add_argument("--depth", type=float, default=0., help="mm: ink only where a dot is at most this far under the "
                    "model's surface (beside, above or below it), however deep Orca's colour goes; 0: wherever the "
                    "colour is (default 0)")
    ap.add_argument("--positions", type=positions, default=DEFAULT_POSITIONS, help="nozzle positions a pass draws on, "
                    "FIRST-LAST (default 28-82: two groups of 14 on each fire line, cyan's 29-83 with its "
                    "registration, so two inks want exactly 4 heaters per fire pulse)")
    ap.add_argument("--feather", type=int, default=6, help="nozzle positions neighbouring sweeps overlap by, their "
                    "dots shared out by a fixed random ramp so the joins show no line; 0: side by side (default 6)")
    ap.add_argument("--ink-accel", type=float, default=1000., help="mm/s^2 at most while a layer's ink prints "
                    "(POLYDYE_INK_MOTION; 0: the print's own) (default 1000)")
    ap.add_argument("--ink-scv", type=float, default=1., help="square corner velocity at most while a layer's ink "
                    "prints, mm/s (default 1)")
    ap.add_argument("--one-direction", dest="bidirectional", action="store_false",
                    help="every pass along +Y (default: alternate directions)")
    ap.add_argument("--dry-run", action="store_true", help="the passes run with live ink off: the moves, no ink, and "
                    "no prewarm bursts or primes")
    ap.add_argument("--prewarm", type=float, default=6., help="seconds of each ink layer's prewarm burst "
                    "(POLYDYE_PREWARM), timed to end as its ink starts; 0: none (default 6)")
    ap.add_argument("--prime-idle", type=float, default=90., help="prime at the sponge (POLYDYE_PRIME) before the "
                    "first ink layer and any using a colour that hasn't ejected for this many seconds; 0: never "
                    "(default 90)")
    ap.add_argument("--pass-speed", type=float, default=59.5, help="polydye.cfg's pass_speed, mm/s, for the timing "
                    "(default 59.5)")
    ap.add_argument("--accel", type=float, default=0., help="mm/s^2 for the timing until the G-code sets one "
                    "(default: the profile's default_acceleration)")
    ap.add_argument("--scv", type=float, default=5., help="Klipper's square_corner_velocity, mm/s, for the timing "
                    "(default 5)")
    ap.add_argument("--max-velocity", type=float, default=0., help="Klipper's max_velocity, mm/s, for the timing "
                    "(default: the profile's maximum X speed)")
    opts = ap.parse_args(argv)
    for name, lo in (('dpi', 1.), ('nozzle_dpi', 1.), ('ink_limit', 0.), ('gap', 0.), ('dry', 0.), ('retract', 0.),
                     ('travel', 1.), ('edge_margin', 0.), ('inset', 0.), ('depth', 0.), ('prewarm', 0.),
                     ('prime_idle', 0.),
                     ('pass_speed', 1.), ('accel', 0.), ('scv', 0.), ('max_velocity', 0.), ('ink_accel', 0.),
                     ('ink_scv', 0.)):
        if getattr(opts, name) < lo:
            ap.error("--%s must be at least %g" % (name.replace('_', '-'), lo))
    if opts.ink_limit > 100.:
        ap.error("--ink-limit is a percentage, at most 100")
    if not 0 <= opts.feather <= (opts.positions[1] - opts.positions[0] + 1) // 2:
        ap.error("--feather must be 0 to half a sweep's width")
    if opts.prewarm > PREWARM_MAX_S:
        ap.error("--prewarm is at most %g s (a burst is at most 65535 columns: 16.6 s at warm_period_us 254)"
                 % (PREWARM_MAX_S,))
    if opts.wall_coats < 0 or opts.top_coats < 0:
        ap.error("coats can't be negative")
    opts.ink_limit /= 100.
    return opts


def main(argv=None):
    opts = options(argv)
    try:
        with open(opts.gcode, encoding="utf-8", errors="surrogateescape", newline="") as f:
            lines = f.readlines()
        out, summary = process(lines, opts)
    except (PostError, OSError) as e:
        sys.stderr.write("polydye_post.py: %s\n" % (e,))
        return 1
    if out is None:
        print("polydye_post.py: %s" % (summary,))
        return 0
    tmp = opts.gcode + ".polydye.tmp"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.writelines(out)
    os.replace(tmp, opts.gcode)
    print("polydye_post.py: %s" % (summary,))
    return 0


if __name__ == "__main__":
    sys.exit(main())
