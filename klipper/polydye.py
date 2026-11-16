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
FLAG_BOOST, FLAG_LIVE, FLAG_RUNNING = 1, 2, 4
TIMING_TOLERANCE = 0.0005  # s: planned pass duration must match the model
STATUS_INTERVAL = 2.  # s between background status queries
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


def pass_geometry(axis, bed_x, bed_y, direction, columns, pitch, run_up, offset, base):
    """Toolhead XY positions for a pass.

    (bed_x, bed_y) is where the first column lands on the bed (the pass-axis coordinate of the first column and
    the cross-axis coordinate of the nozzle-row centre). `offset` is the cartridge's (x, y) offset from the hotend
    nozzle and `base` the G-code base position. Returns (start, first, end): the toolhead XY at the start of the
    run-up, when the first column fires, and at the end of the run-out.
    """
    a = 0 if axis == 'x' else 1
    first = [bed_x - offset[0] + base[0], bed_y - offset[1] + base[1]]
    start, end = list(first), list(first)
    start[a] = first[a] - direction * run_up
    end[a] = first[a] + direction * ((columns - 1) * pitch + run_up)
    return start, first, end


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
        self.dpi = config.getfloat('dpi', 600., above=0.)
        self.pass_speed = config.getfloat('pass_speed', 50., above=0.)
        self.travel_speed = config.getfloat('travel_speed', 150., above=0.)
        self.extra_run_up = config.getfloat('extra_run_up', 1., minval=0.)
        self.settle_time = config.getfloat('settle_time', None, minval=0.)
        self.min_lead_time = config.getfloat('min_lead_time', 0.025, minval=0.005)
        self.query_pass_cmd = self.query_cmd = None
        self.schedule_cmd = self.mode_cmd = self.cancel_cmd = None
        self.last_status = {}
        self.last_pass = None
        self.querying = False
        self.status_timer = None
        printer.register_event_handler('klippy:ready', self._handle_ready)
        printer.register_event_handler('klippy:shutdown', self._handle_shutdown)
        gcode = printer.lookup_object('gcode')
        gcode.register_command('POLYDYE_PASS', self.cmd_POLYDYE_PASS, desc=self.cmd_POLYDYE_PASS_help)
        gcode.register_command('POLYDYE_ENABLE', self.cmd_POLYDYE_ENABLE, desc=self.cmd_POLYDYE_ENABLE_help)
        gcode.register_command('POLYDYE_DISABLE', self.cmd_POLYDYE_DISABLE, desc=self.cmd_POLYDYE_DISABLE_help)
        gcode.register_command('POLYDYE_CANCEL', self.cmd_POLYDYE_CANCEL, desc=self.cmd_POLYDYE_CANCEL_help)
        gcode.register_command('POLYDYE_STATUS', self.cmd_POLYDYE_STATUS, desc=self.cmd_POLYDYE_STATUS_help)
        gcode.register_command('POLYDYE_UPLOAD_TEST_PASS', self.cmd_POLYDYE_UPLOAD_TEST_PASS,
                               desc=self.cmd_POLYDYE_UPLOAD_TEST_PASS_help)

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
            self.printer.get_reactor().update_timer(self.status_timer, self.printer.get_reactor().NEVER)

    def _status_event(self, eventtime):
        if not self.querying:
            try:
                self._query_status()
            except Exception:
                logging.exception("polydye: status query failed")
        return eventtime + STATUS_INTERVAL

    def _query_status(self):
        if self.querying:
            return self.last_status  # another query is in flight; its answer is moments away
        self.querying = True
        try:
            params = self.query_cmd.send([self.oid])
        finally:
            self.querying = False
        flags = params['flags']
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
        }
        return self.last_status

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
                                          (self.offset_x, self.offset_y), gcode_move.base_position)
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

    def cmd_POLYDYE_ENABLE(self, gcmd):
        live = gcmd.get_int('LIVE', 1, minval=0, maxval=1)
        self.mode_cmd.send([self.oid, 1, live])
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
        st = self._query_status()
        gcmd.respond_info(
            "PolyDye: boost=%d live=%d running=%d queued=%d passes=%d late_max=%.1fus faults=%d"
            " stored_passes=%d store_free=%dkB\npass_axis=%s offset_x=%.3f offset_y=%.3f dpi=%.0f pass_speed=%.1f"
            % (st['boost'], st['live'], st['running'], st['queued'], st['passes'], st['late_max_us'],
               st['faults'], st['store_count'], st['store_free'] // 1024, self.pass_axis, self.offset_x,
               self.offset_y, self.dpi, self.pass_speed))

    cmd_POLYDYE_UPLOAD_TEST_PASS_help = "Upload a test pattern pass to the PolyDye board (for dry runs; not during a print)"

    def cmd_POLYDYE_UPLOAD_TEST_PASS(self, gcmd):
        layer = gcmd.get_int('LAYER', 0, minval=0, maxval=65535)
        pass_num = gcmd.get_int('PASS', 0, minval=0, maxval=65535)
        columns = gcmd.get_int('COLUMNS', 600, minval=1, maxval=65535)
        pattern = gcmd.get('PATTERN', 'alternate').lower()
        full, empty = b"\xff" * NOZDATA_SIZE, b"\x00" * NOZDATA_SIZE
        if pattern == 'alternate':
            data = b"".join(full if i % 2 == 0 else empty for i in range(columns))
        elif pattern in ('all', 'none'):
            data = (full if pattern == 'all' else empty) * columns
        else:
            raise gcmd.error("POLYDYE_UPLOAD_TEST_PASS: PATTERN must be alternate, all or none")
        if not self.data_port:
            raise gcmd.error("POLYDYE_UPLOAD_TEST_PASS: set data_port in [polydye]")
        try:
            self.data_seq = (self.data_seq + 1) & 0xff
            hello = data_port_request(self.data_port, self.data_seq, DATA_HELLO)
            cart = 1 if hello.get('cart') == 'black' else 0
            self.data_seq = (self.data_seq + 1) & 0xff
            data_port_request(self.data_port, self.data_seq, DATA_PUT,
                              struct.pack("<HHBBH", layer, pass_num, cart, 0, columns) + data)
        except (DataPortError, OSError, ValueError) as e:
            raise gcmd.error("POLYDYE_UPLOAD_TEST_PASS: %s" % (e,))
        gcmd.respond_info("PolyDye: stored test pass layer %d pass %d (%d columns, %s)"
                          % (layer, pass_num, columns, pattern))

    def get_status(self, eventtime):
        return dict(self.last_status, last_pass=self.last_pass, pass_axis=self.pass_axis, offset_x=self.offset_x,
                    offset_y=self.offset_y)


def load_config(config):
    return PolyDye(config)
