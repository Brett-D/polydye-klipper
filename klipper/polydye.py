# PolyDye inkjet support: schedules ink passes on the PolyDye board, a secondary Klipper MCU.
#
# Install by running install.sh from the polydye-klipper repo (see README.md).
#
# Copyright (C) 2026  polydye-klipper contributors
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import json
import logging
import math
import os
import struct
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
# the head hasn't fired since Klipper started or not for a minute (it was cold again within 4 minutes): every nozzle at
# 1 word (0.35 us) every 254 us for 10 s. Brett's ladder on 48483ab (2026-11-25), each warm-up on a hot head 17 mm
# before a cyan text pass: 0.35 us every 508 us lost the word's first 10 mm; 0.35 us every 254 us printed it complete
# with no dot where it warmed (2 of 39,370 column times missed); 0.70 us every 508 us was complete but left a dot, and
# every 192 us a blob; 2.80 us spat a puddle. The pulse width matters, not just the energy. No ink may land on the glass
# bed (Brett, 2026-11-25), and the warm-up's spot (with any pre-spit) is often beyond the print: so it runs over the
# wipe sponge at the X gantry's right end by default (warm_position: sponge); `pass` (AUTO_WARM_CLEARANCE mm behind the
# pass's run-up start, warm_clearance) only suits a print with plastic there, such as a base under the part.
AUTO_WARM_S, AUTO_WARM_WORDS, AUTO_WARM_PERIOD_US, AUTO_WARM_IDLE_S = 10., 1, 254, 60.
AUTO_WARM_CLEARANCE = 10.
# An optional pre-spit right after a pass's warm-up, at the warm spot (the sponge by default), in the same board run
# ([polydye] warm_prespit,
# POLYDYE_PASS PRESPIT): the stock pattern every STOCK_PERIOD_US for that many seconds (at most PRESPIT_MAX_S). Off by
# default; for testing whether cyan, which still missed the first 9-14 mm of some passes after a gentle warm-up, needs
# real ejection first.
PRESPIT_MAX_S = 5.
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
DATA_HELLO, DATA_PUT, DATA_ERR = 0x01, 0x10, 0xFF


class DataPortError(Exception):
    pass


def data_port_request(path, seq, ftype, payload=b"", timeout=3.):
    """One request on the board's data port (protocol in README.md, "Data port"). Returns the JSON reply."""
    import serial  # pyserial is part of klippy's environment
    ser = serial.Serial(path, 115200, timeout=timeout)
    try:
        head = struct.pack("<BBI", ftype, seq, len(payload))
        crc = zlib.crc32(payload, zlib.crc32(head)) & 0xffffffff
        ser.write(b"PD" + head + payload + struct.pack("<I", crc))
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
        # Pass direction and the cartridge nozzle-row centre relative to the hotend nozzle, in bed coordinates.
        # Required: they depend on the mount, and a wrong guess would put ink in the wrong place.
        self.pass_axis = config.getchoice('pass_axis', {'x': 'x', 'y': 'y'})
        self.offset_x = config.getfloat('offset_x')
        self.offset_y = config.getfloat('offset_y')
        # Moves the ink of reverse (DIR=-1) passes along the pass axis, to line them up with forward passes.
        self.direction_trim = config.getfloat('direction_trim', 0.)
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
        self.warm_prespit = config.getfloat('warm_prespit', 0., minval=0., maxval=PRESPIT_MAX_S)
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
        gcode.register_command('POLYDYE_CLOCK_TEST', self.cmd_POLYDYE_CLOCK_TEST,
                               desc=self.cmd_POLYDYE_CLOCK_TEST_help)

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
        prespit = gcmd.get_float('PRESPIT', None, minval=0., maxval=PRESPIT_MAX_S)
        if prespit and 'stock' not in self.spit_patterns:
            raise gcmd.error("POLYDYE_PASS: PRESPIT needs newer PolyDye board firmware (the stock pattern); update it "
                             "from the PolyDye page (Board firmware: Update to bundled firmware)")
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
        length = (columns - 1) * pitch
        run_up = speed * speed / (2. * accel) + speed * self._settle() + self.extra_run_up
        # Klipper's minimum_cruise_ratio caps the peak speed of moves that don't cruise for that fraction of
        # their length; make the pass long enough that it always reaches full speed.
        mcr = th_status.get('minimum_cruise_ratio', 0.)
        min_distance = speed * speed / (accel * (1. - mcr))
        run_up = max(run_up, (min_distance - length) / 2. + 0.001)

        # Bed coordinates of the ink -> toolhead coordinates of the hotend.
        start, first, end = pass_geometry(self.pass_axis, bed_x, bed_y, direction, columns, pitch, run_up,
                                          (self.offset_x, self.offset_y), gcode_move.base_position,
                                          self.direction_trim)
        axis = 0 if self.pass_axis == 'x' else 1
        live = self._auto_warm(gcmd, "POLYDYE_PASS", start, direction, warm, warm_words, warm_period, prespit)

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
        if head_cold(self.last_fire, now, self.warm_idle_time):
            why = ("the head hasn't fired since Klipper started" if self.last_fire is None else
                   "the head won't have fired for %.0f s when the pass starts" % (now - self.last_fire,))
            self._warm_head(gcmd, cmd, why, self.warm_seconds, self.warm_pulse, self.warm_period_us, start, direction,
                            prespit)
        return True

    def _warm_head(self, gcmd, cmd, why, seconds, words, period, start, direction, prespit=0.):
        """A warm-up right before a pass: once the moves and passes before it are done, `seconds` of every nozzle at
        `words` every `period` us (None: the warm-up defaults, _warm_plan()), then `prespit` seconds of the stock
        pattern (if any), standing still at the warm spot (_go_to_warm_spot()), in one board run with live ink left
        on. Raises, and the pass doesn't run, if the board can't be seen idle afterwards."""
        toolhead = self.printer.lookup_object('toolhead')
        reactor = self.printer.get_reactor()
        toolhead.wait_moves()
        end = reactor.monotonic() + 5.
        while True:
            st = self._fresh_status()
            if not (st['running'] or st['queued']):
                break
            if reactor.monotonic() > end:
                raise gcmd.error("%s: the board is still busy with passes, so the head can't be warmed" % (cmd,))
            reactor.pause(reactor.monotonic() + 0.05)
        self._check_current_pulse(gcmd, cmd, st)
        spit = int(round(prespit * 1e6 / STOCK_PERIOD_US)) if prespit else 0
        if spit and (st.get('cartridge_template') or '').startswith('black'):
            raise gcmd.error("%s: the pre-spit is the stock pattern, for a colour cartridge template, not %s"
                             % (cmd, st['cartridge_template']))
        words, period, columns = self._warm_plan(gcmd, cmd, seconds, words, period)
        back, where = self._go_to_warm_spot(gcmd, cmd, start, direction)
        gcmd.respond_info("PolyDye: %s, so a head %s%s comes first, %s" % (
            why, warm_text(columns, words, period),
            ", then a %g s pre-spit (%d stock-pattern columns every %d us)" % (prespit, spit, STOCK_PERIOD_US)
            if spit else "", where))
        self.testing = True
        try:
            self.spit_run_cmd.send([self.oid, spit, SPIT_PATTERNS['stock' if spit else 'all'], 0,
                                    STOCK_PERIOD_US if spit else SPIT_PERIOD_US, columns, words, period])
            deadline = reactor.monotonic() + (columns * period + spit * STOCK_PERIOD_US) / 1e6 + 5.
            while True:
                reactor.pause(reactor.monotonic() + 0.2)
                if self.printer.is_shutdown():
                    raise gcmd.error(self._test_shutdown_text(cmd, "warm-up", True))
                if not self._fresh_status()['running']:
                    break
                if reactor.monotonic() > deadline:
                    raise gcmd.error("%s: the board didn't finish the warm-up" % (cmd,))
        finally:
            self.testing = False
            idle = self._back_from_warm_spot(gcmd, back)
        if not idle:
            raise gcmd.error("%s: the board may still be firing after the warm-up, so the cartridge stays over the "
                             "warm-up spot and the pass doesn't run" % (cmd,))
        self.last_fire = self.mcu.estimated_print_time(reactor.monotonic())

    def _go_to_warm_spot(self, gcmd, cmd, start, direction):
        """Moves the cartridge to where the warm-up runs (warm_position), off the print and waits there:
        - `pass`: warm_clearance mm behind the pass's run-up start along pass_axis (within the axis), at the current Z;
          the pass travels on from there (a short way, so the head stays hot).
        - `sponge` or a bed spot: up by POLYDYE_WIPE's z_hop (within the Z axis), then over the wipe sponge
          (POLYDYE_WIPE's X) or the spot; _back_from_warm_spot() brings it back.
        Returns (where to come back to for _back_from_warm_spot(), None for `pass`; a description for messages). If
        the move fails, the G-code state is put back and the error raised: nothing has fired."""
        toolhead = self.printer.lookup_object('toolhead')
        reactor = self.printer.get_reactor()
        eventtime = reactor.monotonic()
        th = toolhead.get_status(eventtime)
        if self.warm_position == 'pass':
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
        if self.warm_position == 'sponge':
            if 'x' not in variables:
                raise gcmd.error("%s: warm_position is the wipe sponge, but there's no POLYDYE_WIPE macro to say where "
                                 "it is: include polydye_macros.cfg, or set warm_position in polydye.cfg" % (cmd,))
            x, y, where = float(variables['x']), None, "over the wipe sponge (X=%.1f)" % (float(variables['x']),)
        else:
            x, y = self.warm_position[0] - self.offset_x, self.warm_position[1] - self.offset_y
            where = "over X=%.1f Y=%.1f" % self.warm_position
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
        boost_was_on = True
        if live:
            st = self._fresh_status()
            self._check_current_pulse(gcmd, "POLYDYE_ENABLE", st)
            boost_was_on = st['boost']
        self.mode_cmd.send([self.oid, 1, live])
        if not boost_was_on:
            # The rails need a moment after the boost turns on, and the board refuses live ink that comes sooner:
            # keep the next moves (and so the next pass) far enough out.
            toolhead = self.printer.lookup_object('toolhead')
            ready = self.mcu.estimated_print_time(self.printer.get_reactor().monotonic()) + BOOST_SETTLE
            wait = ready - toolhead.get_last_move_time()
            if wait > 0.:
                toolhead.dwell(wait)
        gcmd.respond_info("PolyDye boost on, %s" % ("live ink" if live else "dry run (no firing)",))

    cmd_POLYDYE_DISABLE_help = "Wait for queued moves, then turn off PolyDye live output and the boost"

    def cmd_POLYDYE_DISABLE(self, gcmd):
        self.printer.lookup_object('toolhead').wait_moves()
        self.mode_cmd.send([self.oid, 0, 0])
        gcmd.respond_info("PolyDye off")

    cmd_POLYDYE_CANCEL_help = "Drop PolyDye passes that are queued but not started"

    def cmd_POLYDYE_CANCEL(self, gcmd):
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
            " stored_passes=%d store_free=%dkB\n%s\npass_axis=%s offset_x=%.3f offset_y=%.3f"
            " direction_trim=%.3f dpi=%.0f pass_speed=%.1f%s"
            % (st['boost'], st['live'], st['running'], st['queued'], st['passes'], st['late_max_us'],
               st['faults'], st['store_count'], st['store_free'] // 1024, cartridge_text(st), self.pass_axis,
               self.offset_x, self.offset_y, self.direction_trim, self.dpi, self.pass_speed,
               "\nsignal check: %s held %s, %d s left" % (level['line'], level['state'], level['remaining'])
               if level else ""))

    def _respond_offsets(self, gcmd):
        gcmd.respond_info("PolyDye: offset_x=%.3f offset_y=%.3f direction_trim=%.3f (until the next restart: Store "
                          "on the PolyDye page or edit polydye.cfg to keep them)"
                          % (self.offset_x, self.offset_y, self.direction_trim))

    cmd_POLYDYE_OFFSET_help = "Set the cartridge offset (X, Y) and direction trim (TRIM) in mm from the next pass on"

    def cmd_POLYDYE_OFFSET(self, gcmd):
        # Parse everything before changing anything, so a bad value leaves the offsets alone.
        x = gcmd.get_float('X', self.offset_x)
        y = gcmd.get_float('Y', self.offset_y)
        trim = gcmd.get_float('TRIM', self.direction_trim)
        self.offset_x, self.offset_y, self.direction_trim = x, y, trim
        self._respond_offsets(gcmd)

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
            self.data_seq = (self.data_seq + 1) & 0xff
            hello = data_port_request(self.data_port, self.data_seq, DATA_HELLO)
            cart = 1 if hello.get('cart') == 'black' else 0
            if cart and pattern == 'stock':
                raise ValueError("PATTERN=%s is for a colour cartridge template" % (pattern,))
            self.data_seq = (self.data_seq + 1) & 0xff
            data_port_request(self.data_port, self.data_seq, DATA_PUT,
                              struct.pack("<HHBBH", layer, pass_num, cart, 0, total) + data)
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
        columns = max(1, int(round(warm * 1e6 / period)))
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
            logging.exception("polydye: fire test cleanup failed")

    def get_status(self, eventtime):
        return dict(self.last_status, last_pass=self.last_pass, pass_axis=self.pass_axis, offset_x=self.offset_x,
                    offset_y=self.offset_y, direction_trim=self.direction_trim, spit_patterns=list(self.spit_patterns),
                    spit_random=self.spit_random_cmd is not None, warm_max_us=self.warm_max_us,
                    warm_max_s=self.warm_max_s, warm_duty_ppm=self.warm_duty_ppm, templates=list(self.templates),
                    board_soft_reset=self.board_soft_reset,
                    auto_warm={'available': self.spit_run_cmd is not None and self.warm_duty_ppm is not None,
                               'seconds': self.warm_seconds, 'pulse_words': self.warm_pulse,
                               'period_us': self.warm_period_us, 'idle_time': self.warm_idle_time,
                               'position': self.warm_position if isinstance(self.warm_position, str)
                               else list(self.warm_position), 'clearance': self.warm_clearance,
                               'prespit': self.warm_prespit},
                    head_idle_s=None if self.last_fire is None else
                    round(max(0., self.mcu.estimated_print_time(eventtime) - self.last_fire), 1))


def load_config(config):
    return PolyDye(config)
