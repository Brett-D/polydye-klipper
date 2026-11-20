# PolyDye inkjet support: schedules ink passes on the PolyDye board, a secondary Klipper MCU.
#
# Install by running install.sh from the polydye-klipper repo (see README.md).
#
# Copyright (C) 2026  polydye-klipper contributors
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import json
import logging
import struct
import zlib

import mcu as mcu_mod

PASS_INFO = "polydye_pass_info oid=%c layer=%hu pass=%hu present=%c columns=%hu cart=%c"
STATUS = ("polydye_status oid=%c flags=%c queued=%c passes=%u late_max=%u"
          " faults=%u store_count=%hu store_free=%u")
CARTRIDGE = "polydye_cartridge oid=%c template=%c pulse=%c"
LEVEL = "polydye_level oid=%c line=%c state=%c remaining=%hu"
# Cartridge waveform templates in polydye_set_template numbering: Sprite_tm's four, then color_b_late (color_b with
# the stock PolyDye firmware's timing), which needs board firmware with polydye_set_clock.
TEMPLATES = ('color_a', 'color_b', 'black_a', 'black_b', 'color_b_late')
NEW_TEMPLATES = ('color_b_late',)
CLOCK = "polydye_clock oid=%c stock=%c nominal_hz=%u measured_hz=%u"
# Bus clocks: ours (POLYDYE_BUS_HZ) and the stock firmware's setup. The stock setup's divider of 1 acts as 2 on the
# board (measured 2.82-2.83 MHz), so it runs at 2.857 MHz, not the 5.714 MHz it looks like on paper.
CLOCKS = ('ours', 'stock')
CLOCK_HZ = {'ours': 3333333., 'stock': 2857142.}
# Longest fire pulse for live ink, whatever the clock (the board's POLYDYE_PULSE_MAX_NS): 100 % of the heater energy
# of Sprite_tm's 1.8 us at the cartridge's 16 V, at the 12.6 V the PolyDye high rail measures.
PULSE_MAX_US = 2.9
RAIL_V, REF_V, REF_US = 12.6, 16., 1.8
# polydye_set_pulse range and the cartridge bus clock (one word of fire pulse = 1 / PULSE_WORD_HZ s): the board's
# POLYDYE_PULSE_MIN, POLYDYE_PULSE_MAX and POLYDYE_BUS_HZ.
PULSE_MIN, PULSE_MAX, PULSE_WORD_HZ = 6, 12, 3333333.
FLAG_BOOST, FLAG_LIVE, FLAG_RUNNING, FLAG_LEVEL = 1, 2, 4, 8
# Signal check (polydye_set_level): the non-fire cartridge lines in bus-bit order, and their rail. The low-rail lines
# go through the CD4504 on the reference design's "9 V" rail, the others through the one on its "16 V" rail.
LEVEL_LINES = ('D1', 'D2', 'D3', 'CSYNC', 'S2', 'S4', 'S1', 'S5', 'DCLK', 'S3')
LOW_RAIL_LINES = ('D1', 'D2', 'D3', 'CSYNC')
LEVEL_STATES = {'off': 0, 'high': 1, 'low': 2}
LEVEL_STATE_NAMES = {1: 'high', 2: 'low', 3: 'slow'}
LINE_NONE = 255
# POLYDYE_FIRE_TEST's default stripes, TEMPLATE:PULSE[:CLOCK]: an A/B matrix of our timing and the stock firmware's
# (template and bus clock) at two pulse lengths, or, for board firmware without the stock options, five pairs.
# The longer pulses are the longest within PULSE_MAX_US: 9 words (2.7 us) at our clock, 8 (2.8 us) at the stock one.
FIRE_TEST_STRIPES = ("color_b:6:ours,color_b_late:6:ours,color_b:6:stock,color_b_late:6:stock,"
                     "color_b:9:ours,color_b_late:9:ours,color_b:8:stock,color_b_late:8:stock")
FIRE_TEST_STRIPES_BASIC = "color_b:6,color_b:8,color_b:9,color_a:6,color_a:9"
# polydye_spit: the board's POLYDYE_SPIT_HZ, and POLYDYE_SPIT_MAX columns
SPIT_HZ, SPIT_MAX_S = 1000, 10.
# Test patterns: every nozzle, or positions 14-90 of each colour (what the stock firmware fires).
TEST_PATTERNS = ('all', 'mid')
MID_POSITIONS = range(14, 91)
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


def parse_stripes(text):
    """POLYDYE_FIRE_TEST STRIPES: "template:pulse[:clock],..." -> [(template, pulse, clock or None)].
    Raises ValueError."""
    stripes = []
    for item in text.replace(' ', '').split(','):
        if not item:
            continue
        parts = item.lower().split(':')
        if len(parts) not in (2, 3) or parts[0] not in TEMPLATES:
            raise ValueError("each stripe is TEMPLATE:PULSE[:CLOCK] with TEMPLATE one of %s, e.g. color_b:8 or "
                             "color_b_late:10:stock" % (", ".join(TEMPLATES),))
        try:
            words = int(parts[1])
        except ValueError:
            raise ValueError("pulse '%s' is not a whole number of words" % (parts[1],))
        if not PULSE_MIN <= words <= PULSE_MAX:
            raise ValueError("pulse %d is outside %d-%d words" % (words, PULSE_MIN, PULSE_MAX))
        clock = parts[2] if len(parts) == 3 else None
        if clock is not None and clock not in CLOCKS:
            raise ValueError("clock '%s' must be ours or stock" % (clock,))
        stripes.append((parts[0], words, clock))
    if not stripes:
        raise ValueError("no stripes given")
    return stripes


def pulse_us(words, clock):
    """Fire pulse length in us at a bus clock."""
    return words * 1e6 / CLOCK_HZ.get(clock or 'ours', PULSE_WORD_HZ)


def pulse_energy_pct(us):
    """Heater energy of a pulse on the PolyDye high rail, in % of Sprite_tm's 1.8 us at 16 V (energy ~ V^2 t)."""
    return 100. * RAIL_V * RAIL_V * us / (REF_V * REF_V * REF_US)


def max_pulse_words(clock):
    """Longest pulse in bus words that stays within PULSE_MAX_US at a clock."""
    return min(PULSE_MAX, int(PULSE_MAX_US * CLOCK_HZ[clock or 'ours'] / 1e6 + 1e-9))


def pulse_limit_error(words, clock):
    """None, or why `words` at `clock` is longer than the live-ink limit."""
    clock = clock or 'ours'
    if words <= max_pulse_words(clock):
        return None
    return ("fire pulse %d words is %.2f us at the %s bus clock; the limit is %.1f us (at most %d words there)"
            % (words, pulse_us(words, clock), clock, PULSE_MAX_US, max_pulse_words(clock)))


def color_column(positions):
    """One column of nozzle data firing the given positions (0-111) of all three colours."""
    col = bytearray(NOZDATA_SIZE)
    for pos in positions:
        for c in range(3):
            col[COLOR_BYTE_ORDER[c][pos % 14] + 14 * c] |= 1 << (pos // 14)
    return bytes(col)


def stripe_positions(axis, x, y, spacing, count):
    """Bed (X, Y) of each fire-test stripe's first column: stripes run along the pass axis, side by side across it."""
    if axis == 'y':
        return [(x + i * spacing, y) for i in range(count)]
    return [(x, y + i * spacing) for i in range(count)]


def test_pass_data(pattern, columns, repeat=1):
    """Nozzle data for POLYDYE_UPLOAD_TEST_PASS: `columns` columns of `pattern`, each sent `repeat` times in a row.
    Returns (data, column count). Raises ValueError for an unknown pattern."""
    full, empty = b"\xff" * NOZDATA_SIZE, b"\x00" * NOZDATA_SIZE
    if pattern == 'alternate':
        cols = [full if i % 2 == 0 else empty for i in range(columns)]
    elif pattern in ('all', 'none', 'mid'):
        cols = [{'all': full, 'none': empty, 'mid': color_column(MID_POSITIONS)}[pattern]] * columns
    else:
        raise ValueError("PATTERN must be alternate, all, none or mid")
    return b"".join(col * repeat for col in cols), columns * repeat


def init_pulse_clock(pulse_words, bus_clock, has_clock):
    """The pulse and clock settings to send on connect, as (words, None) / (None, clock) steps in order.

    The board refuses any moment where the pulse would be over the limit, and after a Klipper RESTART it still has the
    previous pulse and clock. The slower stock clock goes after a pulse that's within the limit there; our clock goes
    first, since a pulse within the limit at the stock clock is within it at ours. With pulse_words but no bus_clock,
    the clock goes back to ours (the board's default) so the pulse is checked against the clock it was set for.
    """
    clock = (bus_clock or ('ours' if pulse_words is not None else None)) if has_clock else None
    if clock == 'stock':
        return [(pulse_words or PULSE_MIN, None), (None, 'stock')]
    steps = [(None, 'ours')] if clock == 'ours' else []
    if pulse_words is not None:
        steps.append((pulse_words, None))
    return steps


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
        # Waveform template for the board to use; unset leaves the board's own choice (color_b unless changed on
        # its console). Needs board firmware with polydye_set_template.
        self.cartridge_template = config.get('cartridge_template', None)
        if self.cartridge_template is not None:
            self.cartridge_template = self.cartridge_template.strip().lower()
            if self.cartridge_template not in TEMPLATES:
                raise config.error("[polydye] cartridge_template must be one of: %s" % (", ".join(TEMPLATES),))
        # F3/F5 fire pulse in bus words of 0.3 us; unset leaves the board's own (6 = 1.8 us, Sprite_tm's value). Longer
        # pulses put more energy into each drop, e.g. when the boost rails are below the cartridge's nominal voltage.
        self.pulse_words = config.getint('pulse_words', None, minval=PULSE_MIN, maxval=PULSE_MAX)
        # Cartridge bus clock: ours (3.333 MHz) or the stock PolyDye firmware's setup (5.714 MHz nominal); unset leaves
        # the board's own (ours). Needs board firmware with polydye_set_clock.
        self.bus_clock = config.get('bus_clock', None)
        if self.bus_clock is not None:
            self.bus_clock = self.bus_clock.strip().lower()
            if self.bus_clock not in CLOCKS:
                raise config.error("[polydye] bus_clock must be ours or stock")
        if self.pulse_words is not None and pulse_limit_error(self.pulse_words, self.bus_clock):
            raise config.error("[polydye] pulse_words: %s" % (pulse_limit_error(self.pulse_words, self.bus_clock),))
        self.dpi = config.getfloat('dpi', 600., above=0.)
        self.pass_speed = config.getfloat('pass_speed', 50., above=0.)
        self.travel_speed = config.getfloat('travel_speed', 150., above=0.)
        self.extra_run_up = config.getfloat('extra_run_up', 1., minval=0.)
        self.settle_time = config.getfloat('settle_time', None, minval=0.)
        self.min_lead_time = config.getfloat('min_lead_time', 0.025, minval=0.005)
        self.query_pass_cmd = self.query_cmd = None
        self.schedule_cmd = self.mode_cmd = self.cancel_cmd = None
        self.set_template_cmd = self.set_pulse_cmd = self.query_cartridge_cmd = None
        self.set_level_cmd = self.query_level_cmd = self.spit_cmd = None
        self.set_clock_cmd = self.query_clock_cmd = None
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

    def _build_config(self):
        self.mcu.add_config_cmd("config_polydye oid=%d" % (self.oid,))
        cq = self.mcu.alloc_command_queue()
        self.query_pass_cmd = self.mcu.lookup_query_command(
            "polydye_query_pass oid=%c layer=%hu pass=%hu", PASS_INFO, oid=self.oid, cq=cq)
        self.query_cmd = self.mcu.lookup_query_command("polydye_query oid=%c", STATUS, oid=self.oid, cq=cq)
        self.schedule_cmd = self.mcu.lookup_command(
            "polydye_schedule oid=%c clock=%u period=%u frac=%hu layer=%hu pass=%hu", cq=cq)
        self.mode_cmd = self.mcu.lookup_command("polydye_set_mode oid=%c boost=%c live=%c", cq=cq)
        self.cancel_cmd = self.mcu.lookup_command("polydye_cancel oid=%c", cq=cq)
        # The signal check came after the cartridge commands; older board firmware just doesn't offer it.
        if self.mcu.try_lookup_command("polydye_query_level oid=%c") is not None:
            self.set_level_cmd = self.mcu.lookup_command("polydye_set_level oid=%c line=%c state=%c", cq=cq)
            self.query_level_cmd = self.mcu.lookup_query_command("polydye_query_level oid=%c", LEVEL, oid=self.oid,
                                                                 cq=cq)
            self.spit_cmd = self.mcu.lookup_command("polydye_spit oid=%c columns=%hu pattern=%c", cq=cq)
        # color_b_late and the bus clock choice came with the stock-firmware comparison.
        if self.mcu.try_lookup_command("polydye_query_clock oid=%c") is not None:
            self.set_clock_cmd = self.mcu.lookup_command("polydye_set_clock oid=%c stock=%c", cq=cq)
            self.query_clock_cmd = self.mcu.lookup_query_command("polydye_query_clock oid=%c", CLOCK, oid=self.oid,
                                                                 cq=cq)
        else:
            for option, value in (('cartridge_template', self.cartridge_template in NEW_TEMPLATES),
                                  ('bus_clock', self.bus_clock is not None)):
                if value:
                    raise self.printer.config_error(
                        "[polydye] %s: %s needs newer PolyDye board firmware. Update it from the PolyDye page (Board "
                        "firmware), or change it in polydye.cfg." % (option, getattr(self, option)))
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
        # Sent on every connect: the board forgets both when it restarts.
        if self.cartridge_template is not None:
            self.mcu.add_config_cmd("polydye_set_template oid=%d template=%d"
                                    % (self.oid, TEMPLATES.index(self.cartridge_template)), is_init=True)
        for words, clock in init_pulse_clock(self.pulse_words, self.bus_clock, self.set_clock_cmd is not None):
            if words is not None:
                self.mcu.add_config_cmd("polydye_set_pulse oid=%d words=%d" % (self.oid, words), is_init=True)
            else:
                self.mcu.add_config_cmd("polydye_set_clock oid=%d stock=%d" % (self.oid, CLOCKS.index(clock)),
                                        is_init=True)

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
                template = TEMPLATES[cart['template']] if cart['template'] < len(TEMPLATES) else str(cart['template'])
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
        bus_clock = None if clock is None else (CLOCKS[clock['stock']] if clock['stock'] < len(CLOCKS)
                                                else str(clock['stock']))
        # The clock's real rate, not the board's nominal one (firmware b195932 reports 5.714 MHz for the stock clock).
        bus_hz = CLOCK_HZ.get(bus_clock, PULSE_WORD_HZ)
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

    cmd_POLYDYE_PASS_help = "Print one ink pass along pass_axis (the pass data must already be uploaded)"

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
        logging.info("polydye: layer %d pass %d: %d columns along %s, first at print_time %.6f (clock %d), "
                     "period %.4f ticks, t0 %.6f, run-up %.4f mm, toolhead first column %.3f,%.3f", layer, pass_num,
                     columns, self.pass_axis, t_first, clock_first, period_ticks + frac / 65536., t0, run_up,
                     first[0], first[1])

    cmd_POLYDYE_ENABLE_help = "Turn on the PolyDye boost (LIVE=1 also allows real ink; LIVE=0 is a dry run)"

    def _check_current_pulse(self, gcmd, name, st):
        """Refuses to go on from a board whose current pulse and clock are over the limit (older firmware doesn't
        stop that combination), so live ink never uses it and a test can't put it back afterwards."""
        if st.get('pulse_words') is not None and pulse_limit_error(st['pulse_words'], st.get('bus_clock')):
            clock = st.get('bus_clock') or 'ours'
            raise gcmd.error("%s: the board is set to an over-limit pulse: %s. Set a shorter one first, e.g. "
                             "POLYDYE_CARTRIDGE PULSE=%d" % (name, pulse_limit_error(st['pulse_words'], clock),
                                                             max_pulse_words(clock)))

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

    cmd_POLYDYE_CARTRIDGE_help = ("Set the cartridge waveform template (TEMPLATE=color_a|color_b|black_a|black_b|"
                                  "color_b_late), fire pulse (PULSE=6..12 words) and bus clock (CLOCK=ours|stock) "
                                  "until the next restart; PolyDye must be off and idle")

    def cmd_POLYDYE_CARTRIDGE(self, gcmd):
        if self.query_cartridge_cmd is None:
            raise gcmd.error("POLYDYE_CARTRIDGE: the PolyDye board firmware is too old; update it from the PolyDye "
                             "page (Board firmware)")
        name = gcmd.get('TEMPLATE', None)
        if name is not None:
            name = name.lower()
            if name not in TEMPLATES:
                raise gcmd.error("POLYDYE_CARTRIDGE: TEMPLATE must be one of %s" % (", ".join(TEMPLATES),))
        pulse = gcmd.get_int('PULSE', None, minval=PULSE_MIN, maxval=PULSE_MAX)
        clock = gcmd.get('CLOCK', None)
        if clock is not None:
            clock = clock.lower()
            if clock not in CLOCKS:
                raise gcmd.error("POLYDYE_CARTRIDGE: CLOCK must be ours or stock")
        if self.set_clock_cmd is None and (clock is not None or name in NEW_TEMPLATES):
            raise gcmd.error("POLYDYE_CARTRIDGE: %s needs newer PolyDye board firmware; update it from the PolyDye "
                             "page (Board firmware)" % ("CLOCK" if clock is not None else name,))
        if name is not None or pulse is not None or clock is not None:
            # The board refuses a change while powered or busy by shutting down; check first for a plain error.
            self.printer.lookup_object('toolhead').wait_moves()
            st = self._fresh_status()
            change = (name is not None and name != st['cartridge_template']) or (
                pulse is not None and pulse != st['pulse_words']) or (
                clock is not None and clock != st.get('bus_clock'))
            # Also when nothing changes: the result must be within the limit, not just different.
            limit = pulse_limit_error(pulse if pulse is not None else st['pulse_words'],
                                      clock if clock is not None else st.get('bus_clock'))
            if limit:
                raise gcmd.error("POLYDYE_CARTRIDGE: %s" % (limit,))
            if change and (st['boost'] or st['live'] or st['running'] or st['queued']):
                raise gcmd.error("POLYDYE_CARTRIDGE: turn PolyDye off (POLYDYE_DISABLE) and let queued passes "
                                 "finish before changing the template, the pulse or the clock")
            self._send_cartridge(st, name, pulse, clock)
        st = self._fresh_status()
        gcmd.respond_info("PolyDye %s (set cartridge_template, pulse_words and bus_clock in polydye.cfg to keep them "
                          "after a restart)" % (cartridge_text(st),))

    def _send_cartridge(self, st, name, pulse, clock):
        """Sends the changed settings, in an order the board accepts: it refuses any moment where the pulse would be
        over the limit, so a slower clock (longer words) goes after the pulse and a faster one before it."""
        old_clock = st.get('bus_clock') or 'ours'
        clock_first = clock is not None and CLOCK_HZ[clock] > CLOCK_HZ[old_clock]
        if name is not None and name != st['cartridge_template']:
            self.set_template_cmd.send([self.oid, TEMPLATES.index(name)])
        if clock_first:
            self.set_clock_cmd.send([self.oid, CLOCKS.index(clock)])
        if pulse is not None:
            self.set_pulse_cmd.send([self.oid, pulse])
        if clock is not None and not clock_first:
            self.set_clock_cmd.send([self.oid, CLOCKS.index(clock)])

    cmd_POLYDYE_UPLOAD_TEST_PASS_help = ("Upload a test pattern pass (PATTERN=alternate|all|none|mid, each column REPEAT "
                                         "times) to the PolyDye board; not during a print")

    def cmd_POLYDYE_UPLOAD_TEST_PASS(self, gcmd):
        layer = gcmd.get_int('LAYER', 0, minval=0, maxval=65535)
        pass_num = gcmd.get_int('PASS', 0, minval=0, maxval=65535)
        columns = gcmd.get_int('COLUMNS', 600, minval=1, maxval=65535)
        pattern = gcmd.get('PATTERN', 'alternate').lower()
        repeat = gcmd.get_int('REPEAT', 1, minval=1, maxval=8)
        try:
            data, total = test_pass_data(pattern, columns, repeat)
        except ValueError as e:
            raise gcmd.error("POLYDYE_UPLOAD_TEST_PASS: %s" % (e,))
        if total > 65535:
            raise gcmd.error("POLYDYE_UPLOAD_TEST_PASS: COLUMNS x REPEAT must be at most 65535 (it is %d)" % (total,))
        if not self.data_port:
            raise gcmd.error("POLYDYE_UPLOAD_TEST_PASS: set data_port in [polydye]")
        try:
            self.data_seq = (self.data_seq + 1) & 0xff
            hello = data_port_request(self.data_port, self.data_seq, DATA_HELLO)
            cart = 1 if hello.get('cart') == 'black' else 0
            if cart and pattern == 'mid':
                raise ValueError("PATTERN=mid is for a colour cartridge template")
            self.data_seq = (self.data_seq + 1) & 0xff
            data_port_request(self.data_port, self.data_seq, DATA_PUT,
                              struct.pack("<HHBBH", layer, pass_num, cart, 0, total) + data)
        except (DataPortError, OSError, ValueError) as e:
            raise gcmd.error("POLYDYE_UPLOAD_TEST_PASS: %s" % (e,))
        gcmd.respond_info("PolyDye: stored test pass layer %d pass %d (%d columns, %s%s)"
                          % (layer, pass_num, total, pattern,
                             ", each of %d sent %d times" % (columns, repeat) if repeat > 1 else ""))

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
        """Waits until the board reports the boost and live output off and nothing queued or printing."""
        reactor = self.printer.get_reactor()
        end = reactor.monotonic() + timeout
        while True:
            st = self._fresh_status()
            if not (st['boost'] or st['live'] or st['running'] or st['queued']):
                return st
            if reactor.monotonic() > end:
                raise self.printer.command_error("PolyDye: the board didn't turn off and go idle")
            reactor.pause(reactor.monotonic() + 0.1)

    cmd_POLYDYE_FIRE_TEST_help = ("LIVE INK on paper: one stripe per TEMPLATE:PULSE[:CLOCK] in STRIPES, side by side "
                                  "from X, Y, SPACING mm apart, at Z if given, PATTERN=all|mid; restores the template, "
                                  "pulse, clock and G-code state afterwards. UPLOAD=0 prints the pass already stored as "
                                  "LAYER, PASS instead")

    def cmd_POLYDYE_FIRE_TEST(self, gcmd):
        if self.query_cartridge_cmd is None:
            raise gcmd.error("POLYDYE_FIRE_TEST: the PolyDye board firmware is too old; update it from the PolyDye "
                             "page (Board firmware)")
        try:
            stripes = parse_stripes(gcmd.get('STRIPES', FIRE_TEST_STRIPES if self.set_clock_cmd
                                             else FIRE_TEST_STRIPES_BASIC))
        except ValueError as e:
            raise gcmd.error("POLYDYE_FIRE_TEST: STRIPES: %s" % (e,))
        if self.set_clock_cmd is None and any(clock or tpl in NEW_TEMPLATES for tpl, _, clock in stripes):
            raise gcmd.error("POLYDYE_FIRE_TEST: color_b_late and the clock need newer PolyDye board firmware; "
                             "update it from the PolyDye page (Board firmware)")
        pattern = gcmd.get('PATTERN', 'all').lower()
        if pattern not in TEST_PATTERNS:
            raise gcmd.error("POLYDYE_FIRE_TEST: PATTERN must be all or mid")
        x = gcmd.get_float('X', 70.)
        y = gcmd.get_float('Y', 60.)
        z = gcmd.get_float('Z', None, minval=0.2)
        spacing = gcmd.get_float('SPACING', 18., above=0.)
        columns = gcmd.get_int('COLUMNS', 600, minval=1, maxval=4800)
        upload = gcmd.get_int('UPLOAD', 1, minval=0, maxval=1)
        layer = gcmd.get_int('LAYER', 0, minval=0, maxval=65535)
        pass_num = gcmd.get_int('PASS', 0, minval=0, maxval=65535)
        if not upload:
            info = self.query_pass_cmd.send([self.oid, layer, pass_num])
            if not info['present']:
                raise gcmd.error("POLYDYE_FIRE_TEST: layer %d pass %d has not been uploaded" % (layer, pass_num))
            stored = 'black' if info['cart'] == 1 else 'color'
            if any(('black' if tpl.startswith('black') else 'color') != stored for tpl, _, _ in stripes):
                raise gcmd.error("POLYDYE_FIRE_TEST: layer %d pass %d is for a %s cartridge; use %s templates only, or "
                                 "UPLOAD=1" % (layer, pass_num, stored, stored))
            columns = info['columns']
        self._check_not_printing(gcmd, "POLYDYE_FIRE_TEST")
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.wait_moves()
        st = self._fresh_status()
        if st['running'] or st['queued']:
            raise gcmd.error("POLYDYE_FIRE_TEST: wait for queued passes to finish")
        self._check_current_pulse(gcmd, "POLYDYE_FIRE_TEST", st)
        before = (st['cartridge_template'], st['pulse_words'], st.get('bus_clock'))
        stripes = [(tpl, words, clock or before[2]) for tpl, words, clock in stripes]
        for tpl, words, clock in stripes:
            if pulse_limit_error(words, clock):
                raise gcmd.error("POLYDYE_FIRE_TEST: stripe %s:%d: %s" % (tpl, words, pulse_limit_error(words, clock)))
        positions = stripe_positions(self.pass_axis, x, y, spacing, len(stripes))
        gcmd.respond_info("PolyDye fire test, live ink, %d stripes of %d %s columns along %s:\n%s" % (
            len(stripes), columns, pattern if upload else "stored", self.pass_axis.upper(), "\n".join(
                "  %d: X=%.1f Y=%.1f  %s, pulse %d words (%.2f us)%s" % (
                    i + 1, px, py, tpl, words, pulse_us(words, clock), ", %s clock" % (clock,) if clock else "")
                for i, ((tpl, words, clock), (px, py)) in enumerate(zip(stripes, positions)))))
        run = self.printer.lookup_object('gcode').run_script_from_command
        uploaded = None if upload else 'stored'
        # Everything is checked by now; from here on the G-code state and the board settings are always put back.
        run("SAVE_GCODE_STATE NAME=pd_fire_test")
        try:
            if z is not None:
                run("G90\nG1 Z%.3f F900" % (z,))
            for i, ((tpl, words, clock), (px, py)) in enumerate(zip(stripes, positions)):
                run("POLYDYE_DISABLE")
                self._wait_off_and_idle()
                run("POLYDYE_CARTRIDGE TEMPLATE=%s PULSE=%d%s" % (tpl, words, " CLOCK=%s" % (clock,) if clock else ""))
                # The pass store keeps the cartridge type a pass was uploaded for.
                cart = 'black' if tpl.startswith('black') else 'color'
                if upload and cart != uploaded:
                    run("POLYDYE_UPLOAD_TEST_PASS LAYER=%d PASS=%d COLUMNS=%d PATTERN=%s"
                        % (layer, pass_num, columns, pattern))
                    uploaded = cart
                run("POLYDYE_ENABLE LIVE=1")
                run("POLYDYE_PASS LAYER=%d PASS=%d X=%.3f Y=%.3f DIR=1" % (layer, pass_num, px, py))
                gcmd.respond_info("PolyDye fire test: stripe %d/%d queued at X=%.1f Y=%.1f (%s, pulse %d%s)"
                                  % (i + 1, len(stripes), px, py, tpl, words, ", %s clock" % (clock,) if clock else ""))
        finally:
            self._restore_after_test(before)
            if not self.printer.is_shutdown():
                try:
                    run("RESTORE_GCODE_STATE NAME=pd_fire_test")
                except Exception:
                    logging.exception("polydye: fire test couldn't restore the G-code state")
        gcmd.respond_info("PolyDye fire test done; %s" % (cartridge_text(self._fresh_status()),))

    cmd_POLYDYE_SPIT_help = ("LIVE INK, no motion: fire every nozzle (PATTERN=all) or positions 14-90 (mid) for "
                             "SECONDS (max 10) at 1 kHz into a paper towel held under the cartridge, optionally with "
                             "TEMPLATE, PULSE and CLOCK; needs CONFIRM=1")

    def cmd_POLYDYE_SPIT(self, gcmd):
        if self.spit_cmd is None:
            raise gcmd.error("POLYDYE_SPIT: the PolyDye board firmware is too old; update it from the PolyDye page "
                             "(Board firmware)")
        seconds = gcmd.get_float('SECONDS', 2., above=0., maxval=SPIT_MAX_S)
        name = gcmd.get('TEMPLATE', None)
        if name is not None:
            name = name.lower()
            if name not in TEMPLATES:
                raise gcmd.error("POLYDYE_SPIT: TEMPLATE must be one of %s" % (", ".join(TEMPLATES),))
        pulse = gcmd.get_int('PULSE', None, minval=PULSE_MIN, maxval=PULSE_MAX)
        clock = gcmd.get('CLOCK', None)
        if clock is not None:
            clock = clock.lower()
            if clock not in CLOCKS:
                raise gcmd.error("POLYDYE_SPIT: CLOCK must be ours or stock")
        pattern = gcmd.get('PATTERN', 'all').lower()
        if pattern not in TEST_PATTERNS:
            raise gcmd.error("POLYDYE_SPIT: PATTERN must be all or mid")
        if gcmd.get_int('CONFIRM', 0) != 1:
            raise gcmd.error("POLYDYE_SPIT fires every nozzle with live ink: hold a folded paper towel under the "
                             "cartridge, then run it again with CONFIRM=1")
        self._check_not_printing(gcmd, "POLYDYE_SPIT")
        reactor = self.printer.get_reactor()
        self.printer.lookup_object('toolhead').wait_moves()
        st = self._fresh_status()
        if st['running'] or st['queued']:
            raise gcmd.error("POLYDYE_SPIT: wait for queued passes to finish")
        self._check_current_pulse(gcmd, "POLYDYE_SPIT", st)
        before = (st['cartridge_template'], st['pulse_words'], st.get('bus_clock'))
        limit = pulse_limit_error(pulse if pulse is not None else st['pulse_words'], clock or before[2])
        if limit:
            raise gcmd.error("POLYDYE_SPIT: %s" % (limit,))
        columns = max(1, int(round(seconds * SPIT_HZ)))
        try:
            self.mode_cmd.send([self.oid, 0, 0])
            self._wait_off_and_idle()
            if name is not None or pulse is not None or clock is not None:
                self.printer.lookup_object('gcode').run_script_from_command(
                    "POLYDYE_CARTRIDGE" + (" TEMPLATE=%s" % (name,) if name else "")
                    + (" PULSE=%d" % (pulse,) if pulse else "") + (" CLOCK=%s" % (clock,) if clock else ""))
            self.mode_cmd.send([self.oid, 1, 1])
            st = self._fresh_status()
            if not st['live']:
                raise gcmd.error("POLYDYE_SPIT: the board didn't turn live ink on (disarmed on its console?)")
            gcmd.respond_info("PolyDye spit test: %d %s columns at %d Hz, %s"
                              % (columns, "all-nozzle" if pattern == 'all' else "positions 14-90", SPIT_HZ,
                                 cartridge_text(st)))
            # The board starts it no sooner than 0.1 s after the boost came on.
            self.spit_cmd.send([self.oid, columns, TEST_PATTERNS.index(pattern)])
            end = reactor.monotonic() + seconds + 5.
            while True:
                reactor.pause(reactor.monotonic() + 0.2)
                if self.printer.is_shutdown():
                    raise gcmd.error("POLYDYE_SPIT: Klipper shut down during the spit test")
                if not self._fresh_status()['running']:
                    break
                if reactor.monotonic() > end:
                    raise gcmd.error("POLYDYE_SPIT: the board didn't finish the spit test")
        finally:
            self._restore_after_test(before)
        st = self._fresh_status()
        gcmd.respond_info("PolyDye spit test done (faults=%d); %s" % (st['faults'], cartridge_text(st)))

    def _restore_after_test(self, before):
        """Always turn PolyDye off and put the template, pulse and clock back, even after an error (which is re-raised by
        the caller); problems here are logged so they don't hide that error."""
        if self.printer.is_shutdown():
            return
        try:
            self.printer.lookup_object('toolhead').wait_moves()
            self.mode_cmd.send([self.oid, 0, 0])
            st = self._wait_off_and_idle()
            self._send_cartridge(st, before[0], before[1] if before[1] != st['pulse_words'] else None,
                                 before[2] if before[2] is not None and before[2] != st.get('bus_clock') else None)
            self._query_status()
        except Exception:
            logging.exception("polydye: fire test cleanup failed")

    def get_status(self, eventtime):
        return dict(self.last_status, last_pass=self.last_pass, pass_axis=self.pass_axis, offset_x=self.offset_x,
                    offset_y=self.offset_y, direction_trim=self.direction_trim)


def load_config(config):
    return PolyDye(config)
