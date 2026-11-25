# PolyDye inkjet support: schedules ink passes on the PolyDye board, a secondary Klipper MCU.
#
# Install by running install.sh from the polydye-klipper repo (see README.md).
#
# Copyright (C) 2026  polydye-klipper contributors
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import base64
import json
import logging
import math
import os
import re
import stat
import struct
import threading
import time
import zlib

import mcu as mcu_mod

PASS_INFO = "polydye_pass_info oid=%c layer=%hu pass=%hu present=%c columns=%hu cart=%c"
STATUS = ("polydye_status oid=%c flags=%c queued=%c passes=%u late_max=%u"
          " faults=%u store_count=%hu store_free=%u")
CARTRIDGE = "polydye_cartridge oid=%c template=%c pulse=%c"
LEVEL = "polydye_level oid=%c line=%c state=%c remaining=%hu"
# Cartridge waveform templates in polydye_set_template numbering: Sprite_tm's four, then PolyDye's color_b_late (color_b
# with the stock PolyDye firmware's timing, the board's default) and deskjet. The add-on offers TEMPLATES; board
# firmware has color_b_late from polydye_set_clock on. The stock-only cleanup removed the others (REMOVED_TEMPLATES),
# but older board firmware may still report one.
BOARD_TEMPLATES = ('color_a', 'color_b', 'black_a', 'black_b', 'color_b_late', 'deskjet')
TEMPLATES = ('color_b_late', 'black_a', 'black_b')
REMOVED_TEMPLATES = ('color_a', 'color_b', 'deskjet')
STOCK_TEMPLATE = 'color_b_late'
CLOCK = "polydye_clock oid=%c stock=%c nominal_hz=%u measured_hz=%u"
# Prewarm bursts (POLYDYE_PREWARM, board firmware with POLYDYE_PREWARM_WORDS) and the last one's state.
PREWARM = "polydye_prewarm oid=%c clock=%u columns=%hu period_us=%hu"
PREWARM_STATE = "polydye_prewarm_state oid=%c state=%c start=%u end=%u columns=%u"
PREWARM_STATES = ('none', 'queued', 'firing', 'done', 'made way', 'skipped')
# The board's last column run (board firmware with polydye_query_run, also answered after a shutdown): why it stopped
# (a shutdown reason, or "none"), columns sent, warm-up columns not sent, column times missed with no column ready
# (underruns) or the bus still busy (overruns), and its worst lateness and slowest column preparation in clock ticks.
RUN = ("polydye_run oid=%c fault_static_string_id=%hu columns=%u warm_left=%u underruns=%u overruns=%u late_max=%u "
       "fill_max=%u")
# The cartridge bus runs only at the stock PolyDye firmware's clock: its setup's divider of 1 acts as 2 on the board
# (measured 2.82-2.83 MHz), so 2.857 MHz, not the 5.714 MHz it looks like on paper. One word of fire pulse is 1 / BUS_HZ
# s (0.35 us). Klipper sets it on connect. polydye_set_clock numbers the clocks as CLOCK_NAMES: board firmware from
# before the stock-only cleanup also had ours and half, and firmware without polydye_set_clock runs ours (OURS_HZ).
BUS_HZ, OURS_HZ = 2857142., 3333333.
CLOCK_NAMES = ('ours', 'stock', 'half')
# Longest fire pulse for live ink (the board's POLYDYE_PULSE_MAX_NS): 100 % of the heater energy of Sprite_tm's 1.8 us
# at the cartridge's 16 V, at the 12.6 V the PolyDye high rail measures.
PULSE_MAX_US = 2.9
RAIL_V, REF_V, REF_US = 12.6, 16., 1.8
# Fire pulses to ask for, in bus words: 6 (2.10 us: Sprite_tm's 1.8 us rounded up to whole words, the stock firmware's
# pulse and the board's default) to 8 (2.80 us, the longest within PULSE_MAX_US).
PULSE_MIN, PULSE_MAX = 6, 8
FLAG_BOOST, FLAG_LIVE, FLAG_RUNNING, FLAG_LEVEL = 1, 2, 4, 8
# Signal check (polydye_set_level): the non-fire cartridge lines in bus-bit order, and their rail. The low-rail lines
# go through the CD4504 on the reference design's "9 V" rail, the others through the one on its "16 V" rail.
LEVEL_LINES = ('D1', 'D2', 'D3', 'CSYNC', 'S2', 'S4', 'S1', 'S5', 'DCLK', 'S3')
LOW_RAIL_LINES = ('D1', 'D2', 'D3', 'CSYNC')
LEVEL_STATES = {'off': 0, 'high': 1, 'low': 2}
LEVEL_STATE_NAMES = {1: 'high', 2: 'low', 3: 'slow'}
LINE_NONE = 255
# POLYDYE_FIRE_TEST's default stripes, TEMPLATE:PULSE: the stock firmware's settings (color_b_late, 2.10 us).
FIRE_TEST_STRIPES = "color_b_late:6"
# A stock-pattern spit test's default column period: the stock firmware's (one 1,452-word buffer at 2.857 MHz).
STOCK_PERIOD_US = 508
# polydye_spit: the board's POLYDYE_SPIT_HZ, and POLYDYE_SPIT_MAX columns
SPIT_HZ, SPIT_MAX_S = 1000, 10.
SPIT_PERIOD_US, SPIT_MAX_COLUMNS = 1000000 // SPIT_HZ, int(SPIT_MAX_S * SPIT_HZ)
# Head warm-up (POLYDYE_SPIT WARM=, POLYDYE_WARM; board firmware with polydye_spit_run): every nozzle fired, right before
# the spit in the same board run. Board firmware with POLYDYE_WARM_MAX_DUTY_PPM takes any pulse up to the live limit (the
# board's POLYDYE_WARM_MAX_NS, 2.9 us: 12 heaters on every fire pulse heat the head rather than spit), at most that
# heater duty (pulse / period, the board's POLYDYE_WARM_MAX_DUTY_PPM), for up to its POLYDYE_WARM_MAX_MS; older board
# firmware takes 1 or 2 words, at most WARM_OLD_US, for at most WARM_OLD_S. The default pulse is the longest within
# WARM_SHORT_US (too short to boil ink): WARM_WORDS, 0.70 us. The default period is the shortest the column and the duty
# allow. At most WARM_MAX_COLUMNS columns.
WARM_OLD_S, WARM_OLD_WORDS, WARM_OLD_US, WARM_MAX_COLUMNS = 10., 2, 0.75, 65535
WARM_SHORT_US = 0.75
WARM_WORDS = int(WARM_SHORT_US * BUS_HZ / 1e6)
WARM_MAX_S = 30.  # the longest warm-up any board firmware takes (POLYDYE_WARM_MAX_MS); older firmware's is WARM_OLD_S
# A spit test with PERIOD_US (board firmware with polydye_spit_run) keeps the board's column limit, SPIT_MAX_COLUMNS, so
# a shorter period means a shorter spit. The period must be at least the column's time on the bus plus
# PERIOD_MARGIN_US (the board's printcart_min_period_us()): 14 packets of the template plus 8 idle words, the template
# 27 words and 2 more for every fire-pulse word over 6.
TEMPLATE_WORDS, BASE_PULSE_WORDS, PACKETS, IDLE_WORDS, PERIOD_MARGIN_US = 27, 6, 14, 8, 20
# Spit and fire test patterns, with their polydye_spit numbers: every nozzle, or stock: the stock PolyDye firmware's
# firing (a random 255/1024 of positions 14-85 of each colour, every column twice), in the spit test by default with
# its template and pulse too (board firmware with POLYDYE_SPIT_PATTERNS over 3). The stock-only cleanup removed 1 mid
# and 2 sparse (REMOVED_PATTERNS).
SPIT_PATTERNS = {'all': 0, 'stock': 3}
# Automatic head warm-up before a live pass ([polydye] warm_seconds, warm_pulse, warm_period_us, warm_idle_time), when
# the head hasn't fired (or been warmed by a POLYDYE_PREWARM burst) since Klipper started or not for 5 s: it warms in
# about 4 s and is too cold to eject again in under 30 s (2026-11-25: 10 s of warm-up then 30 s idle printed almost
# nothing; 60 s at a quarter of the rate printed worse than 10 s at the full rate right before; after 30 s idle a burst
# of 1-3 s printed nothing to streaks, 4-5 s full magenta and cyan, 6-10 s every line full and solid). Every nozzle at
# 1 word (0.35 us) every 254 us for 6 s (4 s and half again). Brett's ladder on 48483ab (2026-11-25), each warm-up on a
# hot head 17 mm
# before a cyan text pass: 0.35 us every 508 us lost the word's first 10 mm; 0.35 us every 254 us printed it complete
# with no dot where it warmed (2 of 39,370 column times missed); 0.70 us every 508 us was complete but left a dot, and
# every 192 us a blob; 2.80 us spat a puddle. The pulse width matters, not just the energy. No ink may land on the glass
# bed (Brett, 2026-11-25), and the warm-up's spot (with any pre-spit) is often beyond the print: so it runs over the
# wipe sponge at the X gantry's right end by default (warm_position: sponge); `pass` (AUTO_WARM_CLEARANCE mm behind the
# pass's run-up start, warm_clearance) only suits a print with plastic there, such as a base under the part.
AUTO_WARM_S, AUTO_WARM_WORDS, AUTO_WARM_PERIOD_US, AUTO_WARM_IDLE_S = 6., 1, 254, 5.
AUTO_PREWARM_S = 6.  # POLYDYE_PREWARM's burst ([polydye] prewarm_seconds), the same recipe
AUTO_WARM_CLEARANCE = 10.
# An optional pre-spit right after a pass's warm-up, at the warm spot (the sponge by default), in the same board run
# ([polydye] warm_prespit,
# POLYDYE_PASS PRESPIT): the stock pattern every STOCK_PERIOD_US for that many seconds (at most PRESPIT_MAX_S). Off by
# default; for testing whether cyan, which still missed the first 9-14 mm of some passes after a gentle warm-up, needs
# real ejection first.
PRESPIT_MAX_S = 5.
# POLYDYE_PRIME: a nozzle that hasn't ejected for a while (cyan: 90 s on, 2026-11-25) misses about its first 60
# full-energy fires, and a warm-up burst (0.35 us) warms the head without ejecting, so an ink layer whose colours have
# been idle starts with a short stock-pattern spit at the sponge (it rides on the X gantry, so its gap is fixed; spits
# from over ~5 mm mist). PRIME_S seconds of it by default.
PRIME_S = 0.3
# POLYDYE_INK_MOTION: an ink block's moves are gentler than the plastic's, so the head and bed shake less during the
# passes (P box #4's ink blocks, 2026-11-25): acceleration and square corner velocity at most these by default.
INK_ACCEL, INK_SCV = 1000., 1.
# The board's warm-up duty limit (POLYDYE_WARM_MAX_DUTY_PPM), for checking polydye.cfg before connecting.
WARM_MAX_DUTY_PPM = 5800
REMOVED_PATTERNS = ('mid', 'sparse')
TEST_PATTERN_TEXT = {'all': "all-nozzle",
                     'stock': "stock-emulation (a random 24.9 % of positions 14-85, each column twice)"}
# POLYDYE_UPLOAD_TEST_PASS patterns: every other column all nozzles (the dry run's), every nozzle, none, or stock.
TEST_PASS_PATTERNS = ('alternate', 'all', 'none', 'stock')
# The random pattern (board firmware with polydye_set_spit_random, its spit pattern RANDOM_PATTERN): the stock pattern
# with knobs, for finding what makes a cartridge eject. PATTERN=stock with DENSITY (% of positions), REPEAT (each random
# column sent that many times), COLORS (any of c, m, y), FIRST/LAST (Sprite_tm's colour positions 0-111) or PER_PULSE
# (exactly that many random heaters on every fire pulse instead of DENSITY); those not given are the stock pattern's.
# The fire test and test passes make the same columns here (random_column), for any board firmware.
RANDOM_PATTERN = 4
RANDOM_KNOBS = ('DENSITY', 'REPEAT', 'COLORS', 'FIRST', 'LAST', 'PER_PULSE')
RANDOM_FULL, RANDOM_REPEAT_MAX, RANDOM_PER_PULSE_MAX, COLOR_POSITIONS = 1024, 16, 12, 112
STOCK_RANDOM = {'density': 255, 'repeat': 2, 'colors': 7, 'first': 14, 'last': 85, 'per_pulse': 0}
COLOR_NAMES = ('cyan', 'magenta', 'yellow')
# The board refuses live ink within 0.1 s of the boost turning on; POLYDYE_ENABLE keeps the next moves this far out.
BOOST_SETTLE = 0.15
# Byte order of the colour nozzles in a column, from Sprite_tm's printcart_genwaveform.c (printcart_fire_nozzle_color;
# Beer-ware licence, Jeroen Domburg): position p of colour c is bit p // 14 of byte COLOR_BYTE_ORDER[c][p % 14] + 14 c.
COLOR_BYTE_ORDER = ((8, 13, 4, 9, 0, 5, 10, 1, 6, 11, 2, 7, 12, 3),
                    (11, 2, 7, 12, 3, 8, 13, 4, 9, 0, 5, 10, 1, 6),
                    (0, 5, 10, 1, 6, 11, 2, 7, 12, 3, 8, 13, 4, 9))
TIMING_TOLERANCE = 0.0005  # s: planned pass duration must match the model
STATUS_INTERVAL = 2.  # s between background status queries
MAX_NUDGE = 5.  # mm per POLYDYE_NUDGE value; more is almost certainly a typo
NOZDATA_SIZE = 42
# At most this many heaters on one fire pulse (a packet, one fire line, across the colours): cyan and yellow at full
# width printed solid green with 3-4 per pulse and failed at 6; all three at 3 printed near-black, at ~9 failed; one
# colour at full width is ~3 (2026-11-25).
HEATERS_PER_PULSE = 4
# F3 and F5 fire in the same slot from the one 12.6 V rail: each line alone takes 4 heaters, but with F3 at 4 F5
# streaked even at 3; 3 + 2 printed solid (ladder 904, 2026-11-25). So at most this many on both lines of a packet
# together; a job pass that would need more (two or three inks, solid) goes to the board as two passes, one per fire
# line.
HEATERS_PER_SLOT = 5
# The second of those passes (fire line F5) is stored as pass number | SPLIT_PASS; job passes are numbered below it.
SPLIT_PASS = 0x8000
_NIBBLES = tuple(bin(v & 15).count('1') for v in range(256))
_HIGH_NIBBLES = tuple(bin(v >> 4).count('1') for v in range(256))
DATA_HELLO, DATA_PUT, DATA_ERR = 0x01, 0x10, 0xFF
DATA_CHUNK = 16384


class DataPortError(Exception):
    pass


def data_port_request(path, seq, ftype, payload=b"", timeout=3., rate=None):
    """One request on the board's data port (protocol in README.md, "Data port"). Returns the JSON reply. `rate`
    (bytes/s) paces a big request's writing, in DATA_CHUNK pieces."""
    import serial  # pyserial is part of klippy's environment
    ser = serial.Serial(path, 115200, timeout=timeout)
    try:
        head = struct.pack("<BBI", ftype, seq, len(payload))
        crc = zlib.crc32(payload, zlib.crc32(head)) & 0xffffffff
        frame = b"PD" + head + payload + struct.pack("<I", crc)
        if rate:
            due = time.time()
            for off in range(0, len(frame), DATA_CHUNK):
                if due > time.time():
                    time.sleep(due - time.time())
                ser.write(frame[off:off + DATA_CHUNK])
                due = max(due, time.time()) + DATA_CHUNK / float(rate)
        else:
            ser.write(frame)
        ser.flush()
        prev = b""
        while True:
            c = ser.read(1)
            if not c:
                raise DataPortError("no reply from the PolyDye data port")
            if prev == b"P" and c == b"D":
                break
            prev = c
        rhead = ser.read(6)
        if len(rhead) != 6:
            raise DataPortError("short reply")
        rtype, rseq, length = struct.unpack("<BBI", rhead)
        body = ser.read(length)
        rcrc = ser.read(4)
        if len(body) != length or len(rcrc) != 4 or \
                zlib.crc32(body, zlib.crc32(rhead)) & 0xffffffff != struct.unpack("<I", rcrc)[0]:
            raise DataPortError("corrupt reply")
        reply = json.loads(body.decode() or "{}")
        if rtype == DATA_ERR:
            raise DataPortError(reply.get("error", "error"))
        return reply
    finally:
        ser.close()


def pass_timing(t0, speed, accel, run_up, columns, pitch):
    """Times for a pass that starts from rest at print time t0.

    The toolhead accelerates at `accel` to `speed`; the first column is `run_up` mm from the start. Returns
    (t_first, period, duration) where duration is the length of the whole rest-to-rest move, including an
    equal run-out after the last column.
    """
    d_accel = speed * speed / (2. * accel)
    if run_up < d_accel:
        raise ValueError("run-up %.3f mm is shorter than the %.3f mm needed to reach %.1f mm/s"
                         % (run_up, d_accel, speed))
    t_accel = speed / accel
    t_first = t0 + t_accel + (run_up - d_accel) / speed
    length = (columns - 1) * pitch
    distance = length + 2. * run_up
    duration = 2. * t_accel + (distance - 2. * d_accel) / speed
    return t_first, pitch / speed, duration


def shaper_duration(printer, axis):
    """Length of the input shaper's impulse train for `axis`, or 0 without one."""
    input_shaper = printer.lookup_object('input_shaper', None)
    if input_shaper is None:
        return 0.
    for shaper in input_shaper.get_shapers():
        if shaper.get_name() == 'shaper_' + axis:
            n, A, T = shaper.get_shaper()
            if n:
                return max(T) - min(T)
    return 0.


def period_to_ticks(start_clock, end_clock, spans):
    """Column period in MCU ticks as (integer, 16-bit fraction)."""
    ticks = float(end_clock - start_clock) / spans
    whole = int(ticks)
    frac = int(round((ticks - whole) * 65536.))
    if frac >= 65536:
        whole, frac = whole + 1, 0
    return whole, frac


def pass_geometry(axis, bed_x, bed_y, direction, columns, pitch, run_up, offset, base, trim=0.):
    """Toolhead XY positions for a pass.

    (bed_x, bed_y) is where the first column lands on the bed (the pass-axis coordinate of the first column and
    the cross-axis coordinate of the nozzle-row centre). `offset` is the cartridge's (x, y) offset from the hotend
    nozzle and `base` the G-code base position. `trim` moves the ink of reverse (direction -1) passes along the pass
    axis, to line them up with forward passes. Returns (start, first, end): the toolhead XY at the start of the
    run-up, when the first column fires, and at the end of the run-out.
    """
    a = 0 if axis == 'x' else 1
    first = [bed_x - offset[0] + base[0], bed_y - offset[1] + base[1]]
    if direction < 0:
        first[a] += trim
    start, end = list(first), list(first)
    start[a] = first[a] - direction * run_up
    end[a] = first[a] + direction * ((columns - 1) * pitch + run_up)
    return start, first, end


def cartridge_text(st):
    """The board's cartridge settings from a status dict, for messages."""
    if st.get('cartridge_template') is None:
        return "cartridge template and fire pulse: unknown (board firmware too old)"
    text = "cartridge template %s, fire pulse %d words = %.2f us (%.0f %% energy)" % (
        st['cartridge_template'], st['pulse_words'], st['pulse_us'], pulse_energy_pct(st['pulse_us']))
    if st.get('bus_clock'):
        measured = st.get('bus_hz_measured')
        text += ", bus clock %s (%.3f MHz%s)" % (st['bus_clock'], st['bus_hz'] / 1e6,
                                                 ", last column measured %.3f MHz" % (measured / 1e6,)
                                                 if measured else "")
    return text


def rail_text(line):
    """Which rail a signal-check line reads when held high, for messages."""
    if line in LOW_RAIL_LINES:
        return "the low rail (the reference design's 9 V; about 7.6 V on a stock PolyDye board)"
    return "the high rail (the reference design's 16 V; about 12.6 V on a stock PolyDye board)"


def template_error(name):
    """None for a template the add-on offers, or why `name` isn't one: removed in the stock-only cleanup, or unknown."""
    if name in TEMPLATES:
        return None
    return "%s %s: use color_b_late, black_a or black_b" % (
        name, "was removed (stock baseline)" if name in REMOVED_TEMPLATES else "is not a cartridge template")


def clock_error(name):
    """None for the stock clock (CLOCK=stock and :stock stripes are still taken, as no-ops), or why `name` is
    refused."""
    if name == 'stock':
        return None
    return "CLOCK=%s %s: the bus runs only at the stock clock (2.857 MHz)" % (
        name, "was removed" if name in CLOCK_NAMES else "is not a bus clock")


def pattern_error(name, patterns):
    """None if `name` is one of `patterns`, or why not: the removed mid and sparse say what to use instead."""
    patterns = tuple(patterns)
    if name in patterns:
        return None
    if name in REMOVED_PATTERNS:
        return ("PATTERN=%s was removed: use stock (its knobs, e.g. PER_PULSE=1 for one heater per fire pulse) or all"
                % (name,))
    return "PATTERN must be %s or %s" % (", ".join(patterns[:-1]), patterns[-1])


def parse_stripes(text):
    """POLYDYE_FIRE_TEST STRIPES: "template:pulse,..." -> [(template, pulse)]. A ":stock" clock after the pulse (from
    before the stock-only cleanup) is taken and dropped. Raises ValueError."""
    stripes = []
    for item in text.replace(' ', '').split(','):
        if not item:
            continue
        parts = item.lower().split(':')
        if len(parts) not in (2, 3):
            raise ValueError("each stripe is TEMPLATE:PULSE with TEMPLATE one of %s, e.g. color_b_late:8"
                             % (", ".join(TEMPLATES),))
        if template_error(parts[0]):
            raise ValueError("%s: TEMPLATE=%s" % (item, template_error(parts[0])))
        if len(parts) == 3 and clock_error(parts[2]):
            raise ValueError("%s: %s" % (item, clock_error(parts[2])))
        try:
            words = int(parts[1])
        except ValueError:
            raise ValueError("pulse '%s' is not a whole number of words" % (parts[1],))
        if pulse_range_error(words):
            raise ValueError("%s: %s" % (item, pulse_range_error(words)))
        stripes.append((parts[0], words))
    if not stripes:
        raise ValueError("no stripes given")
    return stripes


def pulse_us(words):
    """Fire pulse length in us at the stock bus clock."""
    return words * 1e6 / BUS_HZ


def pulse_ns(words):
    """Fire pulse length in whole ns at the stock bus clock, rounded up like the board's printcart_pulse_ns()."""
    return -(-words * 10 ** 9 // int(BUS_HZ))


def pulse_energy_pct(us):
    """Heater energy of a pulse on the PolyDye high rail, in % of Sprite_tm's 1.8 us at 16 V (energy ~ V^2 t)."""
    return 100. * RAIL_V * RAIL_V * us / (REF_V * REF_V * REF_US)


def bus_clock_of(clock):
    """The bus clock's name and rate from a polydye_clock reply: the stock clock (whose rate board firmware b195932
    reports as 5.714 MHz), or on older board firmware switched to another on its console, that one's own rate."""
    n = clock['stock']
    if n == CLOCK_NAMES.index('stock'):
        return 'stock', BUS_HZ
    return (CLOCK_NAMES[n] if n < len(CLOCK_NAMES) else str(n)), clock['nominal_hz']


def warm_text(columns, words, period_us):
    """A warm-up's settings and total energy, for messages: per nozzle, as heating time and in reference pulses."""
    us = pulse_us(words)
    pct = pulse_energy_pct(us)
    note = ("too short to boil ink" if us <= WARM_SHORT_US + 1e-9
            else "12 heaters on every pulse heat the head rather than spit")
    return ("warm-up %.1f s: %d all-nozzle columns every %d us, %d-word pulses of %.2f us (%.0f %% energy each, %s); "
            "per nozzle %.0f us of heating in all, the energy of %.0f reference pulses (heater "
            "duty %.2f %%)" % (columns * period_us / 1e6, columns, period_us, words, us, pct, note, columns * us,
                               columns * pct / 100., 100. * us / period_us))


def auto_warm_error(seconds, words, period_us):
    """None if an automatic warm-up of `seconds` with `words`-word pulses every `period_us` is one the board takes, or
    why not."""
    if not 0. <= seconds <= WARM_MAX_S:
        return "warm_seconds must be 0 (off) to %g" % (WARM_MAX_S,)
    if not 1 <= words <= PULSE_MAX:
        return "warm_pulse must be 1-%d words (at most %.2f us)" % (PULSE_MAX, pulse_us(PULSE_MAX))
    if period_us < min_spit_period_us(words):
        return ("warm_period_us %d is shorter than the warm-up column (%d us at %d words)"
                % (period_us, min_spit_period_us(words), words))
    if pulse_ns(words) * 1000 > WARM_MAX_DUTY_PPM * period_us:
        return ("warm_pulse %d words (%.2f us) every %d us is over the warm-up's %.2f %% heater duty: use a period of at "
                "least %d us" % (words, pulse_us(words), period_us, WARM_MAX_DUTY_PPM / 1e4,
                                 -(-pulse_ns(words) * 1000 // WARM_MAX_DUTY_PPM)))
    if seconds * 1e6 / period_us > WARM_MAX_COLUMNS:
        return "warm_seconds %g is too long at %d us" % (seconds, period_us)
    return None


def warm_columns(seconds, period_us, max_s):
    """Columns of a warm-up of `seconds` every `period_us`, rounded, but never more than the board takes: its rule is
    columns x period_us at most its longest warm-up (POLYDYE_WARM_MAX_MS, `max_s`) in whole us. Rounding 30 s at
    1016 us up to 29528 columns made 30.000448 s, which the board refused with a shutdown (2026-11-25)."""
    return max(1, min(int(round(seconds * 1e6 / period_us)), int(round(max_s * 1e6)) // period_us))


def pass_warm_settings(words, period, cfg_words, cfg_period):
    """The pulse and period of a warm-up asked for before a pass (POLYDYE_PASS WARM): polydye.cfg's warm_pulse and
    warm_period_us unless given; a pulse given without a period gets the fastest its column and duty allow (None)."""
    if words is None and period is None:
        return cfg_words, cfg_period
    return (cfg_words if words is None else words), period


def head_cold(last_fire, now, idle_time):
    """Does the head need warming before firing at print time `now`? Yes if it hasn't fired since Klipper started
    (`last_fire` None) or last fired more than `idle_time` seconds before."""
    return last_fire is None or now - last_fire > idle_time


def column_us(words):
    """Time a column with a `words`-word pulse takes on the bus, in us, rounded up (printcart_column_us())."""
    length = TEMPLATE_WORDS + 2 * max(0, words - BASE_PULSE_WORDS)
    hz = int(BUS_HZ)
    return (PACKETS * (length + IDLE_WORDS) * 1000000 + hz - 1) // hz


def min_spit_period_us(words):
    """Shortest spit column period the board accepts for that column, in us (printcart_min_period_us())."""
    return column_us(words) + PERIOD_MARGIN_US


def spit_rate_text(period_us):
    """A spit test's column rate, for messages."""
    if period_us == SPIT_PERIOD_US:
        return "at %d Hz" % (SPIT_HZ,)
    return "every %d us (%.0f Hz)" % (period_us, 1e6 / period_us)


def clock_off_pct(measured_hz, nominal_hz):
    return (measured_hz - nominal_hz) * 100. / nominal_hz


def clock_wrong(measured_hz, nominal_hz, check):
    """True if the board's clock check (`check`: % fast, % slow) refuses a measured bus clock."""
    return check is not None and not -check[1] <= clock_off_pct(measured_hz, nominal_hz) <= check[0]


def measured_clock_text(measured_hz, nominal_hz, name, check=None):
    """The bus clock the board measured against its setting, for messages; `check` is the board's (% fast, % slow)
    limits, None if it doesn't check the clock."""
    text = "bus clock measured %.3f MHz (%s%.3f MHz, %+.1f %%)" % (
        measured_hz / 1e6, "%s clock " % (name,) if name else "set to ", nominal_hz / 1e6,
        clock_off_pct(measured_hz, nominal_hz))
    if check is None:
        return text
    if not clock_wrong(measured_hz, nominal_hz, check):
        return text + ", within the board's +%d/-%d %% clock check" % check
    return text + ": WRONG, outside the board's +%d/-%d %% clock check, so it fires nothing at it" % check


def run_fault(run):
    """Why the board's last run stopped (its shutdown reason without the "polydye: " prefix), or None."""
    fault = run['fault']
    if fault == 'none':
        return None
    return fault[len("polydye: "):] if fault.startswith("polydye: ") else fault


def run_text(run):
    """The board's account of its last run (polydye_run), for messages."""
    text = "%d columns sent" % (run['columns'],)
    if run['warm_left']:
        text += " (%d warm-up columns not sent)" % (run['warm_left'],)
    missed = run['underruns'] + run['overruns']
    if missed:
        text += ", %d column time%s missed (%d with no column ready, %d with the bus busy)" % (
            missed, "" if missed == 1 else "s", run['underruns'], run['overruns'])
    return text + ", worst lateness %.1f us, slowest column preparation %.1f us" % (run['late_max_us'],
                                                                                   run['fill_max_us'])


def pulse_range_text():
    """The pulses to ask for, for messages."""
    return "use %d-%d words (%.2f-%.2f us)" % (PULSE_MIN, PULSE_MAX, pulse_us(PULSE_MIN), pulse_us(PULSE_MAX))


def pulse_limit_error(words):
    """None, or why `words` is longer than the live-ink limit."""
    if words <= PULSE_MAX:
        return None
    return ("fire pulse %d words is %.2f us at the stock clock, over the %.1f us limit; %s"
            % (words, pulse_us(words), PULSE_MAX_US, pulse_range_text()))


def pulse_range_error(words):
    """None, or why `words` isn't a pulse to ask for: over the live-ink limit, or under Sprite_tm's 1.8 us."""
    if words < PULSE_MIN:
        return ("fire pulse %d words is %.2f us at the stock clock, less than Sprite_tm's %.1f us; %s"
                % (words, pulse_us(words), REF_US, pulse_range_text()))
    return pulse_limit_error(words)


def parse_colors(text):
    """COLORS: any of c, m and y, each once -> the board's colour bits (cyan 1, magenta 2, yellow 4). Raises ValueError."""
    t = text.strip().lower()
    if not t or len(set(t)) != len(t) or any(ch not in "cmy" for ch in t):
        raise ValueError("COLORS must be any of c, m and y, e.g. cmy or m (not %r)" % (text,))
    return sum(1 << "cmy".index(ch) for ch in t)


def random_text(r):
    """The random pattern's settings, for messages."""
    colours = "/".join(name for i, name in enumerate(COLOR_NAMES) if r['colors'] >> i & 1)
    which = ("%d heater%s on every fire pulse" % (r['per_pulse'], "" if r['per_pulse'] == 1 else "s")
             if r['per_pulse'] else "%.1f %%" % (r['density'] * 100. / RANDOM_FULL))
    times = {1: "once", 2: "twice"}.get(r['repeat'], "%d times" % (r['repeat'],))
    return "random: %s of positions %d-%d of %s, each column %s" % (which, r['first'], r['last'], colours, times)


_MASK32 = 0xFFFFFFFF
_SEED_DENSITY, _SEED_PER_PULSE, _GOLDEN = 0x5DEECE66, 0xA5F1523B, 0x9E3779B9


def _fmix32(h):
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & _MASK32
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & _MASK32
    return h ^ (h >> 16)


def random_plan(r):
    """For each packet and fire line (F3: bits 0-3, F5: bits 4-7), the heaters the random pattern may pick, as data line
    * 8 + bit, in the board's order (printcart_random_plan)."""
    plan = [[[], []] for _ in range(14)]
    for c in range(3):
        if r['colors'] >> c & 1:
            for p in range(r['first'], r['last'] + 1):
                byte, bit = COLOR_BYTE_ORDER[c][p % 14] + 14 * c, p // 14
                slots = plan[byte % 14][bit // 4]
                if len(slots) < RANDOM_PER_PULSE_MAX:
                    slots.append((byte // 14) * 8 + bit)
    return plan


def random_column(k, r, plan=None):
    """Column k of the random pattern with settings `r`, exactly as the board makes it (printcart_pattern_random):
    random column k // repeat. With per_pulse 0 each selected position fires with probability density / 1024, drawn in
    the stock pattern's order; otherwise every fire pulse gets exactly per_pulse of its eligible heaters (all of them if
    fewer). `plan` is random_plan(r), worked out once for many columns."""
    col = bytearray(NOZDATA_SIZE)
    m = (k // r['repeat']) & _MASK32
    if not r['per_pulse']:
        s = _fmix32(m ^ _SEED_DENSITY)
        for c in range(3):
            for p in range(r['first'], r['last'] + 1):
                s = (s + _GOLDEN) & _MASK32
                if r['colors'] >> c & 1 and (_fmix32(s) >> 22) < r['density']:
                    col[COLOR_BYTE_ORDER[c][p % 14] + 14 * c] |= 1 << (p // 14)
        return bytes(col)
    plan = plan or random_plan(r)
    s = _fmix32(m ^ _SEED_PER_PULSE)
    for packet in range(14):
        for half in range(2):
            pick = list(plan[packet][half])
            n = len(pick)
            for i in range(min(r['per_pulse'], n)):  # a partial Fisher-Yates shuffle: distinct heaters
                s = (s + _GOLDEN) & _MASK32
                j = i + _fmix32(s) % (n - i)
                pick[i], pick[j] = pick[j], pick[i]
                col[(pick[i] >> 3) * 14 + packet] |= 1 << (pick[i] & 7)
    return bytes(col)


def random_settings(gcmd, cmd):
    """The random pattern's settings from DENSITY (%), REPEAT, COLORS, FIRST, LAST and PER_PULSE, the stock pattern's
    for those not given; None if none is given."""
    if all(gcmd.get(k, None) is None for k in RANDOM_KNOBS):
        return None
    r = dict(STOCK_RANDOM)
    density = gcmd.get_float('DENSITY', None, minval=0., maxval=100.)
    if density is not None:
        r['density'] = int(round(density * RANDOM_FULL / 100.))
    r['repeat'] = gcmd.get_int('REPEAT', r['repeat'], minval=1, maxval=RANDOM_REPEAT_MAX)
    colors = gcmd.get('COLORS', None)
    if colors is not None:
        try:
            r['colors'] = parse_colors(colors)
        except ValueError as e:
            raise gcmd.error("%s: %s" % (cmd, e))
    r['first'] = gcmd.get_int('FIRST', r['first'], minval=0, maxval=COLOR_POSITIONS - 1)
    r['last'] = gcmd.get_int('LAST', r['last'], minval=0, maxval=COLOR_POSITIONS - 1)
    if r['first'] > r['last']:
        raise gcmd.error("%s: FIRST=%d is after LAST=%d" % (cmd, r['first'], r['last']))
    r['per_pulse'] = gcmd.get_int('PER_PULSE', r['per_pulse'], minval=0, maxval=RANDOM_PER_PULSE_MAX)
    return r


def random_params(r):
    """The random pattern's settings as G-code parameters (for POLYDYE_UPLOAD_TEST_PASS)."""
    return " DENSITY=%s REPEAT=%d COLORS=%s FIRST=%d LAST=%d PER_PULSE=%d" % (
        repr(r['density'] * 100. / RANDOM_FULL), r['repeat'], "".join(ch for i, ch in enumerate("cmy")
                                                                     if r['colors'] >> i & 1),
        r['first'], r['last'], r['per_pulse'])


def stripe_positions(axis, x, y, spacing, count):
    """Bed (X, Y) of each fire-test stripe's first column: stripes run along the pass axis, side by side across it."""
    if axis == 'y':
        return [(x + i * spacing, y) for i in range(count)]
    return [(x, y + i * spacing) for i in range(count)]


def test_pass_data(pattern, columns, repeat=1, random=None):
    """Nozzle data for POLYDYE_UPLOAD_TEST_PASS: `columns` columns of `pattern`, each sent `repeat` times in a row.
    `stock` is the random pattern with settings `random` (the stock pattern's if None) and that repeat. Returns (data,
    column count). Raises ValueError for a pattern not in TEST_PASS_PATTERNS."""
    if pattern_error(pattern, TEST_PASS_PATTERNS):
        raise ValueError(pattern_error(pattern, TEST_PASS_PATTERNS))
    full, empty = b"\xff" * NOZDATA_SIZE, b"\x00" * NOZDATA_SIZE
    if pattern == 'stock':
        r = dict(random or STOCK_RANDOM, repeat=repeat)
        plan = random_plan(r)
        # Random column k is column k // repeat of the pattern: work each one out once and repeat its bytes.
        cols = [random_column(i * repeat, r, plan) for i in range(columns)]
        return b"".join(col * repeat for col in cols), columns * repeat
    if pattern == 'alternate':
        cols = [full if i % 2 == 0 else empty for i in range(columns)]
    else:
        cols = [full if pattern == 'all' else empty] * columns
    return b"".join(col * repeat for col in cols), columns * repeat


def init_pulse_clock(oid, pulse_words, has_clock, stock_only):
    """polydye_set_pulse and polydye_set_clock commands to send on connect, in order: polydye.cfg's pulse, then the
    stock clock.

    Board firmware with POLYDYE_STOCK_ONLY runs only the stock clock and takes it as a no-op. Older board firmware may
    be at another clock (it booted on ours, or kept ours or half across a Klipper RESTART) and refuses any moment where
    the pulse would be over the limit. So it goes to ours first, the fastest clock, where any pulse within the limit at
    some clock is within it too; then a pulse within the limit at the stock clock, 6 words unless polydye.cfg sets one
    (9 words left from ours would be 3.15 us there, and 6-8 words set at half would be 3.6-4.8 us); then the stock
    clock. Firmware without polydye_set_clock gets just the pulse, if set.
    """
    legacy = has_clock and not stock_only
    if pulse_words is None and legacy:
        pulse_words = PULSE_MIN
    cmds = ["polydye_set_clock oid=%d stock=%d" % (oid, CLOCK_NAMES.index('ours'))] if legacy else []
    if pulse_words is not None:
        cmds.append("polydye_set_pulse oid=%d words=%d" % (oid, pulse_words))
    if has_clock:
        cmds.append("polydye_set_clock oid=%d stock=%d" % (oid, CLOCK_NAMES.index('stock')))
    return cmds


def nudge_offsets(offset_x, offset_y, trim, dx, dy, dtrim):
    """New (offset_x, offset_y, direction_trim) after moving the ink by (dx, dy) on the bed and reverse passes by
    dtrim along the pass axis.

    Ink lands at target - configured offset + true offset, so moving the ink by d means subtracting d from the
    configured offset. Direction trim is already an ink movement. Rounded to 0.1 um so repeated nudges stay tidy.
    """
    return round(offset_x - dx, 4), round(offset_y - dy, 4), round(trim + dtrim, 4)



# ---- calibration prints ---------------------------------------------------------------------------------------------
# POLYDYE_ALIGN_PRINT and POLYDYE_OFFSET_PRINT draw their passes here on the Pi, store them on the board as layer
# CAL_LAYER, passes 0, 1, ... (replacing what was there), and print them with POLYDYE_PASS. A pass's dots are drawn in
# bed order (draw_pass()): column index i counts bus columns along +pass_axis from the drawing's origin, p is Sprite_tm's
# colour nozzle position 0-111. With pass_axis y as PolyDye mounts the cartridge, positions go towards -X, and text drawn
# with image rows going down in position and image columns along +Y reads correctly from above (vernier page 1).
CAL_LAYER = 65000
# The big strips go up at CAL_UPLOAD_RATE: a whole firmware image at full USB speed once upset a Raspberry Pi 3's USB.
CAL_UPLOAD_RATE = 128 * 1024
# Colour registration ([polydye] cyan_columns, cyan_positions, yellow_columns, yellow_positions; POLYDYE_ALIGN): where
# cyan's and yellow's dots go against magenta's. The colour rows sit side by side along the pass, so their offset is
# fixed on the bed (vernier page 2, 2026-11-25: the corrected word printed without colour fringes in both directions): a
# colour's dot drawn at bed column i fires at bed column i + <colour>_columns (counted along +pass_axis, DIR=1's order;
# DIR=-1 passes print the same bed columns backwards) and at nozzle position p + <colour>_positions. Applied to every
# pass the add-on draws.
REG_KEYS = ('cyan_columns', 'cyan_positions', 'yellow_columns', 'yellow_positions')
REG_MAX_COLUMNS, REG_MAX_POSITIONS = 300, 30
# The cartridge's nozzle row isn't square to the pass: yawed about 1.7 degrees, nozzle position p's dots land
# nozzle_skew x (p - SKEW_PIVOT) nozzle pitches further along +pass_axis than position SKEW_PIVOT's (0.03 measured,
# 2026-11-25: 100 % walls of several swaths showed a sawtooth step at every join). Every pass the add-on draws moves
# each position's dots back by that, in whole bus columns fixed on the bed like the colour registration
# (skew_shifts()). The pivot is the position POLYDYE_PASS's X names, so the cartridge offset stays as calibrated.
SKEW_PIVOT, SKEW_MAX = 50, 0.2
CYAN, MAGENTA, YELLOW = 0, 1, 2
# Every calibration strip starts with a priming block, printed first, then PRIME_GAP empty columns: nozzles that hadn't
# fired for a while missed their first marks even right after a gentle warm-up, but words led in by 300 columns of the
# stock pattern (a random quarter of the positions of every colour) printed every colour from their first column
# (vernier page 2). PRIME is the stock pattern over positions 14-85.
PRIME_COLUMNS, PRIME_GAP = 300, 100
PRIME = STOCK_RANDOM
# POLYDYE_ALIGN_PRINT, the vernier page (ported from the alignment page printed on 2026-11-24): blocks k = -16..16,
# ALIGN_BLOCK columns apart, and an empty block at each end. In each row the mark that lines up with its magenta
# reference names k, which POLYDYE_ALIGN turns into the correction:
# - C, Y (along the pass): a magenta bar on positions 20-49 at the block centre, the colour's bar on 50-79 3k columns
#   further: CYAN=k / YELLOW=k move that colour's dots by 3k columns (<colour>_columns += 3k).
# - D (direction): magenta bars on 20-49 printed forwards, and on 50-79 by a DIR=-1 pass 2k columns further on:
#   DIRECTION=k moves reverse passes by 2k columns (direction_trim += 2k x 25.4 / dpi mm).
# - CX, YX (across the pass): 2-position magenta lines at 49-50 and the colour's at 49+k to 50+k: CYAN_X=k /
#   YELLOW_X=k move that colour's dots by k positions.
# The rows are side by side across the pass, ALIGN_SPACING mm apart, between two rows of block numbers.
ALIGN_BLOCK, ALIGN_K = 96, 16
ALIGN_COLUMNS = ALIGN_BLOCK * (2 * ALIGN_K + 3)
ALIGN_SPACING = 8.
# The paper the page needs: ALIGN_PAPER (across, along the pass, mm) with ALIGN_PAPER_MARGIN all round its ink (the
# rows, the nozzle row's width at 10 % more than nozzle_dpi, the priming blocks at both ends). Settings that need more
# are refused, so the size the page asks for is always enough.
ALIGN_PAPER, ALIGN_PAPER_MARGIN = (70., 140.), 5.
ALIGN_ROWS = ('labels', 'C', 'Y', 'D', 'CX', 'YX', 'labels')
# No ink on the glass bed, ever (Brett, 2026-11-25): calibration ink and its priming blocks go on paper (the alignment
# page) or printed plastic (the offset print), and the warm-up before them (which fires every nozzle) over the wipe
# sponge at the X gantry's right end (POLYDYE_WIPE's X; Brett confirmed it is fitted), whatever warm_position says.
# Their passes don't warm again.
# POLYDYE_OFFSET_PRINT (pass_axis y): a PLA plaque, then two ink scales across a cross-shaped groove, OFFSET_SLOT mm wide,
# in its top OFFSET_GROOVE_LAYERS layers, centred on the X, Y asked for. The X scale: lines along Y every OFFSET_X_TICK
# nozzle positions (about 0.25 mm at nozzle_dpi 300), across the groove that runs along Y; the Y scale: lines along X
# about every OFFSET_Y_TICK_MM (whole columns: 8 at dpi 847; offset_y_tick()), across the groove that runs along X. Tick 0 is where the groove's
# centre should be; the tick in the middle of the groove, k, is the reading: POLYDYE_OFFSET X_TICK=k / Y_TICK=k moves
# the ink by k ticks. Every 4th tick is long and every 8th numbered. OFFSET_REF_POSITION is the nozzle position
# POLYDYE_PASS's X, Y name across the pass.
OFFSET_REF_POSITION = 50
OFFSET_X_TICK, OFFSET_X_TICKS = 3, 11
OFFSET_Y_TICK_MM, OFFSET_Y_TICKS = 0.24, 12
# Its numbers' rows are OFFSET_TEXT_ROW_MM along the pass, so the layout keeps its size at another dpi.
OFFSET_TEXT_ROW_MM = 0.12
OFFSET_SLOT = 1.
OFFSET_LAYERS, OFFSET_GROOVE_LAYERS, OFFSET_LAYER_HEIGHT, OFFSET_GAP = 5, 3, 0.2, 1.
# The plaque around the groove's centre (-X, +X, -Y, +Y mm): the ink (priming included) spans -3.3..+9.9 mm in X (at
# nozzle pitches up to 1/270") and -14.1..+10.5 mm in Y, and the plaque reaches OFFSET_MARGIN mm beyond, so ink that lands
# that far off still lands on plastic. Where the grooves end: the one along X OFFSET_SLOT_END[0] mm to +X, before the Y
# scale's numbers; the one along Y OFFSET_SLOT_END[1] mm up, below the X scale's. The Y scale's pass is
# OFFSET_Y_SCALE_X mm to +X. The plaque's retraction and travel are the coordinator's ink-on-PLA plaque's
# (2026-11-24).
OFFSET_MARGIN = 6.5
OFFSET_PLAQUE = (3.3 + OFFSET_MARGIN, 9.9 + OFFSET_MARGIN, 14.1 + OFFSET_MARGIN, 10.5 + OFFSET_MARGIN)
OFFSET_SLOT_END = (7.35, 9.3)
OFFSET_Y_SCALE_X = 6.5
PLAQUE_RETRACT, PLAQUE_TRAVEL_F = 0.8, 9000
# POLYDYE_TEST_PAGE: on paper, whether the head prints the way a colour print needs it to. Drawn here like the
# calibration prints (test_page_drawings()) and printed as a colour print's passes are: the heater cap, split by fire
# line where two inks need it (drawing_parts()), positions TEST_POSITIONS (polydye_post.py's default) and the ink
# blocks' gentle moves (POLYDYE_INK_MOTION); first one warm-up and prime over the wipe sponge (POLYDYE_SPIT WARM=).
# Seen from the front, left to right, a column each for cyan, magenta and yellow: at the front its nozzle check, every
# nozzle 14-85 a TEST_DASH-column dash in turn, in staircases of TEST_STAIR nozzles (a missing or weak nozzle leaves a
# gap in its staircase; all should look alike), then its solid square; at the back red, green and blue squares (two
# inks: two passes each, one per fire line). On the right two sweeps of blue the page's length, their join feathered
# over TEST_FEATHER positions like polydye_post.py's and printed in opposite directions like a print's sweeps.
TEST_POSITIONS = (28, 82)
TEST_Z = 1.
TEST_COLUMN_MM, TEST_GAP_MM = 8., 3.
TEST_DASH, TEST_STEP, TEST_STAIR = 16, 24, 8
TEST_FEATHER = 6
# Digits and row names in a 5 x 7 font.
FONT = {
    '0': ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    '1': ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    '2': ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    '3': ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    '4': ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    '5': ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    '6': ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    '7': ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    '8': ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    '9': ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    '-': ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    'C': ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    'D': ("11100", "10010", "10001", "10001", "10001", "10010", "11100"),
    'X': ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    'Y': ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
}


def text_size(text):
    """(width, height) of `text` in font pixels: 5 x 7 characters one pixel apart."""
    return 6 * len(text) - 1, 7


class PassDrawing:
    """The dots of a pass the add-on draws, in bed order (see CAL_LAYER), before colour registration (pass_data())."""

    def __init__(self, lo=0, hi=-1):
        self.dots = set()  # (column, colour, position)
        self.raw = {}  # column -> nozzle data put in as it is (the priming block)
        self.lo, self.hi = lo, hi  # columns the pass covers even where they're empty

    def dot(self, i, c, p):
        self.dots.add((i, c, p))

    def bar(self, i0, i1, c, p0, p1):
        """Every dot from column i0 to i1 and position p0 to p1, both inclusive."""
        for i in range(i0, i1 + 1):
            for p in range(p0, p1 + 1):
                self.dots.add((i, c, p))

    def text(self, text, c, i, p, col_px, pos_px, rotated=False):
        """`text` in FONT, each font pixel col_px columns by pos_px positions. Normally it reads along +pass from
        column i (its left edge), its top row at position p and rows going down in position. `rotated`: it reads down
        in position from p (its left edge), its top row at column i and rows going back along the pass (pass_axis y:
        along +X, the top towards +Y)."""
        for n, ch in enumerate(text):
            for r, row in enumerate(FONT[ch]):
                for x, bit in enumerate(row):
                    if bit != '1':
                        continue
                    across = (6 * n + x) if rotated else r
                    along = r if rotated else (6 * n + x)
                    if rotated:
                        i0, p0 = i - along * col_px - (col_px - 1), p - across * pos_px - (pos_px - 1)
                    else:
                        i0, p0 = i + along * col_px, p - across * pos_px - (pos_px - 1)
                    self.bar(i0, i0 + col_px - 1, c, p0, p0 + pos_px - 1)

    def prime(self, i0):
        """The priming block (PRIME, PRIME_COLUMNS) from column i0 on."""
        plan = random_plan(PRIME)
        for k in range(PRIME_COLUMNS):
            self.raw[i0 + k] = random_column(k, PRIME, plan)


def skew_shifts(skew, dpi, nozzle_dpi):
    """The bed columns each nozzle position's dots move by (a tuple of COLOR_POSITIONS) to undo nozzle_skew `skew`:
    -skew x (p - SKEW_PIVOT) nozzle pitches, rounded to whole columns. None when there's nothing to move."""
    shifts = tuple(int(math.floor(-skew * (p - SKEW_PIVOT) * dpi / nozzle_dpi + 0.5)) for p in range(COLOR_POSITIONS))
    return shifts if any(shifts) else None


def pass_data(drawing, direction, registration=(0, 0, 0, 0), skew=None):
    """Nozzle data to print `drawing` in `direction` with colour registration (cyan_columns, cyan_positions,
    yellow_columns, yellow_positions; dots moved beyond the nozzle row are left out) and the nozzle row's skew (`skew`:
    skew_shifts(), or None). Returns (data, columns, first): first is the drawing column of the pass's first column, the
    one POLYDYE_PASS's X, Y name."""
    cols = _drawing_cols(drawing, registration, skew)
    for i, raw in drawing.raw.items():
        col = cols.setdefault(i, bytearray(NOZDATA_SIZE))
        for n, b in enumerate(raw):
            col[n] |= b
    lo = min([drawing.lo] + list(cols))
    hi = max([drawing.hi] + list(cols))
    order = range(lo, hi + 1) if direction > 0 else range(hi, lo - 1, -1)
    empty = bytes(NOZDATA_SIZE)
    return b"".join(bytes(cols[i]) if i in cols else empty for i in order), hi - lo + 1, lo if direction > 0 else hi


def _drawing_cols(drawing, registration, skew):
    """`drawing`'s dots (not its priming blocks) as nozzle data with the colour registration and skew: {drawing column:
    bytearray}."""
    along = (registration[0], 0, registration[2])
    across = (registration[1], 0, registration[3])
    cols = {}
    for i, c, p in drawing.dots:
        p += across[c]
        if 0 <= p < COLOR_POSITIONS:
            col = cols.setdefault(i + along[c] + (skew[p] if skew else 0), bytearray(NOZDATA_SIZE))
            col[COLOR_BYTE_ORDER[c][p % 14] + 14 * c] |= 1 << (p // 14)
    return cols


def drawing_parts(drawing, direction, registration=(0, 0, 0, 0), skew=None):
    """The board passes that print `drawing`'s dots (not its priming blocks) the way a colour print's pass is printed
    (job_pass_parts()): with the colour registration and skew like pass_data(), the heater cap, and split by fire line
    where two or more inks need it. [(data, columns, first)] like pass_data()'s; empty if no dot is left."""
    cols = _drawing_cols(drawing, registration, skew)
    if not cols:
        return []
    lo, hi = min(cols), max(cols)
    empty = bytes(NOZDATA_SIZE)
    out = bytearray(b"".join(bytes(cols[i]) if i in cols else empty for i in range(lo, hi + 1)))
    inks = len(set(c for _, c, _ in drawing.dots))
    return [part[1:] for part in _pass_parts(out, lo, inks, direction)]


def align_centre(k):
    """Drawing column of block k's centre on the vernier page."""
    return ALIGN_BLOCK // 2 + (k + ALIGN_K + 1) * ALIGN_BLOCK


def align_drawings():
    """The vernier page's passes as (row, drawing, direction), in ALIGN_ROWS order with the D row's reverse pass after
    its forward one; the label row is printed again as the last row. The drawing's column 0 is the page's start."""
    ks = range(-ALIGN_K, ALIGN_K + 1)

    def new(row, direction):
        d = PassDrawing(0, ALIGN_COLUMNS - 1)
        # Printed first: before the page going forwards, after it going backwards.
        d.prime(-PRIME_GAP - PRIME_COLUMNS if direction > 0 else ALIGN_COLUMNS + PRIME_GAP)
        if row not in ('labels', 'D-'):
            w = text_size(row)[0] * 7
            d.text(row, MAGENTA, (ALIGN_BLOCK - w) // 2, 64, 7, 5)
        return d

    out = []
    d = new('labels', 1)
    for k in ks:
        d.bar(align_centre(k) - 2, align_centre(k) + 1, MAGENTA, 14, 28 if k == 0 else 21)
        if k % 2 == 0:
            s = "%d" % (k,)
            d.text(s, MAGENTA, align_centre(k) - text_size(s)[0] * 9 // 2, 64, 9, 5)
    out.append(('labels', d, 1))
    for row, colour in (('C', CYAN), ('Y', YELLOW)):
        d = new(row, 1)
        for k in ks:
            d.bar(align_centre(k) - 2, align_centre(k) + 1, MAGENTA, 20, 49)
            d.bar(align_centre(k) + 3 * k - 2, align_centre(k) + 3 * k + 1, colour, 50, 79)
        out.append((row, d, 1))
    d = new('D', 1)
    for k in ks:
        d.bar(align_centre(k) - 2, align_centre(k) + 1, MAGENTA, 20, 49)
    out.append(('D', d, 1))
    d = new('D-', -1)
    for k in ks:
        d.bar(align_centre(k) + 2 * k - 2, align_centre(k) + 2 * k + 1, MAGENTA, 50, 79)
    out.append(('D-', d, -1))
    for row, colour in (('CX', CYAN), ('YX', YELLOW)):
        d = new(row, 1)
        for k in ks:
            d.bar(align_centre(k) - 20, align_centre(k) + 19, MAGENTA, 49, 50)
            d.bar(align_centre(k) - 20, align_centre(k) + 19, colour, 49 + k, 50 + k)
        out.append((row, d, 1))
    return out


def offset_y_tick(columns_per_mm):
    """Columns between the offset print's Y ticks: OFFSET_Y_TICK_MM in whole columns."""
    return max(1, int(round(OFFSET_Y_TICK_MM * columns_per_mm)))


def offset_drawings(columns_per_mm):
    """POLYDYE_OFFSET_PRINT's two passes (pass_axis y, DIR=1), the X scale then the Y scale, as drawings whose column 0
    is the groove's centre line along Y; `columns_per_mm` is dpi / 25.4. Each starts with a priming block on the plaque
    below the groove's centre."""
    mm = lambda v: int(round(v * columns_per_mm))
    # A nozzle firing one column on its own doesn't eject (the P-box walls, 2026-11-25): the numbers' font pixels are
    # at least 2 columns long, like everything else drawn.
    row = max(2, int(round(OFFSET_TEXT_ROW_MM * columns_per_mm)))
    x = PassDrawing()
    x.prime(mm(-11.))
    t0 = mm(3.)
    for k in range(-OFFSET_X_TICKS, OFFSET_X_TICKS + 1):
        p = OFFSET_REF_POSITION - OFFSET_X_TICK * k
        x.bar(t0, t0 + mm(6. if k == 0 else 5. if k % 4 == 0 else 3.5), MAGENTA, p, p)
        if k % 8 == 0:
            # Above the ticks, past the groove's end, reading along +X like the Y scale's.
            s = "%d" % (k,)
            x.text(s, MAGENTA, mm(OFFSET_SLOT_END[1] + 0.3) + 7 * row - 1, p + text_size(s)[0], row, 2, rotated=True)
    y = PassDrawing()
    y.prime(mm(-14.))
    for k in range(-OFFSET_Y_TICKS, OFFSET_Y_TICKS + 1):
        i = offset_y_tick(columns_per_mm) * k
        w = 2 if k == 0 else 1
        y.bar(i - w, i + w, MAGENTA, 52 if k % 4 else 44, 85)
        if k % 8 == 0:
            s = "%d" % (k,)
            y.text(s, MAGENTA, i + 7 * row // 2, 35, row, 2, rotated=True)
    return x, y


def drawing_extent(drawing):
    """(first column, last column, lowest position, highest position) of a drawing's dots, its priming included."""
    cols = [i for i, c, p in drawing.dots] + list(drawing.raw)
    positions = [p for i, c, p in drawing.dots] + ([PRIME['first'], PRIME['last']] if drawing.raw else [])
    return min(cols), max(cols), min(positions), max(positions)


def offset_ink_extent(dpi, nozzle_dpi):
    """Where POLYDYE_OFFSET_PRINT's ink can land around the groove's centre, (x0, y0, x1, y1) mm, from its drawings at
    `dpi`, with nozzle positions up to 10 % further apart than `nozzle_dpi` says (its photo estimate's spread)."""
    pitch, widest = 25.4 / dpi, 25.4 / nozzle_dpi * 1.1
    xs, ys = [], []
    for d, across in zip(offset_drawings(dpi / 25.4), (0., OFFSET_Y_SCALE_X)):
        c0, c1, p0, p1 = drawing_extent(d)
        xs += [across - (p1 - OFFSET_REF_POSITION) * widest, across + (OFFSET_REF_POSITION - p0) * widest]
        ys += [c0 * pitch, c1 * pitch]
    return min(xs), min(ys), max(xs), max(ys)


def plaque_rects(x, y, slot=OFFSET_SLOT, size=OFFSET_PLAQUE, end=OFFSET_SLOT_END):
    """A plaque layer's rectangles (x0, y0, x1, y1) around a cross-shaped groove centred on x, y, `slot` mm wide,
    extending `size` mm (-X, +X, -Y, +Y). The grooves end `end` mm from the centre (+X, +Y): the plaque beyond holds the
    numbers."""
    h = slot / 2.
    xs = ((x - size[0], x - h), (x + h, x + end[0]))
    ys = ((y - size[2], y - h), (y + h, y + end[1]))
    return ([(xa, ya, xb, yb) for ya, yb in ys for xa, xb in xs] +
            [(x + end[0], y - size[2], x + size[1], y + end[1]), (x - size[0], y + end[1], x + size[1], y + size[3])])


def plaque_gcode(layers, height, width, filament_area, first_width=None):
    """G-code (absolute XYZ, relative E) for a PLA plaque: `layers` lists each layer's rectangles ((x0, y0, x1, y1) in
    G-code coordinates), layers `height` mm. In each layer and rectangle a perimeter, then a zigzag fill, along Y in
    even layers and along X in odd ones, slightly overlapping the perimeter. The first layer is `first_width` wide
    (default `width`) and slower; the part fan comes on from the second layer. Starts with a prime line 3 mm left of
    the plaque at the first layer's height; ends retracted with the fan off."""
    first_width = first_width or width
    out = []
    pos = [None, None]

    def travel(x, y, z):
        out.extend(["G1 E-%.2f F2700" % (PLAQUE_RETRACT,), "G1 Z%.3f F600" % (z + 0.4,),
                    "G1 X%.3f Y%.3f F%d" % (x, y, PLAQUE_TRAVEL_F), "G1 Z%.3f F600" % (z,),
                    "G1 E%.2f F2700" % (PLAQUE_RETRACT,)])
        pos[:] = [x, y]

    def line(x, y, w, h, f):
        d = math.hypot(x - pos[0], y - pos[1])
        out.append("G1 X%.3f Y%.3f E%.5f F%d" % (x, y, d * w * h / filament_area, f))
        pos[:] = [x, y]

    every = [r for rects in layers for r in rects]
    x_lo = min(r[0] for r in every)
    y_lo, y_hi = min(r[1] for r in every), max(r[3] for r in every)
    out.extend(["G1 Z%.3f F600" % (height + 0.4,), "G1 X%.3f Y%.3f F%d" % (x_lo - 3., y_lo, PLAQUE_TRAVEL_F),
                "G1 Z%.3f F600" % (height,), "G1 E3 F300"])
    pos[:] = [x_lo - 3., y_lo]
    line(x_lo - 3., y_hi, 0.6, height, 1200)
    for n, rects in enumerate(layers):
        z = height * (n + 1)
        w = first_width if n == 0 else width
        f_per, f_fill = (1200, 1500) if n == 0 else (2400, 3000)
        if n == 1:
            out.append("M106 S255")
        for x0, y0, x1, y1 in rects:
            i = w / 2.
            travel(x0 + i, y0 + i, z)
            for x, y in ((x1 - i, y0 + i), (x1 - i, y1 - i), (x0 + i, y1 - i), (x0 + i, y0 + i)):
                line(x, y, w, height, f_per)
            i = 1.5 * w - 0.1
            step = 0.95 * w
            if n % 2 == 0:
                if x1 - x0 < 2 * i:
                    continue
                xs = [x0 + i + m * step for m in range(int((x1 - x0 - 2 * i) / step) + 1)]
                travel(xs[0], y0 + i, z)
                for m, x in enumerate(xs):
                    ya, yb = (y0 + i, y1 - i) if m % 2 == 0 else (y1 - i, y0 + i)
                    if m:
                        line(x, ya, w, height, f_fill)
                    line(x, yb, w, height, f_fill)
            else:
                if y1 - y0 < 2 * i:
                    continue
                ys = [y0 + i + m * step for m in range(int((y1 - y0 - 2 * i) / step) + 1)]
                travel(x0 + i, ys[0], z)
                for m, y in enumerate(ys):
                    xa, xb = (x0 + i, x1 - i) if m % 2 == 0 else (x1 - i, x0 + i)
                    if m:
                        line(xa, y, w, height, f_fill)
                    line(xb, y, w, height, f_fill)
    out.extend(["G1 E-2 F2700", "M106 S0"])
    return out


def feather_hash(q, r):
    """A fixed pseudo-random value 0-1 for cell q, r (integers): polydye_post.py's cell_hash(), in its 64-bit
    arithmetic."""
    def i64(v):
        v &= 0xffffffffffffffff
        return v - (1 << 64) if v >> 63 else v
    h = i64((q * 73856093) ^ (r * 19349663))
    h = i64((h ^ (h >> 13)) * 1274126177)
    return ((h ^ (h >> 16)) & 0xffff) / 65536.


def test_page_drawings(dpi, nozzle_dpi, registration=(0, 0, 0, 0)):
    """POLYDYE_TEST_PAGE's passes in printing order, as (drawing, direction, x): directions alternating, x the pass's X
    (mm; nozzle position SKEW_PIVOT's, the one POLYDYE_PASS names) from the first pass's. Every drawing's column 0 is
    the page's front edge (the lowest Y)."""
    pitch, nozzle = 25.4 / dpi, 25.4 / nozzle_dpi
    lo, hi = TEST_POSITIONS
    width = hi - lo + 1
    # Whole column pairs throughout, so the cap keeps both columns of a pair alike.
    square = 2 * max(1, int(round(width * nozzle / pitch / 2.)))
    gap = 2 * max(1, int(round(TEST_GAP_MM / pitch / 2.)))
    rows = (0, (TEST_STAIR - 1) * TEST_STEP + TEST_DASH + gap)  # the nozzle checks; the one-ink squares behind them
    rows += (rows[1] + square + gap,)  # the two-ink squares
    length = rows[2] + square
    across = (registration[1], 0, registration[3])
    out = []
    for k, c in enumerate((CYAN, MAGENTA, YELLOW)):
        d = PassDrawing()
        for q in range(JOB_NOZZLES[0], JOB_NOZZLES[1] + 1):
            # Nozzle q fires its dash: drawn where the colour registration takes it to that nozzle.
            i = (q - JOB_NOZZLES[0]) % TEST_STAIR * TEST_STEP
            d.bar(i, i + TEST_DASH - 1, c, q - across[c], q - across[c])
        d.bar(rows[1], rows[1] + square - 1, c, lo, hi)
        out.append((d, k * TEST_COLUMN_MM))
    for k, inks in enumerate(((MAGENTA, YELLOW), (CYAN, YELLOW), (CYAN, MAGENTA))):  # red, green, blue
        d = PassDrawing()
        for c in inks:
            d.bar(rows[2], rows[2] + square - 1, c, lo, hi)
        out.append((d, k * TEST_COLUMN_MM))
    # Two sweeps of blue the page's length, the second `step` positions towards +X, TEST_GAP_MM right of the yellow
    # nozzle check. In the TEST_FEATHER positions they share, each cell (4 columns: two pairs; one position) goes to
    # the second where its feather_hash() is under a ramp rising across the band, else to the first (polydye_post.py's
    # feathered joins, here with the page's columns and grid positions g counted towards +X).
    step = width - TEST_FEATHER
    x = 2 * TEST_COLUMN_MM + (hi - JOB_NOZZLES[0]) * nozzle + TEST_GAP_MM
    for j in (0, 1):
        d = PassDrawing()
        for p in range(lo, hi + 1):
            g = j * step + hi - p
            band = g - step
            for i in range(0, length, 4):
                if 0 <= band < TEST_FEATHER and (feather_hash(i // 4, g) < (band + .5) / TEST_FEATHER) != (j == 1):
                    continue
                for c in (CYAN, MAGENTA):
                    d.bar(i, min(i + 3, length - 1), c, p, p)
        out.append((d, x + j * step * nozzle))
    return [(d, 1 if n % 2 == 0 else -1, x) for n, (d, x) in enumerate(out)]


def cfg_number(v):
    """A value as polydye.cfg gets it: at most 4 decimals, no trailing zeros (like the page's)."""
    s = ("%.4f" % (v,)).rstrip('0').rstrip('.')
    return '0' if s in ('-0', '') else s


def cfg_section(lines):
    """(first, end) line numbers of the [polydye] section's settings in polydye.cfg's `lines`, or None."""
    start = next((n for n, l in enumerate(lines) if re.match(r'^\s*\[polydye\]\s*$', l)), None)
    if start is None:
        return None
    return start + 1, next((n for n in range(start + 1, len(lines)) if re.match(r'^\s*\[', lines[n])), len(lines))


def cfg_key_re(key):
    """A setting's line, active or commented out: (indent, comment mark, value, trailing comment)."""
    return re.compile(r'^(\s*)(#\s*)?%s\s*[:=]\s*([^#]*?)\s*(#.*)?$' % (re.escape(key),))


def cfg_value(text, key):
    """The value of an active setting in polydye.cfg's [polydye] section, or None."""
    lines = text.split('\n')
    section = cfg_section(lines)
    for n in range(*(section or (0, 0))):
        m = cfg_key_re(key).match(lines[n])
        if m and not m.group(2):
            return m.group(3)
    return None


def cfg_with(text, values):
    """polydye.cfg `text` with these [polydye] settings (key -> string), like the page's Settings: an active setting is
    replaced, else a commented-out example reused, else the setting added at the top of [polydye]; further active
    copies are commented out (Klipper refuses an option set twice). Raises ValueError without a [polydye] section."""
    lines = text.split('\n')
    section = cfg_section(lines)
    if section is None:
        raise ValueError("polydye.cfg has no [polydye] section")
    start, end = section
    added = start  # new settings go at the top of the section, in order
    for key, value in values.items():
        rx = cfg_key_re(key)
        hits = [(n, rx.match(lines[n])) for n in range(start, end)]
        hits = [(n, m) for n, m in hits if m]
        active = [(n, m) for n, m in hits if not m.group(2)]
        target = (active or hits or [None])[0]
        if target is None:
            lines.insert(added, "%s: %s" % (key, value))
            added += 1
            end += 1
            continue
        n, m = target
        lines[n] = "%s%s: %s%s" % (m.group(1), key, value, "  " + m.group(4) if m.group(4) else "")
        for n, m in active[1:]:
            lines[n] = "%s#%s: %s%s" % (m.group(1), key, m.group(3), "  " + m.group(4) if m.group(4) else "")
    return '\n'.join(lines)


# ---- Ink jobs -------------------------------------------------------------------------------------------------------
# A colour print sliced in OrcaSlicer with scripts/polydye_post.py as its post-processing script carries its ink passes
# in the G-code, after the print (format in polydye_post.py): POLYDYE_JOB at the start loads the job's index from the
# print's own file and streams the passes to the board in order, in a thread, ahead of the print (at most
# JOB_AHEAD_BYTES / JOB_AHEAD_PASSES on the board unprinted), each stored to be deleted once printed
# (PASSSTORE_FLAG_ONCE); after each layer POLYDYE_JOB_PASS N=n prints pass n with POLYDYE_PASS. The passes are drawn
# without colour registration: it's applied here, from polydye.cfg, so a recalibration needs no re-slice.
JOB_VERSION = 2
JOB_VERSIONS = (1, 2)  # 2: POLYDYE_INK_MOTION in its ink blocks
JOB_BEGIN, JOB_END = b"; POLYDYE_JOB_BEGIN", b"; POLYDYE_JOB_END"
JOB_NOZZLES = (STOCK_RANDOM['first'], STOCK_RANDOM['last'])  # the positions the stock firmware fires, 14-85
JOB_AHEAD_BYTES, JOB_AHEAD_PASSES = 3 * 1024 * 1024, 256
JOB_UPLOAD_RATE = CAL_UPLOAD_RATE
JOB_RETRIES = 3  # tries for each pass's upload (a PUT just replaces the pass)
JOB_WAIT_S = 60.  # POLYDYE_JOB_PASS gives up after this long without any upload progress
JOB_WAIT_EVERY = 4  # job passes between waits for the moves (the board queues at most 8)
# The run-up the reach check allows for: Orca lowers the acceleration to as little as 500 mm/s^2 (first layer).
JOB_REACH_ACCEL = 500.
DATA_DELETE, DATA_CLEAR = 0x11, 0x12
PASS_FLAG_ONCE = 1  # a stored pass the board deletes once printed (PASSSTORE_FLAG_ONCE)
_BYTE_SHIFTS = {}
_MASK_TABLES = {}


class JobError(Exception):
    pass


class JobStopped(Exception):
    """A stopped job's upload thread tried the data port (a new job may have cleared the store since)."""


def job_find(f):
    """(start, end) byte offsets of the job in the open binary G-code file `f`: its BEGIN line's start and its END
    line's start, searched for from the file's end (the job is near it)."""
    f.seek(0, 2)
    pos, tail, end = f.tell(), b"", None
    while pos > 0:
        n = min(1 << 20, pos)
        pos -= n
        f.seek(pos)
        buf = f.read(n) + tail
        if end is None:
            k = buf.rfind(b"\n" + JOB_END)
            if k >= 0:
                end = pos + k + 1
        if end is not None:
            k = buf.rfind(b"\n" + JOB_BEGIN, 0, end - pos)
            if k >= 0:
                return pos + k + 1, end
        tail = buf[:len(JOB_BEGIN) + 1]
    raise JobError("no PolyDye ink job in the G-code (slice it with polydye_post.py as Orca's post-processing script)")


def _job_fields(line):
    # UTF-8, as polydye_post.py writes it: object names must match EXCLUDE_OBJECT's.
    return dict(kv.split('=', 1) for kv in line.decode('utf-8', 'replace').split() if '=' in kv)


def job_load(path):
    """Reads a job's index: (header, records). header has version, dpi, nozzle_dpi, pass_axis, positions (lo, hi),
    passes, layers, how many POLYDYE_PREWARM and POLYDYE_PRIME its G-code has (prewarms, primes), the lowest
    acceleration its G-code sets for an ink block (ink_accel, 0: none), the highest Z an ink block goes to (ink_z) and
    its bursts' SECONDS (prewarm_s, 0: none given);
    each record n, layer, pass, coat, object, dir, x, y, columns, colours, size (nozzle data bytes, at most) and
    where its data lines are (start, end)."""
    with open(path, 'rb') as f:
        start, end = job_find(f)
        f.seek(start)
        header, records, rec, pos = None, [], None, start
        while pos < end:
            line = f.readline()
            if not line:
                break
            if line.startswith(b";PDD "):
                if rec is None:
                    raise JobError("pass data before any pass")
                rec['end'] = pos + len(line)
            elif line.startswith(b";PDP "):
                rec = _job_fields(line[5:])
                rec['start'] = rec['end'] = pos + len(line)
                records.append(rec)
            elif line.startswith(b";PDJ "):
                header = _job_fields(line[5:])
            pos += len(line)
    if header is None:
        raise JobError("the job has no ;PDJ line")
    try:
        if int(header.get('version', 0)) not in JOB_VERSIONS:
            raise JobError("the job is version %s, this add-on reads version %d or older: update the add-on, or slice "
                           "with the polydye_post.py that comes with it" % (header.get('version'), JOB_VERSION))
        lo, hi = (int(v) for v in header['positions'].split('-'))
        header = {'version': JOB_VERSION, 'dpi': float(header['dpi']), 'nozzle_dpi': float(header['nozzle_dpi']),
                  'pass_axis': header.get('pass_axis', 'y'), 'positions': (lo, hi), 'passes': int(header['passes']),
                  'layers': int(header.get('layers', 0)), 'prewarms': int(header.get('prewarms', 0)),
                  'primes': int(header.get('primes', 0)), 'ink_accel': float(header.get('ink_accel', 0)),
                  'ink_z': float(header.get('ink_z', 0)), 'prewarm_s': float(header.get('prewarm_s', 0))}
        for n, rec in enumerate(records):
            for key in ('n', 'layer', 'pass', 'coat', 'dir', 'columns'):
                rec[key] = int(rec[key])
            rec['x'], rec['y'] = float(rec['x']), float(rec['y'])
            if rec['n'] != n or rec['dir'] not in (1, -1) or not 0 < rec['columns'] <= 65535 or \
                    not 0 <= rec['pass'] < SPLIT_PASS or \
                    not rec['layer'] < CAL_LAYER or not rec['colours'] or set(rec['colours']) - set('cmy') or \
                    rec['end'] == rec['start']:
                raise JobError("pass %d is malformed" % (n,))
            rec['object'] = rec.get('object', '-')
    except (KeyError, ValueError) as e:
        raise JobError("the job's index is malformed (%s)" % (e,))
    if header['passes'] != len(records) or not records:
        raise JobError("the job says %d passes but has %d (cut short?)" % (header['passes'], len(records)))
    if not JOB_NOZZLES[0] <= lo <= hi <= JOB_NOZZLES[1]:
        raise JobError("the job draws on nozzle positions %d-%d, beyond the %d-%d the cartridge fires"
                       % (lo, hi, JOB_NOZZLES[0], JOB_NOZZLES[1]))
    return header, records


def job_record_data(f, rec):
    """A job pass's colour planes (polydye_post.py's format), read from the open binary G-code file `f`."""
    f.seek(rec['start'])
    raw = f.read(rec['end'] - rec['start'])
    text = b"".join(line[5:].strip() for line in raw.split(b"\n") if line.startswith(b";PDD "))
    try:
        data = zlib.decompress(base64.b64decode(text))
    except (ValueError, zlib.error) as e:
        raise JobError("pass %d's data is corrupt (%s)" % (rec['n'], e))
    if len(data) != len(rec['colours']) * rec['columns'] * 14:
        raise JobError("pass %d's data is %d bytes, not %d" % (rec['n'], len(data), len(rec['colours']) *
                                                                rec['columns'] * 14))
    return data


def _byte_shift(s):
    if s not in _BYTE_SHIFTS:
        _BYTE_SHIFTS[s] = bytes(((v << s) & 0xff) if s >= 0 else (v >> -s) for v in range(256))
    return _BYTE_SHIFTS[s]


def plane_shift(plane, k):
    """A colour plane (columns of 14 bytes, byte j holding nozzle positions p with p % 14 == j as bit p // 14) with its
    dots moved k positions; dots moved off positions 0-111 are dropped."""
    if not k:
        return plane
    out = bytearray(len(plane))
    for j in range(14):
        s = (j + k) // 14
        if -8 < s < 8:
            out[(j + k) % 14::14] = plane[j::14].translate(_byte_shift(s))
    return bytes(out)


def _mask_table(mask):
    if mask not in _MASK_TABLES:
        _MASK_TABLES[mask] = bytes(v & mask for v in range(256))
    return _MASK_TABLES[mask]


def job_pass_data(data, colours, columns, direction, registration=(0, 0, 0, 0), skew=None):
    """Nozzle data to print a job pass (its colour planes `data`, `columns` long, for `colours`) in `direction` with
    colour registration and skew, like pass_data() for a drawing of the same dots, then the heater cap (cap_heaters()):
    (data, columns, first), first the drawing column of the pass's first column (its X, Y name column 0). None if no
    dot is left. As one board pass: job_pass_parts() splits it by fire line where it needs to."""
    raw = _job_columns(data, colours, columns, registration, skew)
    built = None if raw is None else _job_part(raw[0], raw[1], direction)
    return built if built and built[1] else None


def job_pass_parts(data, colours, columns, direction, registration=(0, 0, 0, 0), skew=None):
    """A job pass as the board passes that print it: [(pass number offset, data, columns, first)], like
    job_pass_data(). One, unless the pass has two or more inks and a packet would want more than HEATERS_PER_SLOT
    heaters on its two fire lines together (each line counted up to HEATERS_PER_PULSE): then two, the dots on fire
    line F3 (data positions 0-55) at offset 0 and those on F5 (56-111) at SPLIT_PASS, each trimmed to its own columns
    and capped. A single ink stays one pass, capped (3 + 3 at positions 14-84 becomes 3 + 2): splitting it would double
    its time for a dot or two. Empty if no dot is left."""
    raw = _job_columns(data, colours, columns, registration, skew)
    return [] if raw is None else _pass_parts(raw[0], raw[1], len(colours), direction)


def _pass_parts(out, lo, inks, direction):
    """job_pass_parts() for nozzle data `out` (bytearray, columns from drawing column `lo` on in DIR=1's order, with
    the registration and skew, uncapped) of `inks` inks."""
    if inks < 2 or not needs_split(out):
        built = _job_part(out, lo, direction)
        return [(0,) + built] if built[1] else []  # the cap can drop every fire: a lone dot, say
    parts = []
    for offset, mask in ((0, 0x0f), (SPLIT_PASS, 0xf0)):
        half = out.translate(_mask_table(mask))
        stripped = half.strip(b"\0")
        if not stripped:
            continue
        a = (len(half) - len(half.lstrip(b"\0"))) // NOZDATA_SIZE
        b = (len(half.rstrip(b"\0")) + NOZDATA_SIZE - 1) // NOZDATA_SIZE
        built = _job_part(bytearray(half[a * NOZDATA_SIZE:b * NOZDATA_SIZE]), lo + a, direction)
        if built[1]:
            parts.append((offset,) + built)
    return parts


def needs_split(out):
    """Whether some packet of the nozzle data `out` wants more than HEATERS_PER_SLOT heaters on its two fire lines,
    each counted up to HEATERS_PER_PULSE."""
    for base in range(0, len(out), NOZDATA_SIZE):
        for k in range(base, base + 14):
            a, b, c = out[k], out[k + 14], out[k + 28]
            if a | b | c and (min(_NIBBLES[a] + _NIBBLES[b] + _NIBBLES[c], HEATERS_PER_PULSE) +
                              min(_HIGH_NIBBLES[a] + _HIGH_NIBBLES[b] + _HIGH_NIBBLES[c], HEATERS_PER_PULSE)
                              > HEATERS_PER_SLOT):
                return True
    return False


def _job_part(out, lo, direction):
    """`out` (columns from drawing column `lo` on, DIR=1's order) capped (cap_heaters()), trimmed of columns the cap
    emptied at its ends, and in `direction`'s order: (data, columns, first)."""
    cap_heaters(out, lo)
    a = (len(out) - len(out.lstrip(b"\0"))) // NOZDATA_SIZE
    b = (len(out.rstrip(b"\0")) + NOZDATA_SIZE - 1) // NOZDATA_SIZE
    if b <= a:
        return b"", 0, lo
    out, lo = out[a * NOZDATA_SIZE:b * NOZDATA_SIZE], lo + a
    n = len(out) // NOZDATA_SIZE
    if direction < 0:
        out = b"".join(out[i * NOZDATA_SIZE:(i + 1) * NOZDATA_SIZE] for i in range(n - 1, -1, -1))
    return bytes(out), n, lo if direction > 0 else lo + n - 1


def _job_columns(data, colours, columns, registration, skew):
    """A job pass's dots as nozzle data with the registration and skew, uncapped: (bytearray of columns in DIR=1's
    order, the drawing column of the first), or None if no dot is left."""
    along = (registration[0], 0, registration[2])
    across = (registration[1], 0, registration[3])
    size = columns * 14
    # Byte j of a plane holds positions j + 14 bit: the bits of each byte grouped by how far the skew moves them.
    groups = []
    for j in range(14):
        moves = {}
        for bit in range(8):
            shift = skew[j + 14 * bit] if skew else 0
            moves[shift] = moves.get(shift, 0) | 1 << bit
        groups.append(sorted(moves.items()))
    lanes = []  # (byte in an output column, the bed column of the lane's first byte, the lane: a byte per column)
    for k, name in enumerate(colours):
        c = 'cmy'.index(name)
        plane = plane_shift(data[k * size:(k + 1) * size], across[c])
        for j in range(14):
            lane = plane[j::14]
            if not lane.strip(b"\0"):
                continue
            for shift, mask in groups[j]:
                part = lane if mask == 0xff else lane.translate(_mask_table(mask))
                if part.strip(b"\0"):
                    lanes.append((14 * c + COLOR_BYTE_ORDER[c][j], along[c] + shift, part))
    if not lanes:
        return None
    lo = min(off + len(part) - len(part.lstrip(b"\0")) for _, off, part in lanes)
    hi = max(off + len(part.rstrip(b"\0")) - 1 for _, off, part in lanes)
    n = hi - lo + 1
    # Each lane as one big number, a byte per column, moved to its place among the output's n columns.
    merged = {}
    for dest, off, part in lanes:
        shift = 8 * (n - columns - (off - lo))
        v = int.from_bytes(part, 'big')
        merged[dest] = merged.get(dest, 0) | (v << shift if shift >= 0 else v >> -shift)
    out = bytearray(n * NOZDATA_SIZE)
    for dest, v in merged.items():
        out[dest::NOZDATA_SIZE] = v.to_bytes(n, 'big')
    return out, lo


def cap_heaters(out, first, cap=HEATERS_PER_PULSE, slot=HEATERS_PER_SLOT):
    """Keeps at most `cap` heaters firing on each fire pulse of the nozzle data `out` (a bytearray of columns,
    changed in place; `first` the drawing column of its first), and at most `slot` on a packet's two pulses together: a
    fire pulse is one packet (board byte % 14) on one fire line (F3: bits 0-3, F5: bits 4-7), across the colours.
    Where both lines are over `slot` together, F5 gives way first (3 + 2 printed solid). Where more want to fire than
    a line keeps (m), they share by an even rotation, the same for both columns of a pair (drawing columns 2k, 2k + 1):
    the wanted heaters in (bit, colour) order, keeping want[(s + t) % n] for t < m with s = (pair * m) % n. A fire
    left on its own in a column (none in the columns either side), which doesn't eject, is dropped. Returns the fires
    dropped."""
    n = len(out) // NOZDATA_SIZE
    dropped = 0
    for i in range(n):
        base = i * NOZDATA_SIZE
        pair = (first + i) // 2
        for k in range(14):
            v = (out[base + k], out[base + 14 + k], out[base + 28 + k])
            n3 = _NIBBLES[v[0]] + _NIBBLES[v[1]] + _NIBBLES[v[2]]
            n5 = _HIGH_NIBBLES[v[0]] + _HIGH_NIBBLES[v[1]] + _HIGH_NIBBLES[v[2]]
            if n3 <= cap and n5 <= cap and n3 + n5 <= slot:
                continue
            m3, m5 = min(n3, cap), min(n5, cap)
            while m3 + m5 > slot:
                if m5 >= m3:
                    m5 -= 1
                else:
                    m3 -= 1
            for line, m in ((0, m3), (4, m5)):
                want = [(bit, c) for bit in range(line, line + 4) for c in range(3) if v[c] >> bit & 1]
                if len(want) <= m:
                    continue
                s0 = (pair * m) % len(want)
                keep = set(want[(s0 + t) % len(want)] for t in range(m))
                for bit, c in want:
                    if (bit, c) not in keep:
                        out[base + 14 * c + k] &= ~(1 << bit) & 0xff
                        dropped += 1
    if n:
        # Per byte lane, as one big number with a column per byte: a fire with neither neighbour column firing.
        mask = (1 << (8 * n)) - 1
        for b in range(NOZDATA_SIZE):
            lane = int.from_bytes(out[b::NOZDATA_SIZE], 'big')
            single = lane & ~(lane >> 8) & ~((lane << 8) & mask)
            if single:
                dropped += bin(single).count('1')
                out[b::NOZDATA_SIZE] = (lane & ~single).to_bytes(n, 'big')
    return dropped


class JobStreamer:
    """Uploads a job's passes to the board in order, in a thread, ahead of printing: at most `ahead_bytes` and
    `ahead_passes` of them stored and not yet printed (consume()), but always the next one. `put(payload)` stores one
    (a data port PUT), `delete(layer, pass)` drops one never printed (skipped)."""

    def __init__(self, path, records, registration, put, delete, ahead_bytes, ahead_passes, skew=None,
                 stopping=None):
        self.path, self.records, self.registration, self.skew = path, records, registration, skew
        self.put, self.delete = put, delete
        self.ahead_bytes, self.ahead_passes = ahead_bytes, ahead_passes
        self.lock = threading.Lock()
        # Set by stop(); `put` and `delete` should refuse once it's set (JobStopped), checked where the data port is
        # used one request at a time, so nothing of this job reaches the board after a new job clears it.
        self.stopping = stopping or threading.Event()
        self.ready = {}  # n -> [(board pass number, columns, first)]: the board passes that print it (job_pass_parts())
        self.sizes = {}  # n -> bytes stored, until consumed
        self.consumed, self.skipped, self.deletes = set(), set(), []
        self.holding = {}  # n -> print time its pass ends: printed, but still on the board until then
        self.next = 0  # the next pass to upload
        self.printed = 0
        self.error = None
        self.thread = threading.Thread(target=self._run, name="polydye-job")
        self.thread.daemon = True

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()

    def consume(self, n, skipped=False, until=None):
        """Pass n is printed, or skipped (a skipped one stored is deleted). A printed one scheduled to end at print time
        `until` still counts as ahead (its data is on the board, which drops it once printed) until release() passes
        that time; with `until` None, at once."""
        with self.lock:
            if n in self.consumed:
                return
            self.consumed.add(n)
            if skipped:
                self.skipped.add(n)
            else:
                self.printed += 1
            if n in self.sizes:
                if skipped:
                    del self.sizes[n]
                    self.deletes.append(n)
                elif until is None:
                    del self.sizes[n]
                else:
                    self.holding[n] = until

    def release(self, print_time):
        """The passes scheduled to end by `print_time` are printed, so off the board: no longer ahead."""
        with self.lock:
            for n in [n for n, until in self.holding.items() if until <= print_time]:
                del self.holding[n]
                self.sizes.pop(n, None)

    def state(self, n):
        """(error, pass n's board passes [(number, columns, first)] once on the board or None, passes uploaded so
        far)."""
        with self.lock:
            return self.error, self.ready.get(n), self.next

    def status(self):
        with self.lock:
            return {'passes': len(self.records), 'uploaded': self.next, 'printed': self.printed,
                    'skipped': len(self.skipped), 'error': self.error}

    def _run(self):
        try:
            with open(self.path, 'rb') as f:
                while not self.stopping.is_set():
                    with self.lock:
                        deletes, self.deletes = self.deletes, []
                        n = self.next
                        size = self.records[n].get('size', 0) if n < len(self.records) else 0
                        room = not self.sizes or (len(self.sizes) < self.ahead_passes and
                                                  sum(self.sizes.values()) + size <= self.ahead_bytes)
                        skip = n in self.skipped
                    for d in deletes:
                        for number, _, _ in self.ready.get(d, ()):
                            self._retry(lambda: self.delete(self.records[d]['layer'], number))
                    if n >= len(self.records) or skip:
                        if skip:
                            with self.lock:
                                self.next += 1
                        else:
                            self.stopping.wait(0.1)  # all up: still deletes passes skipped later
                        continue
                    if not room:
                        self.stopping.wait(0.05)
                        continue
                    self._upload(f, n)
        except JobStopped:
            logging.info("polydye: ink job uploads stopped")
        except Exception as e:
            logging.exception("polydye: ink job upload failed")
            with self.lock:
                self.error = str(e) or e.__class__.__name__

    def _upload(self, f, n):
        rec = self.records[n]
        parts = job_pass_parts(job_record_data(f, rec), rec['colours'], rec['columns'], rec['dir'],
                               self.registration, self.skew)
        if not parts:
            raise JobError("pass %d has no dots left after the colour registration and the heater cap" % (n,))
        ready = []
        for offset, data, columns, first in parts:
            number = rec['pass'] | offset
            self._retry(lambda: self.put(struct.pack("<HHBBH", rec['layer'], number, 0, PASS_FLAG_ONCE, columns) +
                                         data))
            ready.append((number, columns, first))
        with self.lock:
            self.ready[n] = ready
            self.next = n + 1
            if n in self.consumed:  # skipped while it went up
                self.deletes.append(n)
            else:
                self.sizes[n] = sum(len(data) for _, data, _, _ in parts)

    def _retry(self, request):
        for attempt in range(JOB_RETRIES):
            try:
                return request()
            except (DataPortError, OSError) as e:
                if attempt == JOB_RETRIES - 1 or self.stopping.is_set():
                    raise
                logging.info("polydye: ink job: %s; trying again", e)
                self.stopping.wait(1.)


class PolyDye:
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        mcu_name = config.get('mcu', 'polydye')
        # Everything pending goes to the SD card before Klipper connects to its MCUs, which can restart the PolyDye
        # board: a Raspberry Pi 3 has frozen when the board left USB and came back, and files written just before (an
        # update of this add-on) were lost. The Klipper service's start hook (polydye-toggle.sh) syncs too.
        os.sync()
        self.mcu = mcu_mod.get_printer_mcu(printer, mcu_name)
        self.oid = self.mcu.create_oid()
        self.mcu.register_config_callback(self._build_config)
        # The data port is the board's second USB serial port: same by-id name, interface 02 instead of 00.
        mcu_serial = config.getsection('mcu' if mcu_name == 'mcu' else 'mcu ' + mcu_name).get('serial', '')
        default_data_port = mcu_serial[:-5] + '-if02' if mcu_serial.endswith('-if00') else None
        self.data_port = config.get('data_port', default_data_port)
        self.data_seq = 0
        # Data port requests one at a time: the ink job's uploads run in their own thread (JobStreamer).
        self.data_lock = threading.Lock()
        self.job = None  # the ink job loaded (POLYDYE_JOB): {'streamer', 'header', 'records', ...}
        # (max_accel, square_corner_velocity, lowered in a print) POLYDYE_INK_MOTION puts back afterwards
        self.ink_motion = None
        # Pass direction and the cartridge nozzle-row centre relative to the hotend nozzle, in bed coordinates.
        # Required: they depend on the mount, and a wrong guess would put ink in the wrong place.
        self.pass_axis = config.getchoice('pass_axis', {'x': 'x', 'y': 'y'})
        self.offset_x = config.getfloat('offset_x')
        self.offset_y = config.getfloat('offset_y')
        # Moves the ink of reverse (DIR=-1) passes along the pass axis, to line them up with forward passes.
        self.direction_trim = config.getfloat('direction_trim', 0.)
        # Colour registration (REG_KEYS), from the alignment page (POLYDYE_ALIGN_PRINT, POLYDYE_ALIGN): applied to the
        # passes the add-on draws.
        self.registration = tuple(
            config.getint(key, 0, minval=-limit, maxval=limit) for key, limit in
            zip(REG_KEYS, (REG_MAX_COLUMNS, REG_MAX_POSITIONS, REG_MAX_COLUMNS, REG_MAX_POSITIONS)))
        # Nozzles per inch across the pass: sizes POLYDYE_OFFSET_PRINT's X scale. Photos of the alignment page put the
        # colour cartridge's at 300-330.
        self.nozzle_dpi = config.getfloat('nozzle_dpi', 300., above=0.)
        # The nozzle row's yaw (SKEW_PIVOT): applied to every pass the add-on draws.
        self.nozzle_skew = config.getfloat('nozzle_skew', 0., minval=-SKEW_MAX, maxval=SKEW_MAX)
        self.cfg_saves = 0  # times polydye.cfg was written (POLYDYE_SAVE, the reading commands), so the page re-reads it
        # Waveform template for the board to use; unset leaves the board's own (color_b_late, the stock firmware's).
        # Needs board firmware with polydye_set_template, and for color_b_late with polydye_set_clock.
        self.cartridge_template = config.get('cartridge_template', None)
        if self.cartridge_template is not None:
            self.cartridge_template = self.cartridge_template.strip().lower()
            if template_error(self.cartridge_template):
                raise config.error("[polydye] cartridge_template: %s" % (template_error(self.cartridge_template),))
        # F3/F5 fire pulse in bus words (0.35 us each), PULSE_MIN-PULSE_MAX; unset leaves the board's own (6 = 2.10 us,
        # the stock firmware's). Longer pulses put more energy into each drop, e.g. when the boost rails are below the
        # cartridge's nominal voltage.
        self.pulse_words = config.getint('pulse_words', None)
        if self.pulse_words is not None and pulse_range_error(self.pulse_words):
            raise config.error("[polydye] pulse_words: %s" % (pulse_range_error(self.pulse_words),))
        # The bus clock choice was removed in the stock-only cleanup: stock is still taken, as a no-op.
        bus_clock = config.get('bus_clock', None)
        if bus_clock is not None:
            bus_clock = bus_clock.strip().lower()
            if bus_clock != 'stock':
                raise config.error("[polydye] bus_clock: %s %s: the board runs only the stock clock (2.857 MHz); "
                                   "remove bus_clock from polydye.cfg"
                                   % (bus_clock, "was removed" if bus_clock in CLOCK_NAMES else "is not a bus clock"))
            logging.info("polydye: bus_clock: stock is the only bus clock now; it can go from polydye.cfg")
        # Bus columns per inch along the pass and the pass speed: by default the stock firmware's cadence, 3,333 bus
        # columns per 100 mm (each 423 dpi image column twice) at 59.5 mm/s, a column every 504 us (stock: ~508 us).
        # Automatic head warm-up before a live pass when the head is cold (head_cold()); warm_seconds: 0 turns it off.
        # warm_position: where a pass's warm-up (and pre-spit) runs, never over the print (a hot head puts ink down
        # during a 0.70 us warm-up: a 4 mm C/M/Y blob on the PolyDye text print, 48483ab) nor the glass bed. `sponge`
        # (the default): the wipe sponge at POLYDYE_WIPE's X; X, Y: a bed spot for the cartridge (like POLYDYE_PASS's X
        # and Y) over printed plastic, e.g. a prime pad; or `pass`: warm_clearance mm behind the pass's run-up start
        # along pass_axis, so the pass follows after a short travel (140 mm of travel from a park spot lost the first
        # mm of the pass), only where printed plastic is under it.
        self.warm_seconds = config.getfloat('warm_seconds', AUTO_WARM_S, minval=0., maxval=WARM_MAX_S)
        self.warm_pulse = config.getint('warm_pulse', AUTO_WARM_WORDS, minval=1, maxval=PULSE_MAX)
        self.warm_period_us = config.getint('warm_period_us', AUTO_WARM_PERIOD_US, minval=1, maxval=65535)
        self.warm_idle_time = config.getfloat('warm_idle_time', AUTO_WARM_IDLE_S, minval=0.)
        error = auto_warm_error(self.warm_seconds, self.warm_pulse, self.warm_period_us)
        if error:
            raise config.error("[polydye] %s" % (error,))
        self.warm_clearance = config.getfloat('warm_clearance', AUTO_WARM_CLEARANCE, minval=0.)
        # POLYDYE_PREWARM's burst without SECONDS: a burst is at most WARM_MAX_COLUMNS columns of warm_period_us.
        self.prewarm_seconds = config.getfloat('prewarm_seconds', AUTO_PREWARM_S, above=0., maxval=WARM_MAX_S)
        if self.prewarm_seconds * 1e6 / self.warm_period_us > WARM_MAX_COLUMNS + 0.5:
            raise config.error("[polydye] prewarm_seconds %g is too long: a burst is at most %d columns, %.1f s at "
                               "warm_period_us %d" % (self.prewarm_seconds, WARM_MAX_COLUMNS,
                                                      WARM_MAX_COLUMNS * self.warm_period_us / 1e6,
                                                      self.warm_period_us))
        self.warm_prespit = config.getfloat('warm_prespit', 0., minval=0., maxval=PRESPIT_MAX_S)
        # POLYDYE_PRIME's spit at the sponge without SECONDS: 0.2 s left a full-width cyan bar's start ragged, 0.3 s
        # didn't (2026-11-25).
        self.prime_seconds = config.getfloat('prime_seconds', PRIME_S, above=0., maxval=PRESPIT_MAX_S)
        position = config.get('warm_position', 'sponge').strip().lower()
        if position in ('pass', 'sponge'):
            self.warm_position = position
        else:
            try:
                self.warm_position = tuple(float(v) for v in position.split(','))
            except ValueError:
                self.warm_position = ()
            if len(self.warm_position) != 2:
                raise config.error("[polydye] warm_position must be sponge (the default), X, Y (bed coordinates "
                                   "of the cartridge over printed plastic, off the print) or pass, not %r"
                                   % (position,))
        self.last_fire = None  # print time the head last fired (a live pass, spit test or warm-up); None: not yet
        self.prewarm_cmd = self.query_prewarm_cmd = None  # board firmware with prewarm bursts (POLYDYE_PREWARM)
        self.prewarm_sent = None  # the last burst sent: {'start', 'end'} print times, 'seconds'; None: none yet
        self.prewarm_state = None  # the board's account of it, as last asked (_query_prewarm())
        self.boost_ready = None  # print time the boost has settled after the add-on last turned it on
        # Bumped by everything that drops the board's bursts (a cancel, live ink off, a shutdown): a POLYDYE_PREWARM
        # still waiting for its moves to be planned then sends nothing.
        self.prewarm_gen = 0
        self.print_live = False  # a print turned live ink on and hasn't been seen to end yet (_check_print_end())
        self.live_off_pending = False  # a test's cleanup failed: live ink may be on (_check_print_end() retries)
        self.dpi = config.getfloat('dpi', 847., above=0.)
        self.pass_speed = config.getfloat('pass_speed', 59.5, above=0.)
        self.travel_speed = config.getfloat('travel_speed', 150., above=0.)
        self.extra_run_up = config.getfloat('extra_run_up', 1., minval=0.)
        self.settle_time = config.getfloat('settle_time', None, minval=0.)
        self.min_lead_time = config.getfloat('min_lead_time', 0.025, minval=0.005)
        self.query_pass_cmd = self.query_cmd = None
        self.schedule_cmd = self.mode_cmd = self.cancel_cmd = None
        self.set_template_cmd = self.set_pulse_cmd = self.query_cartridge_cmd = None
        self.set_level_cmd = self.query_level_cmd = self.spit_cmd = None
        self.spit_patterns = ()  # spit test patterns the board offers (SPIT_PATTERNS)
        self.spit_run_cmd = None  # a spit test with its own column period and a warm-up, on board firmware with it
        self.spit_random_cmd = None  # the random pattern's settings, on board firmware with it
        # The board's warm-up limits (board firmware with POLYDYE_WARM_MAX_DUTY_PPM; older: WARM_OLD_*, no duty limit).
        self.warm_max_words, self.warm_max_us, self.warm_max_s = WARM_OLD_WORDS, WARM_OLD_US, WARM_OLD_S
        self.warm_duty_ppm = None
        self.set_clock_cmd = self.query_clock_cmd = None
        self.bus_hz_check = None  # (% fast, % slow) the board refuses to fire at; None: it doesn't check the clock
        # Seconds the board keeps an unused boost on (POLYDYE_BOOST_IDLE_S): board firmware with it takes template and
        # pulse changes with the boost on, so tests leave the boost on (live ink off) afterwards, as the stock firmware
        # keeps it on from boot. None: older firmware, so tests turn PolyDye off afterwards.
        self.boost_idle_s = None
        self.query_run_cmd = None  # the board's last run, on board firmware with polydye_query_run
        self.testing = False  # a spit test or warm-up is running: it reports a shutdown itself
        self.templates = ()  # templates the board offers (of TEMPLATES); none: no template choice
        self.board_soft_reset = None  # does Klipper's reset keep the board on USB? None until connected
        self.last_status = {}
        self.last_pass = None
        self.querying = False
        self.query_failing = False
        self.status_timer = None
        printer.register_event_handler('klippy:ready', self._handle_ready)
        printer.register_event_handler('klippy:shutdown', self._handle_shutdown)
        gcode = printer.lookup_object('gcode')
        gcode.register_command('POLYDYE_PASS', self.cmd_POLYDYE_PASS, desc=self.cmd_POLYDYE_PASS_help)
        gcode.register_command('POLYDYE_ENABLE', self.cmd_POLYDYE_ENABLE, desc=self.cmd_POLYDYE_ENABLE_help)
        gcode.register_command('POLYDYE_DISABLE', self.cmd_POLYDYE_DISABLE, desc=self.cmd_POLYDYE_DISABLE_help)
        gcode.register_command('POLYDYE_CANCEL', self.cmd_POLYDYE_CANCEL, desc=self.cmd_POLYDYE_CANCEL_help)
        gcode.register_command('POLYDYE_STATUS', self.cmd_POLYDYE_STATUS, desc=self.cmd_POLYDYE_STATUS_help)
        gcode.register_command('POLYDYE_OFFSET', self.cmd_POLYDYE_OFFSET, desc=self.cmd_POLYDYE_OFFSET_help)
        gcode.register_command('POLYDYE_NUDGE', self.cmd_POLYDYE_NUDGE, desc=self.cmd_POLYDYE_NUDGE_help)
        gcode.register_command('POLYDYE_CARTRIDGE', self.cmd_POLYDYE_CARTRIDGE,
                               desc=self.cmd_POLYDYE_CARTRIDGE_help)
        gcode.register_command('POLYDYE_UPLOAD_TEST_PASS', self.cmd_POLYDYE_UPLOAD_TEST_PASS,
                               desc=self.cmd_POLYDYE_UPLOAD_TEST_PASS_help)
        gcode.register_command('POLYDYE_LEVEL', self.cmd_POLYDYE_LEVEL, desc=self.cmd_POLYDYE_LEVEL_help)
        gcode.register_command('POLYDYE_FIRE_TEST', self.cmd_POLYDYE_FIRE_TEST, desc=self.cmd_POLYDYE_FIRE_TEST_help)
        gcode.register_command('POLYDYE_SPIT', self.cmd_POLYDYE_SPIT, desc=self.cmd_POLYDYE_SPIT_help)
        gcode.register_command('POLYDYE_WARM', self.cmd_POLYDYE_WARM, desc=self.cmd_POLYDYE_WARM_help)
        gcode.register_command('POLYDYE_PREWARM', self.cmd_POLYDYE_PREWARM, desc=self.cmd_POLYDYE_PREWARM_help)
        gcode.register_command('POLYDYE_PRIME', self.cmd_POLYDYE_PRIME, desc=self.cmd_POLYDYE_PRIME_help)
        gcode.register_command('POLYDYE_INK_MOTION', self.cmd_POLYDYE_INK_MOTION,
                               desc=self.cmd_POLYDYE_INK_MOTION_help)
        gcode.register_command('POLYDYE_CLOCK_TEST', self.cmd_POLYDYE_CLOCK_TEST,
                               desc=self.cmd_POLYDYE_CLOCK_TEST_help)
        gcode.register_command('POLYDYE_ALIGN_PRINT', self.cmd_POLYDYE_ALIGN_PRINT,
                               desc=self.cmd_POLYDYE_ALIGN_PRINT_help)
        gcode.register_command('POLYDYE_ALIGN', self.cmd_POLYDYE_ALIGN, desc=self.cmd_POLYDYE_ALIGN_help)
        gcode.register_command('POLYDYE_TEST_PAGE', self.cmd_POLYDYE_TEST_PAGE,
                               desc=self.cmd_POLYDYE_TEST_PAGE_help)
        gcode.register_command('POLYDYE_OFFSET_PRINT', self.cmd_POLYDYE_OFFSET_PRINT,
                               desc=self.cmd_POLYDYE_OFFSET_PRINT_help)
        gcode.register_command('POLYDYE_SAVE', self.cmd_POLYDYE_SAVE, desc=self.cmd_POLYDYE_SAVE_help)
        gcode.register_command('POLYDYE_JOB', self.cmd_POLYDYE_JOB, desc=self.cmd_POLYDYE_JOB_help)
        gcode.register_command('POLYDYE_JOB_PASS', self.cmd_POLYDYE_JOB_PASS, desc=self.cmd_POLYDYE_JOB_PASS_help)
        gcode.register_command('POLYDYE_JOB_END', self.cmd_POLYDYE_JOB_END, desc=self.cmd_POLYDYE_JOB_END_help)

    def _build_config(self):
        self.mcu.add_config_cmd("config_polydye oid=%d" % (self.oid,))
        constants = self.mcu.get_constants()
        # Older board firmware restarts the chip on Klipper's reset (FIRMWARE_RESTART, and Klipper's own restart when
        # the configuration changed), so the board leaves USB and comes back.
        self.board_soft_reset = 'POLYDYE_SOFT_RESET' in constants
        if not self.board_soft_reset:
            logging.warning("polydye: this board firmware leaves USB and comes back whenever Klipper resets it, which "
                            "has frozen Raspberry Pi 3s: update it (polydye-klipper README, \"Raspberry Pi 3\")")
        # Board firmware from before the stock-only cleanup still works with what it has; Klipper sets its bus clock to
        # stock below.
        stock_only = 'POLYDYE_STOCK_ONLY' in constants
        if not stock_only:
            logging.warning("polydye: this board firmware predates the stock-only cleanup: update it from the PolyDye "
                            "page (Board firmware: Update to bundled firmware)")
        cq = self.mcu.alloc_command_queue()
        self.query_pass_cmd = self.mcu.lookup_query_command(
            "polydye_query_pass oid=%c layer=%hu pass=%hu", PASS_INFO, oid=self.oid, cq=cq)
        self.query_cmd = self.mcu.lookup_query_command("polydye_query oid=%c", STATUS, oid=self.oid, cq=cq)
        self.schedule_cmd = self.mcu.lookup_command(
            "polydye_schedule oid=%c clock=%u period=%u frac=%hu layer=%hu pass=%hu", cq=cq)
        self.mode_cmd = self.mcu.lookup_command("polydye_set_mode oid=%c boost=%c live=%c", cq=cq)
        self.cancel_cmd = self.mcu.lookup_command("polydye_cancel oid=%c", cq=cq)
        # What stopped a run, with its column counts, came after the warm-up.
        if self.mcu.try_lookup_command("polydye_query_run oid=%c") is not None:
            self.query_run_cmd = self.mcu.lookup_query_command("polydye_query_run oid=%c", RUN, oid=self.oid, cq=cq)
        # The signal check came after the cartridge commands; older board firmware just doesn't offer it.
        if self.mcu.try_lookup_command("polydye_query_level oid=%c") is not None:
            self.set_level_cmd = self.mcu.lookup_command("polydye_set_level oid=%c line=%c state=%c", cq=cq)
            self.query_level_cmd = self.mcu.lookup_query_command("polydye_query_level oid=%c", LEVEL, oid=self.oid,
                                                                 cq=cq)
            self.spit_cmd = self.mcu.lookup_command("polydye_spit oid=%c columns=%hu pattern=%c", cq=cq)
            # The stock pattern came with the column period and the warm-up (POLYDYE_SPIT_PATTERNS over 3).
            stock = int(constants.get('POLYDYE_SPIT_PATTERNS', 0)) > SPIT_PATTERNS['stock']
            self.spit_patterns = tuple(p for p in SPIT_PATTERNS if p != 'stock' or stock)
            fmt = ("polydye_spit_run oid=%c columns=%hu pattern=%c ext_words=%c period_us=%hu warm_columns=%hu "
                   "warm_words=%c warm_period_us=%hu")
            if self.mcu.try_lookup_command(fmt) is not None:
                self.spit_run_cmd = self.mcu.lookup_command(fmt, cq=cq)
                if 'POLYDYE_WARM_MAX_DUTY_PPM' in constants:
                    self.warm_max_words = int(constants['POLYDYE_WARM_MAX_WORDS'])
                    self.warm_max_us = constants['POLYDYE_WARM_MAX_NS'] / 1000.
                    self.warm_duty_ppm = int(constants['POLYDYE_WARM_MAX_DUTY_PPM'])
                    self.warm_max_s = constants['POLYDYE_WARM_MAX_MS'] / 1000.
            # Then the random pattern's knobs.
            fmt = "polydye_set_spit_random oid=%c density=%hu repeat=%c colors=%c first=%c last=%c per_pulse=%c"
            if self.mcu.try_lookup_command(fmt) is not None:
                self.spit_random_cmd = self.mcu.lookup_command(fmt, cq=cq)
        # Prewarm bursts came with POLYDYE_PREWARM_WORDS (their pulse, fixed in the board firmware at 1 word).
        if 'POLYDYE_PREWARM_WORDS' in constants:
            self.prewarm_cmd = self.mcu.lookup_command(PREWARM, cq=cq)
            self.query_prewarm_cmd = self.mcu.lookup_query_command("polydye_query_prewarm oid=%c", PREWARM_STATE,
                                                                   oid=self.oid, cq=cq)
        # color_b_late and the stock bus clock came with the stock-firmware comparison.
        if self.mcu.try_lookup_command("polydye_query_clock oid=%c") is not None:
            self.set_clock_cmd = self.mcu.lookup_command("polydye_set_clock oid=%c stock=%c", cq=cq)
            self.query_clock_cmd = self.mcu.lookup_query_command("polydye_query_clock oid=%c", CLOCK, oid=self.oid,
                                                                 cq=cq)
            # The board's bus clock check (refuses to fire at a wrong clock) came with these constants.
            if 'POLYDYE_BUS_HZ_FAST_PCT' in constants:
                self.bus_hz_check = (constants['POLYDYE_BUS_HZ_FAST_PCT'], constants['POLYDYE_BUS_HZ_SLOW_PCT'])
            if 'POLYDYE_BOOST_IDLE_S' in constants:
                self.boost_idle_s = int(constants['POLYDYE_BOOST_IDLE_S'])
        # Board firmware before the cartridge commands still works, just without a template or pulse choice.
        if self.mcu.try_lookup_command("polydye_query_cartridge oid=%c") is None:
            for option, value in (('cartridge_template', self.cartridge_template), ('pulse_words', self.pulse_words)):
                if value is not None:
                    raise self.printer.config_error(
                        "[polydye] %s needs newer PolyDye board firmware. Update it from the PolyDye page (Board "
                        "firmware), or remove %s from polydye.cfg." % (option, option))
            return
        self.set_template_cmd = self.mcu.lookup_command("polydye_set_template oid=%c template=%c", cq=cq)
        self.set_pulse_cmd = self.mcu.lookup_command("polydye_set_pulse oid=%c words=%c", cq=cq)
        self.query_cartridge_cmd = self.mcu.lookup_query_command("polydye_query_cartridge oid=%c", CARTRIDGE,
                                                                 oid=self.oid, cq=cq)
        self.templates = tuple(t for t in TEMPLATES if t != STOCK_TEMPLATE or self.set_clock_cmd is not None)
        if self.cartridge_template is not None and self.cartridge_template not in self.templates:
            raise self.printer.config_error(
                "[polydye] cartridge_template: %s needs newer PolyDye board firmware. Update it from the PolyDye page "
                "(Board firmware: Update to bundled firmware), or change it in polydye.cfg."
                % (self.cartridge_template,))
        # Sent on every connect: the board forgets them when it restarts.
        if self.cartridge_template is not None:
            self.mcu.add_config_cmd("polydye_set_template oid=%d template=%d"
                                    % (self.oid, BOARD_TEMPLATES.index(self.cartridge_template)), is_init=True)
        for cmd in init_pulse_clock(self.oid, self.pulse_words, self.set_clock_cmd is not None, stock_only):
            self.mcu.add_config_cmd(cmd, is_init=True)

    def _settle(self):
        if self.settle_time is not None:
            return self.settle_time
        return shaper_duration(self.printer, self.pass_axis)

    # Keeps printer.polydye current for Mainsail and the PolyDye page.
    def _handle_ready(self):
        reactor = self.printer.get_reactor()
        self.status_timer = reactor.register_timer(self._status_event, reactor.NOW)

    def _handle_shutdown(self):
        self.prewarm_gen += 1
        self._stop_job("Klipper shut down")
        if self.status_timer is not None:
            logging.info("polydye: Klipper shut down; board status updates stopped until a restart")
            self.printer.get_reactor().update_timer(self.status_timer, self.printer.get_reactor().NEVER)
        # A pass or the fire test stopped by a cartridge driver fault: show what the board says about it. A spit test
        # or warm-up puts it in its own error.
        if self.query_run_cmd is not None and not self.testing:
            self.printer.get_reactor().register_callback(self._report_fault)

    def _report_fault(self, eventtime):
        run = self._last_run()
        if run is None:
            return
        clock = self._measured_clock()
        if run_fault(run) is not None:
            msg = "PolyDye board: %s, after %s%s" % (run_fault(run), run_text(run), "; %s" % (clock[0],) if clock
                                                     else "")
        else:
            # A pass refused at a wrong bus clock never started: no run fault, but the board says what it measured.
            if not clock or not clock[1]:
                return
            msg = "PolyDye board: %s" % (clock[0],)
        logging.warning("polydye: %s", msg)
        self.printer.lookup_object('gcode').respond_info(msg)

    def _last_run(self):
        """The board's account of its last run (polydye_run), or None: also works after a shutdown."""
        if self.query_run_cmd is None:
            return None
        try:
            params = self.query_run_cmd.send([self.oid])
        except Exception as e:  # the board gone (unplugged): nothing to report
            logging.info("polydye: couldn't ask the board about its last run: %s", e)
            return None
        ticks_us = 1e6 / self.mcu.get_constant_float('CLOCK_FREQ')
        return {'fault': str(params['fault_static_string_id']), 'columns': params['columns'],
                'warm_left': params['warm_left'], 'underruns': params['underruns'], 'overruns': params['overruns'],
                'late_max_us': params['late_max'] * ticks_us, 'fill_max_us': params['fill_max'] * ticks_us}

    def _status_event(self, eventtime):
        # Klipper ignores update_timer() for a timer whose callback is running. Unplugging the board shuts Klipper down
        # while a query here is still waiting for the board, so _handle_shutdown can't stop this timer: it has to stop
        # itself, or it would retry a dead link every STATUS_INTERVAL until the next restart.
        if not self.printer.is_shutdown() and not self.querying:
            self._check_job_end(eventtime)
            if self.job is not None:
                self.job['streamer'].release(self.mcu.estimated_print_time(eventtime))
            try:
                self._check_print_end(eventtime)
            except Exception:
                logging.exception("polydye: couldn't turn PolyDye off after the print ended")
            try:
                self._query_status()
                if self.query_failing:
                    logging.info("polydye: status queries work again")
                self.query_failing = False
            except Exception:
                if not self.printer.is_shutdown() and not self.query_failing:
                    logging.exception("polydye: status query failed (not logged again until one succeeds)")
                self.query_failing = True
        if self.printer.is_shutdown():
            return self.printer.get_reactor().NEVER
        return eventtime + STATUS_INTERVAL

    def _printing(self, eventtime):
        """Whether a print is running or paused (virtual_sdcard, print_stats)."""
        sdcard = self.printer.lookup_object('virtual_sdcard', None)
        print_stats = self.printer.lookup_object('print_stats', None)
        state = print_stats.get_status(eventtime).get('state') if print_stats is not None else None
        return (sdcard is not None and sdcard.is_active()) or state in ('printing', 'paused')

    def _check_print_end(self, eventtime):
        """A print cancelled (CANCEL_PRINT waits for the command running, a pass's warm-up say) or ended by an error
        never runs its own POLYDYE_ENABLE LIVE=0, and live ink stayed on (2026-11-25). So once a print that turned live
        ink on (print_live, set by POLYDYE_ENABLE) is no longer running or paused, however it ended and however soon,
        the board drops its queued passes and stops any run (a warm-up's, a pass's), and live ink goes off; the boost
        too unless the board keeps it on (boost_idle_s). The same after a test whose cleanup failed (live_off_pending,
        _restore_after_test()). Run by the status poll and before POLYDYE_ENABLE; until it works, it's tried again."""
        ended = self.print_live and not self._printing(eventtime)
        if not (ended or self.live_off_pending) or self.cancel_cmd is None:
            return
        print_stats = self.printer.lookup_object('print_stats', None)
        state = print_stats.get_status(eventtime).get('state') if print_stats is not None else None
        self.prewarm_gen += 1
        self.cancel_cmd.send([self.oid])
        self.mode_cmd.send([self.oid, 0 if self.boost_idle_s is None else 1, 0])
        # Done only once both went out: if one fails, the next poll tries again.
        self.print_live = self.live_off_pending = False
        self.last_fire = None  # the dropped passes never fired
        msg = "PolyDye: the print %s, so its queued passes were dropped and live ink is off" % (
            {'cancelled': "was cancelled", 'error': "stopped with an error",
             'complete': "finished with live ink still on"}.get(state, "ended"),) if ended else (
            "PolyDye: live ink is off now (turning it off after the last test failed)")
        logging.info("polydye: %s", msg)
        self.printer.lookup_object('gcode').respond_info(msg)

    def _query_status(self):
        if self.querying:
            return self.last_status  # another query is in flight; its answer is moments away
        self.querying = True
        try:
            params = self.query_cmd.send([self.oid])
            template = pulse = level = clock = None
            if self.query_cartridge_cmd is not None:
                cart = self.query_cartridge_cmd.send([self.oid])
                n = cart['template']
                template = BOARD_TEMPLATES[n] if n < len(BOARD_TEMPLATES) else str(n)
                pulse = cart['pulse']
            # Only while a line is held, so the usual poll stays at two queries.
            if self.query_level_cmd is not None and params['flags'] & FLAG_LEVEL:
                lv = self.query_level_cmd.send([self.oid])
                if lv['line'] < len(LEVEL_LINES):
                    level = {'line': LEVEL_LINES[lv['line']], 'state': LEVEL_STATE_NAMES.get(lv['state'], 'unknown'),
                             'remaining': lv['remaining']}
            if self.query_clock_cmd is not None:
                clock = self.query_clock_cmd.send([self.oid])
            if self.query_prewarm_cmd is not None and self.prewarm_sent is not None:
                self._query_prewarm()
        finally:
            self.querying = False
        flags = params['flags']
        # Firmware without polydye_query_clock runs the bus at ours.
        bus_clock, bus_hz = (None, OURS_HZ) if clock is None else bus_clock_of(clock)
        self.last_status = {
            'boost': bool(flags & FLAG_BOOST),
            'live': bool(flags & FLAG_LIVE),
            'running': bool(flags & FLAG_RUNNING),
            'queued': params['queued'],
            'passes': params['passes'],
            'late_max_us': params['late_max'] * 1000000. / self.mcu.get_constant_float('CLOCK_FREQ'),
            'faults': params['faults'],
            'store_count': params['store_count'],
            'store_free': params['store_free'],
            'cartridge_template': template,
            'pulse_words': pulse,
            'pulse_us': None if pulse is None else round(pulse * 1e6 / bus_hz, 2),
        }
        if clock is not None:
            self.last_status.update(bus_clock=bus_clock, bus_hz=bus_hz, bus_hz_measured=clock['measured_hz'] or None)
        if self.query_level_cmd is not None:
            self.last_status['level'] = level  # None: no line held. Absent: the board can't do the signal check
        return self.last_status

    def _fresh_status(self):
        """Board status queried now, after any background query in flight (whose answer may predate a command)."""
        reactor = self.printer.get_reactor()
        while self.querying:
            reactor.pause(reactor.monotonic() + 0.05)
        return self._query_status()

    cmd_POLYDYE_PASS_help = ("Print one ink pass along pass_axis (the pass data must already be uploaded); with live "
                             "ink a cold head is warmed first, off the print (WARM seconds, WARM_PULSE, WARM_PERIOD_US "
                             "ask for a warm-up, WARM=0 for none; PRESPIT seconds of stock-pattern spit after it)")

    def cmd_POLYDYE_PASS(self, gcmd):
        layer = gcmd.get_int('LAYER', minval=0, maxval=65535)
        pass_num = gcmd.get_int('PASS', minval=0, maxval=65535)
        bed_x = gcmd.get_float('X')
        bed_y = gcmd.get_float('Y')
        direction = gcmd.get_int('DIR', 1)
        if direction not in (1, -1):
            raise gcmd.error("POLYDYE_PASS: DIR must be 1 or -1")
        info = self.query_pass_cmd.send([self.oid, layer, pass_num])
        if not info['present']:
            raise gcmd.error("POLYDYE_PASS: layer %d pass %d has not been uploaded to the PolyDye board"
                             % (layer, pass_num))
        columns = info['columns']
        if columns < 1:
            raise gcmd.error("POLYDYE_PASS: layer %d pass %d is empty" % (layer, pass_num))
        # A warm-up asked for (WARM seconds, 0 for none) instead of the automatic one; checked before anything moves.
        warm = gcmd.get_float('WARM', None, minval=0., maxval=WARM_MAX_S)
        warm_words = gcmd.get_int('WARM_PULSE', None, minval=1)
        warm_period = gcmd.get_int('WARM_PERIOD_US', None, minval=1, maxval=65535)
        # Checked against the board when a pre-spit is planned (_warm_head()): with no warm-up there's none.
        prespit = gcmd.get_float('PRESPIT', None, minval=0., maxval=PRESPIT_MAX_S)
        if warm:
            if self.spit_run_cmd is None:
                raise gcmd.error("POLYDYE_PASS: WARM needs newer PolyDye board firmware; update it from the PolyDye "
                                 "page (Board firmware: Update to bundled firmware)")
            if warm > self.warm_max_s + 1e-9:
                raise gcmd.error("POLYDYE_PASS: a warm-up lasts at most %g s with this board firmware%s"
                                 % (self.warm_max_s, "; newer firmware takes up to %g s (Board firmware: Update to "
                                    "bundled firmware)" % (WARM_MAX_S,) if self.warm_duty_ppm is None else ""))
            self._warm_plan(gcmd, "POLYDYE_PASS", warm,
                            *pass_warm_settings(warm_words, warm_period, self.warm_pulse, self.warm_period_us))

        toolhead = self.printer.lookup_object('toolhead')
        gcode_move = self.printer.lookup_object('gcode_move')
        eventtime = self.printer.get_reactor().monotonic()
        th_status = toolhead.get_status(eventtime)
        accel = th_status['max_accel']
        speed = gcmd.get_float('SPEED', self.pass_speed, above=0.)
        if speed > th_status['max_velocity']:
            raise gcmd.error("POLYDYE_PASS: SPEED %.1f exceeds max_velocity %.1f"
                             % (speed, th_status['max_velocity']))
        pitch = 25.4 / self.dpi
        run_up = self._run_up(speed, th_status, columns)

        # Bed coordinates of the ink -> toolhead coordinates of the hotend.
        start, first, end = pass_geometry(self.pass_axis, bed_x, bed_y, direction, columns, pitch, run_up,
                                          (self.offset_x, self.offset_y), gcode_move.base_position,
                                          self.direction_trim)
        axis = 0 if self.pass_axis == 'x' else 1
        live = self._auto_warm(gcmd, "POLYDYE_PASS", start, direction, warm, warm_words, warm_period, prespit)
        if live:
            self._wait_for_boost()  # a POLYDYE_PREWARM may have turned the boost on just before

        # Position (through any bed mesh transform), then stop, so the pass starts from rest at a known time.
        pos = list(gcode_move.last_position)
        pos[0], pos[1] = start[0], start[1]
        gcode_move.move_with_transform(pos, self.travel_speed)
        toolhead.dwell(0.001)
        t0 = toolhead.get_last_move_time()
        # The pass is one plain toolhead move along the pass axis at constant Z, so its trapezoid is exactly
        # predictable.
        newpos = toolhead.get_position()
        newpos[axis] = end[axis]
        toolhead.move(newpos, speed)
        t_end = toolhead.get_last_move_time()
        gcode_move.reset_last_position()

        t_first, period, duration = pass_timing(t0, speed, accel, run_up, columns, pitch)
        if abs((t_end - t0) - duration) > TIMING_TOLERANCE:
            raise gcmd.error("POLYDYE_PASS: planned pass took %.4f s, expected %.4f s; no ink was scheduled"
                             % (t_end - t0, duration))
        est = self.mcu.estimated_print_time(self.printer.get_reactor().monotonic())
        if t_first - est < self.min_lead_time:
            raise gcmd.error("POLYDYE_PASS: not enough time to schedule the pass (%.3f s ahead)" % (t_first - est,))
        clock_first = self.mcu.print_time_to_clock(t_first)
        spans = max(columns - 1, 1)
        clock_span = self.mcu.print_time_to_clock(t_first + spans * period)
        period_ticks, frac = period_to_ticks(clock_first, clock_span, spans)
        self.schedule_cmd.send([self.oid, clock_first & 0xffffffff, period_ticks, frac, layer, pass_num],
                               reqclock=clock_first)
        self.last_pass = {'layer': layer, 'pass': pass_num, 'columns': columns, 'axis': self.pass_axis,
                          'print_time': t_first, 'clock': clock_first, 'period_ticks': period_ticks + frac / 65536.}
        if live:
            self.last_fire = t_first + spans * period
        logging.info("polydye: layer %d pass %d: %d columns along %s, first at print_time %.6f (clock %d), "
                     "period %.4f ticks, t0 %.6f, run-up %.4f mm, toolhead first column %.3f,%.3f", layer, pass_num,
                     columns, self.pass_axis, t_first, clock_first, period_ticks + frac / 65536., t0, run_up,
                     first[0], first[1])

    def _run_up(self, speed, th_status, columns):
        """Run-up in mm before a pass of `columns` at `speed` (toolhead status `th_status`): accelerating, settling and
        extra_run_up. Klipper's minimum_cruise_ratio caps the peak speed of moves that don't cruise for that fraction
        of their length, so the pass is also made long enough to always reach full speed."""
        accel = th_status['max_accel']
        length = (columns - 1) * 25.4 / self.dpi
        run_up = speed * speed / (2. * accel) + speed * self._settle() + self.extra_run_up
        mcr = th_status.get('minimum_cruise_ratio', 0.)
        min_distance = speed * speed / (accel * (1. - mcr))
        return max(run_up, (min_distance - length) / 2. + 0.001)

    def _auto_warm(self, gcmd, cmd, start, direction, warm=None, words=None, period=None, prespit=None):
        """Before a pass whose run-up starts at toolhead `start` (going `direction` along pass_axis): warms the head
        (_warm_head()) for `warm` seconds if given (0: not at all), else automatically if the automatic warm-up is on
        and the head will be cold when the pass starts (head_cold() at the end of the moves already queued, the
        earliest the pass can fire). A warm-up is followed by `prespit` seconds of pre-spit (None: warm_prespit).
        Returns whether live ink is on, whatever the warm-up: a live pass keeps the head warm, a dry-run pass fires
        nothing."""
        prespit = self.warm_prespit if prespit is None else prespit
        st = self._fresh_status()
        if not st['live']:
            if warm:
                raise gcmd.error("%s: WARM needs live ink on (POLYDYE_ENABLE LIVE=1)" % (cmd,))
            return False
        if warm is not None:
            if warm:
                words, period = pass_warm_settings(words, period, self.warm_pulse, self.warm_period_us)
                self._warm_head(gcmd, cmd, "a %g s head warm-up was asked for" % (warm,), warm, words, period, start,
                                direction, prespit)
            return True
        if not self.warm_seconds or self.spit_run_cmd is None or self.warm_duty_ppm is None:
            return True
        now = max(self.printer.lookup_object('toolhead').get_last_move_time(),
                  self.mcu.estimated_print_time(self.printer.get_reactor().monotonic()))
        warmed = self._warmed_until(now)
        if head_cold(warmed, now, self.warm_idle_time):
            why = ("the head hasn't fired since Klipper started" if warmed is None else
                   "the head won't have fired for %.0f s when the pass starts" % (now - warmed,))
            self._warm_head(gcmd, cmd, why, self.warm_seconds, self.warm_pulse, self.warm_period_us, start, direction,
                            prespit)
        return True

    def _warmed_until(self, now):
        """The print time the head last fired or was warmed until (by `now`, when a pass is due): the last fire, or the
        end of the last prewarm burst if later. A burst still queued or firing warms until its planned end, or `now`
        if the pass cuts it short first; one skipped, or ended before firing a column, doesn't count."""
        if self.prewarm_sent is not None and self.query_prewarm_cmd is not None:
            self._query_prewarm()
        return self._warmed_cached(now)

    def _warmed_cached(self, now):
        """_warmed_until() from the board's last answer, without asking it again (for get_status())."""
        st = self.prewarm_state
        last = self.last_fire
        if st is None or st['state'] not in ('queued', 'firing', 'done', 'made way'):
            return last
        if st['state'] in ('done', 'made way') and not st['columns']:
            return last
        end = min(now, st['end']) if st['state'] in ('queued', 'firing') else st['end']
        return end if last is None else max(last, end)

    def _query_prewarm(self):
        """The board's account of the last prewarm burst (the last one sent: each replaces the board's record): state,
        its start and end as print times, columns fired. Its 32-bit clocks are within seconds of the clock the burst
        was sent for, not of now: the record stays as it was for as long as no burst follows, beyond the 7 minutes
        after which a 32-bit clock read against now comes round again."""
        params = self.query_prewarm_cmd.send([self.oid])
        n = params['state']
        sent = self.prewarm_sent['clock']

        def to_time(clock):
            diff = (clock - sent) & 0xffffffff
            return self.mcu.clock_to_print_time(sent + diff - ((diff & 0x80000000) << 1))
        self.prewarm_state = {'state': PREWARM_STATES[n] if n < len(PREWARM_STATES) else str(n),
                              'start': to_time(params['start']), 'end': to_time(params['end']),
                              'columns': params['columns']}
        return self.prewarm_state

    cmd_POLYDYE_PREWARM_help = ("Warm the head in the background during the moves that follow, so it's hot for the "
                                "next pass: SECONDS (prewarm_seconds, 6) of every nozzle at 1 word (0.35 us, which doesn't "
                                "eject) every warm_period_us, from where the moves before it end; turns live ink on. "
                                "Passes never wait for it: one that comes first cuts it short, and needs no warm-up")

    def cmd_POLYDYE_PREWARM(self, gcmd):
        cmd = "POLYDYE_PREWARM"
        if self.prewarm_cmd is None:
            raise gcmd.error("%s: needs newer PolyDye board firmware; update it from the PolyDye page (Board firmware: "
                             "Update to bundled firmware)" % (cmd,))
        seconds = gcmd.get_float('SECONDS', self.prewarm_seconds, above=0., maxval=self.warm_max_s)
        period = self.warm_period_us
        columns = warm_columns(seconds, period, self.warm_max_s)
        if columns > WARM_MAX_COLUMNS:
            raise gcmd.error("%s: SECONDS=%g is too long: a burst is at most %d columns, %.1f s at warm_period_us %d"
                             % (cmd, seconds, WARM_MAX_COLUMNS, WARM_MAX_COLUMNS * period / 1e6, period))
        # Live ink on, without POLYDYE_ENABLE's pause for the boost to settle: the moves go on, and the board starts
        # the burst only once the boost has settled.
        self._enable(gcmd, cmd, True, settle=False)
        gen = self.prewarm_gen
        self.printer.lookup_object('toolhead').register_lookahead_callback(
            lambda print_time: self._send_prewarm(print_time, columns, period, gen))

    def _send_prewarm(self, print_time, columns, period, gen):
        """Schedules a burst to start at `print_time` (the moves before POLYDYE_PREWARM done), unless a cancel, live
        ink going off or a shutdown came since POLYDYE_PREWARM (`gen`)."""
        if gen != self.prewarm_gen:
            logging.info("polydye: prewarm burst at print_time %.3f not sent: cancelled before its moves", print_time)
            return
        clock = self.mcu.print_time_to_clock(print_time)
        self.prewarm_cmd.send([self.oid, clock & 0xffffffff, columns, period], reqclock=clock)
        self.prewarm_sent = {'start': print_time, 'end': print_time + columns * period / 1e6,
                             'seconds': columns * period / 1e6, 'clock': clock}
        logging.info("polydye: prewarm burst at print_time %.3f: %d columns every %d us (%.1f s)", print_time,
                     columns, period, columns * period / 1e6)

    def _warm_head(self, gcmd, cmd, why, seconds, words, period, start, direction, prespit=0.):
        """A warm-up right before a pass: once the moves and passes before it are done, `seconds` of every nozzle at
        `words` every `period` us (None: the warm-up defaults, _warm_plan()), then `prespit` seconds of the stock
        pattern (if any), standing still at the warm spot (_go_to_warm_spot()), in one board run with live ink left
        on. Raises, and the pass doesn't run, if the board can't be seen idle afterwards."""
        st = self._board_idle(gcmd, cmd, "the head can't be warmed")
        spit = int(round(prespit * 1e6 / STOCK_PERIOD_US)) if prespit else 0
        if spit and 'stock' not in self.spit_patterns:
            raise gcmd.error("%s: the pre-spit needs newer PolyDye board firmware (the stock pattern); update it from "
                             "the PolyDye page (Board firmware: Update to bundled firmware), or set PRESPIT=0"
                             % (cmd,))
        if spit and (st.get('cartridge_template') or '').startswith('black'):
            raise gcmd.error("%s: the pre-spit is the stock pattern, for a colour cartridge template, not %s"
                             % (cmd, st['cartridge_template']))
        words, period, columns = self._warm_plan(gcmd, cmd, seconds, words, period)
        self._fire_at_spot(gcmd, cmd, "warm-up", (columns, words, period), spit, start, direction, None,
                           lambda where: "PolyDye: %s, so a head %s%s comes first, %s" % (
                               why, warm_text(columns, words, period),
                               ", then a %g s pre-spit (%d stock-pattern columns every %d us)"
                               % (prespit, spit, STOCK_PERIOD_US) if spit else "", where))

    def _board_idle(self, gcmd, cmd, why):
        """Waits for the moves, and so the passes, before this, then for the board to be idle (5 s at most: else
        `why`), with a pulse within the limit. Returns its status."""
        toolhead = self.printer.lookup_object('toolhead')
        reactor = self.printer.get_reactor()
        toolhead.wait_moves()
        end = reactor.monotonic() + 5.
        while True:
            st = self._fresh_status()
            if not (st['running'] or st['queued']):
                break
            if reactor.monotonic() > end:
                raise gcmd.error("%s: the board is still busy with passes, so %s" % (cmd, why))
            reactor.pause(reactor.monotonic() + 0.05)
        self._check_current_pulse(gcmd, cmd, st)
        return st

    def _fire_at_spot(self, gcmd, cmd, what, warm, spit, start, direction, position, message):
        """At the warm spot (_go_to_warm_spot(), `position` if given), one board run with live ink left on: `warm`
        ((columns, words, period), or None) of every nozzle, then `spit` columns of the stock pattern; then back.
        `message(where)` says what happens. Raises if the board can't be seen idle afterwards."""
        columns, words, period = warm or (0, 0, 0)
        reactor = self.printer.get_reactor()
        back, where = self._go_to_warm_spot(gcmd, cmd, start, direction, position)
        gcmd.respond_info(message(where))
        self.testing = True
        try:
            self.spit_run_cmd.send([self.oid, spit, SPIT_PATTERNS['stock' if spit else 'all'], 0,
                                    STOCK_PERIOD_US if spit else SPIT_PERIOD_US, columns, words, period])
            deadline = reactor.monotonic() + (columns * period + spit * STOCK_PERIOD_US) / 1e6 + 5.
            while True:
                reactor.pause(reactor.monotonic() + 0.2)
                if self.printer.is_shutdown():
                    raise gcmd.error(self._test_shutdown_text(cmd, what, bool(columns)))
                if not self._fresh_status()['running']:
                    break
                if reactor.monotonic() > deadline:
                    raise gcmd.error("%s: the board didn't finish the %s" % (cmd, what))
        finally:
            self.testing = False
            idle = self._back_from_warm_spot(gcmd, back)
        if not idle:
            raise gcmd.error("%s: the board may still be firing after the %s, so the cartridge stays over the %s"
                             % (cmd, what, "warm-up spot and the pass doesn't run" if warm
                                else "sponge and the passes after it don't run"))
        self.last_fire = self.mcu.estimated_print_time(reactor.monotonic())

    cmd_POLYDYE_PRIME_help = ("Prime the nozzles over the wipe sponge, for a layer's ink after its colours have been "
                              "idle: SECONDS (prime_seconds, 0.3) of the stock pattern (positions 14-85 of every "
                              "colour), then back; turns live ink on. Allowed during a print, no CONFIRM; waits for the "
                              "passes before it")

    def cmd_POLYDYE_PRIME(self, gcmd):
        cmd = "POLYDYE_PRIME"
        seconds = gcmd.get_float('SECONDS', self.prime_seconds, above=0., maxval=PRESPIT_MAX_S)
        if self.spit_run_cmd is None or 'stock' not in self.spit_patterns:
            raise gcmd.error("%s: needs newer PolyDye board firmware (the stock pattern); update it from the PolyDye "
                             "page (Board firmware: Update to bundled firmware)" % (cmd,))
        macro = self.printer.lookup_object('gcode_macro POLYDYE_WIPE', None)
        if 'x' not in getattr(macro, 'variables', {}):
            raise gcmd.error("%s: primes over the wipe sponge, but there's no POLYDYE_WIPE macro to say where it is: "
                             "include polydye_macros.cfg" % (cmd,))
        reactor = self.printer.get_reactor()
        printing = self._printing(reactor.monotonic())
        st = self._board_idle(gcmd, cmd, "the nozzles can't be primed")
        # A print that ended while this waited has had live ink turned off (_check_print_end()): turning it on again
        # here would leave it on, no longer the print's.
        if printing and not self._printing(reactor.monotonic()):
            raise gcmd.error("%s: the print ended while it waited for the passes before it, so it doesn't prime"
                             % (cmd,))
        if (st.get('cartridge_template') or '').startswith('black'):
            raise gcmd.error("%s: primes with the stock pattern, for a colour cartridge template, not %s"
                             % (cmd, st['cartridge_template']))
        # Live ink on; if the boost was off, the moves to the sponge wait for it to settle.
        self._enable(gcmd, cmd, True)
        spit = max(1, int(round(seconds * 1e6 / STOCK_PERIOD_US)))
        self._fire_at_spot(gcmd, cmd, "prime", None, spit, None, 1, 'sponge',
                           lambda where: "PolyDye: priming, %g s of the stock pattern (%d columns every %d us) %s"
                           % (seconds, spit, STOCK_PERIOD_US, where))

    def _go_to_warm_spot(self, gcmd, cmd, start, direction, position=None):
        """Moves the cartridge to where the warm-up runs (warm_position), off the print and waits there:
        - `pass`: warm_clearance mm behind the pass's run-up start along pass_axis (within the axis), at the current Z;
          the pass travels on from there (a short way, so the head stays hot).
        - `sponge` or a bed spot: up by POLYDYE_WIPE's z_hop (within the Z axis), then over the wipe sponge
          (POLYDYE_WIPE's X) or the spot; _back_from_warm_spot() brings it back.
        `position` overrides warm_position. Returns (where to come back to for _back_from_warm_spot(), None for `pass`;
        a description for messages). If the move fails, the G-code state is put back and the error raised: nothing has
        fired."""
        position = position or self.warm_position
        toolhead = self.printer.lookup_object('toolhead')
        reactor = self.printer.get_reactor()
        eventtime = reactor.monotonic()
        th = toolhead.get_status(eventtime)
        if position == 'pass':
            a = 0 if self.pass_axis == 'x' else 1
            lo, hi = (th['axis_minimum'].x, th['axis_maximum'].x) if a == 0 else (th['axis_minimum'].y,
                                                                                 th['axis_maximum'].y)
            gcode_move = self.printer.lookup_object('gcode_move')
            pos = list(gcode_move.last_position)
            pos[0], pos[1] = start[0], start[1]
            pos[a] = min(hi, max(lo, start[a] - direction * self.warm_clearance))
            gcode_move.move_with_transform(pos, self.travel_speed)
            gcode_move.reset_last_position()
            toolhead.wait_moves()
            return None, "%.1f mm before the pass starts" % (abs(start[a] - pos[a]),)
        macro = self.printer.lookup_object('gcode_macro POLYDYE_WIPE', None)
        variables = getattr(macro, 'variables', {}) if macro is not None else {}
        hop = float(variables.get('z_hop', 1.))
        if position == 'sponge':
            if 'x' not in variables:
                raise gcmd.error("%s: warm_position is the wipe sponge, but there's no POLYDYE_WIPE macro to say where "
                                 "it is: include polydye_macros.cfg, or set warm_position in polydye.cfg" % (cmd,))
            x, y, where = float(variables['x']), None, "over the wipe sponge (X=%.1f)" % (float(variables['x']),)
        else:
            x, y = position[0] - self.offset_x, position[1] - self.offset_y
            where = "over X=%.1f Y=%.1f" % position
        gm = self.printer.lookup_object('gcode_move').get_status(eventtime)
        pos = gm['gcode_position']
        # Up by the hop, but no higher than the Z axis allows (in G-code coordinates, like _POLYDYE_WIPE_MOVES).
        z_max = th['axis_maximum'].z - (gm['position'].z - pos.z)
        run = self.printer.lookup_object('gcode').run_script_from_command
        run("SAVE_GCODE_STATE NAME=polydye_warm")
        try:
            run("G90\nG1 Z%.3f F900\nG1 X%.3f%s F%d" % (max(pos.z, min(pos.z + hop, z_max)), x,
                                                       "" if y is None else " Y%.3f" % (y,), self.travel_speed * 60))
            toolhead.wait_moves()
        except Exception:
            if not self.printer.is_shutdown():
                run("RESTORE_GCODE_STATE NAME=polydye_warm")
            raise
        return (pos.x, pos.y, pos.z), where

    def _back_from_warm_spot(self, gcmd, back):
        """After a warm-up: makes sure the board isn't firing (a run still going, timed out say, is cancelled), then
        goes back to where _go_to_warm_spot() came from, across at the hopped height and down (`back`; None: stay, the
        pass travels on). If the board can't be seen idle, the cartridge stays over the warm-up spot rather than cross
        the print. The G-code state is put back either way. Returns whether the board was seen idle."""
        if self.printer.is_shutdown():
            return False
        reactor = self.printer.get_reactor()
        idle = False
        try:
            st = self._fresh_status()
            if st['running']:
                self.cancel_cmd.send([self.oid])
                end = reactor.monotonic() + 2.
                while st['running'] and reactor.monotonic() < end:
                    reactor.pause(reactor.monotonic() + 0.1)
                    st = self._fresh_status()
            idle = not st['running']
        except Exception:
            logging.exception("polydye: couldn't check the board after the warm-up")
        if self.printer.is_shutdown():
            return False
        if not idle:
            gcmd.respond_info("PolyDye: the board may still be firing, so the cartridge stays over the warm-up spot")
        if back is not None:
            run = self.printer.lookup_object('gcode').run_script_from_command
            try:
                if idle:
                    run("G1 X%.3f Y%.3f F%d\nG1 Z%.3f F900" % (back[0], back[1], self.travel_speed * 60, back[2]))
            finally:
                if not self.printer.is_shutdown():
                    run("RESTORE_GCODE_STATE NAME=polydye_warm")
        return idle

    cmd_POLYDYE_ENABLE_help = "Turn on the PolyDye boost (LIVE=1 also allows real ink; LIVE=0 is a dry run)"

    def _check_board_clock(self, gcmd, name, st):
        """Refuses to go on from a board on a clock other than the stock one (older firmware's console can still switch
        it): the add-on's pulse limit, column and warm-up timing are all worked out for the stock clock, so a setting
        or test it allows could be over the limit there and shut the board down."""
        clock = st.get('bus_clock')
        if clock not in (None, 'stock'):
            raise gcmd.error("%s: the board runs the %s bus clock (set on its console?), but only the stock clock is "
                             "left: FIRMWARE_RESTART sets it back, and updating the board firmware from the PolyDye "
                             "page (Board firmware: Update to bundled firmware) removes the others" % (name, clock))

    def _check_current_pulse(self, gcmd, name, st):
        """Refuses to go on from a board whose current pulse is over the limit (older firmware doesn't always stop
        that), so live ink never uses it and a test can't put it back afterwards, or from one on another clock."""
        self._check_board_clock(gcmd, name, st)
        if st.get('pulse_words') is not None and pulse_limit_error(st['pulse_words']):
            raise gcmd.error("%s: the board is set to an over-limit pulse: %s. Set a shorter one first, e.g. "
                             "POLYDYE_CARTRIDGE PULSE=%d" % (name, pulse_limit_error(st['pulse_words']), PULSE_MAX))

    def cmd_POLYDYE_ENABLE(self, gcmd):
        live = gcmd.get_int('LIVE', 1, minval=0, maxval=1)
        self._enable(gcmd, "POLYDYE_ENABLE", live)
        gcmd.respond_info("PolyDye boost on, %s" % ("live ink" if live else "dry run (no firing)",))

    def _enable(self, gcmd, cmd, live, settle=True):
        """Boost on, live ink on or off. With `settle`, the moves after it wait for the boost to settle if it was
        off."""
        printing = self._printing(self.printer.get_reactor().monotonic())
        # A print that turned live ink on has ended, or a test's cleanup failed, but the status poll hasn't turned live
        # ink off yet: do it now, so it can't come later and turn off what's enabled here (a fire test, say).
        self._check_print_end(self.printer.get_reactor().monotonic())
        boost_was_on = True
        if not live:
            self.prewarm_gen += 1  # live ink off drops the board's bursts, and one not sent yet
        if live:
            st = self._fresh_status()
            self._check_current_pulse(gcmd, cmd, st)
            boost_was_on = st['boost']
        self.mode_cmd.send([self.oid, 1, live])
        # Live ink turned on by a print: turned off again however the print ends (_check_print_end()). Turned off, or
        # on outside a print: not the print's.
        self.print_live = bool(live and printing)
        if not boost_was_on:
            # The rails need a moment after the boost turns on, and the board refuses live ink that comes sooner:
            # keep the next moves (and so the next pass) far enough out, or (not `settle`: the moves go on) just the
            # next pass (boost_ready).
            self.boost_ready = self.mcu.estimated_print_time(self.printer.get_reactor().monotonic()) + BOOST_SETTLE
            if settle:
                self._wait_for_boost()

    def _wait_for_boost(self):
        """Keeps the moves after this from starting before the boost has settled (boost_ready)."""
        if self.boost_ready is None:
            return
        toolhead = self.printer.lookup_object('toolhead')
        wait = self.boost_ready - toolhead.get_last_move_time()
        if wait > 0.:
            toolhead.dwell(wait)

    cmd_POLYDYE_DISABLE_help = "Wait for queued moves, then turn off PolyDye live output and the boost"

    def cmd_POLYDYE_DISABLE(self, gcmd):
        self.printer.lookup_object('toolhead').wait_moves()
        self.prewarm_gen += 1
        self.mode_cmd.send([self.oid, 0, 0])
        self.print_live = False
        gcmd.respond_info("PolyDye off")

    cmd_POLYDYE_CANCEL_help = "Drop PolyDye passes that are queued but not started"

    def cmd_POLYDYE_CANCEL(self, gcmd):
        self.prewarm_gen += 1  # a POLYDYE_PREWARM whose moves aren't planned yet is cancelled too
        self.cancel_cmd.send([self.oid])
        # The passes dropped never fired, but last_fire counted them: forget it, so the next live pass warms the head.
        self.last_fire = None

    cmd_POLYDYE_STATUS_help = "Report PolyDye board status"

    def cmd_POLYDYE_STATUS(self, gcmd):
        # Fresh, not the cached answer: right after connecting, the first background query may still be in flight
        # with nothing cached yet.
        st = self._fresh_status()
        level = st.get('level')
        gcmd.respond_info(
            "PolyDye: boost=%d live=%d running=%d queued=%d passes=%d late_max=%.1fus faults=%d"
            " stored_passes=%d store_free=%dkB\n%s\n%s\npass_axis=%s %s dpi=%.0f pass_speed=%.1f%s"
            % (st['boost'], st['live'], st['running'], st['queued'], st['passes'], st['late_max_us'],
               st['faults'], st['store_count'], st['store_free'] // 1024, cartridge_text(st), self._prewarm_text(),
               self.pass_axis, self._offsets_text(), self.dpi, self.pass_speed,
               "\nsignal check: %s held %s, %d s left" % (level['line'], level['state'], level['remaining'])
               if level else ""))

    def _prewarm_text(self):
        """The last prewarm burst, for POLYDYE_STATUS."""
        if self.prewarm_cmd is None:
            return "prewarm bursts: need newer board firmware"
        if self.prewarm_sent is None:
            return "prewarm burst: none since Klipper started"
        st = self._query_prewarm()
        now = self.mcu.estimated_print_time(self.printer.get_reactor().monotonic())
        fired = st['columns'] * self.warm_period_us / 1e6
        if st['state'] in ('queued', 'firing'):
            return "prewarm burst: %s (%.1f s asked for)" % (st['state'], self.prewarm_sent['seconds'])
        if st['state'] == 'skipped':
            return "prewarm burst: skipped (live ink off, a spit test, or a pass already there)"
        return "prewarm burst: %s after %.1f s, %.0f s ago" % (st['state'], fired, max(0., now - st['end']))

    def _offsets_text(self):
        return "offset_x=%.3f offset_y=%.3f direction_trim=%.3f %s nozzle_skew=%g" % (
            self.offset_x, self.offset_y, self.direction_trim,
            " ".join("%s=%d" % kv for kv in zip(REG_KEYS, self.registration)), self.nozzle_skew)

    def _skew(self):
        """skew_shifts() for nozzle_skew at this dpi and nozzle_dpi."""
        return skew_shifts(self.nozzle_skew, self.dpi, self.nozzle_dpi)

    def _respond_offsets(self, gcmd):
        gcmd.respond_info("PolyDye: %s (until the next restart: POLYDYE_SAVE, Store on the PolyDye page, or edit "
                          "polydye.cfg to keep them)" % (self._offsets_text(),))

    def _x_tick_mm(self):
        return OFFSET_X_TICK * 25.4 / self.nozzle_dpi

    def _y_tick_mm(self):
        return offset_y_tick(self.dpi / 25.4) * 25.4 / self.dpi

    cmd_POLYDYE_OFFSET_help = ("Set the cartridge offset (X, Y) and direction trim (TRIM) in mm, the colour "
                               "registration (CYAN_COLUMNS, CYAN_POSITIONS, YELLOW_COLUMNS, YELLOW_POSITIONS) and the "
                               "nozzle row's skew (SKEW) from the next pass on, or move the ink by the offset print's "
                               "readings (X_TICK, Y_TICK; stored in polydye.cfg unless SAVE=0)")

    def cmd_POLYDYE_OFFSET(self, gcmd):
        cmd = "POLYDYE_OFFSET"
        # Parse everything before changing anything, so a bad value leaves the offsets alone.
        x_tick = gcmd.get_float('X_TICK', None, minval=-OFFSET_X_TICKS - 1., maxval=OFFSET_X_TICKS + 1.)
        y_tick = gcmd.get_float('Y_TICK', None, minval=-OFFSET_Y_TICKS - 1., maxval=OFFSET_Y_TICKS + 1.)
        ticks = x_tick is not None or y_tick is not None
        if ticks and self.pass_axis != 'y':
            raise gcmd.error("%s: X_TICK and Y_TICK are POLYDYE_OFFSET_PRINT's readings, for pass_axis: y" % (cmd,))
        for name, tick in (('X', x_tick), ('Y', y_tick)):
            if tick is not None and gcmd.get(name, None) is not None:
                raise gcmd.error("%s: give %s or %s_TICK, not both" % (cmd, name, name))
        x = gcmd.get_float('X', self.offset_x)
        y = gcmd.get_float('Y', self.offset_y)
        trim = gcmd.get_float('TRIM', self.direction_trim)
        registration = tuple(
            gcmd.get_int(key.upper(), now, minval=-limit, maxval=limit) for key, now, limit in
            zip(REG_KEYS, self.registration, (REG_MAX_COLUMNS, REG_MAX_POSITIONS, REG_MAX_COLUMNS, REG_MAX_POSITIONS)))
        skew = gcmd.get_float('SKEW', self.nozzle_skew, minval=-SKEW_MAX, maxval=SKEW_MAX)
        self._check_no_job(gcmd, cmd, registration, skew)
        save = gcmd.get_int('SAVE', 1 if ticks else 0, minval=0, maxval=1)
        # The tick in the groove is where the ink landed against where it should be: move it back by that many ticks.
        dx, dy = (x_tick or 0.) * self._x_tick_mm(), (y_tick or 0.) * self._y_tick_mm()
        x, y, trim = nudge_offsets(x, y, trim, dx, dy, 0.)
        # Stored first: if polydye.cfg can't be written nothing changes, so the readings can simply be entered again.
        if save:
            self._save(gcmd, cmd, (x, y, trim, registration, skew))
        self.offset_x, self.offset_y, self.direction_trim, self.registration = x, y, trim, registration
        self.nozzle_skew = skew
        if ticks:
            gcmd.respond_info("PolyDye: ink moved %+.3f mm in X (%+g ticks) and %+.3f mm in Y (%+g ticks)"
                              % (dx, x_tick or 0., dy, y_tick or 0.))
        if not save:
            self._respond_offsets(gcmd)

    cmd_POLYDYE_ALIGN_help = ("Enter the alignment page's readings (the block number where each row lines up: CYAN, "
                              "YELLOW, DIRECTION, CYAN_X, YELLOW_X) to correct the colour registration and the "
                              "direction trim; stored in polydye.cfg unless SAVE=0")

    def cmd_POLYDYE_ALIGN(self, gcmd):
        cmd = "POLYDYE_ALIGN"
        limit = ALIGN_K + 0.5
        read = dict((name, gcmd.get_float(name, None, minval=-limit, maxval=limit))
                    for name in ('CYAN', 'YELLOW', 'DIRECTION', 'CYAN_X', 'YELLOW_X'))
        if all(v is None for v in read.values()):
            raise gcmd.error("%s: give the alignment page's readings, the block number where each row's marks line "
                             "up: CYAN= YELLOW= (rows C, Y), DIRECTION= (row D), CYAN_X= YELLOW_X= (rows CX, YX)"
                             % (cmd,))
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        registration = list(self.registration)
        for n, name, step in ((0, 'CYAN', 3), (1, 'CYAN_X', 1), (2, 'YELLOW', 3), (3, 'YELLOW_X', 1)):
            if read[name] is not None:
                registration[n] += int(round(step * read[name]))
        for key, v, most in zip(REG_KEYS, registration,
                                (REG_MAX_COLUMNS, REG_MAX_POSITIONS, REG_MAX_COLUMNS, REG_MAX_POSITIONS)):
            if abs(v) > most:
                raise gcmd.error("%s: %s would be %d, beyond +-%d; check the reading" % (cmd, key, v, most))
        self._check_no_job(gcmd, cmd, tuple(registration), self.nozzle_skew)
        trim = self.direction_trim
        if read['DIRECTION'] is not None:
            trim = round(trim + 2. * read['DIRECTION'] * 25.4 / self.dpi, 4)
        # Stored first: if polydye.cfg can't be written nothing changes, so the readings can simply be entered again.
        if save:
            self._save(gcmd, cmd, (self.offset_x, self.offset_y, trim, tuple(registration), self.nozzle_skew))
        self.registration, self.direction_trim = tuple(registration), trim
        if not save:
            self._respond_offsets(gcmd)

    def _check_no_job(self, gcmd, cmd, registration, skew):
        """Refuses a new colour registration or nozzle skew while an ink job is loaded: its passes are made (some
        already uploaded) with the ones in use."""
        if self.job is not None and (tuple(registration) != tuple(self.registration) or skew != self.nozzle_skew):
            raise gcmd.error("%s: an ink job is loaded, and its passes are made with the colour registration and "
                             "nozzle skew in use: change them between prints (POLYDYE_JOB_END unloads it)" % (cmd,))

    cmd_POLYDYE_SAVE_help = ("Store the live cartridge offsets, direction trim, colour registration and nozzle skew in "
                             "polydye.cfg (next to printer.cfg)")

    def cmd_POLYDYE_SAVE(self, gcmd):
        self._save(gcmd, "POLYDYE_SAVE", (self.offset_x, self.offset_y, self.direction_trim, self.registration,
                                          self.nozzle_skew))

    def _save(self, gcmd, cmd, values):
        """Writes `values` (offset_x, offset_y, direction_trim, colour registration, nozzle_skew) to the [polydye]
        section of polydye.cfg (cfg_with()), safely: a new file, synced, then renamed over the old one. The registration
        and skew go in once they aren't zero or polydye.cfg already has them. Raises if it can't."""
        offset_x, offset_y, trim, registration, skew = values
        config_file = self.printer.get_start_args().get('config_file')
        if not config_file:
            raise gcmd.error("%s: Klipper didn't say where its configuration is" % (cmd,))
        path = os.path.realpath(os.path.join(os.path.dirname(config_file), 'polydye.cfg'))
        values = {'offset_x': cfg_number(offset_x), 'offset_y': cfg_number(offset_y),
                  'direction_trim': cfg_number(trim)}
        tmp = path + '.polydye-tmp'
        try:
            with open(path) as f:
                text = f.read()
            for key, v in zip(REG_KEYS, registration):
                if v or cfg_value(text, key) is not None:
                    values[key] = "%d" % (v,)
            if skew or cfg_value(text, 'nozzle_skew') is not None:
                values['nozzle_skew'] = cfg_number(skew)
            updated = cfg_with(text, values)
            with open(tmp, 'w') as f:
                f.write(updated)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
            os.replace(tmp, path)
        except (OSError, ValueError) as e:
            raise gcmd.error("%s: couldn't store the values in %s: %s" % (cmd, path, e))
        os.sync()  # a Raspberry Pi 3 has lost files written just before it froze
        self.cfg_saves += 1
        gcmd.respond_info("PolyDye: stored in %s: %s" % (path, ", ".join("%s %s" % kv for kv in values.items())))

    # ---- calibration prints ------------------------------------------------------------------------------------------

    def _data_sync(self, ftype, payload=b"", rate=None, stopped=None):
        """A data port request (data_port_request()) from any thread, one at a time; raises JobStopped instead if the
        event `stopped` (a job's) is set by the time it's this request's turn."""
        with self.data_lock:
            if stopped is not None and stopped.is_set():
                raise JobStopped()
            self.data_seq = (self.data_seq + 1) & 0xff
            return data_port_request(self.data_port, self.data_seq, ftype, payload, rate=rate)

    def _in_thread(self, fn):
        """fn() in a thread while Klipper's reactor keeps running (a big pass takes seconds to go up, longer than
        heaters and MCUs can wait): its result, or its error raised."""
        result = {}

        def work():
            try:
                result['reply'] = fn()
            except Exception as e:
                result['error'] = e
        worker = threading.Thread(target=work, name="polydye-data")
        worker.daemon = True
        worker.start()
        reactor = self.printer.get_reactor()
        while worker.is_alive():
            reactor.pause(reactor.monotonic() + 0.05)
        if 'error' in result:
            raise result['error']
        return result['reply']

    def _data_request(self, ftype, payload=b"", rate=None):
        """A data port request in a thread while Klipper's reactor keeps running (_in_thread())."""
        return self._in_thread(lambda: self._data_sync(ftype, payload, rate))

    def _upload_cal(self, gcmd, cmd, passes):
        """Stores each (data, columns) of `passes` on the board as layer CAL_LAYER, passes 0, 1, ..., for the colour
        cartridge."""
        gcmd.respond_info("PolyDye: sending %d passes to the board (%d kB)..."
                          % (len(passes), sum(len(data) for data, _ in passes) // 1024))
        try:
            hello = self._data_request(DATA_HELLO)
            if hello.get('cart') == 'black':
                raise ValueError("the board is set for a black cartridge; the calibration prints are for the colour "
                                 "cartridge")
            for n, (data, columns) in enumerate(passes):
                self._data_request(DATA_PUT, struct.pack("<HHBBH", CAL_LAYER, n, 0, 0, columns) + data,
                                   CAL_UPLOAD_RATE)
        except (DataPortError, OSError, ValueError) as e:
            raise gcmd.error("%s: %s" % (cmd, e))

    def _cal_start(self, gcmd, cmd, axes):
        """Checks before a calibration print, before anything moves: board firmware and data port, not during a print,
        `axes` homed, and the board idle at a pulse within the limit with a colour template. Returns its status."""
        if self.query_cartridge_cmd is None:
            raise gcmd.error("%s: the PolyDye board firmware is too old; update it from the PolyDye page (Board "
                             "firmware)" % (cmd,))
        if not self.data_port:
            raise gcmd.error("%s: set data_port in [polydye]" % (cmd,))
        self._check_not_printing(gcmd, cmd)
        toolhead = self.printer.lookup_object('toolhead')
        homed = toolhead.get_status(self.printer.get_reactor().monotonic())['homed_axes']
        missing = [a for a in axes if a not in homed]
        if missing:
            raise gcmd.error("%s: home %s first (G28)" % (cmd, "".join(missing).upper()))
        toolhead.wait_moves()
        st = self._fresh_status()
        if st['running'] or st['queued']:
            raise gcmd.error("%s: wait for queued passes to finish" % (cmd,))
        self._check_current_pulse(gcmd, cmd, st)
        if (st.get('cartridge_template') or '').startswith('black'):
            raise gcmd.error("%s: is for the colour cartridge, but the board is set to %s (POLYDYE_CARTRIDGE "
                             "TEMPLATE=color_b_late)" % (cmd, st['cartridge_template']))
        return st

    def _ink_centre(self, axis):
        """The middle of where the cartridge can put ink along `axis` (0: X, 1: Y) within that axis's own range (the
        bed), in bed coordinates: where a calibration print goes by default."""
        th = self.printer.lookup_object('toolhead').get_status(self.printer.get_reactor().monotonic())
        lo = (th['axis_minimum'].x, th['axis_minimum'].y)[axis]
        hi = (th['axis_maximum'].x, th['axis_maximum'].y)[axis]
        off = (self.offset_x, self.offset_y)[axis]
        return (max(lo, lo + off) + min(hi, hi + off)) / 2.

    def _cal_sponge(self, gcmd, cmd):
        """Where a calibration print warms the head: (X, Z hop) of the wipe sponge from POLYDYE_WIPE, or None when it
        doesn't warm (the automatic warm-up off, or board firmware without it). Refuses, before anything moves, without
        the macro or with its X beyond the X axis."""
        if not (self.warm_seconds and self.spit_run_cmd is not None and self.warm_duty_ppm is not None):
            return None
        return self._sponge_spot(gcmd, cmd, "the head warms over the wipe sponge")

    def _sponge_spot(self, gcmd, cmd, what):
        """(X, Z hop) of the wipe sponge from POLYDYE_WIPE; refuses (`what` says why it's needed) without the macro or
        with its X beyond the X axis (an axis without a range, kinematics: none for testing, has no limits)."""
        macro = self.printer.lookup_object('gcode_macro POLYDYE_WIPE', None)
        variables = getattr(macro, 'variables', {}) if macro is not None else {}
        if 'x' not in variables:
            raise gcmd.error("%s: %s, but there's no POLYDYE_WIPE macro to say where it is: include "
                             "polydye_macros.cfg" % (cmd, what))
        x = float(variables['x'])
        th = self.printer.lookup_object('toolhead').get_status(self.printer.get_reactor().monotonic())
        base = self.printer.lookup_object('gcode_move').base_position
        # Below the axis maximum, like POLYDYE_WIPE: that is the hard stop.
        low, high = th['axis_minimum'].x, th['axis_maximum'].x
        if high > low and not low <= x + base[0] < high:
            raise gcmd.error("%s: the wipe sponge's X (POLYDYE_WIPE, %.1f) must be within the X axis, below its "
                             "maximum" % (cmd, x))
        return x, float(variables.get('z_hop', 1.))

    def _check_reach(self, gcmd, cmd, prints, columns, accel=None):
        """Refuses, before anything moves, passes of `prints` ((pass, X, Y, direction)) that would take the toolhead
        beyond the axis limits; columns[pass] is each pass's length. `accel`: the passes run at most that acceleration
        (POLYDYE_INK_MOTION), so with longer run-ups."""
        th = self.printer.lookup_object('toolhead').get_status(self.printer.get_reactor().monotonic())
        if accel:
            th = dict(th, max_accel=min(th['max_accel'], accel))
        base = self.printer.lookup_object('gcode_move').base_position
        lo, hi = th['axis_minimum'], th['axis_maximum']
        pitch = 25.4 / self.dpi
        for n, x, y, direction in prints:
            run_up = self._run_up(self.pass_speed, th, columns[n])
            start, _, end = pass_geometry(self.pass_axis, x, y, direction, columns[n], pitch, run_up,
                                          (self.offset_x, self.offset_y), base, self.direction_trim)
            for px, py in (start, end):
                if not (lo.x <= px <= hi.x and lo.y <= py <= hi.y):
                    raise gcmd.error("%s: the pass at X=%.1f Y=%.1f would take the toolhead to X=%.1f Y=%.1f, beyond "
                                     "the axis limits; choose another X, Y" % (cmd, x, y, px, py))

    def _run_cal(self, gcmd, cmd, st, prints, sponge, setup=None, z=None, warm=None, ink_motion=False):
        """Prints `prints` ((pass, X, Y, direction) of layer CAL_LAYER) with live ink, after the G-code `setup` (the
        plaque), a move to `z` and, with `sponge` ((X, Z hop) of the wipe sponge), a head warm-up over the sponge: up by
        its Z hop (within the Z axis), across, warm, back over the first pass at that height and down. The warm-up has
        the automatic warm-up's settings, or with `warm` (seconds of warm-up, seconds of prime) that long with its pulse
        and period, then that long of the stock pattern at the template and pulse the passes print with, in the same
        board run (POLYDYE_SPIT WARM=). The passes don't warm again; with `ink_motion` they move gently
        (POLYDYE_INK_MOTION, its own: one lowered from the console before carries on afterwards, from the limits it
        set). Afterwards, also after an error, the motion limits are put back, live ink goes off
        (_restore_after_test()) and the G-code state back, and after a plaque the hotend and the part fan are off."""
        toolhead = self.printer.lookup_object('toolhead')
        run = self.printer.lookup_object('gcode').run_script_from_command
        before = (st['cartridge_template'], st['pulse_words'])
        lowered, outer = False, self.ink_motion
        run("SAVE_GCODE_STATE NAME=polydye_cal")
        try:
            if setup:
                run(setup)
            if z is not None:
                run("G90\nG1 Z%.3f F900" % (z,))
            if sponge is not None:
                eventtime = self.printer.get_reactor().monotonic()
                gm = self.printer.lookup_object('gcode_move').get_status(eventtime)
                here = gm['gcode_position'].z
                top = toolhead.get_status(eventtime)['axis_maximum'].z - (gm['position'].z - here)
                up = max(here, min(here + sponge[1], top))
                run("G90\nG1 Z%.3f F900\nG1 X%.3f F%d" % (up, sponge[0], self.travel_speed * 60))
                seconds, spit = warm or (min(self.warm_seconds, self.warm_max_s), 0.)
                if spit:
                    run("POLYDYE_SPIT SECONDS=%g WARM=%g WARM_PULSE=%d WARM_PERIOD_US=%d%s CONFIRM=1"
                        % (spit, seconds, self.warm_pulse, self.warm_period_us,
                           "" if None in before else " TEMPLATE=%s PULSE=%d" % before))
                else:
                    run("POLYDYE_WARM SECONDS=%g WARM_PULSE=%d WARM_PERIOD_US=%d CONFIRM=1"
                        % (seconds, self.warm_pulse, self.warm_period_us))
                n, x, y, direction = prints[0]
                run("G1 X%.3f Y%.3f F%d\nG1 Z%.3f F900" % (x - self.offset_x, y - self.offset_y,
                                                           self.travel_speed * 60, here))
            run("POLYDYE_ENABLE LIVE=1")
            if ink_motion:
                lowered, self.ink_motion = True, None
                run("POLYDYE_INK_MOTION")
            for i, (n, x, y, direction) in enumerate(prints):
                if i and i % 4 == 0:
                    toolhead.wait_moves()  # the board queues at most 8 passes
                run("POLYDYE_PASS LAYER=%d PASS=%d X=%.3f Y=%.3f DIR=%d WARM=0" % (CAL_LAYER, n, x, y, direction))
        finally:
            if lowered and not self.printer.is_shutdown():
                try:
                    run("POLYDYE_INK_MOTION RESTORE=1")
                except Exception:
                    logging.exception("polydye: %s couldn't put the motion limits back", cmd)
                self.ink_motion = outer
            if setup and not self.printer.is_shutdown():
                try:
                    run("M104 S0\nM106 S0")
                except Exception:
                    logging.exception("polydye: %s couldn't turn the hotend and the part fan off", cmd)
            self._restore_after_test(before)
            if not self.printer.is_shutdown():
                try:
                    run("RESTORE_GCODE_STATE NAME=polydye_cal")
                except Exception:
                    logging.exception("polydye: %s couldn't restore the G-code state", cmd)

    cmd_POLYDYE_ALIGN_PRINT_help = ("LIVE INK, on paper only (taped over the glass, the page and a margin): the "
                                    "alignment page, vernier rows for the colour registration along (C, Y) and across "
                                    "(CX, YX) the pass and the direction trim (D), SPACING mm apart from X, Y, at Z if "
                                    "given; enter the block number where each row lines up with POLYDYE_ALIGN")

    def cmd_POLYDYE_ALIGN_PRINT(self, gcmd):
        cmd = "POLYDYE_ALIGN_PRINT"
        spacing = gcmd.get_float('SPACING', ALIGN_SPACING, minval=5.)
        z = gcmd.get_float('Z', None, minval=0.2)
        pitch = 25.4 / self.dpi
        a = 0 if self.pass_axis == 'x' else 1  # the pass axis
        along = self._ink_centre(a) - ALIGN_COLUMNS * pitch / 2.
        across = self._ink_centre(1 - a) - (len(ALIGN_ROWS) - 1) * spacing / 2.
        x = gcmd.get_float('X', along if a == 0 else across)
        y = gcmd.get_float('Y', across if a == 0 else along)
        st = self._cal_start(gcmd, cmd, 'xyz' if z is not None else 'xy')
        drawings = align_drawings()
        built = [pass_data(d, direction, self.registration, self._skew()) for _, d, direction in drawings]
        index = dict((row, n) for n, (row, _, _) in reversed(list(enumerate(drawings))))
        start, side = (y, x) if a == 1 else (x, y)
        prints = []
        for r, row in enumerate(ALIGN_ROWS):
            for name in (('D', 'D-') if row == 'D' else (row,)):
                n = index[name]
                first = start + built[n][2] * pitch
                cross = side + r * spacing
                prints.append((n, cross, first, drawings[n][2]) if a == 1 else (n, first, cross, drawings[n][2]))
        paper = self._align_paper(gcmd, cmd, drawings, built, start, side, spacing)
        sponge = self._cal_sponge(gcmd, cmd)
        self._check_reach(gcmd, cmd, prints, [b[1] for b in built])
        self._upload_cal(gcmd, cmd, [(b[0], b[1]) for b in built])
        gcmd.respond_info("PolyDye alignment page, live ink on paper: %d rows %.0f mm apart from X=%.1f Y=%.1f. Its "
                          "ink spans %s: the paper must cover that with %g mm to spare, at least %.0f x %.0f mm"
                          % (len(ALIGN_ROWS), spacing, x, y, paper, ALIGN_PAPER_MARGIN, ALIGN_PAPER[0],
                             ALIGN_PAPER[1]))
        self._run_cal(gcmd, cmd, st, prints, sponge, z=z)
        gcmd.respond_info(
            "PolyDye alignment page done. In each row find the block where its marks line up best (C, Y: the colour "
            "bar on the magenta one; D: the two magenta bars; CX, YX: the two lines make one) and read its number "
            "off the label rows; halfway between two blocks reads as e.g. -11.5. Then\n"
            "  POLYDYE_ALIGN CYAN=<C> YELLOW=<Y> DIRECTION=<D> CYAN_X=<CX> YELLOW_X=<YX>\n"
            "corrects and stores them; print the page again to check that every row reads 0.")

    def _align_paper(self, gcmd, cmd, drawings, built, start, side, spacing):
        """The bed area the alignment page's ink can cover (all rows, the nozzle row at 10 % more than nozzle_dpi, the
        priming blocks, the reverse pass moved by direction_trim), as text, where it is on the bed (with the G-code
        offset, as pass_geometry() puts it); refuses, before anything moves, settings that need more paper than
        ALIGN_PAPER with ALIGN_PAPER_MARGIN all round."""
        pitch, wide = 25.4 / self.dpi, 25.4 / self.nozzle_dpi * 1.1
        ends = [(b[2] * pitch, (b[2] + b[1] - 1) * pitch) if d[2] > 0 else
                ((b[2] - b[1] + 1) * pitch + self.direction_trim, b[2] * pitch + self.direction_trim)
                for b, d in zip(built, drawings)]
        along = (start + min(e[0] for e in ends), start + max(e[1] for e in ends))
        extents = [drawing_extent(d) for _, d, _ in drawings]
        half = max(max(e[3] for e in extents) - OFFSET_REF_POSITION,
                   OFFSET_REF_POSITION - min(e[2] for e in extents)) * wide
        across = (side - half, side + (len(ALIGN_ROWS) - 1) * spacing + half)
        need = (across[1] - across[0] + 2 * ALIGN_PAPER_MARGIN, along[1] - along[0] + 2 * ALIGN_PAPER_MARGIN)
        if need[0] > ALIGN_PAPER[0] or need[1] > ALIGN_PAPER[1]:
            raise gcmd.error("%s: at SPACING %g and dpi %g the page would need %.0f x %.0f mm of paper (with %g mm "
                             "margins), more than the %.0f x %.0f mm it asks for: use a smaller SPACING, or the "
                             "default dpi" % (cmd, spacing, self.dpi, need[0], need[1], ALIGN_PAPER_MARGIN,
                                              ALIGN_PAPER[0], ALIGN_PAPER[1]))
        base = self.printer.lookup_object('gcode_move').base_position
        x, y = (across, along) if self.pass_axis == 'y' else (along, across)
        return "X %.0f-%.0f, Y %.0f-%.0f" % (x[0] + base[0], x[1] + base[0], y[0] + base[1], y[1] + base[1])

    cmd_POLYDYE_TEST_PAGE_help = ("LIVE INK, on paper only: the test page, whether the head prints the way a colour "
                                  "print needs: a nozzle check and a solid square per colour, red, green and blue "
                                  "squares and two sweeps' feathered join, centred on X, Y, at Z (1), after a WARM s "
                                  "(prewarm_seconds) warm-up and a PRIME s (prime_seconds) prime over the wipe sponge")

    def cmd_POLYDYE_TEST_PAGE(self, gcmd):
        cmd = "POLYDYE_TEST_PAGE"
        if self.pass_axis != 'y':
            raise gcmd.error("%s: needs pass_axis: y, like the colour prints it checks" % (cmd,))
        z = gcmd.get_float('Z', TEST_Z, minval=0.2)
        warm = gcmd.get_float('WARM', self.prewarm_seconds, minval=0., maxval=self.warm_max_s)
        prime = gcmd.get_float('PRIME', self.prime_seconds, minval=0., maxval=PRESPIT_MAX_S)
        if (warm and self.spit_run_cmd is None) or (prime and 'stock' not in self.spit_patterns):
            raise gcmd.error("%s: warms and primes the head the way a colour print does, which needs newer PolyDye "
                             "board firmware; update it from the PolyDye page (Board firmware: Update to bundled "
                             "firmware)" % (cmd,))
        if warm > self.warm_max_s + 1e-9:  # prewarm_seconds, which polydye.cfg bounds only by the newest board's
            raise gcmd.error("%s: a %g s warm-up is longer than the board takes (%g s): set WARM, or a shorter "
                             "prewarm_seconds" % (cmd, warm, self.warm_max_s))
        if warm:
            self._warm_plan(gcmd, cmd, warm, self.warm_pulse, self.warm_period_us)
        lo, hi = TEST_POSITIONS
        reg = self.registration
        moves = (reg[1], reg[3], 0)
        if lo + min(moves) < JOB_NOZZLES[0] or hi + max(moves) > JOB_NOZZLES[1]:
            raise gcmd.error("%s: the colour registration (cyan_positions %d, yellow_positions %d) moves the page's "
                             "squares, on positions %d-%d, beyond the %d-%d the cartridge fires"
                             % (cmd, reg[1], reg[3], lo, hi, JOB_NOZZLES[0], JOB_NOZZLES[1]))
        x = gcmd.get_float('X', None)
        y = gcmd.get_float('Y', None)
        st = self._cal_start(gcmd, cmd, 'xyz')
        x = self._ink_centre(0) if x is None else x
        y = self._ink_centre(1) if y is None else y
        sponge = self._sponge_spot(gcmd, cmd, "the head warms and primes over the wipe sponge") if warm or prime \
            else None
        pitch, nozzle, wide = 25.4 / self.dpi, 25.4 / self.nozzle_dpi, 25.4 / self.nozzle_dpi * 1.1
        skew = self._skew()
        built, layout, across = [], [], []
        for d, direction, px in test_page_drawings(self.dpi, self.nozzle_dpi, reg):
            p0, p1 = min(p for _, _, p in d.dots), max(p for _, _, p in d.dots)
            across.append((px, p0, p1))
            for data, columns, first in drawing_parts(d, direction, reg, skew):
                ends = (first, first + columns - 1) if direction > 0 else (first - columns + 1, first)
                layout.append((len(built), px, first, direction, ends))
                built.append((data, columns))
        # Centred on X, Y; the paper it needs with the nozzle row up to 10 % wider than nozzle_dpi, and the reverse
        # passes' ink moved by direction_trim.
        x0 = min(px - (p1 - SKEW_PIVOT) * nozzle for px, p0, p1 in across)
        x1 = max(px + (SKEW_PIVOT - p0) * nozzle for px, p0, p1 in across)
        dx = x - (x0 + x1) / 2.
        dy = y - (min(e[0] for *_, e in layout) + max(e[1] for *_, e in layout)) * pitch / 2.
        prints = [(n, px + dx, dy + first * pitch, direction) for n, px, first, direction, _ in layout]
        self._check_reach(gcmd, cmd, prints, [b[1] for b in built], accel=INK_ACCEL)
        base = self.printer.lookup_object('gcode_move').base_position
        trim = [self.direction_trim if direction < 0 else 0. for _, _, _, direction, _ in layout]
        paper = (min(px - (p1 - SKEW_PIVOT) * wide for px, p0, p1 in across) + dx + base[0],
                 max(px + (SKEW_PIVOT - p0) * wide for px, p0, p1 in across) + dx + base[0],
                 min(dy + e[0] * pitch + t for (*_, e), t in zip(layout, trim)) + base[1],
                 max(dy + e[1] * pitch + t for (*_, e), t in zip(layout, trim)) + base[1])
        self._upload_cal(gcmd, cmd, built)
        gcmd.respond_info("PolyDye test page, live ink on paper: %d passes (%d on the board) centred on X=%.1f Y=%.1f, "
                          "Z=%.2f. Its ink spans X %.0f-%.0f, Y %.0f-%.0f: the paper must cover that with %g mm to "
                          "spare" % (len(across), len(built), x, y, z, paper[0], paper[1], paper[2], paper[3],
                                     ALIGN_PAPER_MARGIN))
        self._run_cal(gcmd, cmd, st, prints, sponge, z=z, warm=(warm, prime), ink_motion=True)
        gcmd.respond_info(
            "PolyDye test page done. Seen from the front, left to right: cyan, magenta and yellow, each with its "
            "nozzle check at the front (9 staircases of 8 dashes, one per nozzle 14-85: a missing or faint dash is a "
            "nozzle that didn't fire; every staircase should look alike) and a solid square behind it; red, green "
            "and blue squares at the back (two inks, each printed as two passes, one per fire line); on the right two "
            "sweeps of blue whose join should show no line. Pale or streaky squares: a cold head or a weak fire line; "
            "a line at the join: check direction_trim (alignment page, row D).")

    cmd_POLYDYE_OFFSET_PRINT_help = ("PLA and LIVE INK: a PLA plaque with a cross-shaped groove centred on X, Y "
                                     "(LAYERS of LAYER_HEIGHT, the groove in the top 3; heats to EXTRUDER_TEMP and "
                                     "BED_TEMP if given, else needs the hotend's target set; the hotend is off "
                                     "afterwards), then ink scales across the groove from GAP mm above the plaque; "
                                     "enter the tick in the groove on each scale with POLYDYE_OFFSET X_TICK= Y_TICK=. "
                                     "PLAQUE=0: only the ink, onto a plaque printed before (at its top plus GAP, or Z)")

    def cmd_POLYDYE_OFFSET_PRINT(self, gcmd):
        cmd = "POLYDYE_OFFSET_PRINT"
        if self.pass_axis != 'y':
            raise gcmd.error("%s: needs pass_axis: y (its X scale is drawn along the nozzle row, across X)" % (cmd,))
        plaque = gcmd.get_int('PLAQUE', 1, minval=0, maxval=1)
        layers = gcmd.get_int('LAYERS', OFFSET_LAYERS, minval=OFFSET_GROOVE_LAYERS + 1, maxval=12)
        height = gcmd.get_float('LAYER_HEIGHT', OFFSET_LAYER_HEIGHT, minval=0.05, maxval=0.6)
        gap = gcmd.get_float('GAP', OFFSET_GAP, minval=0.2, maxval=5.)
        # With the plaque the ink goes GAP above its top: a lower Z would drive the hotend into it.
        if plaque and gcmd.get('Z', None) is not None:
            raise gcmd.error("%s: Z goes with PLAQUE=0; with the plaque, GAP sets the height above it" % (cmd,))
        # PLAQUE=0 inks a plaque printed before (or paper, with Z): by default its top plus GAP, like with the plaque.
        z = gcmd.get_float('Z', layers * height + gap, minval=0.2)
        # Every drop on the plaque, OFFSET_MARGIN mm inside its edges, at this dpi and nozzle_dpi.
        ink = offset_ink_extent(self.dpi, self.nozzle_dpi)
        size = OFFSET_PLAQUE
        if not (-size[0] + OFFSET_MARGIN <= ink[0] and ink[2] <= size[1] - OFFSET_MARGIN and
                -size[2] + OFFSET_MARGIN <= ink[1] and ink[3] <= size[3] - OFFSET_MARGIN):
            raise gcmd.error("%s: at dpi %g and nozzle_dpi %g the scales would reach X %.1f to %.1f, Y %.1f to %.1f mm "
                             "around the groove, beyond the plaque less its %g mm margin (X %.1f to %.1f, Y %.1f to "
                             "%.1f): the print is laid out for dpi 847 and nozzle_dpi about 300"
                             % (cmd, self.dpi, self.nozzle_dpi, ink[0], ink[2], ink[1], ink[3], OFFSET_MARGIN,
                                -size[0] + OFFSET_MARGIN, size[1] - OFFSET_MARGIN, -size[2] + OFFSET_MARGIN,
                                size[3] - OFFSET_MARGIN))
        e_temp = gcmd.get_float('EXTRUDER_TEMP', None, minval=0.)
        b_temp = gcmd.get_float('BED_TEMP', None, minval=0.)
        # The plaque spans the groove's centre -OFFSET_PLAQUE[0]..+OFFSET_PLAQUE[1] in X, and so on: centred by default.
        x = gcmd.get_float('X', self._ink_centre(0) - (OFFSET_PLAQUE[1] - OFFSET_PLAQUE[0]) / 2.)
        y = gcmd.get_float('Y', self._ink_centre(1) + (OFFSET_PLAQUE[2] - OFFSET_PLAQUE[3]) / 2.)
        setup = None
        if plaque:
            setup = self._plaque_setup(gcmd, cmd, x, y, layers, height, e_temp, b_temp, z)
        st = self._cal_start(gcmd, cmd, 'xyz')
        pitch = 25.4 / self.dpi
        built = [pass_data(d, 1, self.registration, self._skew()) for d in offset_drawings(self.dpi / 25.4)]
        prints = [(0, x, y + built[0][2] * pitch, 1), (1, x + OFFSET_Y_SCALE_X, y + built[1][2] * pitch, 1)]
        sponge = self._cal_sponge(gcmd, cmd)
        self._check_reach(gcmd, cmd, prints, [b[1] for b in built])
        self._upload_cal(gcmd, cmd, [(b[0], b[1]) for b in built])
        gcmd.respond_info("PolyDye offset print: %sink scales across the groove at X=%.1f Y=%.1f, Z=%.2f"
                          % ("a %d-layer PLA plaque, then " % (layers,) if plaque else "", x, y, z))
        self._run_cal(gcmd, cmd, st, prints, sponge, setup=setup, z=z)
        gcmd.respond_info(
            "PolyDye offset print done. On each scale find the tick in the middle of the groove: the X scale's lines "
            "run along Y, %.3f mm apart; the Y scale's run along X, %.3f mm apart. Tick 0 is the longest, every 4th "
            "is long and every 8th numbered; count from the nearest number (between two ticks reads as e.g. 2.5). "
            "Then\n  POLYDYE_OFFSET X_TICK=<X scale> Y_TICK=<Y scale>\nmoves the ink there and stores offset_x and "
            "offset_y; print again to check that both read 0." % (self._x_tick_mm(), self._y_tick_mm()))

    def _plaque_setup(self, gcmd, cmd, x, y, layers, height, e_temp, b_temp, z):
        """The G-code for POLYDYE_OFFSET_PRINT's plaque around the groove centred on x, y: heating (to e_temp and
        b_temp if given, else to the hotend's current target, which must be enough to extrude), the plaque
        (plaque_gcode(): solid layers, then OFFSET_GROOVE_LAYERS with the groove) and then up to `z` with the hotend
        off, so it doesn't ooze onto the plaque while the ink goes on."""
        eventtime = self.printer.get_reactor().monotonic()
        extruder = self.printer.lookup_object('extruder', None)
        settings = self.printer.lookup_object('configfile').get_status(eventtime)['settings'].get('extruder')
        if extruder is None or settings is None:
            raise gcmd.error("%s: there's no [extruder] for the plaque (PLAQUE=0 prints only the ink)" % (cmd,))
        temp = e_temp if e_temp is not None else extruder.get_status(eventtime)['target']
        if temp < float(settings.get('min_extrude_temp', 170.)):
            raise gcmd.error("%s: heat the hotend for PLA first (e.g. M104 S210) or give EXTRUDER_TEMP" % (cmd,))
        filament_area = math.pi * (float(settings.get('filament_diameter', 1.75)) / 2.) ** 2
        nozzle = float(settings.get('nozzle_diameter', 0.4))
        size = OFFSET_PLAQUE
        solid = [(x - size[0], y - size[2], x + size[1], y + size[3])]
        plaque = [solid] * (layers - OFFSET_GROOVE_LAYERS) + [plaque_rects(x, y)] * OFFSET_GROOVE_LAYERS
        th = self.printer.lookup_object('toolhead').get_status(eventtime)
        base = self.printer.lookup_object('gcode_move').base_position
        lo, hi = th['axis_minimum'], th['axis_maximum']
        x_lo, x_hi = solid[0][0] - 3. + base[0], solid[0][2] + base[0]
        y_lo, y_hi = solid[0][1] + base[1], solid[0][3] + base[1]
        if x_lo < lo.x or x_hi > hi.x or y_lo < lo.y or y_hi > hi.y:
            raise gcmd.error("%s: the plaque (X %.1f-%.1f, Y %.1f-%.1f with its prime line) doesn't fit within the "
                             "axis limits; choose another X, Y" % (cmd, x_lo, x_hi, y_lo, y_hi))
        lines = []
        if b_temp is not None:
            lines.append("M140 S%.0f" % (b_temp,))
        if e_temp is not None:
            lines.append("M104 S%.0f" % (e_temp,))
        if b_temp is not None:
            lines.append("M190 S%.0f" % (b_temp,))
        # The plaque's extrusion and speeds assume 100 %: an earlier M221/M220 would thin it out or leave no plaque
        # under the ink. RESTORE_GCODE_STATE puts the factors back afterwards.
        lines += ["M109 S%.0f" % (temp,), "G90", "M83", "M220 S100", "M221 S100"]
        lines += plaque_gcode(plaque, height, nozzle * 1.125, filament_area, nozzle * 1.25)
        lines += ["G1 Z%.3f F600" % (z,), "M104 S0"]
        return "\n".join(lines)

    # ---- ink jobs (polydye_post.py) ----------------------------------------------------------------------------------

    cmd_POLYDYE_JOB_help = ("Load the PolyDye ink job in the G-code being printed (polydye_post.py puts it there; "
                            "FILE= names another file) and stream its passes to the board; POLYDYE_JOB_PASS prints "
                            "them")

    def cmd_POLYDYE_JOB(self, gcmd):
        cmd = "POLYDYE_JOB"
        if self.query_cartridge_cmd is None:
            raise gcmd.error("%s: the PolyDye board firmware is too old; update it from the PolyDye page (Board "
                             "firmware)" % (cmd,))
        if not self.data_port:
            raise gcmd.error("%s: set data_port in [polydye]" % (cmd,))
        if self.pass_axis != 'y':
            raise gcmd.error("%s: ink jobs are drawn for pass_axis: y, the PolyDye mount's" % (cmd,))
        path = gcmd.get('FILE', None)
        from_print = path is None
        if from_print:
            sdcard = self.printer.lookup_object('virtual_sdcard', None)
            printing = self._printing(self.printer.get_reactor().monotonic())
            path = sdcard.file_path() if sdcard is not None and printing else None
            if not path:
                raise gcmd.error("%s: goes at the start of a print's G-code, whose ink job it loads (FILE= names "
                                 "another G-code file)" % (cmd,))
        else:
            path = os.path.expanduser(path)
        self._stop_job("another job was loaded")
        try:
            header, records = self._in_thread(lambda: job_load(path))
        except (JobError, OSError) as e:
            raise gcmd.error("%s: %s" % (cmd, e))
        self._check_job(gcmd, cmd, header, records)
        # The G-code's bursts and primes: refused now rather than in the middle of the print.
        if header['prewarms'] and self.prewarm_cmd is None:
            raise gcmd.error("%s: the job's prewarm bursts (POLYDYE_PREWARM) need newer PolyDye board firmware; update "
                             "it from the PolyDye page (Board firmware: Update to bundled firmware)" % (cmd,))
        if header['prewarms']:
            seconds, period = header['prewarm_s'] or self.prewarm_seconds, self.warm_period_us
            longest = min(self.warm_max_s, WARM_MAX_COLUMNS * period / 1e6)
            if seconds > self.warm_max_s or warm_columns(seconds, period, self.warm_max_s) > WARM_MAX_COLUMNS:
                raise gcmd.error("%s: the job's prewarm bursts are %g s, longer than the board takes at "
                                 "warm_period_us %d (%.1f s): slice again with --prewarm %g or less"
                                 % (cmd, seconds, period, longest, math.floor(longest * 10) / 10))
        if header['primes']:
            if self.spit_run_cmd is None or 'stock' not in self.spit_patterns:
                raise gcmd.error("%s: the job's primes (POLYDYE_PRIME) need newer PolyDye board firmware; update it "
                                 "from the PolyDye page (Board firmware: Update to bundled firmware)" % (cmd,))
            self._sponge_spot(gcmd, cmd, "the job primes the nozzles over the wipe sponge")
        if self.warm_position == 'sponge':
            self._cal_sponge(gcmd, cmd)  # the automatic warm-up's sponge must be there before the print starts
        largest = max(records, key=lambda rec: rec['size'])
        try:
            if self._data_request(DATA_HELLO).get('cart') == 'black':
                raise ValueError("the board is set for a black cartridge; ink jobs are for the colour cartridge")
            # The passes of an earlier job (a cancelled print's) are dropped: they'd never be printed. Then the room
            # left is the board's.
            self._data_request(DATA_CLEAR)
            free = int(self._data_request(DATA_HELLO).get('free') or 0)
            if free and largest['size'] > free * 0.9:
                raise ValueError("pass %d needs %d kB on the board, which has %d kB free"
                                 % (largest['n'], largest['size'] // 1024, free // 1024))
        except (DataPortError, OSError, ValueError) as e:
            raise gcmd.error("%s: %s" % (cmd, e))
        ahead = max(min(JOB_AHEAD_BYTES, free // 2) if free else JOB_AHEAD_BYTES, largest['size'])
        stopping = threading.Event()
        streamer = JobStreamer(path, records, self.registration,
                               lambda payload: self._data_sync(DATA_PUT, payload, JOB_UPLOAD_RATE, stopping),
                               lambda layer, n: self._data_sync(DATA_DELETE, struct.pack("<HH", layer, n), None,
                                                                stopping),
                               ahead, JOB_AHEAD_PASSES, self._skew(), stopping)
        self.job = {'streamer': streamer, 'header': header, 'records': records, 'from_print': from_print,
                    'path': path, 'since_wait': 0}
        streamer.start()
        size = sum(rec['size'] for rec in records)
        logging.info("polydye: ink job %s: %d passes on %d layers, up to %d kB; %d kB ahead", path, len(records),
                     header['layers'], size // 1024, ahead // 1024)
        gcmd.respond_info("PolyDye ink job: %d passes on %d layers (%.1f MB), going to the board as the print runs"
                          % (len(records), header['layers'], size / 1e6))

    def _check_job(self, gcmd, cmd, header, records):
        """Refuses, before anything moves, a job sliced for other settings, one the colour registration would move off
        the nozzles, or one that would take the toolhead beyond the axis limits (each pass's run-up at the acceleration
        the G-code sets for the ink, or JOB_REACH_ACCEL if it sets none; the ink blocks' height). Notes each pass's
        size on the board (at most: its colours spread by the registration and skew) as its 'size'."""
        if header['pass_axis'] != 'y':
            raise gcmd.error("%s: the job is drawn for pass_axis %s" % (cmd, header['pass_axis']))
        for key, have in (('dpi', self.dpi), ('nozzle_dpi', self.nozzle_dpi)):
            if abs(header[key] - have) > 1e-3 * have:
                raise gcmd.error("%s: the job was sliced for %s %g, but polydye.cfg has %g: slice it again with "
                                 "polydye_post.py --%s %g" % (cmd, key, header[key], have, key.replace('_', '-'),
                                                              have))
        lo, hi = header['positions']
        reg = self.registration
        moves = (reg[1], reg[3], 0)
        if lo + min(moves) < JOB_NOZZLES[0] or hi + max(moves) > JOB_NOZZLES[1]:
            raise gcmd.error("%s: the job draws on nozzle positions %d-%d, and the colour registration (cyan_positions "
                             "%d, yellow_positions %d) moves dots beyond the %d-%d the cartridge fires: slice it again "
                             "with polydye_post.py --positions %d-%d"
                             % (cmd, lo, hi, reg[1], reg[3], JOB_NOZZLES[0], JOB_NOZZLES[1],
                                max(lo, JOB_NOZZLES[0] - min(moves)), min(hi, JOB_NOZZLES[1] - max(moves))))
        eventtime = self.printer.get_reactor().monotonic()
        th = self.printer.lookup_object('toolhead').get_status(eventtime)
        base = self.printer.lookup_object('gcode_move').base_position
        if self.pass_speed > th['max_velocity']:
            raise gcmd.error("%s: the passes go at pass_speed %.1f mm/s, over max_velocity %.1f: raise max_velocity "
                             "or lower pass_speed (and slice again with --pass-speed)"
                             % (cmd, self.pass_speed, th['max_velocity']))
        slow = dict(th, max_accel=min(th['max_accel'], header['ink_accel'] or JOB_REACH_ACCEL))
        low, high = th['axis_minimum'], th['axis_maximum']
        if high.z > low.z and header['ink_z'] + base[2] > high.z:
            raise gcmd.error("%s: the ink goes up to Z=%.2f (the job's --gap above the layer), beyond the Z axis "
                             "maximum: slice again with a smaller --gap" % (cmd, header['ink_z']))
        pitch = 25.4 / self.dpi
        before, after = min(0, reg[0], reg[2]), max(0, reg[0], reg[2])
        skew = self._skew()
        if skew:
            moved = [skew[p] for p in range(lo + min(moves), hi + max(moves) + 1)]
            before, after = before + min(0, min(moved)), after + max(0, max(moved))
        for rec in records:
            columns = rec['columns'] + after - before
            rec['size'] = 2 * columns * NOZDATA_SIZE  # at most two board passes (job_pass_parts())
            first = rec['y'] + (before if rec['dir'] > 0 else rec['columns'] - 1 + after) * pitch
            # Each board pass sent lies within those columns but can be much shorter (one fire line's half of a split
            # pass is trimmed to its own ink), and with minimum_cruise_ratio a shorter pass has a longer run-up: the
            # longest any can have (a one-column pass's), over the widest extent.
            run_up = self._run_up(self.pass_speed, slow, 1)
            start, _, end = pass_geometry('y', rec['x'], first, rec['dir'], columns, pitch, run_up,
                                          (self.offset_x, self.offset_y), base, self.direction_trim)
            for px, py in (start, end):
                # Axes without a range (kinematics: none, for testing) have no limits to keep to.
                if not ((high.x <= low.x or low.x <= px <= high.x) and (high.y <= low.y or low.y <= py <= high.y)):
                    raise gcmd.error("%s: layer %d's ink at X=%.1f Y=%.1f would take the toolhead to X=%.1f Y=%.1f, "
                                     "beyond the axis limits: move the model on the plate and slice again (the "
                                     "cartridge inks offset_x %.1f, offset_y %.1f mm from the nozzle)"
                                     % (cmd, rec['layer'], rec['x'], first, px, py, self.offset_x, self.offset_y))

    cmd_POLYDYE_JOB_PASS_help = ("Print pass N of the PolyDye ink job (POLYDYE_JOB) with POLYDYE_PASS, once it's on "
                                 "the board")

    def cmd_POLYDYE_JOB_PASS(self, gcmd):
        cmd = "POLYDYE_JOB_PASS"
        job = self.job
        if job is None:
            raise gcmd.error("%s: no PolyDye ink job is loaded (POLYDYE_JOB, at the start of the print)" % (cmd,))
        records, streamer = job['records'], job['streamer']
        n = gcmd.get_int('N', minval=0, maxval=len(records) - 1)
        rec = records[n]
        if rec['object'] != '-' and rec['object'].upper() in self._excluded_objects():
            streamer.consume(n, skipped=True)
            gcmd.respond_info("PolyDye: pass %d skipped: object %s is excluded" % (n, rec['object']))
            return
        # One board pass, or two where its fire lines are printed apart (job_pass_parts()), over the same ground.
        for number, columns, first in self._wait_job_pass(gcmd, cmd, streamer, n):
            if job['since_wait'] >= JOB_WAIT_EVERY:
                self.printer.lookup_object('toolhead').wait_moves()  # the board queues at most 8 passes
                job['since_wait'] = 0
            self.printer.lookup_object('gcode').run_script_from_command(
                "POLYDYE_PASS LAYER=%d PASS=%d X=%.4f Y=%.5f DIR=%d" % (rec['layer'], number, rec['x'],
                                                                       rec['y'] + first * 25.4 / self.dpi, rec['dir']))
            job['since_wait'] += 1
        # On the board until it has printed: it still counts against the room for the passes ahead.
        streamer.consume(n, until=self.printer.lookup_object('toolhead').get_last_move_time())
        streamer.release(self.mcu.estimated_print_time(self.printer.get_reactor().monotonic()))

    def _wait_job_pass(self, gcmd, cmd, streamer, n):
        """Job pass n's board passes [(number, columns, first)] once on the board; raises if its upload failed or
        stalled."""
        reactor = self.printer.get_reactor()
        said, last, since, drained = False, None, reactor.monotonic(), False
        while True:
            streamer.release(self.mcu.estimated_print_time(reactor.monotonic()))
            error, ready, uploaded = streamer.state(n)
            if ready is not None:
                return ready
            if not drained and not error:
                # The room for it may be held by passes still to print: let them print (the board drops each).
                self.printer.lookup_object('toolhead').wait_moves()
                drained = True
                continue
            if error:
                raise gcmd.error("%s: the ink job's upload to the board failed: %s" % (cmd, error))
            if streamer.stopping.is_set():
                raise gcmd.error("%s: the ink job was stopped" % (cmd,))
            now = reactor.monotonic()
            if uploaded != last:
                last, since = uploaded, now
            elif now - since > JOB_WAIT_S:
                raise gcmd.error("%s: pass %d hasn't reached the board: no upload progress for %.0f s"
                                 % (cmd, n, JOB_WAIT_S))
            if not said:
                gcmd.respond_info("PolyDye: waiting for pass %d to reach the board (%d of %d there so far)"
                                  % (n, uploaded, len(streamer.records)))
                said = True
            reactor.pause(now + 0.1)

    def _excluded_objects(self):
        exclude = self.printer.lookup_object('exclude_object', None)
        if exclude is None:
            return set()
        status = exclude.get_status(self.printer.get_reactor().monotonic())
        return set(name.upper() for name in status.get('excluded_objects', ()))

    cmd_POLYDYE_JOB_END_help = "Stop the PolyDye ink job's uploads (at the end of its print) and say what was printed"

    def cmd_POLYDYE_JOB_END(self, gcmd):
        if self.job is None:
            gcmd.respond_info("PolyDye: no ink job is loaded")
            return
        st = self.job['streamer'].status()
        self._stop_job(None)
        gcmd.respond_info("PolyDye ink job done: %d of %d passes printed%s" % (
            st['printed'], st['passes'], ", %d skipped (objects excluded)" % (st['skipped'],) if st['skipped'] else ""))

    def _stop_job(self, why):
        job, self.job = self.job, None
        if job is not None:
            job['streamer'].stop()
            if why:
                logging.info("polydye: ink job stopped: %s", why)

    def _check_job_end(self, eventtime):
        """A job its print loaded stops when the print ends, however it ends; motion limits an ink block lowered
        (POLYDYE_INK_MOTION) go back then too, if the print ended inside one."""
        if self.job is not None and self.job['from_print'] and not self._printing(eventtime):
            self._stop_job("its print ended")
        if self.ink_motion is not None and self.ink_motion[2] and not self._printing(eventtime):
            # G-code can't run from the status timer itself: from a callback, in turn with other commands.
            self.printer.get_reactor().register_callback(lambda e: self._restore_ink_motion("the print ended"))

    cmd_POLYDYE_INK_MOTION_help = ("Gentler moves for an ink block: ACCEL (1000) and SCV (square corner velocity, 1) "
                                   "at most, until POLYDYE_INK_MOTION RESTORE=1 puts the limits before it back")

    def cmd_POLYDYE_INK_MOTION(self, gcmd):
        if gcmd.get_int('RESTORE', 0, minval=0, maxval=1):
            self._restore_ink_motion(None)
            return
        accel = gcmd.get_float('ACCEL', INK_ACCEL, above=0.)
        scv = gcmd.get_float('SCV', INK_SCV, minval=0.)
        st = self.printer.lookup_object('toolhead').get_status(self.printer.get_reactor().monotonic())
        if self.ink_motion is None:
            # Put back by RESTORE=1, or when the print it was lowered in ends (one from the console: RESTORE=1 only).
            self.ink_motion = (st['max_accel'], st['square_corner_velocity'],
                               self._printing(self.printer.get_reactor().monotonic()))
        self.printer.lookup_object('gcode').run_script_from_command(
            "SET_VELOCITY_LIMIT ACCEL=%.3f SQUARE_CORNER_VELOCITY=%.3f"
            % (min(accel, st['max_accel']), min(scv, st['square_corner_velocity'])))

    def _restore_ink_motion(self, why):
        """Puts back the motion limits POLYDYE_INK_MOTION lowered, if it did."""
        saved, self.ink_motion = self.ink_motion, None
        if saved is None:
            return
        script = "SET_VELOCITY_LIMIT ACCEL=%.3f SQUARE_CORNER_VELOCITY=%.3f" % saved[:2]
        try:
            if why:
                self.printer.lookup_object('gcode').run_script(script)
                logging.info("polydye: motion limits put back (%s): %s", why, script)
            else:
                self.printer.lookup_object('gcode').run_script_from_command(script)
        except Exception:
            logging.exception("polydye: couldn't put the motion limits back")

    cmd_POLYDYE_NUDGE_help = ("Move where the ink lands by X, Y mm on the bed, and reverse passes by TRIM mm along the"
                              " pass axis, from the next pass on")

    def cmd_POLYDYE_NUDGE(self, gcmd):
        dx = gcmd.get_float('X', 0., minval=-MAX_NUDGE, maxval=MAX_NUDGE)
        dy = gcmd.get_float('Y', 0., minval=-MAX_NUDGE, maxval=MAX_NUDGE)
        dtrim = gcmd.get_float('TRIM', 0., minval=-MAX_NUDGE, maxval=MAX_NUDGE)
        self.offset_x, self.offset_y, self.direction_trim = nudge_offsets(
            self.offset_x, self.offset_y, self.direction_trim, dx, dy, dtrim)
        self._respond_offsets(gcmd)

    cmd_POLYDYE_CARTRIDGE_help = ("Set the cartridge waveform template (TEMPLATE=color_b_late|black_a|black_b) and "
                                  "fire pulse (PULSE=6..8 bus words, 2.10-2.80 us at the stock clock) until the next "
                                  "restart; live ink must be off and the board idle")

    def cmd_POLYDYE_CARTRIDGE(self, gcmd):
        cmd = "POLYDYE_CARTRIDGE"
        if self.query_cartridge_cmd is None:
            raise gcmd.error("%s: the PolyDye board firmware is too old; update it from the PolyDye page (Board "
                             "firmware)" % (cmd,))
        name = gcmd.get('TEMPLATE', None)
        if name is not None:
            name = name.lower()
            self._check_template(gcmd, cmd, name)
        pulse = gcmd.get_int('PULSE', None)
        if pulse is not None and pulse_range_error(pulse):
            raise gcmd.error("%s: %s" % (cmd, pulse_range_error(pulse)))
        self._refuse_clock(gcmd, cmd)
        if name is not None or pulse is not None:
            # The board refuses a change while powered or busy by shutting down; check first for a plain error.
            self.printer.lookup_object('toolhead').wait_moves()
            st = self._fresh_status()
            self._check_board_clock(gcmd, cmd, st)
            change = (name is not None and name != st['cartridge_template']) or (
                pulse is not None and pulse != st['pulse_words'])
            # Also when nothing changes: the result must be within the limit, not just different.
            if pulse is None and pulse_limit_error(st['pulse_words']):
                raise gcmd.error("%s: %s" % (cmd, pulse_limit_error(st['pulse_words'])))
            if change and ((st['boost'] and self.boost_idle_s is None) or st['live'] or st['running'] or st['queued']):
                raise gcmd.error("%s: turn PolyDye off (POLYDYE_DISABLE) and let queued passes finish before changing "
                                 "the template or the pulse" % (cmd,))
            self._send_cartridge(st, name, pulse)
        st = self._fresh_status()
        gcmd.respond_info("PolyDye %s (set cartridge_template and pulse_words in polydye.cfg to keep them after a "
                          "restart)" % (cartridge_text(st),))

    def _check_template(self, gcmd, cmd, template):
        """Refuses a template the add-on doesn't offer (one removed in the stock-only cleanup, say) or the board
        firmware doesn't have (color_b_late came with polydye_set_clock)."""
        if template_error(template):
            raise gcmd.error("%s: TEMPLATE=%s" % (cmd, template_error(template)))
        if template not in self.templates:
            raise gcmd.error("%s: TEMPLATE=%s needs newer PolyDye board firmware; update it from the PolyDye page "
                             "(Board firmware: Update to bundled firmware)" % (cmd, template))

    def _refuse_clock(self, gcmd, cmd):
        """CLOCK from before the stock-only cleanup: stock is taken as a no-op, the others are refused."""
        clock = gcmd.get('CLOCK', None)
        if clock is not None and clock_error(clock.lower()):
            raise gcmd.error("%s: %s" % (cmd, clock_error(clock.lower())))

    def _send_cartridge(self, st, name, pulse):
        """Sends the template and pulse where they differ from the board's (`st`); None leaves one as it is."""
        if name is not None and name != st['cartridge_template']:
            self.set_template_cmd.send([self.oid, BOARD_TEMPLATES.index(name)])
        if pulse is not None and pulse != st['pulse_words']:
            self.set_pulse_cmd.send([self.oid, pulse])

    cmd_POLYDYE_UPLOAD_TEST_PASS_help = ("Upload a test pattern pass (PATTERN=alternate|all|none|stock, each column "
                                         "REPEAT times; stock takes DENSITY, COLORS, FIRST, LAST and PER_PULSE too) to "
                                         "the PolyDye board; not during a print")

    def cmd_POLYDYE_UPLOAD_TEST_PASS(self, gcmd):
        layer = gcmd.get_int('LAYER', 0, minval=0, maxval=65535)
        pass_num = gcmd.get_int('PASS', 0, minval=0, maxval=65535)
        columns = gcmd.get_int('COLUMNS', 600, minval=1, maxval=65535)
        pattern = gcmd.get('PATTERN', 'alternate').lower()
        random = None
        if pattern == 'stock':
            random = random_settings(gcmd, "POLYDYE_UPLOAD_TEST_PASS") or dict(STOCK_RANDOM)
            repeat = random['repeat']
        else:
            if any(gcmd.get(k, None) is not None for k in RANDOM_KNOBS if k != 'REPEAT'):
                raise gcmd.error("POLYDYE_UPLOAD_TEST_PASS: DENSITY, COLORS, FIRST, LAST and PER_PULSE go with "
                                 "PATTERN=stock")
            repeat = gcmd.get_int('REPEAT', 1, minval=1, maxval=8)
        if columns * repeat > 65535:
            raise gcmd.error("POLYDYE_UPLOAD_TEST_PASS: COLUMNS x REPEAT must be at most 65535 (it is %d)"
                             % (columns * repeat,))
        try:
            data, total = test_pass_data(pattern, columns, repeat, random)
        except ValueError as e:
            raise gcmd.error("POLYDYE_UPLOAD_TEST_PASS: %s" % (e,))
        if not self.data_port:
            raise gcmd.error("POLYDYE_UPLOAD_TEST_PASS: set data_port in [polydye]")
        try:
            hello = self._data_request(DATA_HELLO)
            cart = 1 if hello.get('cart') == 'black' else 0
            if cart and pattern == 'stock':
                raise ValueError("PATTERN=%s is for a colour cartridge template" % (pattern,))
            self._data_request(DATA_PUT, struct.pack("<HHBBH", layer, pass_num, cart, 0, total) + data)
        except (DataPortError, OSError, ValueError) as e:
            raise gcmd.error("POLYDYE_UPLOAD_TEST_PASS: %s" % (e,))
        gcmd.respond_info("PolyDye: stored test pass layer %d pass %d (%d columns, %s%s)"
                          % (layer, pass_num, total, random_text(random) if random else pattern,
                             ", each of %d sent %d times" % (columns, repeat) if repeat > 1 and not random else ""))

    cmd_POLYDYE_LEVEL_help = ("Signal check, cartridge OUT: hold one cartridge line (LINE=D1..D3|CSYNC|S1..S5|DCLK) "
                              "HIGH or LOW for a multimeter (STATE=high|low|off); needs POLYDYE_ENABLE LIVE=0 first")

    def cmd_POLYDYE_LEVEL(self, gcmd):
        if self.set_level_cmd is None:
            raise gcmd.error("POLYDYE_LEVEL: the PolyDye board firmware is too old; update it from the PolyDye page "
                             "(Board firmware)")
        state = gcmd.get('STATE', 'high').lower()
        if state not in LEVEL_STATES:
            raise gcmd.error("POLYDYE_LEVEL: STATE must be high, low or off")
        if state == 'off':
            self.set_level_cmd.send([self.oid, LINE_NONE, LEVEL_STATES['off']])
            gcmd.respond_info("PolyDye signal check: all lines LOW")
            return
        line = gcmd.get('LINE').upper()
        if line in ('PWRA', 'PWRB', 'F3', 'F5'):
            raise gcmd.error("POLYDYE_LEVEL: the fire lines (PWRA/PWRB) are never driven statically; check them "
                             "with the power-off continuity step on the PolyDye page")
        if line not in LEVEL_LINES:
            raise gcmd.error("POLYDYE_LEVEL: LINE must be one of %s" % (", ".join(LEVEL_LINES),))
        # The board refuses with a shutdown; check first for a plain error.
        self.printer.lookup_object('toolhead').wait_moves()
        st = self._fresh_status()
        if st['live']:
            raise gcmd.error("POLYDYE_LEVEL: live ink is on; use POLYDYE_ENABLE LIVE=0 (dry run) for the signal check")
        if not st['boost']:
            raise gcmd.error("POLYDYE_LEVEL: turn the boost on first with POLYDYE_ENABLE LIVE=0 (dry run, no firing)")
        if st['running'] or st['queued']:
            raise gcmd.error("POLYDYE_LEVEL: wait for queued passes to finish")
        self.set_level_cmd.send([self.oid, LEVEL_LINES.index(line), LEVEL_STATES[state]])
        self._query_status()
        if state == 'high':
            gcmd.respond_info("PolyDye signal check: %s HIGH. Its pogo should read %s; every other line reads 0 V. "
                              "Back to LOW with POLYDYE_LEVEL STATE=off, or by itself after 5 minutes."
                              % (line, rail_text(line)))
        else:
            gcmd.respond_info("PolyDye signal check: %s held LOW" % (line,))

    def _check_not_printing(self, gcmd, name):
        """Refuses a test during a print, also a paused one (its layer 0 pass 0 and position must stay as they are)."""
        sdcard = self.printer.lookup_object('virtual_sdcard', None)
        print_stats = self.printer.lookup_object('print_stats', None)
        state = None
        if print_stats is not None:
            state = print_stats.get_status(self.printer.get_reactor().monotonic()).get('state')
        if (sdcard is not None and sdcard.is_active()) or state in ('printing', 'paused'):
            raise gcmd.error("%s: not during a print (cancel it first)" % (name,))

    def _wait_off_and_idle(self, timeout=10.):
        """Waits until the board reports live output off and nothing queued or printing, and the boost off unless the
        board keeps it on between tests (boost_idle_s)."""
        reactor = self.printer.get_reactor()
        end = reactor.monotonic() + timeout
        while True:
            st = self._fresh_status()
            if not ((st['boost'] and self.boost_idle_s is None) or st['live'] or st['running'] or st['queued']):
                return st
            if reactor.monotonic() > end:
                raise self.printer.command_error("PolyDye: the board didn't turn off and go idle")
            reactor.pause(reactor.monotonic() + 0.1)

    def _live_off(self):
        """Live ink off, and the boost too unless the board keeps it on between tests (boost_idle_s); then waits for
        that and for the board to go idle. Returns its status."""
        self.mode_cmd.send([self.oid, 0 if self.boost_idle_s is None else 1, 0])
        self.print_live = False
        return self._wait_off_and_idle()

    cmd_POLYDYE_FIRE_TEST_help = ("LIVE INK on paper: one stripe per TEMPLATE:PULSE in STRIPES, side by side from X, "
                                  "Y, SPACING mm apart, at Z if given, PATTERN=stock (the default, with DENSITY, "
                                  "REPEAT, COLORS, FIRST, LAST, PER_PULSE)|all, each stripe after a head warm-up of "
                                  "WARM seconds (WARM_PULSE, WARM_PERIOD_US) if given; restores the template, pulse "
                                  "and G-code state afterwards. UPLOAD=0 prints the pass already stored as LAYER, PASS "
                                  "instead")

    def cmd_POLYDYE_FIRE_TEST(self, gcmd):
        cmd = "POLYDYE_FIRE_TEST"
        if self.query_cartridge_cmd is None:
            raise gcmd.error("%s: the PolyDye board firmware is too old; update it from the PolyDye page (Board "
                             "firmware)" % (cmd,))
        try:
            stripes = parse_stripes(gcmd.get('STRIPES', FIRE_TEST_STRIPES))
        except ValueError as e:
            raise gcmd.error("%s: STRIPES: %s" % (cmd, e))
        for tpl, _ in stripes:
            self._check_template(gcmd, cmd, tpl)
        random = random_settings(gcmd, cmd)
        pattern = gcmd.get('PATTERN', 'stock').lower()
        if pattern_error(pattern, SPIT_PATTERNS):
            raise gcmd.error("%s: %s" % (cmd, pattern_error(pattern, SPIT_PATTERNS)))
        if random is not None and pattern != 'stock':
            raise gcmd.error("%s: %s go with PATTERN=stock" % (cmd, ", ".join(RANDOM_KNOBS)))
        if pattern == 'stock':
            random = random or dict(STOCK_RANDOM)
        x = gcmd.get_float('X', 70.)
        y = gcmd.get_float('Y', 60.)
        z = gcmd.get_float('Z', None, minval=0.2)
        spacing = gcmd.get_float('SPACING', 18., above=0.)
        columns = gcmd.get_int('COLUMNS', 600, minval=1, maxval=4800)
        upload = gcmd.get_int('UPLOAD', 1, minval=0, maxval=1)
        layer = gcmd.get_int('LAYER', 0, minval=0, maxval=65535)
        pass_num = gcmd.get_int('PASS', 0, minval=0, maxval=65535)
        # stock is a colour pattern: refuse a black stripe before anything moves or prints (a stored pass, UPLOAD=0, is
        # checked against its own cartridge type below).
        if upload and pattern == 'stock':
            black = [tpl for tpl, _ in stripes if tpl.startswith('black')]
            if black:
                raise gcmd.error("%s: PATTERN=stock is for a colour cartridge template, not %s; use PATTERN=all for "
                                 "black stripes" % (cmd, ", ".join(black)))
        # A head warm-up right before each stripe, by its pass (POLYDYE_PASS WARM, with the stripe's settings, at the
        # pass's warm spot), so the head is hot when the pass starts: only a short travel and the run-up come in
        # between. Without WARM each stripe's pass warms a cold head by itself (the automatic warm-up); WARM=0 turns
        # that off for an A/B test.
        warm = gcmd.get_float('WARM', None, minval=0., maxval=WARM_MAX_S)
        explicit_warm, warm = warm is not None, warm or 0.
        warm_words = gcmd.get_int('WARM_PULSE', None, minval=1)
        warm_period = gcmd.get_int('WARM_PERIOD_US', None, minval=1, maxval=65535)
        if warm:
            if self.spit_run_cmd is None:
                raise gcmd.error("%s: WARM needs newer PolyDye board firmware; update it from the PolyDye page (Board "
                                 "firmware: Update to bundled firmware)" % (cmd,))
            if warm > self.warm_max_s + 1e-9:
                raise gcmd.error("%s: a warm-up lasts at most %g s with this board firmware%s"
                                 % (cmd, self.warm_max_s, "; newer firmware takes up to %g s (Board firmware: Update "
                                    "to bundled firmware)" % (WARM_MAX_S,) if self.warm_duty_ppm is None else ""))
            self._warm_plan(gcmd, cmd, warm,
                            *pass_warm_settings(warm_words, warm_period, self.warm_pulse, self.warm_period_us))
        prespit = gcmd.get_float('PRESPIT', None, minval=0., maxval=PRESPIT_MAX_S)
        # The pre-spit is the stock pattern, for colour templates: refuse a black stripe whose pass would pre-spit
        # (PRESPIT, else warm_prespit, after its warm-up: WARM, or the automatic one) before anything moves or prints.
        if (self.warm_prespit if prespit is None else prespit) and (
                warm if explicit_warm else
                self.warm_seconds and self.spit_run_cmd is not None and self.warm_duty_ppm is not None):
            black = [tpl for tpl, _ in stripes if tpl.startswith('black')]
            if black:
                raise gcmd.error("%s: the pre-spit (%s) is the stock pattern, for a colour cartridge template, not %s; "
                                 "use PRESPIT=0 with black stripes"
                                 % (cmd, "PRESPIT" if prespit is not None else "warm_prespit", ", ".join(black)))
        pass_warm = ((" WARM=%g%s%s" % (warm, " WARM_PULSE=%d" % (warm_words,) if warm_words else "",
                                        " WARM_PERIOD_US=%d" % (warm_period,) if warm_period else "")
                      if explicit_warm else "") + (" PRESPIT=%g" % (prespit,) if prespit is not None else ""))
        if not upload:
            info = self.query_pass_cmd.send([self.oid, layer, pass_num])
            if not info['present']:
                raise gcmd.error("%s: layer %d pass %d has not been uploaded" % (cmd, layer, pass_num))
            stored = 'black' if info['cart'] == 1 else 'color'
            if any(('black' if tpl.startswith('black') else 'color') != stored for tpl, _ in stripes):
                raise gcmd.error("%s: layer %d pass %d is for a %s cartridge; use %s templates only, or UPLOAD=1"
                                 % (cmd, layer, pass_num, stored, stored))
            columns = info['columns']
        self._check_not_printing(gcmd, cmd)
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.wait_moves()
        st = self._fresh_status()
        if st['running'] or st['queued']:
            raise gcmd.error("%s: wait for queued passes to finish" % (cmd,))
        self._check_current_pulse(gcmd, cmd, st)
        before = (st['cartridge_template'], st['pulse_words'])
        positions = stripe_positions(self.pass_axis, x, y, spacing, len(stripes))
        # The stock pattern's random columns go up REPEAT times each, so the stripe has whole repeats.
        upload_columns = max(1, columns // random['repeat']) if random else columns
        upload_cmd = ("POLYDYE_UPLOAD_TEST_PASS LAYER=%d PASS=%d COLUMNS=%d PATTERN=%s%s"
                      % (layer, pass_num, upload_columns, pattern, random_params(random) if random else ""))
        if random and upload:
            columns = upload_columns * random['repeat']
        gcmd.respond_info("PolyDye fire test, live ink, %d stripes of %d %s columns along %s%s:\n%s" % (
            len(stripes), columns, (random_text(random) if random else pattern) if upload else "stored",
            self.pass_axis.upper(), ", each after a %g s head warm-up" % (warm,) if warm else "", "\n".join(
                "  %d: X=%.1f Y=%.1f  %s, pulse %d words (%.2f us)" % (i + 1, px, py, tpl, words, pulse_us(words))
                for i, ((tpl, words), (px, py)) in enumerate(zip(stripes, positions)))))
        run = self.printer.lookup_object('gcode').run_script_from_command
        uploaded = None if upload else 'stored'
        # Everything is checked by now; from here on the G-code state and the board settings are always put back.
        run("SAVE_GCODE_STATE NAME=pd_fire_test")
        try:
            if z is not None:
                run("G90\nG1 Z%.3f F900" % (z,))
            for i, ((tpl, words), (px, py)) in enumerate(zip(stripes, positions)):
                toolhead.wait_moves()
                self._live_off()
                run("POLYDYE_CARTRIDGE TEMPLATE=%s PULSE=%d" % (tpl, words))
                # The pass store keeps the cartridge type a pass was uploaded for.
                cart = 'black' if tpl.startswith('black') else 'color'
                if upload and cart != uploaded:
                    run(upload_cmd)
                    uploaded = cart
                run("POLYDYE_ENABLE LIVE=1")
                run("POLYDYE_PASS LAYER=%d PASS=%d X=%.3f Y=%.3f DIR=1%s" % (layer, pass_num, px, py, pass_warm))
                gcmd.respond_info("PolyDye fire test: stripe %d/%d queued at X=%.1f Y=%.1f (%s, pulse %d)"
                                  % (i + 1, len(stripes), px, py, tpl, words))
        finally:
            self._restore_after_test(before)
            if not self.printer.is_shutdown():
                try:
                    run("RESTORE_GCODE_STATE NAME=pd_fire_test")
                except Exception:
                    logging.exception("polydye: fire test couldn't restore the G-code state")
        gcmd.respond_info("PolyDye fire test done; %s" % (cartridge_text(self._fresh_status()),))

    cmd_POLYDYE_SPIT_help = ("LIVE INK, no motion: fire like the stock firmware (PATTERN=stock, the default: every 508 "
                             "us, tuned with DENSITY, REPEAT, COLORS, FIRST, LAST or PER_PULSE) or every nozzle (all, "
                             "at 1 kHz) for SECONDS (max 10), or every PERIOD_US, into a paper towel held under the "
                             "cartridge, optionally with TEMPLATE and PULSE and a head warm-up of WARM seconds (max "
                             "30) first, in the same run; needs CONFIRM=1")

    def cmd_POLYDYE_SPIT(self, gcmd):
        self._spit(gcmd, "POLYDYE_SPIT", warm_only=False)

    cmd_POLYDYE_CLOCK_TEST_help = "Removed in the stock-only cleanup: the bus runs only at the stock clock"

    def cmd_POLYDYE_CLOCK_TEST(self, gcmd):
        # Kept so old macros and notes get the reason rather than Klipper's unknown-command error.
        raise gcmd.error("POLYDYE_CLOCK_TEST was removed: the bus runs only at the stock clock (2.857 MHz), set once at "
                         "boot, so there are no clock switches to test; the board still measures the clock before "
                         "every live run")

    cmd_POLYDYE_WARM_help = ("LIVE, no motion, EXPERIMENTAL: warm the cartridge head for SECONDS (max 30) by firing "
                             "every nozzle (WARM_PULSE words, up to 2.9 us; by default too short to boil ink) as fast "
                             "as the column and a heater duty of 0.58 % allow or every WARM_PERIOD_US, optionally with "
                             "TEMPLATE; needs CONFIRM=1")

    def cmd_POLYDYE_WARM(self, gcmd):
        self._spit(gcmd, "POLYDYE_WARM", warm_only=True)

    def _spit(self, gcmd, cmd, warm_only):
        """POLYDYE_SPIT, or with warm_only POLYDYE_WARM: a warm-up and no spit columns."""
        if self.spit_cmd is None:
            raise gcmd.error("%s: the PolyDye board firmware is too old; update it from the PolyDye page "
                             "(Board firmware)" % (cmd,))
        if not warm_only and gcmd.get_int('EXTENDED', 0, minval=0, maxval=1):
            raise gcmd.error("%s: EXTENDED was removed (stock baseline): the fire pulse is at most %.1f us (%d words)"
                             % (cmd, PULSE_MAX_US, PULSE_MAX))
        self._refuse_clock(gcmd, cmd)
        if warm_only:
            warm = gcmd.get_float('SECONDS', 2., above=0., maxval=WARM_MAX_S)
            seconds, period = 0., None
        else:
            seconds = gcmd.get_float('SECONDS', 2., above=0., maxval=SPIT_MAX_S)
            warm = gcmd.get_float('WARM', 0., minval=0., maxval=WARM_MAX_S)
            period = gcmd.get_int('PERIOD_US', None, minval=1, maxval=65535)
        if warm > self.warm_max_s + 1e-9:
            if self.warm_duty_ppm is None:
                raise gcmd.error("%s: a %g s warm-up needs newer PolyDye board firmware, which warms up for up to %g "
                                 "s; update it from the PolyDye page (Board firmware: Update to bundled firmware). This "
                                 "one warms up for at most %g s" % (cmd, warm, WARM_MAX_S, self.warm_max_s))
            raise gcmd.error("%s: a warm-up lasts at most %g s" % (cmd, self.warm_max_s))
        for option, wanted in (("PERIOD_US", period is not None), ("WARM" if not warm_only else "it", warm > 0)):
            if wanted and self.spit_run_cmd is None:
                raise gcmd.error("%s: %s needs newer PolyDye board firmware; update it from the PolyDye page (Board "
                                 "firmware: Update to bundled firmware)" % (cmd, option))
        random = None if warm_only else random_settings(gcmd, cmd)
        # The stock pattern by default, where the board has it, at the stock firmware's column period.
        stock_ok = 'stock' in self.spit_patterns
        pattern = 'all' if warm_only else gcmd.get('PATTERN', 'stock' if stock_ok or random else 'all').lower()
        if pattern_error(pattern, SPIT_PATTERNS):
            raise gcmd.error("%s: %s" % (cmd, pattern_error(pattern, SPIT_PATTERNS)))
        if period is None and pattern == 'stock' and stock_ok:
            period = STOCK_PERIOD_US
        # The board's column limit holds whatever the period, so a shorter period means a shorter spit.
        column_period = period or SPIT_PERIOD_US
        columns = 0 if warm_only else max(1, int(round(seconds * 1e6 / column_period)))
        if columns > SPIT_MAX_COLUMNS:
            raise gcmd.error("%s: at a %d us column period a spit test lasts at most %.2f s (%d columns)"
                             % (cmd, column_period, SPIT_MAX_COLUMNS * column_period / 1e6, SPIT_MAX_COLUMNS))
        if random is not None:
            if pattern != 'stock':
                raise gcmd.error("%s: %s go with PATTERN=stock" % (cmd, ", ".join(RANDOM_KNOBS)))
            if self.spit_random_cmd is None:
                raise gcmd.error("%s: %s need newer PolyDye board firmware; update it from the PolyDye page (Board "
                                 "firmware: Update to bundled firmware). POLYDYE_FIRE_TEST PATTERN=stock takes them "
                                 "with this one" % (cmd, ", ".join(RANDOM_KNOBS)))
        if pattern not in self.spit_patterns:
            raise gcmd.error("%s: PATTERN=%s needs newer PolyDye board firmware; update it from the PolyDye page "
                             "(Board firmware: Update to bundled firmware)" % (cmd, pattern))
        name = gcmd.get('TEMPLATE', None)
        name = None if name is None else name.lower()
        pulse = None if warm_only else gcmd.get_int('PULSE', None)
        if pulse is not None and pulse_range_error(pulse):
            raise gcmd.error("%s: %s" % (cmd, pulse_range_error(pulse)))
        if pattern == 'stock':
            # The stock firmware's timing unless asked otherwise: color_b_late, 6 words (2.10 us).
            name = name or STOCK_TEMPLATE
            if pulse is None:
                pulse = PULSE_MIN
        if name is not None:
            self._check_template(gcmd, cmd, name)
            if pattern == 'stock' and name.startswith('black'):
                raise gcmd.error("%s: PATTERN=stock is for a colour cartridge template" % (cmd,))
        warm_words = warm_period = None
        if warm:
            # Bounded by the pulse length in _warm_plan, where older board firmware gets the update hint.
            warm_words = gcmd.get_int('WARM_PULSE', None, minval=1)
            warm_period = gcmd.get_int('WARM_PERIOD_US', None, minval=1, maxval=65535)
        if gcmd.get_int('CONFIRM', 0) != 1:
            if warm_only:
                raise gcmd.error("POLYDYE_WARM heats every nozzle's heater with the boost on: fit the cartridge over a "
                                 "folded paper towel, then run it again with CONFIRM=1")
            raise gcmd.error("POLYDYE_SPIT fires every nozzle with live ink: hold a folded paper towel under the "
                             "cartridge, then run it again with CONFIRM=1")
        self._check_not_printing(gcmd, cmd)
        reactor = self.printer.get_reactor()
        self.printer.lookup_object('toolhead').wait_moves()
        st = self._fresh_status()
        if st['running'] or st['queued']:
            raise gcmd.error("%s: wait for queued passes to finish" % (cmd,))
        self._check_current_pulse(gcmd, cmd, st)
        before = (st['cartridge_template'], st['pulse_words'])
        # The columns have to fit their period: a longer pulse makes a longer column.
        fire_words = pulse if pulse is not None else before[1]
        if period is not None and period < min_spit_period_us(fire_words):
            raise gcmd.error("%s: PERIOD_US=%d is too short for a pulse of %d words: the column takes %d us on the "
                             "bus, so the period must be at least %d us"
                             % (cmd, period, fire_words, column_us(fire_words), min_spit_period_us(fire_words)))
        warm_columns = 0
        if warm:
            warm_words, warm_period, warm_columns = self._warm_plan(gcmd, cmd, warm, warm_words, warm_period)
        pattern_no = RANDOM_PATTERN if random is not None else SPIT_PATTERNS[pattern]
        pattern_text = random_text(random) if random is not None else TEST_PATTERN_TEXT[pattern]
        board = {}
        what = "warm-up" if warm_only else "spit test"
        try:
            self.testing = True
            self._live_off()
            if name is not None or pulse is not None:
                self.printer.lookup_object('gcode').run_script_from_command(
                    "POLYDYE_CARTRIDGE" + (" TEMPLATE=%s" % (name,) if name else "")
                    + (" PULSE=%d" % (pulse,) if pulse else ""))
            self.mode_cmd.send([self.oid, 1, 1])
            st = self._fresh_status()
            if not st['live']:
                raise gcmd.error("%s: the board didn't turn live ink on (disarmed on its console?)" % (cmd,))
            board = st
            if warm:
                gcmd.respond_info("PolyDye %s" % (warm_text(warm_columns, warm_words, warm_period),))
            if not warm_only:
                gcmd.respond_info("PolyDye spit test: %d %s columns %s, %s"
                                  % (columns, pattern_text, spit_rate_text(column_period), cartridge_text(st)))
            if random is not None:
                self.spit_random_cmd.send([self.oid, random['density'], random['repeat'], random['colors'],
                                           random['first'], random['last'], random['per_pulse']])
            # The board starts it no sooner than 0.1 s after the boost came on. polydye_spit_run's ext_words is always
            # 0: it was the removed extended spit test's pulse.
            if period is not None or warm or random is not None:
                self.spit_run_cmd.send([self.oid, columns, pattern_no, 0, column_period, warm_columns, warm_words or 0,
                                        warm_period or 0])
            else:
                self.spit_cmd.send([self.oid, columns, pattern_no])
            end = reactor.monotonic() + seconds + warm + 5.
            clock_told = False
            while True:
                reactor.pause(reactor.monotonic() + 0.2)
                if self.printer.is_shutdown():
                    raise gcmd.error(self._test_shutdown_text(cmd, what, warm))
                now = self._fresh_status()
                if not now['running']:
                    break
                # The clock the columns go out at, once: the board checked it before the first column (firmware with
                # the check), and holds every column to it.
                if not clock_told and now.get('bus_hz_measured'):
                    clock_told = True
                    gcmd.respond_info("PolyDye board: %s" % (measured_clock_text(
                        now['bus_hz_measured'], now['bus_hz'], now.get('bus_clock'), self.bus_hz_check),))
                if reactor.monotonic() > end:
                    raise gcmd.error("%s: the board didn't finish the %s" % (cmd, what))
            self.last_fire = self.mcu.estimated_print_time(reactor.monotonic())
        finally:
            self.testing = False
            self._restore_after_test(before)
        st = self._fresh_status()
        run = self._last_run()
        if run is not None:
            gcmd.respond_info("PolyDye board: %s" % (run_text(run),))
        # The bus rate the board measured is that of the spit's last column: it goes with the settings it fired with.
        # Compare the settings, not the text, to tell whether they were put back to something else.
        settings = ('cartridge_template', 'pulse_words')
        restored = [st.get(key) for key in settings] != [board.get(key) for key in settings]
        back = "; now back to %s" % (cartridge_text(dict(st, bus_hz_measured=None)),) if restored else ""
        if warm_only:
            gcmd.respond_info("PolyDye warm-up done (faults=%d)%s" % (st['faults'], back))
            return
        gcmd.respond_info("PolyDye spit test done (faults=%d): fired with %s%s" % (
            st['faults'], cartridge_text(dict(board, bus_hz_measured=st.get('bus_hz_measured'))), back))

    def _test_shutdown_text(self, cmd, what, warm):
        """Why Klipper shut down during a spit test or warm-up: with what the board says stopped it, if it says, and
        the bus clock it last measured."""
        run = self._last_run()
        fault = None if run is None else run_fault(run)
        clock = self._measured_clock()
        clock = "; %s" % (clock[0],) if clock else ""
        if fault is None:
            return "%s: Klipper shut down during the %s%s" % (cmd, what, clock)
        if warm and run['warm_left']:
            what = "warm-up"
        return "%s: Klipper shut down during the %s: %s, after %s%s" % (cmd, what, fault, run_text(run), clock)

    def _measured_clock(self):
        """The bus clock the board last measured (its clock check before a live run, or the last column) as (text,
        wrong: refused by the board's clock check); None if the board can't say. Also works after a shutdown."""
        if getattr(self, 'query_clock_cmd', None) is None:
            return None
        try:
            clock = self.query_clock_cmd.send([self.oid])
        except Exception as e:  # the board gone (unplugged): nothing to report
            logging.info("polydye: couldn't ask the board about its bus clock: %s", e)
            return None
        if not clock['measured_hz']:
            return None
        name, nominal = bus_clock_of(clock)
        check = getattr(self, 'bus_hz_check', None)
        return (measured_clock_text(clock['measured_hz'], nominal, name, check),
                clock_wrong(clock['measured_hz'], nominal, check))

    def _warm_plan(self, gcmd, cmd, warm, words, period):
        """The warm-up's pulse, period and column count for `warm` seconds: by default WARM_WORDS (0.70 us, too short
        to boil ink), as fast as the column and the board's heater duty limit allow. Longer pulses, up to the board's
        limit (2.9 us, 8 words, on board firmware with the duty limit), heat harder; the duty limit then sets the
        shortest period (483 us at 2.80 us)."""
        if words is None:
            words = WARM_WORDS
        us = pulse_us(words)
        if us > self.warm_max_us + 1e-9 or words > self.warm_max_words:
            if self.warm_duty_ppm is None and us <= PULSE_MAX_US + 1e-9:
                raise gcmd.error("%s: WARM_PULSE=%d (%.2f us) needs newer PolyDye board firmware, which takes warm-up "
                                 "pulses of up to %.1f us; update it from the PolyDye page (Board firmware: Update to "
                                 "bundled firmware). This one takes at most %.2f us"
                                 % (cmd, words, us, PULSE_MAX_US, self.warm_max_us))
            raise gcmd.error("%s: WARM_PULSE=%d is %.2f us at the stock clock; a warm-up pulse is at most %.2f us"
                             % (cmd, words, us, self.warm_max_us))
        fastest = min_spit_period_us(words)
        # The board's rule in whole ns and us: pulse_ns * 1e6 / period_us at most warm_duty_ppm.
        duty_min = 0 if self.warm_duty_ppm is None else -(-pulse_ns(words) * 1000 // self.warm_duty_ppm)
        if period is None:
            period = max(fastest, duty_min, int(math.ceil(warm * 1e6 / WARM_MAX_COLUMNS)))
        elif period < fastest:
            raise gcmd.error("%s: WARM_PERIOD_US=%d is too short for a warm-up pulse of %d words: the column takes %d "
                             "us on the bus, so the warm-up period must be at least %d us"
                             % (cmd, period, words, column_us(words), fastest))
        elif period < duty_min:
            raise gcmd.error("%s: WARM_PERIOD_US=%d is too short for a %.2f us warm-up pulse: a warm-up's heater duty is "
                             "at most %.2f %%, so its period must be at least %d us"
                             % (cmd, period, us, self.warm_duty_ppm / 1e4, duty_min))
        columns = warm_columns(warm, period, self.warm_max_s)
        if columns > WARM_MAX_COLUMNS:
            raise gcmd.error("%s: at a %d us warm-up period the warm-up lasts at most %.2f s (%d columns)"
                             % (cmd, period, WARM_MAX_COLUMNS * period / 1e6, WARM_MAX_COLUMNS))
        return words, period, columns

    def _restore_after_test(self, before):
        """Always turn live ink off (and the boost, unless the board keeps it on between tests) and put the template
        and pulse (`before`) back, even after an error (which is re-raised by the caller); problems here are logged so
        they don't hide that error."""
        if self.printer.is_shutdown():
            return
        try:
            self.printer.lookup_object('toolhead').wait_moves()
            self._send_cartridge(self._live_off(), before[0], before[1])
            self._query_status()
        except Exception:
            # Live ink may still be on: the status poll keeps trying to turn it off (_check_print_end()).
            self.live_off_pending = True
            logging.exception("polydye: test cleanup failed; live ink goes off at the next status poll")

    def _head_idle_s(self, eventtime):
        """Seconds since the head last fired or a prewarm burst warmed it, as last known; None: not since Klipper
        started."""
        now = self.mcu.estimated_print_time(eventtime)
        warmed = self._warmed_cached(now)
        return None if warmed is None else round(max(0., now - warmed), 1)

    def _prewarm_status(self):
        st = self.prewarm_state
        return {'available': self.prewarm_cmd is not None,
                'state': 'none' if st is None else st['state'],
                'seconds': None if st is None else round(st['columns'] * self.warm_period_us / 1e6, 1)}

    def get_status(self, eventtime):
        status = dict(self.last_status, last_pass=self.last_pass, pass_axis=self.pass_axis, offset_x=self.offset_x,
                    offset_y=self.offset_y, direction_trim=self.direction_trim, spit_patterns=list(self.spit_patterns),
                    spit_random=self.spit_random_cmd is not None, warm_max_us=self.warm_max_us,
                    warm_max_s=self.warm_max_s, warm_duty_ppm=self.warm_duty_ppm, templates=list(self.templates),
                    board_soft_reset=self.board_soft_reset,
                    auto_warm={'available': self.spit_run_cmd is not None and self.warm_duty_ppm is not None,
                               'seconds': self.warm_seconds, 'pulse_words': self.warm_pulse,
                               'period_us': self.warm_period_us, 'idle_time': self.warm_idle_time,
                               'position': self.warm_position if isinstance(self.warm_position, str)
                               else list(self.warm_position), 'clearance': self.warm_clearance,
                               'prespit': self.warm_prespit, 'prewarm_seconds': self.prewarm_seconds,
                               'prime_seconds': self.prime_seconds},
                    head_idle_s=self._head_idle_s(eventtime),
                    prewarm=self._prewarm_status(),
                    nozzle_dpi=self.nozzle_dpi, cfg_saves=self.cfg_saves,
                    job=self.job['streamer'].status() if self.job is not None else None)
        status.update(zip(REG_KEYS, self.registration))
        status['nozzle_skew'] = self.nozzle_skew
        return status


def load_config(config):
    return PolyDye(config)
