#!/usr/bin/env python3
"""Unit tests for the pass timing and geometry in klipper/polydye.py (no Klipper needed).

This file is part of polydye-klipper (GPLv3).
"""
import math
import os
import sys
import types
import unittest
import zlib
from unittest import mock

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
        self.assertEqual(polydye.parse_stripes(" Black_A:12 , color_b_late:8:STOCK,color_b:4:Half"),
                         [('black_a', 12, None), ('color_b_late', 8, 'stock'), ('color_b', 4, 'half')])
        for bad in ("", "color_c:6", "color_a", "color_a:x", "color_a:2", "color_a:13", "color_a:6:fast",
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
        # Half our clock: a word lasts 0.6 us, so 4 words (2.4 us) are the most within 2.9 us.
        self.assertEqual(polydye.max_pulse_words('half'), 4)
        self.assertAlmostEqual(polydye.pulse_us(3, 'half'), 1.8, places=3)
        self.assertAlmostEqual(polydye.pulse_us(5, 'half'), 3.0, places=3)

    def test_pulse_range_is_1_8_to_2_9_us_at_every_clock(self):
        ranges = {'ours': (6, 9), 'stock': (6, 8), 'half': (3, 4)}
        for clock, (lo, hi) in ranges.items():
            self.assertEqual((polydye.min_pulse_words(clock), polydye.max_pulse_words(clock)), (lo, hi), clock)
            for words in range(polydye.PULSE_MIN, polydye.PULSE_MAX + 1):
                error = polydye.pulse_range_error(words, clock)
                self.assertEqual(error is None, lo <= words <= hi, (clock, words, error))
                self.assertEqual(polydye.pulse_limit_error(words, clock) is None, words <= hi, (clock, words))
        self.assertIn("less than Sprite_tm's 1.8 us; use 6-9 words", polydye.pulse_range_error(5, 'ours'))
        self.assertIn("at most 4 words there", polydye.pulse_range_error(5, 'half'))
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
        # Through our clock (the fastest) first, then the pulse, then a slower clock.
        self.assertEqual(f(8, 'stock', True), [(None, 'ours'), (8, None), (None, 'stock')])
        self.assertEqual(f(None, 'stock', True), [(None, 'ours'), (6, None), (None, 'stock')])
        self.assertEqual(f(4, 'half', True), [(None, 'ours'), (4, None), (None, 'half')])
        self.assertEqual(f(None, 'half', True), [(None, 'ours'), (3, None), (None, 'half')])
        self.assertEqual(f(9, 'ours', True), [(None, 'ours'), (9, None)])
        # A clock without pulse_words gets its 1.8 us pulse: a board left at half with 4 words would otherwise keep
        # them at ours after a RESTART (1.2 us).
        self.assertEqual(f(None, 'ours', True), [(None, 'ours'), (6, None)])
        self.assertEqual(f(9, None, True), [(None, 'ours'), (9, None)])
        self.assertEqual(f(None, None, True), [])
        self.assertEqual(f(9, None, False), [(9, None)])  # board firmware without the clock choice

    def test_no_step_is_over_the_limit_from_any_starting_point(self):
        for pulse, clock in ((9, 'ours'), (8, 'stock'), (None, 'stock'), (9, None), (6, 'ours'), (4, 'half'),
                             (3, 'half'), (None, 'half'), (None, 'ours')):
            for start in ((9, 'ours'), (8, 'stock'), (6, 'ours'), (6, 'stock'), (4, 'half'), (3, 'half')):
                words, now = start
                for step_words, step_clock in polydye.init_pulse_clock(pulse, clock, True):
                    words = step_words if step_words is not None else words
                    now = step_clock or now
                    self.assertIsNone(polydye.pulse_limit_error(words, now), (pulse, clock, start))
                # And it ends at the clock asked for, with a pulse of 1.8-2.9 us there.
                self.assertEqual(now, clock or 'ours', (pulse, clock, start))
                self.assertIsNone(polydye.pulse_range_error(words, now), (pulse, clock, start))


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
        # ours, 9 -> half, 4: 9 words at half would be 5.4 us. half, 4 -> ours, 9: the other way round.
        self.assertEqual(self.sends({'cartridge_template': 'color_b', 'bus_clock': 'ours'}, None, 4, 'half'),
                         [('pulse', 4), ('clock', 'half')])
        self.assertEqual(self.sends({'cartridge_template': 'color_b', 'bus_clock': 'half'}, None, 9, 'ours'),
                         [('clock', 'ours'), ('pulse', 9)])

    def test_no_step_is_over_the_limit_between_any_two_settings(self):
        settings = [(words, clock) for clock in polydye.CLOCKS
                    for words in range(polydye.min_pulse_words(clock), polydye.max_pulse_words(clock) + 1)]
        for start in settings:
            for target in settings:
                now = list(start)
                for step in self.sends({'cartridge_template': 'color_b', 'pulse_words': start[0],
                                        'bus_clock': start[1]}, None, target[0], target[1]):
                    now[0 if step[0] == 'pulse' else 1] = step[1]
                    self.assertIsNone(polydye.pulse_limit_error(*now), (start, target, step))
                self.assertEqual(tuple(now), target)


class FakeBoard:
    """Just enough of the board for POLYDYE_FIRE_TEST / POLYDYE_SPIT: tracks mode, template and pulse."""

    def __init__(self):
        self.boost = self.live = False
        self.template, self.pulse, self.clock = 'color_a', 8, 'ours'
        self.measured = None
        self.sent = []
        self.violations = []  # moments where the board would have refused the pulse at the clock

    def check(self):
        if polydye.pulse_limit_error(self.pulse, self.clock):
            self.violations.append((self.pulse, self.clock))

    def status(self):
        return {'boost': self.boost, 'live': self.live, 'running': False, 'queued': 0, 'faults': 0,
                'cartridge_template': self.template, 'pulse_words': self.pulse, 'pulse_us': self.pulse * .3,
                'bus_clock': self.clock, 'bus_hz': 3333333, 'bus_hz_measured': self.measured}


class TestCleanup(unittest.TestCase):
    """A failing stripe must still leave PolyDye off with the template and pulse put back."""

    def make(self, fail_on):
        board = FakeBoard()
        pd = object.__new__(polydye.PolyDye)
        pd.oid, pd.pass_axis = 0, 'y'
        pd.query_cartridge_cmd = object()
        pd.spit_patterns = len(polydye.SPIT_PATTERNS)
        pd.clock_count = len(polydye.CLOCKS)
        pd.template_count = len(polydye.TEMPLATES)
        pd.spit_ext_cmd = pd.spit_run_cmd = None
        pd.query_run_cmd = None
        pd.spit_random_cmd = None
        pd.warm_max_words, pd.warm_max_us, pd.warm_max_s = polydye.WARM_OLD_WORDS, polydye.WARM_OLD_US, polydye.WARM_OLD_S
        pd.warm_duty_ppm = None
        pd.query_clock_cmd = pd.bus_hz_check = None
        pd.check_clock_cmd = None
        pd.boost_idle_s = None  # board firmware that turns PolyDye off after a test
        pd.pulse_max_ext_us = polydye.PULSE_MAX_EXT_US

        def mode(args):
            board.boost, board.live = bool(args[1]), bool(args[2])
            board.sent.append(('mode', args[1], args[2]))
        pd.mode_cmd = types.SimpleNamespace(send=mode)
        pd.set_template_cmd = types.SimpleNamespace(
            send=lambda args: (setattr(board, 'template', polydye.TEMPLATES[args[1]]), board.sent.append('tpl')))
        pd.set_pulse_cmd = types.SimpleNamespace(
            send=lambda args: (setattr(board, 'pulse', args[1]), board.sent.append('pulse'), board.check()))
        pd.set_clock_cmd = types.SimpleNamespace(
            send=lambda args: (setattr(board, 'clock', polydye.CLOCKS[args[1]]), board.sent.append('clock'),
                               board.check()))
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
                board.check()
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
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: (board.sent.append(('spit', args[1], args[2])),
                                                               setattr(board, 'measured', 3300000)))
        gcmd = self.gcmd(SECONDS=8., PULSE=9, PATTERN='sparse', CONFIRM=1)
        replies = []
        gcmd.respond_info = replies.append
        pd.cmd_POLYDYE_SPIT(gcmd)
        self.assertIn(('spit', 8000, 2), board.sent)
        self.assertIn("8000 sparse (one nozzle per fire pulse) columns", replies[0])
        self.assertIn("done (faults=0): fired with cartridge template color_a, fire pulse 9 words", replies[-1])
        self.assertIn("now back to cartridge template color_a, fire pulse 8 words", replies[-1])
        # The rate measured during the spit is reported once, with the settings it fired with.
        self.assertEqual(replies[-1].count("measured 3.300 MHz"), 1)
        self.assertLess(replies[-1].index("measured"), replies[-1].index("now back to"))
        self.assertEqual((board.boost, board.live, board.pulse), (False, False, 8))

    def test_spit_done_without_changes_names_the_settings_once(self):
        pd, board = self.make(fail_on="NOTHING")
        # The board measures the bus rate of the columns it sends: a new reading isn't a change of settings.
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: setattr(board, 'measured', 3300000))
        gcmd = self.gcmd(SECONDS=1., PATTERN='all', CONFIRM=1)
        replies = []
        gcmd.respond_info = replies.append
        pd.cmd_POLYDYE_SPIT(gcmd)
        self.assertIn("fired with cartridge template color_a, fire pulse 8 words", replies[-1])
        self.assertIn("last column measured 3.300 MHz", replies[-1])
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

    def test_half_clock_spit_and_back_without_an_over_limit_step(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append(('spit', args[1], args[2])))
        pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=8., CLOCK='half', PULSE=4, PATTERN='all', CONFIRM=1))
        self.assertIn("POLYDYE_CARTRIDGE PULSE=4 CLOCK=half", board.sent)
        self.assertIn(('spit', 8000, 0), board.sent)
        # Back to 8 words at our clock: the clock first, since 8 words at half would be 4.8 us.
        after = board.sent[board.sent.index(('spit', 8000, 0)):]
        self.assertLess(after.index('clock'), after.index('pulse'))
        self.assertEqual((board.boost, board.pulse, board.clock), (False, 8, 'ours'))
        self.assertEqual(board.violations, [])

    def test_fire_test_half_clock_stripe(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b:4:half,color_b:9", X=80., Y=60., SPACING=20.,
                                           COLUMNS=600, UPLOAD=1, LAYER=0, PASS=0, PATTERN='all'))
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=color_b PULSE=4 CLOCK=half", board.sent)
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=color_b PULSE=9 CLOCK=ours", board.sent)
        self.assertEqual((board.boost, board.template, board.pulse, board.clock), (False, 'color_a', 8, 'ours'))
        self.assertEqual(board.violations, [])

    def test_half_clock_pulse_range_and_older_board_firmware(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
        # The board's 8 words at half would be 4.8 us; 5 words 3.0 us; 5 words at our clock only 1.5 us.
        for params, message in ((dict(CLOCK='half'), "at most 4 words there"),
                                (dict(CLOCK='half', PULSE=5), "at most 4 words there"),
                                (dict(PULSE=5), "less than Sprite_tm's 1.8 us")):
            with self.assertRaisesRegex(RuntimeError, message):
                pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PATTERN='all', CONFIRM=1, **params))
        with self.assertRaisesRegex(RuntimeError, "at most 4 words there"):
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b:5:half", X=80., Y=60., SPACING=20., COLUMNS=600,
                                               UPLOAD=1, LAYER=0, PASS=0, PATTERN='all'))
        with self.assertRaisesRegex(RuntimeError, "at most 4 words there"):
            pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(CLOCK='half'))
        self.assertEqual(board.sent, [])
        pd.clock_count = polydye.BUS_CLOCKS_OLD  # no POLYDYE_BUS_CLOCKS constant
        for run in (lambda: pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., CLOCK='half', PULSE=4, PATTERN='all',
                                                          CONFIRM=1)),
                    lambda: pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b:4:half", X=80., Y=60., SPACING=20.,
                                                               COLUMNS=600, UPLOAD=1, LAYER=0, PASS=0,
                                                               PATTERN='all')),
                    lambda: pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(CLOCK='half', PULSE=4))):
            with self.assertRaisesRegex(RuntimeError, "CLOCK=half needs newer PolyDye board firmware.*Update to "
                                                      "bundled firmware"):
                run()
        self.assertEqual(board.sent, [])

    def test_deskjet_template_and_older_board_firmware(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
        pd.template_count = polydye.TEMPLATES_OLD  # no POLYDYE_TEMPLATES constant: up to color_b_late
        for run in (lambda: pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(TEMPLATE='deskjet')),
                    lambda: pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., TEMPLATE='DeskJet', PATTERN='all', CONFIRM=1)),
                    lambda: pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b:6,deskjet:6", X=80., Y=60.,
                                                               SPACING=20., COLUMNS=600, UPLOAD=1, LAYER=0, PASS=0,
                                                               PATTERN='all'))):
            with self.assertRaisesRegex(RuntimeError, "TEMPLATE=deskjet needs newer PolyDye board firmware.*Update to "
                                                      "bundled firmware"):
                run()
        self.assertEqual(board.sent, [])
        pd.template_count = len(polydye.TEMPLATES)
        pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., TEMPLATE='deskjet', PULSE=6, PATTERN='all', CONFIRM=1))
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=deskjet PULSE=6", board.sent)
        self.assertIn('spit', board.sent)
        self.assertEqual((board.boost, board.template, board.pulse), (False, 'color_a', 8))
        self.assertEqual(polydye.parse_stripes("deskjet:6"), [('deskjet', 6, None)])

    def extended(self, **params):
        """Runs an EXTENDED=1 spit test on a board that has it; returns (board, replies, extended spits sent)."""
        pd, board = self.make(fail_on="NOTHING")
        spits = []
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
        pd.spit_ext_cmd = types.SimpleNamespace(send=lambda args: spits.append(tuple(args[1:])))
        gcmd = self.gcmd(EXTENDED=1, CONFIRM=1, **params)
        replies = []
        gcmd.respond_info = replies.append
        pd.cmd_POLYDYE_SPIT(gcmd)
        return board, replies, spits

    def test_extended_spit_fires_its_own_pulse_once(self):
        board, replies, spits = self.extended(SECONDS=3., PULSE=13, PATTERN='sparse')
        self.assertEqual(spits, [(3000, 2, 13)])
        self.assertNotIn('spit', board.sent)
        # The configured pulse is never changed: the 13 words go with the spit only.
        self.assertNotIn('pulse', board.sent)
        self.assertFalse([s for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_CARTRIDGE")])
        self.assertEqual(board.violations, [])
        self.assertEqual((board.boost, board.pulse), (False, 8))
        # 3.9 us at 12.6 V: 134 % of Sprite_tm's 1.8 us at 16 V.
        self.assertIn("EXTENDED spit test", replies[0])
        self.assertIn("fire pulse 13 words = 3.90 us (134 % energy)", replies[0])
        self.assertIn("the configured pulse stays 8 words", replies[0])
        self.assertIn("fired EXTENDED with cartridge template color_a, fire pulse 13 words = 3.90 us", replies[-1])
        self.assertNotIn("back to", replies[-1])

    def test_extended_spit_at_the_half_clock(self):
        board, replies, spits = self.extended(SECONDS=2., PULSE=6, CLOCK='half', PATTERN='all')
        # The configured 8 words would be 4.8 us at half: the board's pulse goes to 3 words (1.8 us) for the clock.
        self.assertIn("POLYDYE_CARTRIDGE PULSE=3 CLOCK=half", board.sent)
        self.assertEqual(spits, [(2000, 0, 6)])
        self.assertIn("fire pulse 6 words = 3.60 us", replies[0])
        self.assertEqual((board.pulse, board.clock), (8, 'ours'))
        self.assertEqual(board.violations, [])

    def test_extended_spit_limits(self):
        for params, message in ((dict(SECONDS=3.5, PULSE=13), "at most 3 s"),
                                (dict(SECONDS=1., PULSE=17), "use 6-16 words there \\(1.8-5.0 us\\)"),
                                (dict(SECONDS=1., PULSE=15, CLOCK='stock'), "use 6-14 words there"),
                                (dict(SECONDS=1., PULSE=9, CLOCK='half'), "use 3-8 words there"),
                                (dict(SECONDS=1., PULSE=5), "less than|use 6-16"),
                                (dict(SECONDS=1.), "needs PULSE")):
            with self.assertRaisesRegex(RuntimeError, message):
                self.extended(PATTERN='all', **params)
        # Without EXTENDED=1 nothing changes: 3.0 us is over the limit.
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
        with self.assertRaisesRegex(RuntimeError, "limit is 2.9 us"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PULSE=10, PATTERN='all', CONFIRM=1))
        # Older board firmware has no extended spit.
        with self.assertRaisesRegex(RuntimeError, "EXTENDED=1 needs newer PolyDye board firmware"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PULSE=11, EXTENDED=1, PATTERN='all', CONFIRM=1))
        self.assertEqual(board.sent, [])

    def spit(self, pd=None, board=None, command='cmd_POLYDYE_SPIT', has_random=True, **params):
        """Runs POLYDYE_SPIT (or `command`) on a board with every spit command (without the random pattern's knobs
        unless has_random); returns (board, replies, commands sent)."""
        if pd is None:
            pd, board = self.make(fail_on="NOTHING")
        sent = []
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: sent.append(('spit',) + tuple(args[1:])))
        pd.spit_ext_cmd = types.SimpleNamespace(send=lambda args: sent.append(('ext',) + tuple(args[1:])))
        pd.spit_run_cmd = types.SimpleNamespace(send=lambda args: sent.append(('run',) + tuple(args[1:])))
        if has_random:
            pd.spit_random_cmd = types.SimpleNamespace(send=lambda args: sent.append(('random',) + tuple(args[1:])))
        gcmd = self.gcmd(CONFIRM=1, **params)
        replies = []
        gcmd.respond_info = replies.append
        getattr(pd, command)(gcmd)
        return board, replies, sent

    def test_stock_spit_fires_with_the_stock_timing(self):
        board, replies, sent = self.spit(SECONDS=3., PATTERN='stock', PERIOD_US=508)
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=color_b_late PULSE=6 CLOCK=stock", board.sent)
        self.assertEqual(sent, [('run', 5906, 3, 0, 508, 0, 0, 0)])
        self.assertIn("5906 stock-emulation (a random 24.9 % of positions 14-85, each column twice) columns every "
                      "508 us (1969 Hz)", replies[0])
        self.assertIn("cartridge template color_b_late, fire pulse 6 words", replies[0])
        self.assertIn("bus clock stock", replies[0])
        self.assertIn("now back to cartridge template color_a, fire pulse 8 words", replies[-1])
        self.assertEqual((board.boost, board.template, board.pulse, board.clock), (False, 'color_a', 8, 'ours'))
        self.assertEqual(board.violations, [])

    def test_stock_spit_with_its_own_settings_and_the_default_period(self):
        board, replies, sent = self.spit(PATTERN='stock', TEMPLATE='deskjet', CLOCK='half')
        # 1.8 us at the half clock is 3 words.
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=deskjet PULSE=3 CLOCK=half", board.sent)
        # The stock firmware's column period unless given.
        self.assertEqual(sent, [('run', 3937, 3, 0, 508, 0, 0, 0)])
        self.assertIn("columns every 508 us (1969 Hz)", replies[0])
        with self.assertRaisesRegex(RuntimeError, "PATTERN=stock is for a colour cartridge template"):
            self.spit(PATTERN='stock', TEMPLATE='black_b')

    def test_spit_period_limits(self):
        # color_b_late at the stock clock: 172 us on the bus, so at least 192 us.
        with self.assertRaisesRegex(RuntimeError, "the column takes 172 us on the bus, so the period must be at least "
                                                  "192 us"):
            self.spit(SECONDS=1., PATTERN='stock', PERIOD_US=191)
        _, _, sent = self.spit(SECONDS=1., PATTERN='stock', PERIOD_US=192)
        self.assertEqual(sent, [('run', 5208, 3, 0, 192, 0, 0, 0)])
        # At most 10000 columns (3000 extended) whatever the period.
        with self.assertRaisesRegex(RuntimeError, "at a 254 us column period a spit test lasts at most 2.54 s "
                                                  "\\(10000 columns\\)"):
            self.spit(SECONDS=3., PATTERN='stock', PERIOD_US=254)
        with self.assertRaisesRegex(RuntimeError, "an EXTENDED=1 spit test lasts at most 1.52 s \\(3000 columns\\)"):
            self.spit(SECONDS=2., PULSE=11, PATTERN='stock', PERIOD_US=508, EXTENDED=1)
        # An 11-word extended pulse at the stock clock lengthens the column to 221 us.
        with self.assertRaisesRegex(RuntimeError, "at least 241 us"):
            self.spit(SECONDS=0.5, PULSE=11, PATTERN='stock', PERIOD_US=240, EXTENDED=1)
        _, _, sent = self.spit(SECONDS=1.5, PULSE=11, PATTERN='stock', PERIOD_US=508, EXTENDED=1)
        self.assertEqual(sent, [('run', 2953, 3, 11, 508, 0, 0, 0)])
        # Any pattern takes a period; the configured color_a, 8 words at our clock needs 184 us.
        _, _, sent = self.spit(SECONDS=1., PATTERN='mid', PERIOD_US=400)
        self.assertEqual(sent, [('run', 2500, 1, 0, 400, 0, 0, 0)])
        with self.assertRaisesRegex(RuntimeError, "at least 184 us"):
            self.spit(SECONDS=1., PATTERN='mid', PERIOD_US=183)

    def test_extended_spit_up_to_five_us(self):
        board, replies, sent = self.spit(SECONDS=3., PULSE=16, PATTERN='sparse', EXTENDED=1)
        self.assertEqual(sent, [('ext', 3000, 2, 16)])
        # 4.8 us at 12.6 V: 165 % of Sprite_tm's 1.8 us at 16 V.
        self.assertIn("fire pulse 16 words = 4.80 us (165 % energy)", replies[0])
        _, _, sent = self.spit(SECONDS=1., PULSE=8, CLOCK='half', PATTERN='all', EXTENDED=1)
        self.assertEqual(sent, [('ext', 1000, 0, 8)])
        # Board firmware with the 4.0 us limit: the add-on follows its POLYDYE_PULSE_MAX_EXT_NS.
        pd, board = self.make(fail_on="NOTHING")
        pd.pulse_max_ext_us = 4.0
        with self.assertRaisesRegex(RuntimeError, "use 6-13 words there \\(1.8-4.0 us\\)"):
            self.spit(pd, board, SECONDS=1., PULSE=14, PATTERN='all', EXTENDED=1)

    def test_warm_up_right_before_an_extended_spit(self):
        board, replies, sent = self.spit(SECONDS=3., TEMPLATE='deskjet', PULSE=12, PATTERN='sparse', EXTENDED=1,
                                         WARM=5.)
        # deskjet at our clock: a 131 us column, so a warm-up column every 151 us, 2 words (0.6 us) by default.
        self.assertEqual(sent, [('run', 3000, 2, 12, 1000, 33113, 2, 151)])
        self.assertIn("warm-up 5.0 s: 33113 all-nozzle columns every 151 us, 2-word pulses of 0.60 us (21 % energy "
                      "each, too short to boil ink); per nozzle 19868 us of heating in all, the energy of 6845 "
                      "reference pulses (heater duty 0.40 %)", replies[0])
        self.assertIn("EXTENDED spit test", replies[1])
        self.assertEqual((board.boost, board.template, board.pulse), (False, 'color_a', 8))

    def test_warm_up_pulse_and_period(self):
        # At half our clock 2 words are 1.2 us, too long: 1 word (0.6 us), every 315 us (color_a's column).
        board, replies, sent = self.spit(SECONDS=1., CLOCK='half', PULSE=3, PATTERN='all', WARM=2.)
        self.assertIn("POLYDYE_CARTRIDGE PULSE=3 CLOCK=half", board.sent)
        self.assertEqual(sent, [('run', 1000, 0, 0, 1000, 6349, 1, 315)])
        # This board firmware takes warm-up pulses of up to 0.75 us; newer takes up to 2.9 us.
        with self.assertRaisesRegex(RuntimeError, "WARM_PULSE=2 \\(1.20 us\\) needs newer PolyDye board firmware"):
            self.spit(SECONDS=1., CLOCK='half', PULSE=3, PATTERN='all', WARM=2., WARM_PULSE=2)
        with self.assertRaisesRegex(RuntimeError, "WARM_PULSE=12 is 3.60 us at the ours clock; a warm-up pulse is at "
                                                  "most 0.75 us"):
            self.spit(SECONDS=1., PATTERN='all', WARM=2., WARM_PULSE=12)
        # Longer than its 10 s too; newer firmware takes 30 s.
        with self.assertRaisesRegex(RuntimeError, "a 20 s warm-up needs newer PolyDye board firmware, which warms up "
                                                  "for up to 30 s.*at most 10 s"):
            self.spit(SECONDS=1., PATTERN='all', WARM=20.)
        with self.assertRaisesRegex(RuntimeError, "warm-up period must be at least 168 us"):
            self.spit(SECONDS=1., PATTERN='all', WARM=1., WARM_PERIOD_US=150)
        _, _, sent = self.spit(SECONDS=1., PATTERN='all', WARM=1., WARM_PULSE=1, WARM_PERIOD_US=500)
        self.assertEqual(sent, [('run', 1000, 0, 0, 1000, 2000, 1, 500)])

    def test_warm_up_on_its_own(self):
        board, replies, sent = self.spit(command='cmd_POLYDYE_WARM', SECONDS=5.)
        self.assertEqual(sent, [('run', 0, 0, 0, 1000, 29762, 2, 168)])
        self.assertIn("warm-up 5.0 s: 29762 all-nozzle columns every 168 us", replies[0])
        self.assertEqual(replies[-1], "PolyDye warm-up done (faults=0)")
        self.assertFalse([s for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_CARTRIDGE")])
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
        pd.spit_run_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('run'))
        with self.assertRaisesRegex(RuntimeError, "POLYDYE_WARM heats every nozzle's heater"):
            pd.cmd_POLYDYE_WARM(self.gcmd(SECONDS=5.))
        # Older board firmware has no warm-up.
        pd.spit_run_cmd = None
        with self.assertRaisesRegex(RuntimeError, "POLYDYE_WARM: it needs newer PolyDye board firmware"):
            pd.cmd_POLYDYE_WARM(self.gcmd(SECONDS=5., CONFIRM=1))
        with self.assertRaisesRegex(RuntimeError, "POLYDYE_SPIT: WARM needs newer PolyDye board firmware"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PATTERN='all', WARM=2., CONFIRM=1))
        self.assertEqual(board.sent, [])

    def test_spit_with_the_random_pattern_knobs(self):
        # Any knob means PATTERN=stock with its timing; knobs not given are the stock pattern's.
        board, replies, sent = self.spit(SECONDS=3., PERIOD_US=508, PER_PULSE=1)
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=color_b_late PULSE=6 CLOCK=stock", board.sent)
        self.assertEqual(sent, [('random', 255, 2, 7, 14, 85, 1), ('run', 5906, 4, 0, 508, 0, 0, 0)])
        self.assertIn("5906 random: 1 heater on every fire pulse of positions 14-85 of cyan/magenta/yellow, each "
                      "column twice columns every 508 us", replies[0])
        _, replies, sent = self.spit(SECONDS=1., DENSITY=50., COLORS='M', FIRST=20, LAST=60, REPEAT=1)
        self.assertEqual(sent, [('random', 512, 1, 2, 20, 60, 0), ('run', 1969, 4, 0, 508, 0, 0, 0)])
        self.assertIn("random: 50.0 % of positions 20-60 of magenta, each column once", replies[0])
        # With the warm-up and EXTENDED too, in the same board run.
        _, _, sent = self.spit(SECONDS=1., PULSE=12, EXTENDED=1, REPEAT=3, WARM=1., PERIOD_US=508)
        self.assertEqual(sent[0], ('random', 255, 3, 7, 14, 85, 0))
        self.assertEqual(sent[1][:5], ('run', 1969, 4, 12, 508))
        for params, message in ((dict(PATTERN='all', DENSITY=10.), "go with PATTERN=stock"),
                                (dict(COLORS='cmk'), "COLORS must be any of c, m and y"),
                                (dict(COLORS='cc'), "COLORS must be any of c, m and y"),
                                (dict(FIRST=50, LAST=40), "FIRST=50 is after LAST=40"),
                                (dict(DENSITY=10., TEMPLATE='black_b'), "PATTERN=stock is for a colour cartridge")):
            with self.assertRaisesRegex(RuntimeError, message):
                self.spit(SECONDS=1., **params)
        # Older board firmware: the knobs are refused before anything is sent, with where to find them.
        pd, board = self.make(fail_on="NOTHING")
        with self.assertRaisesRegex(RuntimeError, "need newer PolyDye board firmware.*POLYDYE_FIRE_TEST PATTERN=stock "
                                                  "takes them with this one"):
            self.spit(pd, board, has_random=False, SECONDS=1., DENSITY=10.)
        self.assertEqual(board.sent, [])

    def newer_warm_up(self, pd):
        """Board firmware with the warm-up's duty limit (POLYDYE_WARM_MAX_DUTY_PPM)."""
        pd.warm_max_words, pd.warm_max_us, pd.warm_max_s, pd.warm_duty_ppm = 12, 2.9, 30., 5800

    def test_long_warm_up_then_the_stock_pattern_in_one_run(self):
        pd, board = self.make(fail_on="NOTHING")
        self.newer_warm_up(pd)
        # Brett's recipe: deskjet's all-nozzle 2.7 us (9 words at our clock) every 508 us for 20 s, then the stock
        # pattern straight after, in the same run and so with the same template and clock (1.8 us there).
        board, replies, sent = self.spit(pd, board, SECONDS=3., PATTERN='stock', TEMPLATE='deskjet', CLOCK='ours',
                                         PULSE=6, PERIOD_US=508, WARM=20., WARM_PULSE=9, WARM_PERIOD_US=508)
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=deskjet PULSE=6 CLOCK=ours", board.sent)
        self.assertEqual(sent, [('run', 5906, 3, 0, 508, 39370, 9, 508)])
        self.assertIn("warm-up 20.0 s: 39370 all-nozzle columns every 508 us, 9-word pulses of 2.70 us", replies[0])
        board, replies, sent = self.spit(pd, board, SECONDS=3., PATTERN='stock', PERIOD_US=508, WARM=26.,
                                         WARM_PULSE=8, WARM_PERIOD_US=508)
        # 8 words at the stock clock are 2.80 us: every 508 us a heater duty of 0.55 %, within 0.58 %.
        self.assertEqual(sent, [('run', 5906, 3, 0, 508, 51181, 8, 508)])
        self.assertIn("warm-up 26.0 s: 51181 all-nozzle columns every 508 us, 8-word pulses of 2.80 us", replies[0])
        # By default as fast as the duty allows: 2.80 us needs at least 483 us.
        _, _, sent = self.spit(pd, board, SECONDS=3., PATTERN='stock', PERIOD_US=508, WARM=20., WARM_PULSE=8)
        self.assertEqual(sent[0][5:], (41408, 8, 483))
        with self.assertRaisesRegex(RuntimeError, "WARM_PERIOD_US=482 is too short for a 2.80 us warm-up pulse: a "
                                                  "warm-up's heater duty is at most 0.58 %, so its period must be at "
                                                  "least 483 us"):
            self.spit(pd, board, SECONDS=1., PATTERN='stock', WARM=5., WARM_PULSE=8, WARM_PERIOD_US=482)
        with self.assertRaisesRegex(RuntimeError, "WARM_PULSE=9 is 3.15 us at the stock clock; a warm-up pulse is at "
                                                  "most 2.90 us"):
            self.spit(pd, board, SECONDS=1., PATTERN='stock', WARM=5., WARM_PULSE=9)
        # The warm-up's own column limit.
        with self.assertRaisesRegex(RuntimeError, "at a 300 us warm-up period the warm-up lasts at most 19.66 s"):
            self.spit(pd, board, SECONDS=1., PATTERN='all', WARM=25., WARM_PULSE=1, WARM_PERIOD_US=300)
        self.assertEqual((board.boost, board.template, board.pulse, board.clock), (False, 'color_a', 8, 'ours'))

    def test_shutdown_text_has_the_measured_bus_clock(self):
        pd, _ = self.make(fail_on="NOTHING")
        pd.query_clock_cmd = types.SimpleNamespace(send=lambda args: {'stock': 1, 'nominal_hz': 2857142,
                                                                      'measured_hz': 18491000})
        pd.bus_hz_check = (5, 8)
        self.assertEqual(pd._test_shutdown_text("POLYDYE_SPIT", "spit test", 0.),
                         "POLYDYE_SPIT: Klipper shut down during the spit test; bus clock measured 18.491 MHz (stock "
                         "clock 2.857 MHz, +547.2 %): WRONG, outside the board's +5/-8 % clock check, so it fires "
                         "nothing at it")
        self.assertEqual(polydye.measured_clock_text(2819000, 2857142, 'stock', (5, 8)),
                         "bus clock measured 2.819 MHz (stock clock 2.857 MHz, -1.3 %), within the board's +5/-8 % "
                         "clock check")
        # Older board firmware doesn't check it.
        self.assertEqual(polydye.measured_clock_text(3251000, 3333333, 'ours'),
                         "bus clock measured 3.251 MHz (ours clock 3.333 MHz, -2.5 %)")

    def clock_test(self, bad=(), **params):
        """POLYDYE_CLOCK_TEST on a board whose clock checks fail at the call numbers in `bad`; returns (board, replies,
        error or None)."""
        pd, board = self.make(fail_on="NOTHING")
        board.pulse = 9  # too long for the stock clock: the test uses 6 words and puts 9 back
        calls = []

        def check(args):
            calls.append(board.clock)
            n = len(calls)
            hz = 18491000 if n in bad else int(polydye.CLOCK_HZ[board.clock] * 0.986)
            return {'ok': 0 if n in bad else 1, 'measured_hz': hz, 'probes': 3 * n, 'resets': 2 * len(
                [b for b in bad if b <= n]), 'refused': len([b for b in bad if b <= n])}
        pd.check_clock_cmd = types.SimpleNamespace(send=check)
        replies = []
        gcmd = self.gcmd(**params)
        gcmd.respond_info = replies.append
        try:
            pd.cmd_POLYDYE_CLOCK_TEST(gcmd)
            error = None
        except RuntimeError as e:
            error = str(e)
        board.calls = calls
        return board, replies, error

    def test_clock_test_switches_and_reports(self):
        board, replies, error = self.clock_test(COUNT=3)
        self.assertIsNone(error)
        self.assertEqual(board.calls, ['ours'] + ['ours', 'stock'] * 3)
        self.assertEqual(replies, ["PolyDye clock test: 3 switches to each of ours and stock; ours clock (3.333 MHz) "
                                   "measured 3.287-3.287 MHz; stock clock (2.857 MHz) measured 2.817-2.817 MHz. A first "
                                   "measurement was off 0 times (the board set the clock up again); it stayed off 0 "
                                   "times (a live run would have fired nothing). OK"])
        self.assertEqual((board.pulse, board.clock, board.boost), (9, 'ours', False))
        self.assertEqual(board.violations, [])

    def test_clock_test_names_each_wrong_switch(self):
        board, replies, error = self.clock_test(bad=(5,), COUNT=3)
        self.assertIn("it stayed off 1 time (a live run would have fired nothing). WRONG: switch 2 to stock: "
                      "18.491 MHz", error)
        self.assertIn("stock clock (2.857 MHz) measured 2.817-18.491 MHz", error)
        self.assertEqual((board.pulse, board.clock), (9, 'ours'))
        # Older board firmware, PolyDye on: refused before anything is switched.
        pd, board = self.make(fail_on="NOTHING")
        with self.assertRaisesRegex(RuntimeError, "POLYDYE_CLOCK_TEST needs newer PolyDye board firmware"):
            pd.cmd_POLYDYE_CLOCK_TEST(self.gcmd())
        pd.check_clock_cmd = types.SimpleNamespace(send=lambda args: self.fail("checked"))
        board.boost = True
        with self.assertRaisesRegex(RuntimeError, "turn PolyDye off first"):
            pd.cmd_POLYDYE_CLOCK_TEST(self.gcmd())
        self.assertEqual(board.sent, [])

    def test_fire_test_prints_the_random_pattern(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:6:stock", X=80., Y=60., SPACING=20., COLUMNS=601,
                                           UPLOAD=1, LAYER=0, PASS=0, DENSITY=25., PER_PULSE=0))
        # Whole repeats of each random column: 300 random columns, each twice.
        self.assertIn("POLYDYE_UPLOAD_TEST_PASS LAYER=0 PASS=0 COLUMNS=300 PATTERN=stock DENSITY=25.0 REPEAT=2 "
                      "COLORS=cmy FIRST=14 LAST=85 PER_PULSE=0", board.sent)
        self.assertEqual((board.boost, board.template, board.pulse, board.clock), (False, 'color_a', 8, 'ours'))
        with self.assertRaisesRegex(RuntimeError, "go with PATTERN=stock"):
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b:6", X=80., Y=60., SPACING=20., COLUMNS=600,
                                               UPLOAD=1, LAYER=0, PASS=0, PATTERN='all', COLORS='c'))

    def test_fire_test_warms_the_head_before_each_stripe(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_run_cmd = object()
        pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:6:stock,color_b_late:8:stock", X=130., Y=100.,
                                           SPACING=20., COLUMNS=600, UPLOAD=1, LAYER=0, PASS=0, PATTERN='stock',
                                           WARM=10., WARM_PULSE=8, WARM_PERIOD_US=508))
        warm = "POLYDYE_WARM SECONDS=10 WARM_PULSE=8 WARM_PERIOD_US=508 CONFIRM=1"
        steps = [s for s in board.sent if isinstance(s, str)
                 and s.startswith(("POLYDYE_UPLOAD", "POLYDYE_WARM", "POLYDYE_ENABLE", "POLYDYE_PASS"))]
        # After the stripe's settings and its upload, straight before live ink and the pass.
        self.assertEqual([s.split()[0] for s in steps],
                         ["POLYDYE_UPLOAD_TEST_PASS", "POLYDYE_WARM", "POLYDYE_ENABLE", "POLYDYE_PASS",
                          "POLYDYE_WARM", "POLYDYE_ENABLE", "POLYDYE_PASS"])
        self.assertEqual(steps[1], warm)
        # Older board firmware: refused before anything moves.
        pd, board = self.make(fail_on="NOTHING")
        with self.assertRaisesRegex(RuntimeError, "WARM needs newer PolyDye board firmware"):
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b:6", X=80., Y=60., SPACING=20., COLUMNS=600,
                                               UPLOAD=1, LAYER=0, PASS=0, WARM=5.))
        self.assertEqual(board.sent, [])

    def test_spit_defaults_to_the_stock_firmware(self):
        # No PATTERN: the stock pattern with the stock firmware's template, clock, pulse and column period.
        board, replies, sent = self.spit(SECONDS=1.)
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=color_b_late PULSE=6 CLOCK=stock", board.sent)
        self.assertEqual(sent, [('run', 1969, 3, 0, 508, 0, 0, 0)])
        # Board firmware without the stock pattern: every nozzle at 1 kHz, as before.
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_patterns = 3
        _, _, sent = self.spit(pd, board, SECONDS=1.)
        self.assertEqual(sent, [('spit', 1000, 0)])

    def test_tests_keep_the_boost_on_with_newer_board_firmware(self):
        # Board firmware with POLYDYE_BOOST_IDLE_S takes settings with the boost on, so a test leaves the boost on and
        # only live ink off, as the stock firmware keeps the boost on from boot.
        pd, board = self.make(fail_on="NOTHING")
        pd.boost_idle_s = 1800
        board.boost = True  # on from an earlier test
        board, replies, sent = self.spit(pd, board, SECONDS=1.)
        modes = [s for s in board.sent if isinstance(s, tuple) and s[0] == 'mode']
        self.assertEqual(modes, [('mode', 1, 0), ('mode', 1, 1), ('mode', 1, 0)])
        self.assertEqual((board.boost, board.live, board.template, board.pulse, board.clock),
                         (True, False, 'color_a', 8, 'ours'))
        pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(TEMPLATE='color_b'))
        self.assertEqual((board.boost, board.template), (True, 'color_b'))
        # POLYDYE_DISABLE still turns it all off; older board firmware wants it off for a settings change.
        pd.cmd_POLYDYE_DISABLE(self.gcmd())
        self.assertEqual((board.boost, board.live), (False, False))
        pd.boost_idle_s, board.boost = None, True
        with self.assertRaisesRegex(RuntimeError, "turn PolyDye off"):
            pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(TEMPLATE='color_a'))

    def test_fire_test_defaults_to_the_stock_firmware(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(X=80., Y=60., SPACING=20., COLUMNS=600, UPLOAD=1, LAYER=0, PASS=0))
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=color_b_late PULSE=6 CLOCK=stock", board.sent)
        self.assertIn("POLYDYE_UPLOAD_TEST_PASS LAYER=0 PASS=0 COLUMNS=300 PATTERN=stock DENSITY=24.90234375 REPEAT=2 "
                      "COLORS=cmy FIRST=14 LAST=85 PER_PULSE=0", board.sent)
        self.assertEqual(sum(1 for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_PASS")), 1)

    def test_column_times_match_the_board(self):
        # printcart_column_us() in the board firmware's host tests: 148 us for color_b at our clock, 295 us at half.
        self.assertEqual(polydye.column_us('color_b', 6, 'ours'), 148)
        self.assertEqual(polydye.column_us('color_b', 4, 'half'), 295)
        self.assertEqual(polydye.column_us('color_b', 9, None), 173)
        self.assertEqual(polydye.column_us('color_b_late', 6, 'stock'), 172)
        self.assertEqual(polydye.column_us('deskjet', 6, 'ours'), 131)
        self.assertEqual(polydye.min_spit_period_us('color_b_late', 6, 'stock'), 192)

    def test_stock_spit_and_period_need_newer_board_firmware(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_patterns = 3  # board firmware with the sparse pattern but no stock one
        with self.assertRaisesRegex(RuntimeError, "PATTERN=stock needs newer PolyDye board firmware.*Update to "
                                                  "bundled firmware\\)$"):
            self.spit(pd, board, PATTERN='stock')
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
        with self.assertRaisesRegex(RuntimeError, "PERIOD_US needs newer PolyDye board firmware"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PATTERN='all', PERIOD_US=508, CONFIRM=1))
        self.assertEqual(board.sent, [])
        with self.assertRaisesRegex(RuntimeError, "PATTERN must be all, mid, sparse or stock"):
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b:6", X=80., Y=60., SPACING=20., COLUMNS=600,
                                               UPLOAD=1, LAYER=0, PASS=0, PATTERN='dots'))

    def test_fire_test_uploads_sparse_on_any_board_firmware(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_patterns = polydye.SPIT_PATTERNS_OLD
        pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="black_b:8", X=80., Y=60., SPACING=20., COLUMNS=600,
                                           UPLOAD=1, LAYER=0, PASS=0, PATTERN='sparse'))
        self.assertIn("POLYDYE_UPLOAD_TEST_PASS LAYER=0 PASS=0 COLUMNS=600 PATTERN=sparse", board.sent)
        self.assertEqual((board.boost, board.template, board.pulse), (False, 'color_a', 8))

    NOT_READY = "polydye: cartridge driver fault: a column wasn't ready at its time (columns prepared too slowly)"

    def with_run_report(self, fault='none', columns=6640, warm_left=0, underruns=0, overruns=0):
        """A board with polydye_query_run that reports this run (10 MHz clock ticks)."""
        pd, board = self.make(fail_on="NOTHING")
        pd.mcu = types.SimpleNamespace(get_constant_float=lambda name: 10000000.)
        pd.query_run_cmd = types.SimpleNamespace(send=lambda args: {
            'fault_static_string_id': fault, 'columns': columns, 'warm_left': warm_left, 'underruns': underruns,
            'overruns': overruns, 'late_max': 23, 'fill_max': 1041})
        return pd, board

    def test_spit_reports_how_the_board_ran_it(self):
        pd, board = self.with_run_report(columns=6640)
        _, replies, _ = self.spit(pd, board, SECONDS=1., PATTERN='all', WARM=1.)
        self.assertEqual(replies[-2], "PolyDye board: 6640 columns sent, worst lateness 2.3 us, slowest column "
                                      "preparation 104.1 us")
        self.assertTrue(replies[-1].startswith("PolyDye spit test done (faults=0)"))
        self.assertFalse(pd.testing)

    def test_a_fault_during_the_warm_up_says_what_and_where(self):
        pd, board = self.with_run_report(fault=self.NOT_READY, columns=17, warm_left=33096, underruns=1)
        shut = []
        pd.printer.is_shutdown = lambda: bool(shut)
        pd.spit_cmd = pd.spit_ext_cmd = types.SimpleNamespace(send=lambda args: None)
        pd.spit_run_cmd = types.SimpleNamespace(send=lambda args: shut.append(1))
        gcmd = self.gcmd(SECONDS=3., TEMPLATE='deskjet', PULSE=12, PATTERN='sparse', EXTENDED=1, WARM=5., CONFIRM=1)
        with self.assertRaises(RuntimeError) as e:
            pd.cmd_POLYDYE_SPIT(gcmd)
        self.assertEqual(str(e.exception),
                         "POLYDYE_SPIT: Klipper shut down during the warm-up: cartridge driver fault: a column wasn't "
                         "ready at its time (columns prepared too slowly), after 17 columns sent (33096 warm-up "
                         "columns not sent), 1 column time missed (1 with no column ready, 0 with the bus busy), "
                         "worst lateness 2.3 us, slowest column preparation 104.1 us")
        self.assertFalse(pd.testing)
        # Past the warm-up it is the spit test's; and older board firmware can't say more than that it shut down.
        pd.query_run_cmd = types.SimpleNamespace(send=lambda args: {
            'fault_static_string_id': self.NOT_READY, 'columns': 40000, 'warm_left': 0, 'underruns': 1,
            'overruns': 0, 'late_max': 0, 'fill_max': 0})
        self.assertTrue(pd._test_shutdown_text("POLYDYE_SPIT", "spit test", 5.).startswith(
            "POLYDYE_SPIT: Klipper shut down during the spit test: cartridge driver fault: a column wasn't ready"))
        pd.query_run_cmd = None
        self.assertEqual(pd._test_shutdown_text("POLYDYE_WARM", "warm-up", 5.),
                         "POLYDYE_WARM: Klipper shut down during the warm-up")

    def test_a_pass_fault_is_reported_after_the_shutdown(self):
        pd, board = self.with_run_report(fault=self.NOT_READY, columns=212, underruns=1)
        callbacks, replies = [], []
        reactor = types.SimpleNamespace(NEVER=9e99, register_callback=callbacks.append)
        objects = {'gcode': types.SimpleNamespace(respond_info=replies.append)}
        pd.printer = types.SimpleNamespace(get_reactor=lambda: reactor, lookup_object=lambda name: objects[name])
        pd.status_timer, pd.testing = None, False
        pd._handle_shutdown()
        self.assertEqual(len(callbacks), 1)
        with self.assertLogs(level='WARNING'):
            callbacks.pop()(0.)
        self.assertEqual(replies, ["PolyDye board: cartridge driver fault: a column wasn't ready at its time (columns "
                                   "prepared too slowly), after 212 columns sent, 1 column time missed (1 with no "
                                   "column ready, 0 with the bus busy), worst lateness 2.3 us, slowest column "
                                   "preparation 104.1 us"])
        # No fault (an M112, say), a spit test that reports it itself, older board firmware: nothing.
        pd, _ = self.with_run_report()
        pd.printer = types.SimpleNamespace(get_reactor=lambda: reactor, lookup_object=lambda name: objects[name])
        pd.status_timer, pd.testing = None, False
        pd._handle_shutdown()
        callbacks.pop()(0.)
        pd.testing = True
        pd._handle_shutdown()
        pd.testing, pd.query_run_cmd = False, None
        pd._handle_shutdown()
        self.assertEqual((callbacks, len(replies)), ([], 1))
        # A pass refused at a wrong bus clock never ran: the board's measurement says why.
        pd, _ = self.with_run_report()
        pd.printer = types.SimpleNamespace(get_reactor=lambda: reactor, lookup_object=lambda name: objects[name])
        pd.status_timer, pd.testing, pd.bus_hz_check = None, False, (5, 8)
        pd.query_clock_cmd = types.SimpleNamespace(send=lambda args: {'stock': 1, 'nominal_hz': 2857142,
                                                                      'measured_hz': 18491000})
        pd._handle_shutdown()
        with self.assertLogs(level='WARNING'):
            callbacks.pop()(0.)
        self.assertTrue(replies[-1].startswith("PolyDye board: bus clock measured 18.491 MHz (stock clock 2.857 MHz"))


class RandomPattern(unittest.TestCase):
    """The random pattern's columns are the board's (printcart_pattern_random), so a fire test prints what a spit test
    fires."""

    @staticmethod
    def crc(r, columns=64):
        plan, crc = polydye.random_plan(r), 0
        for k in range(columns):
            crc = zlib.crc32(polydye.random_column(k, r, plan), crc)
        return crc

    @staticmethod
    def fired(col):
        """The (colour, position) of every heater a column fires."""
        out = set()
        for c in range(3):
            for p in range(polydye.COLOR_POSITIONS):
                if col[polydye.COLOR_BYTE_ORDER[c][p % 14] + 14 * c] >> (p // 14) & 1:
                    out.add((c, p))
        return out

    def test_columns_match_the_board_firmware(self):
        # The vectors of test_random_pattern in the board firmware's host tests.
        self.assertEqual(self.crc(polydye.STOCK_RANDOM), 0xfc3951e1)
        self.assertEqual(self.crc(dict(density=512, repeat=1, colors=1, first=0, last=111, per_pulse=0)), 0x502d7ac6)
        self.assertEqual(self.crc(dict(density=0, repeat=2, colors=6, first=10, last=99, per_pulse=3)), 0xebc10779)

    def test_stock_settings(self):
        r = polydye.STOCK_RANDOM
        cols = [polydye.random_column(k, r) for k in range(400)]
        self.assertTrue(all(cols[k] == cols[k + 1] for k in range(0, 400, 2)))
        fired = [self.fired(col) for col in cols[::2]]
        self.assertTrue(all(14 <= p <= 85 for f in fired for _, p in f))
        rate = sum(map(len, fired)) / (200. * 3 * 72)
        self.assertAlmostEqual(rate, 255 / 1024., delta=0.01)

    def test_knobs(self):
        r = dict(polydye.STOCK_RANDOM, colors=2, first=30, last=40, repeat=3, density=1024)
        cols = [polydye.random_column(k, r) for k in range(6)]
        self.assertEqual(self.fired(cols[0]), {(1, p) for p in range(30, 41)})
        self.assertEqual(cols[0], cols[2])
        # PER_PULSE: exactly that many of the eligible heaters on every F3 and every F5 pulse.
        r = dict(polydye.STOCK_RANDOM, per_pulse=2, repeat=1)
        for k in range(20):
            col = polydye.random_column(k, r)
            for packet in range(14):
                for half in (0x0f, 0xf0):
                    self.assertEqual(sum(bin(col[14 * line + packet] & half).count('1') for line in range(3)), 2)

    def test_test_pass_data(self):
        data, total = polydye.test_pass_data('stock', 5, 2)
        self.assertEqual(total, 10)
        self.assertEqual(data, b"".join(polydye.random_column(k, polydye.STOCK_RANDOM) for k in range(10)))
        r = dict(polydye.STOCK_RANDOM, colors=4)
        data, total = polydye.test_pass_data('stock', 3, 1, r)
        self.assertEqual(data, b"".join(polydye.random_column(k, dict(r, repeat=1)) for k in range(3)))
        self.assertEqual(polydye.parse_colors("yC"), 5)


class BuildConfig(unittest.TestCase):
    """The pulse and clock from polydye.cfg, sent on every connect."""

    def build(self, constants, bus_clock, pulse_words, template=None):
        class ConfigError(Exception):
            pass
        pd = object.__new__(polydye.PolyDye)
        pd.oid, pd.cartridge_template, pd.bus_clock, pd.pulse_words = 0, template, bus_clock, pulse_words
        pd.spit_patterns = pd.clock_count = pd.template_count = 0
        pd.spit_ext_cmd = pd.spit_run_cmd = None
        pd.spit_random_cmd = None
        pd.warm_max_words, pd.warm_max_us, pd.warm_max_s = polydye.WARM_OLD_WORDS, polydye.WARM_OLD_US, polydye.WARM_OLD_S
        pd.warm_duty_ppm = None
        pd.bus_hz_check = None
        pd.boost_idle_s = None
        pd.pulse_max_ext_us = polydye.PULSE_MAX_EXT_US
        pd.printer = types.SimpleNamespace(config_error=ConfigError)
        sent = []
        pd.test_sent = sent
        pd.mcu = types.SimpleNamespace(
            add_config_cmd=lambda cmd, is_init=False: sent.append(cmd), alloc_command_queue=lambda: None,
            lookup_command=lambda *a, **kw: object(), lookup_query_command=lambda *a, **kw: object(),
            try_lookup_command=lambda *a, **kw: object(), get_constants=lambda: dict(constants))
        try:
            with mock.patch.object(polydye.logging, "warning") as warn:
                pd._build_config()
            pd.test_warnings = [c[0][0] for c in warn.call_args_list]
        except ConfigError as e:
            return pd, str(e)
        return pd, [cmd for cmd in sent if cmd.startswith(("polydye_set_pulse", "polydye_set_clock"))]

    def test_half_clock_goes_through_ours_then_the_pulse(self):
        pd, sent = self.build({'POLYDYE_BUS_CLOCKS': 3}, 'half', 4)
        self.assertEqual(pd.clock_count, 3)
        self.assertEqual(sent, ["polydye_set_clock oid=0 stock=0", "polydye_set_pulse oid=0 words=4",
                                "polydye_set_clock oid=0 stock=2"])

    def test_board_firmware_that_leaves_usb_on_reset_is_flagged(self):
        pd, _ = self.build({'POLYDYE_SOFT_RESET': 1}, None, None)
        self.assertTrue(pd.board_soft_reset)
        self.assertEqual(pd.test_warnings, [])
        pd, _ = self.build({}, None, None)
        self.assertFalse(pd.board_soft_reset)
        self.assertIn("leaves USB and comes back whenever Klipper resets it", pd.test_warnings[0])
        pd.last_status, pd.last_pass, pd.pass_axis, pd.offset_x, pd.offset_y, pd.direction_trim = {}, None, 'y', 0, 0, 0
        self.assertIs(pd.get_status(0.)['board_soft_reset'], False)  # the page shows its warning

    def test_deskjet_template_needs_newer_board_firmware(self):
        pd, error = self.build({}, None, None, 'deskjet')
        self.assertIn("cartridge_template: deskjet needs newer PolyDye board firmware", error)
        self.assertIn("Update to bundled firmware", error)
        pd, _ = self.build({'POLYDYE_TEMPLATES': 6}, None, None, 'deskjet')
        self.assertEqual(pd.template_count, 6)
        self.assertIn("polydye_set_template oid=0 template=5", pd.test_sent)
        pd.last_status, pd.last_pass, pd.pass_axis, pd.offset_x, pd.offset_y, pd.direction_trim = {}, None, 'y', 0, 0, 0
        self.assertEqual(pd.get_status(0.)['templates'][-1], 'deskjet')

    def test_warm_up_and_clock_check_follow_the_board(self):
        pd, _ = self.build({}, None, None)
        self.assertEqual((pd.warm_max_words, pd.warm_max_us, pd.warm_max_s, pd.warm_duty_ppm, pd.bus_hz_check),
                         (2, 0.75, 10., None, None))
        pd, _ = self.build({'POLYDYE_WARM_MAX_WORDS': 12, 'POLYDYE_WARM_MAX_NS': 2900, 'POLYDYE_WARM_MAX_DUTY_PPM': 5800,
                            'POLYDYE_WARM_MAX_MS': 30000, 'POLYDYE_BUS_HZ_FAST_PCT': 5, 'POLYDYE_BUS_HZ_SLOW_PCT': 8},
                           None, None)
        self.assertEqual((pd.warm_max_words, pd.warm_max_us, pd.warm_max_s, pd.warm_duty_ppm, pd.bus_hz_check),
                         (12, 2.9, 30., 5800, (5, 8)))
        self.assertIsNotNone(pd.spit_random_cmd)
        pd.last_status, pd.last_pass, pd.pass_axis, pd.offset_x, pd.offset_y, pd.direction_trim = {}, None, 'y', 0, 0, 0
        st = pd.get_status(0.)
        self.assertEqual((st['spit_random'], st['warm_max_us'], st['warm_max_s'], st['warm_duty_ppm']),
                         (True, 2.9, 30., 5800))

    def test_extended_limit_follows_the_board(self):
        # The board says how long an extended pulse may be; firmware with the extended spit but no constant has 4.0 us.
        for constants, cap, words in (({'POLYDYE_PULSE_MAX_EXT_NS': 5000}, 5.0, (16, 14, 8)),
                                      ({'POLYDYE_PULSE_MAX_EXT_NS': 4000}, 4.0, (13, 11, 6)),
                                      ({}, 4.0, (13, 11, 6))):
            pd, _ = self.build(constants, None, None)
            self.assertIsNotNone(pd.spit_ext_cmd)
            self.assertEqual(pd.pulse_max_ext_us, cap)
            self.assertEqual(tuple(polydye.max_ext_pulse_words(c, pd.pulse_max_ext_us)
                                   for c in ('ours', 'stock', 'half')), words)
            self.assertIsNone(polydye.ext_pulse_range_error(words[0], 'ours', pd.pulse_max_ext_us))
            self.assertIn("use 6-%d words there" % words[0],
                          polydye.ext_pulse_range_error(words[0] + 1, 'ours', pd.pulse_max_ext_us))

    def test_half_clock_needs_newer_board_firmware(self):
        pd, error = self.build({}, 'half', 4)
        self.assertIn("bus_clock: half needs newer PolyDye board firmware", error)
        self.assertIn("Update to bundled firmware", error)
        pd, sent = self.build({}, 'stock', 8)
        self.assertEqual(pd.clock_count, polydye.BUS_CLOCKS_OLD)
        self.assertEqual(sent[-1], "polydye_set_clock oid=0 stock=1")


if __name__ == "__main__":
    unittest.main()
