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
        pd.querying, pd.last_status, pd.oid, pd.query_level_cmd = True, {}, 0, None

        def pause(waketime):
            pd.querying = False     # the background query finishes
        reactor = types.SimpleNamespace(monotonic=lambda: 0., pause=pause)
        pd.printer = types.SimpleNamespace(get_reactor=lambda: reactor)
        pd.mcu = types.SimpleNamespace(get_constant_float=lambda name: 10e6)
        status = {'flags': polydye.FLAG_BOOST, 'queued': 0, 'passes': 3, 'late_max': 100, 'faults': 0,
                  'store_count': 1, 'store_free': 4096}
        pd.query_cmd = types.SimpleNamespace(send=lambda args: status)
        pd.query_cartridge_cmd = types.SimpleNamespace(send=lambda args: {'template': 4, 'pulse': 6})
        pd.query_clock_cmd = types.SimpleNamespace(send=lambda args: {'stock': 1, 'nominal_hz': 2857142,
                                                                      'measured_hz': 0})
        pd.pass_axis, pd.offset_x, pd.offset_y, pd.direction_trim, pd.dpi, pd.pass_speed = 'y', 1., 2., 0., 600., 50.
        replies = []
        pd.cmd_POLYDYE_STATUS(types.SimpleNamespace(respond_info=replies.append))
        self.assertIn("boost=1", replies[0])
        self.assertIn("passes=3", replies[0])
        self.assertIn("cartridge template color_b_late, fire pulse 6 words = 2.10 us", replies[0])
        self.assertIn("bus clock stock (2.857 MHz)", replies[0])


class Stripes(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(polydye.parse_stripes(polydye.FIRE_TEST_STRIPES), [('color_b_late', 6)])
        # A :stock clock from before the stock-only cleanup is taken and dropped.
        self.assertEqual(polydye.parse_stripes(" Black_A:8 , color_b_late:7:STOCK"),
                         [('black_a', 8), ('color_b_late', 7)])
        for bad, message in (("", "no stripes"), ("color_c:6", "color_c is not a cartridge template"),
                             ("color_b_late", "each stripe is TEMPLATE:PULSE"), ("color_b_late:x", "whole number"),
                             ("color_b_late:5", "less than Sprite_tm's 1.8 us"), ("color_b_late:9", "2.9 us limit"),
                             ("color_b_late:6:fast", "CLOCK=fast is not a bus clock"),
                             ("color_b_late:6:stock:x", "each stripe is TEMPLATE:PULSE"),
                             ("color_b:6", "color_b:6: TEMPLATE=color_b was removed \\(stock baseline\\)"),
                             ("deskjet:6", "TEMPLATE=deskjet was removed"),
                             ("color_b_late:8:ours", "CLOCK=ours was removed")):
            with self.assertRaisesRegex(ValueError, message):
                polydye.parse_stripes(bad)

    def test_pulse_range_is_6_to_8_words_at_the_stock_clock(self):
        # 0.35 us a word: 6 is the first at least Sprite_tm's 1.8 us, 8 the last within the 2.9 us limit.
        self.assertAlmostEqual(polydye.pulse_us(6), 2.1, places=3)
        self.assertTrue(polydye.pulse_us(polydye.PULSE_MIN - 1) < polydye.REF_US <= polydye.pulse_us(polydye.PULSE_MIN))
        self.assertTrue(polydye.pulse_us(polydye.PULSE_MAX) <= polydye.PULSE_MAX_US
                        < polydye.pulse_us(polydye.PULSE_MAX + 1))
        for words in range(1, 13):
            self.assertEqual(polydye.pulse_range_error(words) is None, 6 <= words <= 8, words)
            self.assertEqual(polydye.pulse_limit_error(words) is None, words <= 8, words)
        self.assertEqual(polydye.pulse_range_error(5), "fire pulse 5 words is 1.75 us at the stock clock, less than "
                                                       "Sprite_tm's 1.8 us; use 6-8 words (2.10-2.80 us)")
        self.assertEqual(polydye.pulse_range_error(9), "fire pulse 9 words is 3.15 us at the stock clock, over the 2.9 "
                                                       "us limit; use 6-8 words (2.10-2.80 us)")
        self.assertAlmostEqual(polydye.pulse_energy_pct(polydye.PULSE_MAX_US), 100., delta=1.)

    def test_positions_side_by_side_across_the_pass_axis(self):
        self.assertEqual(polydye.stripe_positions('y', 80., 60., 20., 3), [(80., 60.), (100., 60.), (120., 60.)])
        self.assertEqual(polydye.stripe_positions('x', 80., 60., 20., 2), [(80., 60.), (80., 80.)])


class TestPassData(unittest.TestCase):
    def test_repeat_sends_each_column_n_times(self):
        full, empty = b"\xff" * polydye.NOZDATA_SIZE, b"\x00" * polydye.NOZDATA_SIZE
        data, total = polydye.test_pass_data('alternate', 4, 2)
        self.assertEqual((data, total), (full + full + empty + empty + full + full + empty + empty, 8))
        self.assertEqual(polydye.test_pass_data('none', 3, 2), (empty * 6, 6))
        self.assertEqual(polydye.test_pass_data('all', 5), (full * 5, 5))
        with self.assertRaisesRegex(ValueError, "PATTERN must be alternate, all, none or stock"):
            polydye.test_pass_data('stripes', 5)
        for removed in ('mid', 'sparse'):
            with self.assertRaisesRegex(ValueError, "PATTERN=%s was removed: use stock" % (removed,)):
                polydye.test_pass_data(removed, 5)


class FakeBoard:
    """Just enough of the board for POLYDYE_FIRE_TEST / POLYDYE_SPIT: tracks mode, template and pulse, from the
    stock-only board firmware's power-up state."""

    def __init__(self):
        self.boost = self.live = False
        self.template, self.pulse = 'color_b_late', 6
        self.measured = None
        self.sent = []
        self.violations = []  # pulses the board would have refused

    def check(self):
        if polydye.pulse_limit_error(self.pulse):
            self.violations.append(self.pulse)

    def status(self):
        return {'boost': self.boost, 'live': self.live, 'running': False, 'queued': 0, 'faults': 0,
                'cartridge_template': self.template, 'pulse_words': self.pulse,
                'pulse_us': round(polydye.pulse_us(self.pulse), 2), 'bus_clock': 'stock', 'bus_hz': polydye.BUS_HZ,
                'bus_hz_measured': self.measured}


class TestCleanup(unittest.TestCase):
    """A failing stripe must still leave PolyDye off with the template and pulse put back."""

    def make(self, fail_on):
        board = FakeBoard()
        pd = object.__new__(polydye.PolyDye)
        pd.oid, pd.pass_axis = 0, 'y'
        pd.query_cartridge_cmd = object()
        pd.spit_patterns = tuple(polydye.SPIT_PATTERNS)
        pd.templates = polydye.TEMPLATES
        pd.spit_run_cmd = None
        pd.query_run_cmd = None
        pd.spit_random_cmd = None
        pd.warm_max_words, pd.warm_max_us, pd.warm_max_s = polydye.WARM_OLD_WORDS, polydye.WARM_OLD_US, polydye.WARM_OLD_S
        pd.warm_duty_ppm = None
        pd.query_clock_cmd = pd.bus_hz_check = None
        pd.boost_idle_s = None  # board firmware that turns PolyDye off after a test
        pd.warm_seconds, pd.warm_pulse, pd.warm_period_us, pd.warm_idle_time = 10., 1, 254, 60.
        pd.warm_position, pd.warm_clearance = 'pass', 10.
        pd.last_fire, pd.auto_warm_off = None, False
        pd.mcu = types.SimpleNamespace(estimated_print_time=lambda eventtime: board.print_time)
        board.print_time = 100.

        def mode(args):
            board.boost, board.live = bool(args[1]), bool(args[2])
            board.sent.append(('mode', args[1], args[2]))
        pd.mode_cmd = types.SimpleNamespace(send=mode)
        pd.set_template_cmd = types.SimpleNamespace(
            send=lambda args: (setattr(board, 'template', polydye.BOARD_TEMPLATES[args[1]]), board.sent.append('tpl')))
        pd.set_pulse_cmd = types.SimpleNamespace(
            send=lambda args: (setattr(board, 'pulse', args[1]), board.sent.append('pulse'), board.check()))
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

    FIRE = dict(X=80., Y=60., SPACING=20., COLUMNS=600, UPLOAD=1, LAYER=0, PASS=0)

    def test_fire_test_error_turns_off_and_restores(self):
        pd, board = self.make(fail_on="POLYDYE_PASS")
        with self.assertRaisesRegex(RuntimeError, "out of range"):
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="black_b:8", Z=0.5, PATTERN='all', **self.FIRE))
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=black_b PULSE=8", board.sent)
        self.assertIn("POLYDYE_UPLOAD_TEST_PASS LAYER=0 PASS=0 COLUMNS=600 PATTERN=all", board.sent)
        # The G-code state saved before the Z move is restored after the error.
        self.assertLess(board.sent.index("SAVE_GCODE_STATE NAME=pd_fire_test"), board.sent.index("G90\nG1 Z0.500 F900"))
        self.assertEqual(board.sent[-1], "RESTORE_GCODE_STATE NAME=pd_fire_test")
        self.assertEqual((board.boost, board.live, board.template, board.pulse), (False, False, 'color_b_late', 6))

    def test_fire_test_restores_after_success(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="black_a:7,black_a:8", PATTERN='all', **self.FIRE))
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=black_a PULSE=8", board.sent)
        passes = [s for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_PASS")]
        self.assertEqual(passes, ["POLYDYE_PASS LAYER=0 PASS=0 X=80.000 Y=60.000 DIR=1",
                                  "POLYDYE_PASS LAYER=0 PASS=0 X=100.000 Y=60.000 DIR=1"])
        self.assertEqual(sum(1 for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_UPLOAD")), 1)
        self.assertEqual((board.boost, board.template, board.pulse), (False, 'color_b_late', 6))
        self.assertEqual(['tpl', 'pulse'], [s for s in board.sent if s in ('tpl', 'pulse')])

    def test_refused_during_a_paused_print_before_anything_is_sent(self):
        for state in ('printing', 'paused'):
            pd, board = self.make(fail_on="NOTHING")
            board.print_state = state
            pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
            with self.assertRaisesRegex(RuntimeError, "not during a print"):
                pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:8", PATTERN='all', **self.FIRE))
            with self.assertRaisesRegex(RuntimeError, "not during a print"):
                pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PATTERN='all', CONFIRM=1))
            self.assertEqual(board.sent, [])

    def test_pulses_outside_6_to_8_words_are_refused_before_anything_is_sent(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
        with self.assertRaisesRegex(RuntimeError, "STRIPES: color_b_late:9: fire pulse 9 words is 3.15 us at the stock "
                                                  "clock, over the 2.9 us limit"):
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:6,color_b_late:9", PATTERN='all', **self.FIRE))
        with self.assertRaisesRegex(RuntimeError, "over the 2.9 us limit"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PULSE=9, PATTERN='all', CONFIRM=1))
        with self.assertRaisesRegex(RuntimeError, "less than Sprite_tm's 1.8 us"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PULSE=5, PATTERN='all', CONFIRM=1))
        with self.assertRaisesRegex(RuntimeError, "POLYDYE_CARTRIDGE: fire pulse 3 words"):
            pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(PULSE=3))
        self.assertEqual(board.sent, [])

    def test_cartridge_refuses_an_over_limit_result_even_if_unchanged(self):
        # A board left at 9 words (3.15 us) by older firmware: a template change on its own is refused too.
        pd, board = self.make(fail_on="NOTHING")
        board.pulse = 9
        with self.assertRaisesRegex(RuntimeError, "POLYDYE_CARTRIDGE: fire pulse 9 words .* over the 2.9 us limit"):
            pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(TEMPLATE='black_a'))
        self.assertEqual(board.sent, [])
        pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(PULSE=8))
        self.assertEqual(board.sent, ['pulse'])
        # Settings the board already has aren't sent again.
        pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(TEMPLATE='color_b_late', PULSE=8))
        self.assertEqual(board.sent, ['pulse'])

    def test_tests_and_live_ink_refuse_an_over_limit_starting_setting(self):
        # Older firmware left at 9 words (3.15 us): a test would put it back afterwards, live ink would use it.
        pd, board = self.make(fail_on="NOTHING")
        board.pulse = 9
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
        pd.mode_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('mode'))
        with self.assertRaisesRegex(RuntimeError, "PULSE=8"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PULSE=8, PATTERN='all', CONFIRM=1))
        with self.assertRaisesRegex(RuntimeError, "over-limit pulse"):
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:6", PATTERN='all', **self.FIRE))
        with self.assertRaisesRegex(RuntimeError, "over-limit pulse"):
            pd.cmd_POLYDYE_ENABLE(self.gcmd(LIVE=1))
        self.assertEqual(board.sent, [])

    def test_tests_and_live_ink_refuse_a_board_on_another_clock(self):
        # Older firmware switched to half on its console after connecting: the add-on's timing assumes the stock clock.
        pd, board = self.make(fail_on="NOTHING")
        status = board.status
        pd._fresh_status = pd._query_status = lambda: dict(status(), bus_clock='half')
        pd.spit_cmd = pd.spit_run_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
        pd.mode_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('mode'))
        for run in (lambda: pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=0.2, PERIOD_US=200, CONFIRM=1)),
                    lambda: pd.cmd_POLYDYE_WARM(self.gcmd(SECONDS=0.5, CONFIRM=1)),
                    lambda: pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:6", PATTERN='all', **self.FIRE)),
                    lambda: pd.cmd_POLYDYE_ENABLE(self.gcmd(LIVE=1)),
                    # 8 words would be 4.8 us at half: the board would shut down rather than take it.
                    lambda: pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(PULSE=8)),
                    lambda: pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(TEMPLATE='black_b'))):
            with self.assertRaisesRegex(RuntimeError, "runs the half bus clock .* FIRMWARE_RESTART sets it back"):
                run()
        self.assertEqual(board.sent, [])
        # At the stock clock an over-limit pulse left by older firmware can still be shortened.
        pd._fresh_status = pd._query_status = board.status
        board.pulse = 9
        pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(PULSE=8))
        self.assertEqual((board.sent, board.pulse), (['pulse'], 8))

    def test_removed_settings_are_refused_before_anything_is_sent(self):
        pd, board = self.make(fail_on="NOTHING")
        sent = []
        pd.spit_cmd = pd.spit_run_cmd = types.SimpleNamespace(send=sent.append)
        for run, message in (
                (lambda: pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PULSE=8, EXTENDED=1, CONFIRM=1)),
                 "^POLYDYE_SPIT: EXTENDED was removed \\(stock baseline\\): the fire pulse is at most 2.9 us \\(8 "
                 "words\\)$"),
                (lambda: pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., CLOCK='ours', CONFIRM=1)),
                 "^POLYDYE_SPIT: CLOCK=ours was removed: the bus runs only at the stock clock \\(2.857 MHz\\)$"),
                (lambda: pd.cmd_POLYDYE_WARM(self.gcmd(SECONDS=1., CLOCK='Half', CONFIRM=1)),
                 "^POLYDYE_WARM: CLOCK=half was removed"),
                (lambda: pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(PULSE=7, CLOCK='ours')),
                 "^POLYDYE_CARTRIDGE: CLOCK=ours was removed"),
                (lambda: pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(TEMPLATE='DeskJet')),
                 "^POLYDYE_CARTRIDGE: TEMPLATE=deskjet was removed \\(stock baseline\\): use color_b_late, black_a or "
                 "black_b$"),
                (lambda: pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., TEMPLATE='color_a', PATTERN='all', CONFIRM=1)),
                 "^POLYDYE_SPIT: TEMPLATE=color_a was removed"),
                (lambda: pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PATTERN='sparse', CONFIRM=1)),
                 "^POLYDYE_SPIT: PATTERN=sparse was removed: use stock \\(its knobs, e.g. PER_PULSE=1 for one heater "
                 "per fire pulse\\) or all$"),
                (lambda: pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(PATTERN='mid', **self.FIRE)),
                 "^POLYDYE_FIRE_TEST: PATTERN=mid was removed"),
                (lambda: pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:6:half", **self.FIRE)),
                 "^POLYDYE_FIRE_TEST: STRIPES: color_b_late:6:half: CLOCK=half was removed"),
                (lambda: pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b:6", **self.FIRE)),
                 "^POLYDYE_FIRE_TEST: STRIPES: color_b:6: TEMPLATE=color_b was removed"),
                (lambda: pd.cmd_POLYDYE_CLOCK_TEST(self.gcmd(COUNT=5)),
                 "^POLYDYE_CLOCK_TEST was removed: the bus runs only at the stock clock \\(2.857 MHz\\)")):
            with self.assertRaisesRegex(RuntimeError, message):
                run()
        self.assertEqual((board.sent, sent), ([], []))
        # CLOCK=stock, :stock stripes and EXTENDED=0 are still taken, and change nothing.
        pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PATTERN='all', CLOCK='stock', EXTENDED=0, CONFIRM=1))
        pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:7:stock", PATTERN='all', **self.FIRE))
        self.assertEqual(sent, [[0, 1000, 0]])
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=color_b_late PULSE=7", board.sent)

    def test_spit_needs_confirm(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append(('spit', args[1], args[2])))
        with self.assertRaisesRegex(RuntimeError, "CONFIRM=1"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=2., PATTERN='all'))
        self.assertEqual(board.sent, [])

    def test_spit_sends_columns_then_turns_off_and_restores(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append(('spit', args[1], args[2])))
        pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1.5, TEMPLATE='black_a', PULSE=7, PATTERN='all', CONFIRM=1))
        self.assertIn(('spit', 1500, 0), board.sent)
        self.assertLess(board.sent.index("POLYDYE_CARTRIDGE TEMPLATE=black_a PULSE=7"),
                        board.sent.index(('mode', 1, 1)))
        self.assertEqual((board.boost, board.live, board.template, board.pulse), (False, False, 'color_b_late', 6))

    def test_spit_reports_the_pulse_it_used(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: (board.sent.append(('spit', args[1], args[2])),
                                                               setattr(board, 'measured', 2820000)))
        gcmd = self.gcmd(SECONDS=8., PULSE=8, PATTERN='all', CONFIRM=1)
        replies = []
        gcmd.respond_info = replies.append
        pd.cmd_POLYDYE_SPIT(gcmd)
        self.assertIn(('spit', 8000, 0), board.sent)
        self.assertIn("8000 all-nozzle columns at 1000 Hz", replies[0])
        self.assertIn("done (faults=0): fired with cartridge template color_b_late, fire pulse 8 words = 2.80 us",
                      replies[-1])
        self.assertIn("now back to cartridge template color_b_late, fire pulse 6 words", replies[-1])
        # The rate measured during the spit is reported once, with the settings it fired with.
        self.assertEqual(replies[-1].count("measured 2.820 MHz"), 1)
        self.assertLess(replies[-1].index("measured"), replies[-1].index("now back to"))
        self.assertEqual((board.boost, board.live, board.pulse), (False, False, 6))

    def test_spit_done_without_changes_names_the_settings_once(self):
        pd, board = self.make(fail_on="NOTHING")
        # The board measures the bus rate of the columns it sends: a new reading isn't a change of settings.
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: setattr(board, 'measured', 2820000))
        gcmd = self.gcmd(SECONDS=1., PATTERN='all', CONFIRM=1)
        replies = []
        gcmd.respond_info = replies.append
        pd.cmd_POLYDYE_SPIT(gcmd)
        self.assertIn("fired with cartridge template color_b_late, fire pulse 6 words", replies[-1])
        self.assertIn("last column measured 2.820 MHz", replies[-1])
        self.assertNotIn("back to", replies[-1])

    def test_color_b_late_needs_board_firmware_with_it(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.templates = ('black_a', 'black_b')  # board firmware without polydye_set_clock
        for run in (lambda: pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(TEMPLATE='color_b_late')),
                    lambda: pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(**self.FIRE))):
            with self.assertRaisesRegex(RuntimeError, "TEMPLATE=color_b_late needs newer PolyDye board firmware.*"
                                                      "Update to bundled firmware"):
                run()
        self.assertEqual(board.sent, [])

    def spit(self, pd=None, board=None, command='cmd_POLYDYE_SPIT', has_random=True, **params):
        """Runs POLYDYE_SPIT (or `command`) on a board with every spit command (without the random pattern's knobs
        unless has_random); returns (board, replies, commands sent)."""
        if pd is None:
            pd, board = self.make(fail_on="NOTHING")
        sent = []
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: sent.append(('spit',) + tuple(args[1:])))
        pd.spit_run_cmd = types.SimpleNamespace(send=lambda args: sent.append(('run',) + tuple(args[1:])))
        if has_random:
            pd.spit_random_cmd = types.SimpleNamespace(send=lambda args: sent.append(('random',) + tuple(args[1:])))
        gcmd = self.gcmd(CONFIRM=1, **params)
        replies = []
        gcmd.respond_info = replies.append
        getattr(pd, command)(gcmd)
        return board, replies, sent

    def test_stock_spit_fires_with_the_stock_timing(self):
        pd, board = self.make(fail_on="NOTHING")
        board.template, board.pulse = 'black_b', 8
        board, replies, sent = self.spit(pd, board, SECONDS=3., PATTERN='stock', PERIOD_US=508)
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=color_b_late PULSE=6", board.sent)
        self.assertEqual(sent, [('run', 5906, 3, 0, 508, 0, 0, 0)])
        self.assertIn("5906 stock-emulation (a random 24.9 % of positions 14-85, each column twice) columns every "
                      "508 us (1969 Hz)", replies[0])
        self.assertIn("cartridge template color_b_late, fire pulse 6 words = 2.10 us", replies[0])
        self.assertIn("bus clock stock (2.857 MHz)", replies[0])
        self.assertIn("now back to cartridge template black_b, fire pulse 8 words", replies[-1])
        self.assertEqual((board.boost, board.template, board.pulse), (False, 'black_b', 8))
        self.assertEqual(board.violations, [])

    def test_stock_spit_with_its_own_pulse_and_the_default_period(self):
        board, replies, sent = self.spit(PATTERN='stock', PULSE=7)
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=color_b_late PULSE=7", board.sent)
        # The stock firmware's column period unless given.
        self.assertEqual(sent, [('run', 3937, 3, 0, 508, 0, 0, 0)])
        self.assertIn("columns every 508 us (1969 Hz)", replies[0])
        with self.assertRaisesRegex(RuntimeError, "PATTERN=stock is for a colour cartridge template"):
            self.spit(PATTERN='stock', TEMPLATE='black_b')

    def test_spit_period_limits(self):
        # color_b_late with a 6-word pulse: 172 us on the bus, so at least 192 us.
        with self.assertRaisesRegex(RuntimeError, "the column takes 172 us on the bus, so the period must be at least "
                                                  "192 us"):
            self.spit(SECONDS=1., PATTERN='stock', PERIOD_US=191)
        _, _, sent = self.spit(SECONDS=1., PATTERN='stock', PERIOD_US=192)
        self.assertEqual(sent, [('run', 5208, 3, 0, 192, 0, 0, 0)])
        # At most 10000 columns whatever the period.
        with self.assertRaisesRegex(RuntimeError, "at a 254 us column period a spit test lasts at most 2.54 s "
                                                  "\\(10000 columns\\)"):
            self.spit(SECONDS=3., PATTERN='stock', PERIOD_US=254)
        # An 8-word pulse lengthens the column to 192 us.
        with self.assertRaisesRegex(RuntimeError, "PERIOD_US=211 is too short for a pulse of 8 words: the column takes "
                                                  "192 us on the bus, so the period must be at least 212 us"):
            self.spit(SECONDS=1., PATTERN='stock', PULSE=8, PERIOD_US=211)
        # Any pattern takes a period; the board's 6 words need 192 us.
        _, _, sent = self.spit(SECONDS=1., PATTERN='all', PERIOD_US=400)
        self.assertEqual(sent, [('run', 2500, 0, 0, 400, 0, 0, 0)])
        with self.assertRaisesRegex(RuntimeError, "at least 192 us"):
            self.spit(SECONDS=1., PATTERN='all', PERIOD_US=191)

    def test_warm_up_right_before_a_spit(self):
        board, replies, sent = self.spit(SECONDS=3., PULSE=8, PATTERN='all', WARM=5.)
        # A warm-up column every 192 us (the column plus 20 us), 2 words (0.70 us) by default.
        self.assertEqual(sent, [('run', 3000, 0, 0, 1000, 26042, 2, 192)])
        self.assertEqual(replies[0], "PolyDye warm-up 5.0 s: 26042 all-nozzle columns every 192 us, 2-word pulses of "
                                     "0.70 us (24 % energy each, too short to boil ink); per nozzle 18229 us of "
                                     "heating in all, the energy of 6281 reference pulses (heater duty 0.36 %)")
        self.assertIn("PolyDye spit test: 3000 all-nozzle columns", replies[1])
        self.assertEqual((board.boost, board.template, board.pulse), (False, 'color_b_late', 6))

    def test_warm_up_pulse_and_period(self):
        # This board firmware takes warm-up pulses of up to 0.75 us; newer takes up to 2.9 us.
        with self.assertRaisesRegex(RuntimeError, "WARM_PULSE=3 \\(1.05 us\\) needs newer PolyDye board firmware"):
            self.spit(SECONDS=1., PATTERN='all', WARM=2., WARM_PULSE=3)
        with self.assertRaisesRegex(RuntimeError, "WARM_PULSE=12 is 4.20 us at the stock clock; a warm-up pulse is at "
                                                  "most 0.75 us"):
            self.spit(SECONDS=1., PATTERN='all', WARM=2., WARM_PULSE=12)
        # Longer than its 10 s too; newer firmware takes 30 s.
        with self.assertRaisesRegex(RuntimeError, "a 20 s warm-up needs newer PolyDye board firmware, which warms up "
                                                  "for up to 30 s.*at most 10 s"):
            self.spit(SECONDS=1., PATTERN='all', WARM=20.)
        with self.assertRaisesRegex(RuntimeError, "warm-up period must be at least 192 us"):
            self.spit(SECONDS=1., PATTERN='all', WARM=1., WARM_PERIOD_US=150)
        _, _, sent = self.spit(SECONDS=1., PATTERN='all', WARM=1., WARM_PULSE=1, WARM_PERIOD_US=500)
        self.assertEqual(sent, [('run', 1000, 0, 0, 1000, 2000, 1, 500)])

    def test_warm_up_on_its_own(self):
        board, replies, sent = self.spit(command='cmd_POLYDYE_WARM', SECONDS=5.)
        self.assertEqual(sent, [('run', 0, 0, 0, 1000, 26042, 2, 192)])
        self.assertIn("warm-up 5.0 s: 26042 all-nozzle columns every 192 us", replies[0])
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
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=color_b_late PULSE=6", board.sent)
        self.assertEqual(sent, [('random', 255, 2, 7, 14, 85, 1), ('run', 5906, 4, 0, 508, 0, 0, 0)])
        self.assertIn("5906 random: 1 heater on every fire pulse of positions 14-85 of cyan/magenta/yellow, each "
                      "column twice columns every 508 us", replies[0])
        _, replies, sent = self.spit(SECONDS=1., DENSITY=50., COLORS='M', FIRST=20, LAST=60, REPEAT=1)
        self.assertEqual(sent, [('random', 512, 1, 2, 20, 60, 0), ('run', 1969, 4, 0, 508, 0, 0, 0)])
        self.assertIn("random: 50.0 % of positions 20-60 of magenta, each column once", replies[0])
        # With the warm-up and a longer pulse too, in the same board run.
        _, _, sent = self.spit(SECONDS=1., PULSE=8, REPEAT=3, WARM=1., PERIOD_US=508)
        self.assertEqual(sent[0], ('random', 255, 3, 7, 14, 85, 0))
        self.assertEqual(sent[1][:5], ('run', 1969, 4, 0, 508))
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
        # Brett's recipe: every nozzle at 2.80 us (8 words) every 508 us, then the stock pattern straight after, in the
        # same run.
        board, replies, sent = self.spit(pd, board, SECONDS=3., PATTERN='stock', PERIOD_US=508, WARM=26.,
                                         WARM_PULSE=8, WARM_PERIOD_US=508)
        # 8 words are 2.80 us: every 508 us a heater duty of 0.55 %, within 0.58 %.
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
        self.assertEqual((board.boost, board.template, board.pulse), (False, 'color_b_late', 6))

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
        # Older board firmware doesn't check it, and may report another clock (at its own rate).
        self.assertEqual(polydye.measured_clock_text(2851000, 2857142, 'stock'),
                         "bus clock measured 2.851 MHz (stock clock 2.857 MHz, -0.2 %)")
        self.assertEqual(polydye.bus_clock_of({'stock': 0, 'nominal_hz': 3333333}), ('ours', 3333333))

    def test_fire_test_prints_the_random_pattern(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:7", X=80., Y=60., SPACING=20., COLUMNS=601,
                                           UPLOAD=1, LAYER=0, PASS=0, DENSITY=25., PER_PULSE=0))
        # Whole repeats of each random column: 300 random columns, each twice.
        self.assertIn("POLYDYE_UPLOAD_TEST_PASS LAYER=0 PASS=0 COLUMNS=300 PATTERN=stock DENSITY=25.0 REPEAT=2 "
                      "COLORS=cmy FIRST=14 LAST=85 PER_PULSE=0", board.sent)
        self.assertEqual((board.boost, board.template, board.pulse), (False, 'color_b_late', 6))
        with self.assertRaisesRegex(RuntimeError, "go with PATTERN=stock"):
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:6", PATTERN='all', COLORS='c', **self.FIRE))

    def test_fire_test_warms_the_head_before_each_stripe(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_run_cmd = object()
        self.newer_warm_up(pd)
        pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:6,color_b_late:8", X=130., Y=100.,
                                           SPACING=20., COLUMNS=600, UPLOAD=1, LAYER=0, PASS=0, PATTERN='stock',
                                           WARM=10., WARM_PULSE=8, WARM_PERIOD_US=508))
        steps = [s for s in board.sent if isinstance(s, str)
                 and s.startswith(("POLYDYE_UPLOAD", "POLYDYE_WARM", "POLYDYE_ENABLE", "POLYDYE_PASS"))]
        # After the stripe's settings and its upload, live ink on, then the pass with the warm-up asked for: it warms
        # off the print just before its run-up (POLYDYE_PASS WARM), so only a short travel comes in between.
        self.assertEqual([s.split()[0] for s in steps],
                         ["POLYDYE_UPLOAD_TEST_PASS", "POLYDYE_ENABLE", "POLYDYE_PASS", "POLYDYE_ENABLE",
                          "POLYDYE_PASS"])
        self.assertTrue(steps[2].endswith(" DIR=1 WARM=10 WARM_PULSE=8 WARM_PERIOD_US=508"))
        # WARM=0: each pass told not to warm; no WARM: the passes decide (the automatic warm-up).
        for warm, tail in ((0., " DIR=1 WARM=0"), (None, " DIR=1")):
            pd, board = self.make(fail_on="NOTHING")
            pd.spit_run_cmd = object()
            self.newer_warm_up(pd)
            kw = dict(self.FIRE) if warm is None else dict(self.FIRE, WARM=warm)
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:6", **kw))
            passes = [s for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_PASS")]
            self.assertTrue(len(passes) == 1 and passes[0].endswith(tail), passes)
        # A warm-up pulse over the limit is refused before anything moves: 9 words is 3.15 us.
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_run_cmd = object()
        self.newer_warm_up(pd)
        with self.assertRaisesRegex(RuntimeError, "POLYDYE_FIRE_TEST: WARM_PULSE=9 is 3.15 us at the stock clock"):
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(WARM=10., WARM_PULSE=9, WARM_PERIOD_US=508, **self.FIRE))
        self.assertEqual(board.sent, [])
        # Older board firmware: refused before anything moves.
        pd, board = self.make(fail_on="NOTHING")
        with self.assertRaisesRegex(RuntimeError, "WARM needs newer PolyDye board firmware"):
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:6", WARM=5., **self.FIRE))
        self.assertEqual(board.sent, [])

    def test_spit_defaults_to_the_stock_firmware(self):
        # No PATTERN: the stock pattern with the stock firmware's template, pulse and column period.
        board, replies, sent = self.spit(SECONDS=1.)
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=color_b_late PULSE=6", board.sent)
        self.assertEqual(sent, [('run', 1969, 3, 0, 508, 0, 0, 0)])
        # Board firmware without the stock pattern: every nozzle at 1 kHz, as before.
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_patterns = ('all',)
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
        self.assertEqual((board.boost, board.live, board.template, board.pulse),
                         (True, False, 'color_b_late', 6))
        pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(TEMPLATE='black_a'))
        self.assertEqual((board.boost, board.template), (True, 'black_a'))
        # POLYDYE_DISABLE still turns it all off; older board firmware wants it off for a settings change.
        pd.cmd_POLYDYE_DISABLE(self.gcmd())
        self.assertEqual((board.boost, board.live), (False, False))
        pd.boost_idle_s, board.boost = None, True
        with self.assertRaisesRegex(RuntimeError, "turn PolyDye off"):
            pd.cmd_POLYDYE_CARTRIDGE(self.gcmd(TEMPLATE='black_b'))

    def test_fire_test_defaults_to_the_stock_firmware(self):
        pd, board = self.make(fail_on="NOTHING")
        # The stock pattern is for colour templates: a black stripe is refused before anything moves or prints.
        for stripes in ("color_b_late:6,black_b:6", "black_a:6"):
            with self.assertRaisesRegex(RuntimeError, "PATTERN=stock is for a colour cartridge template, not black"):
                pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES=stripes, **self.FIRE))
        self.assertEqual(board.sent, [])
        pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(**self.FIRE))
        self.assertIn("POLYDYE_CARTRIDGE TEMPLATE=color_b_late PULSE=6", board.sent)
        self.assertIn("POLYDYE_UPLOAD_TEST_PASS LAYER=0 PASS=0 COLUMNS=300 PATTERN=stock DENSITY=24.90234375 REPEAT=2 "
                      "COLORS=cmy FIRST=14 LAST=85 PER_PULSE=0", board.sent)
        self.assertEqual(sum(1 for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_PASS")), 1)

    def test_column_times_match_the_board(self):
        # printcart_column_us() at the stock clock: 172 us with a 6-word pulse (the board firmware's host tests), and
        # 2 words longer packets with 8.
        self.assertEqual(polydye.column_us(6), 172)
        self.assertEqual(polydye.column_us(8), 192)
        self.assertEqual(polydye.min_spit_period_us(6), 192)

    def test_stock_spit_and_period_need_newer_board_firmware(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_patterns = ('all',)  # board firmware without the stock pattern
        with self.assertRaisesRegex(RuntimeError, "PATTERN=stock needs newer PolyDye board firmware.*Update to "
                                                  "bundled firmware\\)$"):
            self.spit(pd, board, PATTERN='stock')
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append('spit'))
        with self.assertRaisesRegex(RuntimeError, "PERIOD_US needs newer PolyDye board firmware"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1., PATTERN='all', PERIOD_US=508, CONFIRM=1))
        self.assertEqual(board.sent, [])
        with self.assertRaisesRegex(RuntimeError, "PATTERN must be all or stock"):
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:6", PATTERN='dots', **self.FIRE))

    NOT_READY = "polydye: cartridge driver fault: a column wasn't ready at its time (columns prepared too slowly)"

    def with_run_report(self, fault='none', columns=6640, warm_left=0, underruns=0, overruns=0):
        """A board with polydye_query_run that reports this run (10 MHz clock ticks)."""
        pd, board = self.make(fail_on="NOTHING")
        pd.mcu = types.SimpleNamespace(get_constant_float=lambda name: 10000000., estimated_print_time=lambda e: 0.)
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
        pd, board = self.with_run_report(fault=self.NOT_READY, columns=17, warm_left=26025, underruns=1)
        shut = []
        pd.printer.is_shutdown = lambda: bool(shut)
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: None)
        pd.spit_run_cmd = types.SimpleNamespace(send=lambda args: shut.append(1))
        gcmd = self.gcmd(SECONDS=3., PULSE=8, PATTERN='all', WARM=5., CONFIRM=1)
        with self.assertRaises(RuntimeError) as e:
            pd.cmd_POLYDYE_SPIT(gcmd)
        self.assertEqual(str(e.exception),
                         "POLYDYE_SPIT: Klipper shut down during the warm-up: cartridge driver fault: a column wasn't "
                         "ready at its time (columns prepared too slowly), after 17 columns sent (26025 warm-up "
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


class AutoWarm(unittest.TestCase):
    """The warm-up before a live pass: when the head is cold (or asked for), off the print, then the pass."""

    def make(self):
        pd, board = TestCleanup.make(TestCleanup(), fail_on="NOTHING")
        pd.warm_max_words, pd.warm_max_us, pd.warm_max_s, pd.warm_duty_ppm = 12, 2.9, 30., 5800
        pd.offset_x, pd.offset_y, pd.pass_axis, pd.warm_clearance = 58.6, -8.7, 'y', 10.
        runs = []
        pd.spit_run_cmd = types.SimpleNamespace(send=runs.append)
        here = types.SimpleNamespace(x=10., y=20., z=0.3, e=0.)
        self.travel = []
        gcode_move = types.SimpleNamespace(
            get_status=lambda eventtime: {'gcode_position': here, 'position': here},
            last_position=[10., 20., 0.3, 0.], move_with_transform=lambda pos, speed: self.travel.append(pos[:2]),
            reset_last_position=lambda: None)
        board.queued_until = 0.  # print time the moves already queued end at
        self.z_max = 250.
        limits = lambda: {'axis_minimum': types.SimpleNamespace(x=0., y=-10., z=0.),
                          'axis_maximum': types.SimpleNamespace(x=235., y=220., z=self.z_max)}
        self.objects = {'toolhead': types.SimpleNamespace(
                            wait_moves=lambda: None, get_last_move_time=lambda: board.queued_until,
                            get_status=lambda eventtime: limits()),
                        'gcode_move': gcode_move,
                        'gcode_macro POLYDYE_WIPE': types.SimpleNamespace(variables={'x': 245, 'z_hop': 1})}
        lookup = pd.printer.lookup_object
        pd.printer.lookup_object = lambda name, default=None: (self.objects[name] if name in self.objects
                                                               else lookup(name, default))
        pd.travel_speed = 150.
        # A reactor whose clock moves on with each pause, so timeouts happen.
        clock = [0.]
        reactor = types.SimpleNamespace(monotonic=lambda: clock[0],
                                        pause=lambda until: clock.__setitem__(0, max(clock[0], until)))
        pd.printer.get_reactor = lambda: reactor
        self.cancels = []
        pd.cancel_cmd = types.SimpleNamespace(send=self.cancels.append)
        self.said = []
        # The G-code a sponge or bed-spot warm-up runs: to the spot and back.
        return pd, board, runs, board.sent

    def gcmd(self):
        return types.SimpleNamespace(respond_info=self.said.append, error=RuntimeError)

    def warm(self, pd, **kw):
        # A pass along +Y whose run-up starts at toolhead (100, 50).
        return pd._auto_warm(self.gcmd(), "POLYDYE_PASS", [100., 50.], 1, **kw)

    def test_head_cold(self):
        self.assertTrue(polydye.head_cold(None, 5., 60.))
        self.assertFalse(polydye.head_cold(100., 160., 60.))
        self.assertTrue(polydye.head_cold(100., 160.5, 60.))

    def test_a_cold_head_is_warmed_just_before_a_live_pass(self):
        pd, board, runs, moves = self.make()
        # A dry run fires nothing: no warm-up, and it doesn't count as firing.
        self.assertIs(self.warm(pd), False)
        self.assertEqual((runs, pd.last_fire), ([], None))
        board.live = True
        # First live pass since Klipper started: 10 s of every nozzle at 1 word (0.35 us) every 254 us, no spit
        # columns, live ink left on; 10 mm behind the run-up start along the pass axis, off the print, and the pass
        # travels on from there.
        self.assertIs(self.warm(pd), True)
        self.assertEqual(runs, [[0, 0, polydye.SPIT_PATTERNS['all'], 0, polydye.SPIT_PERIOD_US, 39370, 1, 254]])
        self.assertEqual((self.travel, moves), ([[100., 40.]], []))
        self.assertIn("the head hasn't fired since Klipper started, so a head warm-up 10.0 s", self.said[0])
        self.assertIn("10.0 mm before the pass starts", self.said[0])
        self.assertEqual((pd.last_fire, board.live), (100., True))
        # Warm now: the next pass goes straight on; a minute later it's warmed again.
        board.print_time = 159.
        self.warm(pd)
        self.assertEqual(len(runs), 1)
        board.print_time = 161.
        self.warm(pd)
        self.assertEqual(len(runs), 2)
        self.assertIn("the head won't have fired for 61 s when the pass starts", self.said[-1])
        # Idle enough only by the time the moves already queued are done: the pass starts then, so it warms.
        pd.last_fire, board.print_time, board.queued_until = 100., 159., 161.
        self.warm(pd)
        self.assertEqual(len(runs), 3)

    def test_the_warm_spot_stays_within_the_axis(self):
        pd, board, runs, moves = self.make()
        board.live = True
        pd._auto_warm(self.gcmd(), "POLYDYE_PASS", [100., -5.], 1)
        pd.last_fire = None
        pd._auto_warm(self.gcmd(), "POLYDYE_PASS", [100., 215.], -1)
        self.assertEqual(self.travel, [[100., -10.], [100., 220.]])

    def test_a_warm_up_asked_for(self):
        pd, board, runs, moves = self.make()
        board.live, pd.last_fire, board.print_time = True, 100., 101.  # warm: the automatic one wouldn't run
        self.warm(pd, warm=2., words=8, period=508)
        self.assertEqual(runs, [[0, 0, 0, 0, polydye.SPIT_PERIOD_US, 3937, 8, 508]])
        self.assertIn("a 2 s head warm-up was asked for", self.said[0])
        # WARM=0: none, even on a cold head; it still counts as firing.
        pd.last_fire = None
        self.assertIs(self.warm(pd, warm=0.), True)
        self.assertEqual(len(runs), 1)
        board.live = False
        with self.assertRaisesRegex(RuntimeError, "WARM needs live ink on"):
            self.warm(pd, warm=2.)

    def test_a_failed_warm_up_doesnt_cross_the_print_while_firing(self):
        pd, board, runs, moves = self.make()
        pd.warm_position = 'sponge'
        board.live = True
        status = board.status
        board.running = False
        pd._fresh_status = pd._query_status = lambda: dict(status(), running=board.running)
        # The board never reports the warm-up done, and cancelling doesn't stop it.
        pd.spit_run_cmd = types.SimpleNamespace(send=lambda args: (runs.append(args), setattr(board, 'running', True)))
        with self.assertRaisesRegex(RuntimeError, "didn't finish the warm-up"):
            self.warm(pd)
        # Cancelled, still running: the cartridge stays over the sponge, with the G-code state put back.
        self.assertEqual(len(self.cancels), 1)
        self.assertEqual(moves[-1], "RESTORE_GCODE_STATE NAME=polydye_warm")
        self.assertFalse(any(m.startswith("G1 X10.000") for m in moves))
        self.assertIn("stays over the warm-up spot", self.said[-1])
        # Cancelling stops it: then back across and down.
        pd.cancel_cmd = types.SimpleNamespace(send=lambda args: setattr(board, 'running', False))
        board.running, pd.last_fire = False, None
        with self.assertRaisesRegex(RuntimeError, "didn't finish the warm-up"):
            self.warm(pd)
        self.assertEqual(moves[-2:], ["G1 X10.000 Y20.000 F9000\nG1 Z0.300 F900",
                                      "RESTORE_GCODE_STATE NAME=polydye_warm"])

    def test_a_warm_up_the_board_cant_be_seen_to_finish_stops_the_pass(self):
        pd, board, runs, moves = self.make()
        board.live = True
        status, calls = board.status, [0]

        def fresh():
            calls[0] += 1
            if calls[0] == 4:  # the check after the warm-up: the board doesn't answer
                raise RuntimeError("timeout")
            return status()
        pd._fresh_status = pd._query_status = fresh
        with self.assertRaisesRegex(RuntimeError, "may still be firing after the warm-up, so the cartridge stays over "
                                                  "the warm-up spot and the pass doesn't run"), \
                mock.patch.object(polydye.logging, "exception"):
            self.warm(pd)
        self.assertEqual((len(runs), pd.last_fire), (1, None))

    def test_the_sponge_and_a_bed_spot(self):
        pd, board, runs, moves = self.make()
        board.live = True
        pd.warm_position = 'sponge'
        self.warm(pd)
        # Up by POLYDYE_WIPE's z_hop, over the wipe sponge, warm, then back across and down.
        self.assertEqual(moves, ["SAVE_GCODE_STATE NAME=polydye_warm", "G90\nG1 Z1.300 F900\nG1 X245.000 F9000",
                                 "G1 X10.000 Y20.000 F9000\nG1 Z0.300 F900", "RESTORE_GCODE_STATE NAME=polydye_warm"])
        self.assertIn("over the wipe sponge (X=245.0)", self.said[0])
        # A bed spot: the toolhead goes to the spot minus the cartridge's offset; the hop stays below Z max.
        pd.warm_position, pd.last_fire, self.z_max = (5., 200.), None, 0.8
        del moves[:]
        self.warm(pd)
        self.assertEqual(moves[1], "G90\nG1 Z0.800 F900\nG1 X-53.600 Y208.700 F9000")
        self.assertIn("over X=5.0 Y=200.0", self.said[-1])
        # A sponge without its macro is refused before anything fires.
        pd.warm_position, pd.last_fire = 'sponge', None
        del self.objects['gcode_macro POLYDYE_WIPE']
        with self.assertRaisesRegex(RuntimeError, "no POLYDYE_WIPE macro to say where it is"):
            self.warm(pd)
        self.assertEqual(len(runs), 2)

    def test_a_refused_move_to_the_spot_puts_the_gcode_state_back(self):
        pd, board, runs, moves = self.make()
        board.live, pd.warm_position = True, 'sponge'
        lookup = pd.printer.lookup_object
        gcode = types.SimpleNamespace(run_script_from_command=lambda script: (
            moves.append(script), script.startswith("G90") and (_ for _ in ()).throw(RuntimeError("Move out of range"))))
        pd.printer.lookup_object = lambda name, default=None: gcode if name == 'gcode' else lookup(name, default)
        with self.assertRaisesRegex(RuntimeError, "Move out of range"):
            self.warm(pd)
        self.assertEqual((moves[0], moves[-1], runs), ("SAVE_GCODE_STATE NAME=polydye_warm",
                                                       "RESTORE_GCODE_STATE NAME=polydye_warm", []))

    def test_off_switches(self):
        pd, board, runs, moves = self.make()
        board.live = True
        # With the automatic warm-up off a live pass still says it is live, so it counts as firing (last_fire).
        pd.warm_seconds = 0.
        self.assertIs(self.warm(pd), True)
        pd.warm_seconds, pd.warm_duty_ppm = 10., None  # older board firmware: its warm-up can't take the limits
        self.assertIs(self.warm(pd), True)
        self.assertEqual(runs, [])

    def test_config(self):
        self.assertIsNone(polydye.auto_warm_error(10., 8, 508))
        self.assertIsNone(polydye.auto_warm_error(polydye.AUTO_WARM_S, polydye.AUTO_WARM_WORDS,
                                                  polydye.AUTO_WARM_PERIOD_US))
        self.assertIsNone(polydye.auto_warm_error(0., 8, 508))
        self.assertIn("heater duty: use a period of at least 483 us", polydye.auto_warm_error(10., 8, 400))
        self.assertIn("shorter than the warm-up column (192 us", polydye.auto_warm_error(10., 2, 150))
        self.assertIn("warm_pulse must be 1-8 words", polydye.auto_warm_error(10., 9, 508))
        self.assertIn("warm_seconds must be 0 (off) to 30", polydye.auto_warm_error(31., 8, 508))


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


STOCK_ONLY = {'POLYDYE_STOCK_ONLY': 1, 'POLYDYE_SOFT_RESET': 1, 'POLYDYE_SPIT_PATTERNS': 5, 'POLYDYE_WARM_MAX_WORDS': 12,
              'POLYDYE_WARM_MAX_NS': 2900, 'POLYDYE_WARM_MAX_DUTY_PPM': 5800, 'POLYDYE_WARM_MAX_MS': 30000}


class BuildConfig(unittest.TestCase):
    """What the board offers, and the settings sent on every connect."""

    def build(self, constants, pulse_words=None, template=None, missing=()):
        """_build_config on board firmware with these constants and every command but those starting with `missing`;
        returns (pd, the polydye_set_* commands sent on connect) or (pd, the config error)."""
        class ConfigError(Exception):
            pass
        pd = object.__new__(polydye.PolyDye)
        pd.oid, pd.cartridge_template, pd.pulse_words = 0, template, pulse_words
        pd.spit_patterns = pd.templates = ()
        pd.spit_run_cmd = pd.spit_random_cmd = None
        pd.warm_max_words, pd.warm_max_us, pd.warm_max_s = polydye.WARM_OLD_WORDS, polydye.WARM_OLD_US, polydye.WARM_OLD_S
        pd.warm_duty_ppm = None
        pd.set_clock_cmd = pd.query_clock_cmd = None
        pd.bus_hz_check = None
        pd.boost_idle_s = None
        pd.warm_seconds, pd.warm_pulse, pd.warm_period_us, pd.warm_idle_time, pd.warm_position = 10., 1, 254, 60., 'pass'
        pd.warm_clearance = 10.
        pd.last_fire = None
        pd.printer = types.SimpleNamespace(config_error=ConfigError)
        sent = []
        pd.mcu = types.SimpleNamespace(
            add_config_cmd=lambda cmd, is_init=False: sent.append(cmd), alloc_command_queue=lambda: None,
            lookup_command=lambda *a, **kw: object(), lookup_query_command=lambda *a, **kw: object(),
            try_lookup_command=lambda fmt, *a, **kw: None if fmt.startswith(tuple(missing)) else object(),
            get_constants=lambda: dict(constants), estimated_print_time=lambda eventtime: eventtime)
        try:
            with mock.patch.object(polydye.logging, "warning") as warn:
                pd._build_config()
            pd.test_warnings = [c[0][0] for c in warn.call_args_list]
        except ConfigError as e:
            return pd, str(e)
        return pd, [cmd for cmd in sent if cmd.startswith("polydye_set_")]

    def status(self, pd):
        pd.last_status, pd.last_pass, pd.pass_axis, pd.offset_x, pd.offset_y, pd.direction_trim = {}, None, 'y', 0, 0, 0
        return pd.get_status(0.)

    def test_connect_sets_the_stock_clock(self):
        # Stock-only board firmware takes it as a no-op; the template and pulse go only if polydye.cfg sets them.
        pd, sent = self.build(STOCK_ONLY)
        self.assertEqual((sent, pd.test_warnings), (["polydye_set_clock oid=0 stock=1"], []))
        pd, sent = self.build(STOCK_ONLY, 8, 'black_b')
        self.assertEqual(sent, ["polydye_set_template oid=0 template=3", "polydye_set_pulse oid=0 words=8",
                                "polydye_set_clock oid=0 stock=1"])
        # Older board firmware may still be at ours with 9 words (3.15 us at the stock clock) or at half with 4 words
        # (6 words there would be 3.6 us): ours first, where any valid pulse is within the limit, then a pulse within
        # it at the stock clock, then the stock clock. It still works, with a warning to update it.
        pd, sent = self.build({'POLYDYE_SOFT_RESET': 1})
        self.assertEqual(sent, ["polydye_set_clock oid=0 stock=0", "polydye_set_pulse oid=0 words=6",
                                "polydye_set_clock oid=0 stock=1"])
        for clock, words in (('ours', 9), ('half', 4), ('stock', 8)):
            for step in sent:
                if step.startswith("polydye_set_clock"):
                    clock = polydye.CLOCK_NAMES[int(step.rsplit("=", 1)[1])]
                else:
                    words = int(step.rsplit("=", 1)[1])
                hz = {'ours': polydye.OURS_HZ, 'stock': polydye.BUS_HZ, 'half': polydye.OURS_HZ // 2}[clock]
                self.assertLessEqual(words * 1e6 / hz, polydye.PULSE_MAX_US, (clock, words, step))
        self.assertEqual(len(pd.test_warnings), 1)
        self.assertIn("predates the stock-only cleanup: update it from the PolyDye page (Board firmware: Update to "
                      "bundled firmware)", pd.test_warnings[0])
        pd, sent = self.build({'POLYDYE_SOFT_RESET': 1}, 7, 'color_b_late')
        self.assertEqual(sent, ["polydye_set_template oid=0 template=4", "polydye_set_clock oid=0 stock=0",
                                "polydye_set_pulse oid=0 words=7", "polydye_set_clock oid=0 stock=1"])
        # Board firmware without polydye_set_clock runs ours, without color_b_late: no clock to send.
        pd, sent = self.build({}, 7, missing=("polydye_query_clock",))
        self.assertEqual(sent, ["polydye_set_pulse oid=0 words=7"])
        pd, error = self.build({}, None, 'color_b_late', missing=("polydye_query_clock",))
        self.assertIn("cartridge_template: color_b_late needs newer PolyDye board firmware", error)
        self.assertIn("Update to bundled firmware", error)

    def test_status_lists_what_the_board_offers(self):
        pd, _ = self.build(STOCK_ONLY)
        st = self.status(pd)
        self.assertEqual((st['templates'], st['spit_patterns']), (['color_b_late', 'black_a', 'black_b'],
                                                                  ['all', 'stock']))
        self.assertNotIn('bus_clocks', st)
        self.assertTrue(st['auto_warm']['available'])
        pd, _ = self.build({}, missing=("polydye_query_clock",))
        st = self.status(pd)
        self.assertEqual((st['templates'], st['spit_patterns']), (['black_a', 'black_b'], ['all']))
        self.assertFalse(st['auto_warm']['available'])  # the page says it needs newer firmware

    def test_board_firmware_that_leaves_usb_on_reset_is_flagged(self):
        pd, _ = self.build(STOCK_ONLY)
        self.assertTrue(pd.board_soft_reset)
        self.assertEqual(pd.test_warnings, [])
        pd, _ = self.build({})
        self.assertFalse(pd.board_soft_reset)
        self.assertIn("leaves USB and comes back whenever Klipper resets it", pd.test_warnings[0])
        self.assertIs(self.status(pd)['board_soft_reset'], False)  # the page shows its warning

    def test_warm_up_and_clock_check_follow_the_board(self):
        pd, _ = self.build({})
        self.assertEqual((pd.warm_max_words, pd.warm_max_us, pd.warm_max_s, pd.warm_duty_ppm, pd.bus_hz_check),
                         (2, 0.75, 10., None, None))
        pd, _ = self.build({'POLYDYE_WARM_MAX_WORDS': 12, 'POLYDYE_WARM_MAX_NS': 2900, 'POLYDYE_WARM_MAX_DUTY_PPM': 5800,
                            'POLYDYE_WARM_MAX_MS': 30000, 'POLYDYE_BUS_HZ_FAST_PCT': 5, 'POLYDYE_BUS_HZ_SLOW_PCT': 8})
        self.assertEqual((pd.warm_max_words, pd.warm_max_us, pd.warm_max_s, pd.warm_duty_ppm, pd.bus_hz_check),
                         (12, 2.9, 30., 5800, (5, 8)))
        self.assertIsNotNone(pd.spit_random_cmd)
        st = self.status(pd)
        self.assertEqual((st['spit_random'], st['warm_max_us'], st['warm_max_s'], st['warm_duty_ppm']),
                         (True, 2.9, 30., 5800))


class ConfigOptions(unittest.TestCase):
    """polydye.cfg's cartridge options: what the stock-only cleanup removed is refused, saying what to do."""

    def load(self, **options):
        """A PolyDye from a polydye.cfg with these options, or the config error."""
        class ConfigError(Exception):
            pass
        values = dict(pass_axis='y', offset_x='58.6', offset_y='-8.7', **options)

        class Config:
            error = ConfigError

            def get_printer(self):
                return types.SimpleNamespace(register_event_handler=lambda *a: None, lookup_object=lambda name: gcode)

            def getsection(self, name):
                return types.SimpleNamespace(get=lambda option, default=None: default)

            def get(self, option, default=None):
                return values.get(option, default)

            def getint(self, option, default=None, **kw):
                return int(values[option]) if option in values else default

            def getfloat(self, option, default=None, **kw):
                return float(values[option]) if option in values else default

            def getchoice(self, option, choices):
                return choices[values[option]]
        gcode = types.SimpleNamespace(register_command=lambda *a, **kw: None)
        mcu = types.SimpleNamespace(create_oid=lambda: 0, register_config_callback=lambda cb: None)
        with mock.patch.object(polydye.os, "sync", create=True), \
                mock.patch.object(polydye.mcu_mod, "get_printer_mcu", create=True, return_value=mcu):
            try:
                return polydye.PolyDye(Config())
            except ConfigError as e:
                return str(e)

    def test_removed_settings_are_refused(self):
        self.assertEqual(self.load(bus_clock='ours'), "[polydye] bus_clock: ours was removed: the board runs only the "
                                                      "stock clock (2.857 MHz); remove bus_clock from polydye.cfg")
        self.assertIn("bus_clock: half was removed", self.load(bus_clock='Half'))
        self.assertEqual(self.load(cartridge_template='DeskJet'), "[polydye] cartridge_template: deskjet was removed "
                                                                  "(stock baseline): use color_b_late, black_a or "
                                                                  "black_b")
        self.assertEqual(self.load(pulse_words='9'), "[polydye] pulse_words: fire pulse 9 words is 3.15 us at the "
                                                     "stock clock, over the 2.9 us limit; use 6-8 words (2.10-2.80 us)")
        self.assertIn("less than Sprite_tm's 1.8 us", self.load(pulse_words='4'))
        # bus_clock: stock is still taken, as a no-op.
        pd = self.load(bus_clock='Stock', cartridge_template='black_b', pulse_words='8')
        self.assertEqual((pd.cartridge_template, pd.pulse_words), ('black_b', 8))

    def test_auto_warm_options(self):
        pd = self.load()
        self.assertEqual((pd.warm_seconds, pd.warm_pulse, pd.warm_period_us, pd.warm_idle_time, pd.warm_position,
                          pd.warm_clearance), (10., 1, 254, 60., 'pass', 10.))
        pd = self.load(warm_seconds='0', warm_position=' 5, 200.5 ')
        self.assertEqual((pd.warm_seconds, pd.warm_position), (0., (5., 200.5)))
        self.assertEqual(self.load(warm_position='Sponge').warm_position, 'sponge')
        self.assertIn("warm_position must be pass (the default), sponge, or X, Y", self.load(warm_position='here'))
        self.assertIn("heater duty", self.load(warm_pulse='8', warm_period_us='400'))
        self.assertIn("warm_position must be pass (the default), sponge, or X, Y", self.load(warm_position='5'))


if __name__ == "__main__":
    unittest.main()
