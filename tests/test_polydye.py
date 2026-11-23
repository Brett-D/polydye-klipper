#!/usr/bin/env python3
"""Unit tests for the pass timing and geometry in klipper/polydye.py (no Klipper needed).

This file is part of polydye-klipper (GPLv3).
"""
import math
import os
import sys
import types
import unittest

# polydye.py imports klippy's "mcu" module at load time; the pure functions under test don't use it.
sys.modules.setdefault("mcu", types.ModuleType("mcu"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "klipper"))
import polydye  # noqa: E402


class PassTiming(unittest.TestCase):
    def test_first_column_after_run_up(self):
        # 50 mm/s at 3000 mm/s^2: 1/60 s and 5/12 mm to reach speed; the rest of a 3 mm run-up is at full speed.
        t_first, period, duration = polydye.pass_timing(10.0, 50., 3000., 3.0, 600, 25.4 / 600)
        self.assertAlmostEqual(t_first, 10.0 + 50. / 3000. + (3.0 - 50. * 50. / 6000.) / 50., places=12)
        self.assertAlmostEqual(period, 25.4 / 600 / 50, places=12)
        length = 599 * 25.4 / 600
        expect = 2 * 50. / 3000. + (length + 6.0 - 2 * 50. * 50. / 6000.) / 50.
        self.assertAlmostEqual(duration, expect, places=12)

    def test_run_up_shorter_than_acceleration_is_refused(self):
        with self.assertRaises(ValueError):
            polydye.pass_timing(0., 100., 1000., 2.0, 10, 0.1)  # needs 5 mm to reach 100 mm/s


class PassGeometry(unittest.TestCase):
    def test_y_pass_forward(self):
        start, first, end = polydye.pass_geometry('y', 100., 40., 1, 600, 25.4 / 600, 3.0, (-0.2, -77.7), (0, 0, 0, 0))
        self.assertAlmostEqual(first[0], 100.2)
        self.assertAlmostEqual(first[1], 117.7)
        self.assertAlmostEqual(start[1], 114.7)
        self.assertAlmostEqual(start[0], 100.2)
        self.assertAlmostEqual(end[1], 117.7 + 599 * 25.4 / 600 + 3.0)

    def test_x_pass_backward_with_gcode_offset(self):
        start, first, end = polydye.pass_geometry('x', 100., 40., -1, 11, 0.5, 2.0, (64., 0.), (1., 2., 0, 0))
        self.assertAlmostEqual(first[0], 100. - 64. + 1.)
        self.assertAlmostEqual(first[1], 42.)
        self.assertAlmostEqual(start[0], first[0] + 2.0)
        self.assertAlmostEqual(end[0], first[0] - (10 * 0.5 + 2.0))
        self.assertAlmostEqual(end[1], first[1])

    def test_direction_trim_moves_only_reverse_passes_along_the_pass_axis(self):
        args = (600, 25.4 / 600, 3.0, (-0.2, -77.7), (0, 0, 0, 0))
        fwd = polydye.pass_geometry('y', 100., 40., 1, *args, trim=0.3)
        self.assertEqual(fwd, polydye.pass_geometry('y', 100., 40., 1, *args))
        plain = polydye.pass_geometry('y', 100., 40., -1, *args)
        rev = polydye.pass_geometry('y', 100., 40., -1, *args, trim=0.3)
        for p, r in zip(plain, rev):
            self.assertAlmostEqual(r[0], p[0])
            self.assertAlmostEqual(r[1], p[1] + 0.3)


class Nudge(unittest.TestCase):
    def test_ink_moves_the_way_of_the_nudge(self):
        # Ink lands at bed target - configured offset + true offset; the toolhead goes to target - offset.
        x, y, trim = polydye.nudge_offsets(62.6, -20.2, 0., 0.1, -0.05, 0.)
        _, first_before, _ = polydye.pass_geometry('y', 100., 40., 1, 10, 0.1, 3., (62.6, -20.2), (0, 0, 0, 0))
        _, first_after, _ = polydye.pass_geometry('y', 100., 40., 1, 10, 0.1, 3., (x, y), (0, 0, 0, 0))
        self.assertAlmostEqual(first_after[0] - first_before[0], 0.1)
        self.assertAlmostEqual(first_after[1] - first_before[1], -0.05)

    def test_trim_and_rounding(self):
        self.assertEqual(polydye.nudge_offsets(0.1, 0.2, 0.1, -0.2, 0., 0.2), (0.3, 0.2, 0.3))


class PeriodTicks(unittest.TestCase):
    def test_fraction(self):
        whole, frac = polydye.period_to_ticks(1000, 1000 + 84667, 10)  # 8466.7 ticks
        self.assertEqual(whole, 8466)
        self.assertEqual(frac, int(round(0.7 * 65536)))

    def test_rounding_carries(self):
        self.assertEqual(polydye.period_to_ticks(0, 999999999, 100000000), (10, 0))


class ShaperDuration(unittest.TestCase):
    def test_no_input_shaper(self):
        printer = types.SimpleNamespace(lookup_object=lambda name, default=None: default)
        self.assertEqual(polydye.shaper_duration(printer, 'y'), 0.)

    def test_uses_pass_axis_shaper(self):
        class Shaper:
            def __init__(self, axis, T):
                self.axis, self.T = axis, T

            def get_name(self):
                return 'shaper_' + self.axis

            def get_shaper(self):
                return len(self.T), [1.] * len(self.T), self.T

        shaper = types.SimpleNamespace(get_shapers=lambda: [Shaper('x', [0., .01]), Shaper('y', [-.01, 0., .012])])
        printer = types.SimpleNamespace(lookup_object=lambda name, default=None: shaper)
        self.assertTrue(math.isclose(polydye.shaper_duration(printer, 'y'), .022))
        self.assertTrue(math.isclose(polydye.shaper_duration(printer, 'x'), .01))


class StatusPoll(unittest.TestCase):
    """PolyDye._status_event, the background query that keeps printer.polydye current."""

    def make(self, query):
        pd = object.__new__(polydye.PolyDye)
        reactor = types.SimpleNamespace(NEVER=9e99)
        state = {'shutdown': False}
        pd.printer = types.SimpleNamespace(is_shutdown=lambda: state['shutdown'], get_reactor=lambda: reactor)
        pd.querying = pd.query_failing = False
        pd._query_status = lambda: query(state)
        return pd, state, reactor

    def test_unplugging_mid_query_stops_the_poll_without_a_traceback(self):
        # The board is unplugged while a query waits for it: Klipper shuts down, then the query fails.
        def query(state):
            state['shutdown'] = True
            raise RuntimeError("mcu 'polydye': No communication with MCU")
        pd, _, reactor = self.make(query)
        with self.assertNoLogs(level='INFO'):
            self.assertEqual(pd._status_event(100.), reactor.NEVER)

    def test_no_query_once_shut_down(self):
        calls = []
        pd, state, reactor = self.make(lambda s: calls.append(1))
        state['shutdown'] = True
        self.assertEqual(pd._status_event(100.), reactor.NEVER)
        self.assertEqual(calls, [])

    def test_failures_log_one_traceback_until_a_query_succeeds(self):
        results = [RuntimeError("timeout"), RuntimeError("timeout"), RuntimeError("timeout"), None,
                   RuntimeError("timeout")]

        def query(state):
            r = results.pop(0)
            if r is not None:
                raise r
        pd, _, _ = self.make(query)
        with self.assertLogs(level='INFO') as logs:
            for t in range(5):
                self.assertEqual(pd._status_event(float(t)), t + polydye.STATUS_INTERVAL)
        self.assertEqual(sum(1 for r in logs.records if r.exc_info), 2)    # the first failure, and the first after
        self.assertEqual(sum(1 for r in logs.records if "work again" in r.getMessage()), 1)   # recovering


class StatusCommand(unittest.TestCase):
    def test_answers_while_the_first_background_query_is_in_flight(self):
        # Just after connecting: the poll's first query is waiting for the board and nothing is cached yet.
        pd = object.__new__(polydye.PolyDye)
        pd.querying, pd.last_status, pd.oid, pd.query_level_cmd, pd.query_clock_cmd = True, {}, 0, None, None

        def pause(waketime):
            pd.querying = False     # the background query finishes
        reactor = types.SimpleNamespace(monotonic=lambda: 0., pause=pause)
        pd.printer = types.SimpleNamespace(get_reactor=lambda: reactor)
        pd.mcu = types.SimpleNamespace(get_constant_float=lambda name: 10e6)
        status = {'flags': polydye.FLAG_BOOST, 'queued': 0, 'passes': 3, 'late_max': 100, 'faults': 0,
                  'store_count': 1, 'store_free': 4096}
        pd.query_cmd = types.SimpleNamespace(send=lambda args: status)
        pd.query_cartridge_cmd = types.SimpleNamespace(send=lambda args: {'template': 0, 'pulse': 8})
        pd.pass_axis, pd.offset_x, pd.offset_y, pd.direction_trim, pd.dpi, pd.pass_speed = 'y', 1., 2., 0., 600., 50.
        replies = []
        pd.cmd_POLYDYE_STATUS(types.SimpleNamespace(respond_info=replies.append))
        self.assertIn("boost=1", replies[0])
        self.assertIn("passes=3", replies[0])
        self.assertIn("color_a", replies[0])


class Stripes(unittest.TestCase):
    def test_parse(self):
        basic = polydye.parse_stripes(polydye.FIRE_TEST_STRIPES_BASIC)
        self.assertEqual(len(basic), 5)
        matrix = polydye.parse_stripes(polydye.FIRE_TEST_STRIPES)
        self.assertEqual(len(matrix), 8)
        self.assertEqual({(tpl, clock) for tpl, _, clock in matrix},
                         {(tpl, clock) for tpl in ('color_b', 'color_b_late') for clock in ('ours', 'stock')})
        # Every default stripe is within the live-ink limit, and the longer ones are the longest allowed.
        for tpl, words, clock in matrix + basic:
            self.assertIsNone(polydye.pulse_limit_error(words, clock), (tpl, words, clock))
        self.assertEqual(max(w for _, w, c in matrix if c == 'ours'), polydye.max_pulse_words('ours'))
        self.assertEqual(max(w for _, w, c in matrix if c == 'stock'), polydye.max_pulse_words('stock'))
        self.assertEqual(polydye.parse_stripes(" Black_A:12 , color_b_late:8:STOCK"),
                         [('black_a', 12, None), ('color_b_late', 8, 'stock')])
        for bad in ("", "color_c:6", "color_a", "color_a:x", "color_a:5", "color_a:13", "color_a:6:fast",
                    "color_a:6:ours:x"):
            with self.assertRaises(ValueError):
                polydye.parse_stripes(bad)

    def test_pulse_us_follows_the_clock(self):
        self.assertAlmostEqual(polydye.pulse_us(6, 'ours'), 1.8, places=3)
        # The stock setup runs at 2.857 MHz on the board (its divider of 1 acts as 2).
        self.assertAlmostEqual(polydye.pulse_us(6, 'stock'), 2.1, places=3)
        self.assertAlmostEqual(polydye.pulse_us(12, 'stock'), 4.2, places=3)
        self.assertAlmostEqual(polydye.pulse_us(10, None), 3.0, places=3)

    def test_pulse_limit_is_in_microseconds(self):
        self.assertEqual((polydye.max_pulse_words('ours'), polydye.max_pulse_words('stock')), (9, 8))
        self.assertIsNone(polydye.pulse_limit_error(9, None))
        self.assertIn("2.9 us", polydye.pulse_limit_error(10, 'ours'))
        self.assertIn("at most 8 words", polydye.pulse_limit_error(9, 'stock'))
        self.assertAlmostEqual(polydye.pulse_energy_pct(polydye.PULSE_MAX_US), 100., delta=1.)
        self.assertAlmostEqual(polydye.pulse_energy_pct(4.2), 145., delta=1.)  # pulse 12, stock clock

    def test_mid_column(self):
        everything = polydye.color_column(range(112))
        self.assertEqual(everything, b"\xff" * polydye.NOZDATA_SIZE)
        mid = polydye.color_column(polydye.MID_POSITIONS)
        self.assertEqual(sum(bin(b).count("1") for b in mid), 77 * 3)
        # Positions are 14 to a bit: 14-27 are bit 1 of every byte, 84-90 part of bit 6, none of bits 0 and 7.
        self.assertTrue(all(b & 0x02 for b in mid))
        self.assertFalse(any(b & 0x81 for b in mid))

    def test_positions_side_by_side_across_the_pass_axis(self):
        self.assertEqual(polydye.stripe_positions('y', 80., 60., 20., 3), [(80., 60.), (100., 60.), (120., 60.)])
        self.assertEqual(polydye.stripe_positions('x', 80., 60., 20., 2), [(80., 60.), (80., 80.)])


class TestPassData(unittest.TestCase):
    def test_repeat_sends_each_column_n_times(self):
        full, empty = b"\xff" * polydye.NOZDATA_SIZE, b"\x00" * polydye.NOZDATA_SIZE
        data, total = polydye.test_pass_data('alternate', 4, 2)
        self.assertEqual((data, total), (full + full + empty + empty + full + full + empty + empty, 8))
        data, total = polydye.test_pass_data('mid', 3, 3)
        self.assertEqual((len(data), total), (9 * polydye.NOZDATA_SIZE, 9))
        self.assertEqual(polydye.test_pass_data('all', 5), (full * 5, 5))
        with self.assertRaises(ValueError):
            polydye.test_pass_data('stripes', 5)

    def test_sparse_enables_one_nozzle_per_fire_pulse_and_covers_every_nozzle(self):
        n = polydye.SPARSE_COLUMNS
        data, total = polydye.test_pass_data('sparse', 2 * n)
        self.assertEqual(total, 2 * n)
        size = polydye.NOZDATA_SIZE
        cols = [data[i * size:(i + 1) * size] for i in range(total)]
        seen = bytearray(size)
        for k, col in enumerate(cols):
            for packet in range(14):
                lines = [col[14 * c + packet] for c in range(3)]
                # F3 fires the bits 0-3 of a packet, F5 the bits 4-7: one nozzle each.
                self.assertEqual(sum(bin(b & 0x0f).count('1') for b in lines), 1, (k, packet))
                self.assertEqual(sum(bin(b & 0xf0).count('1') for b in lines), 1, (k, packet))
            if k < n:
                self.assertFalse(any(s & b for s, b in zip(seen, col)), k)
                seen = bytearray(s | b for s, b in zip(seen, col))
            else:
                self.assertEqual(col, cols[k - n])
        self.assertEqual(bytes(seen), b"\xff" * size)
        # Column 4: D2 (4 % 3), bits 1 and 5 ((4 // 3) % 4 = 1) in every packet, as the board firmware makes it.
        self.assertEqual(polydye.sparse_column(4), b"\x00" * 14 + b"\x22" * 14 + b"\x00" * 14)
        data, total = polydye.test_pass_data('sparse', 2, 3)
        self.assertEqual((data, total), (polydye.sparse_column(0) * 3 + polydye.sparse_column(1) * 3, 6))


class InitOrder(unittest.TestCase):
    """The settings sent on every connect never pass through an over-limit combination."""

    def test_order(self):
        f = polydye.init_pulse_clock
        self.assertEqual(f(8, 'stock', True), [(8, None), (None, 'stock')])
        self.assertEqual(f(None, 'stock', True), [(polydye.PULSE_MIN, None), (None, 'stock')])
        self.assertEqual(f(9, 'ours', True), [(None, 'ours'), (9, None)])
        self.assertEqual(f(9, None, True), [(None, 'ours'), (9, None)])
        self.assertEqual(f(None, None, True), [])
        self.assertEqual(f(9, None, False), [(9, None)])  # board firmware without the clock choice

    def test_no_step_is_over_the_limit_from_any_starting_point(self):
        for pulse, clock in ((9, 'ours'), (8, 'stock'), (None, 'stock'), (9, None), (6, 'ours')):
            for start in ((9, 'ours'), (8, 'stock'), (6, 'ours'), (6, 'stock')):
                words, now = start
                for step_words, step_clock in polydye.init_pulse_clock(pulse, clock, True):
                    words = step_words if step_words is not None else words
                    now = step_clock or now
                    self.assertIsNone(polydye.pulse_limit_error(words, now), (pulse, clock, start))


class SendOrder(unittest.TestCase):
    """The board refuses any moment where the pulse would be over the limit, so the order of the sends matters."""

    def sends(self, st, name, pulse, clock):
        pd = object.__new__(polydye.PolyDye)
        pd.oid, sent = 0, []
        pd.set_template_cmd = types.SimpleNamespace(send=lambda args: sent.append('template'))
        pd.set_pulse_cmd = types.SimpleNamespace(send=lambda args: sent.append(('pulse', args[1])))
        pd.set_clock_cmd = types.SimpleNamespace(send=lambda args: sent.append(('clock', polydye.CLOCKS[args[1]])))
        pd._send_cartridge(st, name, pulse, clock)
        return sent

    def test_slower_clock_after_the_pulse_faster_before(self):
        # ours, 9 words -> stock, 8: 9 words at the stock clock would be 3.15 us.
        self.assertEqual(self.sends({'cartridge_template': 'color_b', 'bus_clock': 'ours'}, None, 8, 'stock'),
                         [('pulse', 8), ('clock', 'stock')])
        # stock, 8 -> ours, 9: 9 words at the stock clock would be 3.15 us.
        self.assertEqual(self.sends({'cartridge_template': 'color_b', 'bus_clock': 'stock'}, None, 9, 'ours'),
                         [('clock', 'ours'), ('pulse', 9)])
        self.assertEqual(self.sends({'cartridge_template': 'color_b', 'bus_clock': 'ours'}, 'color_a', None, None),
                         ['template'])


class FakeBoard:
    """Just enough of the board for POLYDYE_FIRE_TEST / POLYDYE_SPIT: tracks mode, template and pulse."""

    def __init__(self):
        self.boost = self.live = False
        self.template, self.pulse, self.clock = 'color_a', 8, 'ours'
        self.sent = []

    def status(self):
        return {'boost': self.boost, 'live': self.live, 'running': False, 'queued': 0, 'faults': 0,
                'cartridge_template': self.template, 'pulse_words': self.pulse, 'pulse_us': self.pulse * .3,
                'bus_clock': self.clock, 'bus_hz': 3333333, 'bus_hz_measured': None}


class TestCleanup(unittest.TestCase):
    """A failing stripe must still leave PolyDye off with the template and pulse put back."""

    def make(self, fail_on):
        board = FakeBoard()
        pd = object.__new__(polydye.PolyDye)
        pd.oid, pd.pass_axis = 0, 'y'
        pd.query_cartridge_cmd = object()
        pd.spit_patterns = len(polydye.TEST_PATTERNS)

        def mode(args):
            board.boost, board.live = bool(args[1]), bool(args[2])
            board.sent.append(('mode', args[1], args[2]))
        pd.mode_cmd = types.SimpleNamespace(send=mode)
        pd.set_template_cmd = types.SimpleNamespace(
            send=lambda args: (setattr(board, 'template', polydye.TEMPLATES[args[1]]), board.sent.append('tpl')))
        pd.set_pulse_cmd = types.SimpleNamespace(
            send=lambda args: (setattr(board, 'pulse', args[1]), board.sent.append('pulse')))
        pd.set_clock_cmd = types.SimpleNamespace(
            send=lambda args: (setattr(board, 'clock', polydye.CLOCKS[args[1]]), board.sent.append('clock')))
        pd._fresh_status = pd._query_status = board.status

        def run_script(script):
            board.sent.append(script)
            if script.startswith(fail_on):
                raise RuntimeError("Move out of range")
            if script.startswith("POLYDYE_CARTRIDGE"):
                for word in script.split()[1:]:
                    key, value = word.split('=')
                    if key == 'TEMPLATE':
                        board.template = value
                    elif key == 'CLOCK':
                        board.clock = value
                    else:
                        board.pulse = int(value)
            elif script == "POLYDYE_ENABLE LIVE=1":
                board.boost = board.live = True
            elif script == "POLYDYE_DISABLE":
                board.boost = board.live = False
        reactor = types.SimpleNamespace(monotonic=lambda: 0., pause=lambda t: None)
        objects = {'gcode': types.SimpleNamespace(run_script_from_command=run_script),
                   'toolhead': types.SimpleNamespace(wait_moves=lambda: None),
                   'print_stats': types.SimpleNamespace(get_status=lambda eventtime: {'state': board.print_state})}
        board.print_state = 'standby'
        pd.printer = types.SimpleNamespace(lookup_object=lambda name, default=None: objects.get(name, default),
                                           get_reactor=lambda: reactor, is_shutdown=lambda: False,
                                           command_error=RuntimeError)
        return pd, board

    def gcmd(self, **params):
        def get(name, default=None, **kw):
            return params.get(name, default)
        return types.SimpleNamespace(get=get, get_float=get, get_int=get, respond_info=lambda msg: None,
                                     error=RuntimeError)

    def test_fire_test_error_turns_off_and_restores(self):
        pd, board = self.make(fail_on="POLYDYE_PASS")
        with self.assertRaisesRegex(RuntimeError, "out of range"):
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:8:stock", X=80., Y=60., Z=0.5, SPACING=20.,
                                               COLUMNS=600, UPLOAD=1, LAYER=0, PASS=0, PATTERN='mid'))
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=color_b_late PULSE=8 CLOCK=stock", board.sent)
        self.assertIn("POLYDYE_UPLOAD_TEST_PASS LAYER=0 PASS=0 COLUMNS=600 PATTERN=mid", board.sent)
        # The G-code state saved before the Z move is restored after the error.
        self.assertLess(board.sent.index("SAVE_GCODE_STATE NAME=pd_fire_test"), board.sent.index("G90\nG1 Z0.500 F900"))
        self.assertEqual(board.sent[-1], "RESTORE_GCODE_STATE NAME=pd_fire_test")
        self.assertEqual((board.boost, board.live, board.template, board.pulse, board.clock),
                         (False, False, 'color_a', 8, 'ours'))

    def test_fire_test_restores_after_success(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b:7,color_b:9", X=80., Y=60., SPACING=20.,
                                           COLUMNS=600, UPLOAD=1, LAYER=0, PASS=0, PATTERN='all'))
        # A stripe without a clock keeps the clock the test started with.
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=color_b PULSE=9 CLOCK=ours", board.sent)
        passes = [s for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_PASS")]
        self.assertEqual(passes, ["POLYDYE_PASS LAYER=0 PASS=0 X=80.000 Y=60.000 DIR=1",
                                  "POLYDYE_PASS LAYER=0 PASS=0 X=100.000 Y=60.000 DIR=1"])
        self.assertEqual(sum(1 for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_UPLOAD")), 1)
        self.assertEqual((board.boost, board.template, board.pulse), (False, 'color_a', 8))

    def test_refused_during_a_paused_print_before_anything_is_sent(self):
        for state in ('printing', 'paused'):
            pd, board = self.make(fail_on="NOTHING")
            board.print_state = state
            pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
            with self.assertRaisesRegex(RuntimeError, "not during a print"):
                pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b:9", X=80., Y=60., SPACING=20., COLUMNS=600,
                                                   UPLOAD=1, LAYER=0, PASS=0, PATTERN='all'))
            with self.assertRaisesRegex(RuntimeError, "not during a print"):
                pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PATTERN='all', CONFIRM=1))
            self.assertEqual(board.sent, [])

    def test_pulses_over_the_limit_are_refused_before_anything_is_sent(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
        with self.assertRaisesRegex(RuntimeError, "limit is 2.9 us"):
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b:6,color_b:9:stock", X=80., Y=60., SPACING=20.,
                                               COLUMNS=600, UPLOAD=1, LAYER=0, PASS=0, PATTERN='all'))
        with self.assertRaisesRegex(RuntimeError, "limit is 2.9 us"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PULSE=9, CLOCK='stock', PATTERN='all', CONFIRM=1))  # 3.15 us
        with self.assertRaisesRegex(RuntimeError, "limit is 2.9 us"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PULSE=10, PATTERN='all', CONFIRM=1))  # 3.0 us at our clock
        self.assertNotIn('spit', board.sent)
        self.assertFalse([s for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_PASS")])

    def test_cartridge_refuses_an_over_limit_result_even_if_unchanged(self):
        # A board left at the stock clock and 9 words (3.15 us) by older firmware: resending it is refused too.
        pd, board = self.make(fail_on="NOTHING")
        board.clock, board.pulse = 'stock', 9
        with self.assertRaisesRegex(RuntimeError, "limit is 2.9 us"):
            pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(PULSE=9, CLOCK='stock'))
        self.assertEqual(board.sent, [])
        pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(PULSE=8))
        self.assertEqual(board.sent, ['pulse'])

    def test_tests_and_live_ink_refuse_an_over_limit_starting_setting(self):
        # Older firmware left at stock / 9 words (3.15 us): a test would put it back afterwards, live ink would use it.
        pd, board = self.make(fail_on="NOTHING")
        board.clock, board.pulse = 'stock', 9
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
        pd.mode_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('mode'))
        with self.assertRaisesRegex(RuntimeError, "PULSE=8"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PULSE=8, PATTERN='all', CONFIRM=1))
        with self.assertRaisesRegex(RuntimeError, "over-limit pulse"):
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b:6", X=80., Y=60., SPACING=20., COLUMNS=600,
                                               UPLOAD=1, LAYER=0, PASS=0, PATTERN='all'))
        with self.assertRaisesRegex(RuntimeError, "over-limit pulse"):
            pd.cmd_POLYDYE_ENABLE(self.gcmd(LIVE=1))
        self.assertEqual(board.sent, [])

    def test_spit_needs_confirm(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append(('spit', args[1], args[2])))
        with self.assertRaisesRegex(RuntimeError, "CONFIRM=1"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=2., PATTERN='all'))
        self.assertEqual(board.sent, [])

    def test_spit_sends_columns_then_turns_off_and_restores(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append(('spit', args[1], args[2])))
        pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1.5, PULSE=7, CLOCK='stock', PATTERN='mid', CONFIRM=1))
        self.assertIn(('spit', 1500, 1), board.sent)
        self.assertLess(board.sent.index("POLYDYE_CARTRIDGE PULSE=7 CLOCK=stock"), board.sent.index(('mode', 1, 1)))
        self.assertEqual((board.boost, board.live, board.pulse, board.clock), (False, False, 8, 'ours'))

    def test_sparse_spit_sends_pattern_2_and_reports_the_pulse_it_used(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append(('spit', args[1], args[2])))
        gcmd = self.gcmd(SECONDS=8., PULSE=9, PATTERN='sparse', CONFIRM=1)
        replies = []
        gcmd.respond_info = replies.append
        pd.cmd_POLYDYE_SPIT(gcmd)
        self.assertIn(('spit', 8000, 2), board.sent)
        self.assertIn("8000 sparse (one nozzle per fire pulse) columns", replies[0])
        self.assertIn("done (faults=0): fired with cartridge template color_a, fire pulse 9 words", replies[-1])
        self.assertIn("now back to cartridge template color_a, fire pulse 8 words", replies[-1])
        self.assertEqual((board.boost, board.live, board.pulse), (False, False, 8))

    def test_spit_done_without_changes_names_the_settings_once(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: None)
        gcmd = self.gcmd(SECONDS=1., PATTERN='all', CONFIRM=1)
        replies = []
        gcmd.respond_info = replies.append
        pd.cmd_POLYDYE_SPIT(gcmd)
        self.assertIn("fired with cartridge template color_a, fire pulse 8 words", replies[-1])
        self.assertNotIn("back to", replies[-1])

    def test_sparse_spit_needs_board_firmware_with_it(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_patterns = polydye.SPIT_PATTERNS_OLD  # no POLYDYE_SPIT_PATTERNS constant
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
        with self.assertRaisesRegex(RuntimeError, "PATTERN=sparse needs newer PolyDye board firmware"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=8., PULSE=9, PATTERN='sparse', CONFIRM=1))
        self.assertEqual(board.sent, [])
        pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PATTERN='mid', CONFIRM=1))
        self.assertIn('spit', board.sent)

    def test_fire_test_uploads_sparse_on_any_board_firmware(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_patterns = polydye.SPIT_PATTERNS_OLD
        pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="black_b:8", X=80., Y=60., SPACING=20., COLUMNS=600,
                                           UPLOAD=1, LAYER=0, PASS=0, PATTERN='sparse'))
        self.assertIn("POLYDYE_UPLOAD_TEST_PASS LAYER=0 PASS=0 COLUMNS=600 PATTERN=sparse", board.sent)
        self.assertEqual((board.boost, board.template, board.pulse), (False, 'color_a', 8))


if __name__ == "__main__":
    unittest.main()
