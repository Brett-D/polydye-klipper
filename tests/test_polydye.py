#!/usr/bin/env python3
"""Unit tests for the pass timing and geometry in klipper/polydye.py (no Klipper needed).

This file is part of polydye-klipper (GPLv3).
"""
import math
import os
import re
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
import zlib
from unittest import mock

# polydye.py imports klippy's "mcu" module at load time; the pure functions under test don't use it.
sys.modules.setdefault("mcu", types.ModuleType("mcu"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "klipper"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import polydye  # noqa: E402
import polydye_post  # noqa: E402  (its job writer, without numpy)


def bare_polydye():
    """A PolyDye without its __init__ (no Klipper), with the state every command needs."""
    pd = object.__new__(polydye.PolyDye)
    pd.job, pd.data_lock, pd.nozzle_skew = None, threading.Lock(), 0.
    pd.prewarm_cmd = pd.query_prewarm_cmd = pd.prewarm_sent = pd.prewarm_state = pd.boost_ready = None
    pd.prewarm_seconds, pd.prewarm_gen, pd.prime_seconds = polydye.AUTO_PREWARM_S, 0, polydye.PRIME_S
    return pd


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
        pd = bare_polydye()
        reactor = types.SimpleNamespace(NEVER=9e99)
        state = {'shutdown': False}
        pd.printer = types.SimpleNamespace(is_shutdown=lambda: state['shutdown'], get_reactor=lambda: reactor,
                                           lookup_object=lambda name, default=None: default)
        pd.querying = pd.query_failing = pd.print_live = pd.live_off_pending = False
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


class PrintEnd(unittest.TestCase):
    def test_a_cancelled_or_failed_print_turns_live_ink_off(self):
        pd = bare_polydye()
        pd.oid, pd.boost_idle_s, pd.print_live, pd.last_fire, pd.live_off_pending = 0, 1800, False, 50., False
        sent, said = [], []
        pd.cancel_cmd = types.SimpleNamespace(send=lambda args: sent.append('cancel'))
        pd.mode_cmd = types.SimpleNamespace(send=lambda args: sent.append(('mode', args[1], args[2])))
        pd._fresh_status = lambda: {'boost': True}
        pd._check_current_pulse = lambda gcmd, name, st: None
        state = ['standby']
        reactor = types.SimpleNamespace(monotonic=lambda: 0.)
        objects = {'print_stats': types.SimpleNamespace(get_status=lambda eventtime: {'state': state[0]}),
                   'gcode': types.SimpleNamespace(respond_info=said.append)}
        pd.printer = types.SimpleNamespace(lookup_object=lambda name, default=None: objects.get(name, default),
                                           get_reactor=lambda: reactor)
        gcmd = types.SimpleNamespace(get_int=lambda name, default, **kw: 1, respond_info=lambda msg: None)
        off = ['cancel', ('mode', 1, 0)]  # passes dropped, live ink off, the boost kept as the board does

        def run(steps):
            for now, enable, expected in steps:
                state[0] = now
                if enable:
                    pd.cmd_POLYDYE_ENABLE(gcmd)
                del sent[:]
                pd._check_print_end(0.)
                self.assertEqual(sent, expected, now)
        # Live ink outside a print (a fire test) is left alone; a print that turned it on is cleaned up however it
        # ended, even between two polls.
        run((('standby', True, []), ('printing', False, []), ('cancelled', False, []),
             ('printing', True, []), ('paused', False, []), ('cancelled', False, off), ('cancelled', False, []),
             ('printing', True, []), ('error', False, off),
             ('printing', True, []), ('complete', False, off),
             ('standby', False, [])))
        state[0] = 'printing'
        pd.cmd_POLYDYE_ENABLE(gcmd)
        run((('error', False, off),))  # turned on and failed before a poll saw it printing
        self.assertEqual([s.split(", so")[0] for s in said], [
            "PolyDye: the print was cancelled", "PolyDye: the print stopped with an error",
            "PolyDye: the print finished with live ink still on", "PolyDye: the print stopped with an error"])
        self.assertIsNone(pd.last_fire)
        # A print that turned live ink off itself leaves nothing to clean up.
        state[0] = 'printing'
        pd.cmd_POLYDYE_ENABLE(gcmd)
        pd.cmd_POLYDYE_ENABLE(types.SimpleNamespace(get_int=lambda name, default, **kw: 0, respond_info=lambda m: None))
        run((('complete', False, []),))
        # A print that ended with live ink on, and a fire test enabling live ink before the next poll: the print is
        # cleaned up first, then the test's live ink stays on.
        state[0] = 'printing'
        pd.cmd_POLYDYE_ENABLE(gcmd)
        state[0], sent[:] = 'complete', []
        pd.cmd_POLYDYE_ENABLE(gcmd)
        self.assertEqual(sent, off + [('mode', 1, 1)])
        run((('complete', False, []), ('standby', False, [])))
        # A send that fails (the link hiccups) is tried again at the next poll.
        state[0] = 'printing'
        pd.cmd_POLYDYE_ENABLE(gcmd)
        state[0], fail = 'cancelled', [True]

        def mode(args):
            if fail.pop() if fail else False:
                raise RuntimeError("Timeout on wait for 'polydye_status' response")
            sent.append(('mode', args[1], args[2]))
        pd.mode_cmd = types.SimpleNamespace(send=mode)
        with self.assertRaisesRegex(RuntimeError, "Timeout"):
            pd._check_print_end(0.)
        self.assertTrue(pd.print_live)
        run((('cancelled', False, off), ('cancelled', False, [])))
        # A test whose cleanup failed: live ink goes off at the next poll (tried again until it works), or before
        # live ink is enabled again, also by a print.
        pd.mode_cmd = types.SimpleNamespace(send=lambda args: sent.append(('mode', args[1], args[2])))
        del said[:]
        pd.live_off_pending = True
        run((('standby', False, off), ('standby', False, [])))
        self.assertEqual(said, ["PolyDye: live ink is off now (turning it off after the last test failed)"])
        pd.live_off_pending, state[0], sent[:] = True, 'printing', []
        pd.cmd_POLYDYE_ENABLE(gcmd)
        self.assertEqual(sent, off + [('mode', 1, 1)])
        self.assertEqual((pd.live_off_pending, pd.print_live), (False, True))
        run((('complete', False, off),))
        # Older board firmware: the boost goes off too.
        pd.boost_idle_s, state[0] = None, 'printing'
        pd.cmd_POLYDYE_ENABLE(gcmd)
        run((('cancelled', False, ['cancel', ('mode', 0, 0)]),))

    def test_a_failed_test_cleanup_leaves_live_ink_off_pending(self):
        pd, board = TestCleanup.make(TestCleanup(), fail_on="NOTHING")

        def lost(args):
            raise RuntimeError("Lost communication with MCU 'polydye'")
        pd.mode_cmd = types.SimpleNamespace(send=lost)
        with self.assertLogs(level='ERROR'):
            pd._restore_after_test(('color_b_late', 6))
        self.assertTrue(pd.live_off_pending)


class StatusCommand(unittest.TestCase):
    def test_answers_while_the_first_background_query_is_in_flight(self):
        # Just after connecting: the poll's first query is waiting for the board and nothing is cached yet.
        pd = bare_polydye()
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
        pd.registration = (0, 0, 0, 0)
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
        pd = bare_polydye()
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
        pd.warm_position, pd.warm_clearance, pd.warm_prespit = 'pass', 10., 0.
        pd.last_fire, pd.auto_warm_off, pd.print_live, pd.live_off_pending = None, False, False, False
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
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_run_cmd = object()
        self.newer_warm_up(pd)
        pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:6", PRESPIT=0.3, **self.FIRE))
        self.assertTrue([s for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_PASS")][0].endswith(
            " DIR=1 PRESPIT=0.3"))
        # The pre-spit is the stock pattern: a black stripe whose pass would pre-spit (PRESPIT or warm_prespit, after an
        # asked-for or automatic warm-up) is refused before anything moves; with no warm-up or no pre-spit it prints.
        cases = ((dict(WARM=0.5, PRESPIT=0.3), 0., "PRESPIT"), (dict(PRESPIT=0.3), 0., "PRESPIT"),
                 ({}, 0.2, "warm_prespit"), (dict(WARM=0., PRESPIT=0.3), 0., None),
                 (dict(WARM=0.5, PRESPIT=0.), 0.2, None))
        for kw, warm_prespit, refused in cases:
            pd, board = self.make(fail_on="NOTHING")
            pd.spit_run_cmd = object()
            self.newer_warm_up(pd)
            pd.warm_prespit = warm_prespit
            fire = lambda: pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b_late:6,black_b:6", PATTERN='all',
                                                              **dict(self.FIRE, **kw)))
            if refused:
                with self.assertRaisesRegex(RuntimeError, r"the pre-spit \(%s\) is the stock pattern, for a colour "
                                                          "cartridge template, not black_b" % (refused,)):
                    fire()
                self.assertEqual(board.sent, [], kw)
            else:
                fire()
                self.assertEqual(len([s for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_PASS")]), 2)
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
        pd.offset_x, pd.offset_y, pd.pass_axis, pd.warm_clearance, pd.warm_prespit = 58.6, -8.7, 'y', 10., 0.
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
        # WARM alone: polydye.cfg's pulse and period (the gentle recipe); a pulse alone: its fastest period.
        self.warm(pd, warm=1.)
        self.assertEqual(runs[-1], [0, 0, 0, 0, polydye.SPIT_PERIOD_US, 3937, 1, 254])
        self.assertEqual(polydye.pass_warm_settings(None, None, 1, 254), (1, 254))
        self.assertEqual(polydye.pass_warm_settings(8, None, 1, 254), (8, None))
        self.assertEqual(polydye.pass_warm_settings(None, 400, 1, 254), (1, 400))
        del runs[1:]
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

    def test_a_refused_move_back_still_puts_the_gcode_state_back(self):
        pd, board, runs, moves = self.make()
        board.live, pd.warm_position = True, 'sponge'
        lookup = pd.printer.lookup_object
        gcode = types.SimpleNamespace(run_script_from_command=lambda script: (
            moves.append(script),
            script.startswith("G1 X10.000") and (_ for _ in ()).throw(RuntimeError("Move out of range"))))
        pd.printer.lookup_object = lambda name, default=None: gcode if name == 'gcode' else lookup(name, default)
        with self.assertRaisesRegex(RuntimeError, "Move out of range"):
            self.warm(pd)
        self.assertEqual(moves[-1], "RESTORE_GCODE_STATE NAME=polydye_warm")

    def test_a_pass_warm_up_longer_than_the_board_takes_is_refused_before_anything_moves(self):
        pd, board, runs, moves = self.make()
        pd.warm_max_s, pd.warm_duty_ppm = 10., None  # older board firmware: warm-ups of at most 10 s
        pd.query_pass_cmd = types.SimpleNamespace(send=lambda args: {'present': 1, 'columns': 600, 'cart': 0})
        with self.assertRaisesRegex(RuntimeError, "POLYDYE_PASS: a warm-up lasts at most 10 s with this board firmware; "
                                                  "newer firmware takes up to 30 s"):
            pd.cmd_POLYDYE_PASS(TestCleanup.gcmd(self, LAYER=0, PASS=0, X=100., Y=40., WARM=20., WARM_PULSE=2,
                                                 WARM_PERIOD_US=508))
        self.assertEqual((runs, moves, self.travel), ([], [], []))

    def test_an_optional_pre_spit_after_the_warm_up(self):
        pd, board, runs, moves = self.make()
        board.live, pd.warm_prespit = True, 0.2
        self.warm(pd)
        # In the same board run: the warm-up, then 0.2 s of the stock pattern every 508 us.
        self.assertEqual(runs, [[0, 394, polydye.SPIT_PATTERNS['stock'], 0, 508, 39370, 1, 254]])
        self.assertIn("then a 0.2 s pre-spit (394 stock-pattern columns every 508 us)", self.said[0])
        # PRESPIT=0 on the pass turns it off; a black template has no stock pattern: refused before anything moves.
        pd.last_fire = None
        self.warm(pd, prespit=0.)
        self.assertEqual(runs[-1][1:3], [0, polydye.SPIT_PATTERNS['all']])
        pd.last_fire, board.template = None, 'black_b'
        with self.assertRaisesRegex(RuntimeError, "pre-spit is the stock pattern, for a colour cartridge template, not "
                                                  "black_b"):
            self.warm(pd)
        self.assertEqual((len(runs), len(self.travel)), (2, 2))
        # Board firmware without the stock pattern: a planned pre-spit is refused before anything moves; with no
        # warm-up (WARM=0, a dry run) there's none, so nothing is refused.
        pd.last_fire, board.template, pd.spit_patterns = None, 'color_b_late', ('all',)
        with self.assertRaisesRegex(RuntimeError, "the pre-spit needs newer PolyDye board firmware"):
            self.warm(pd)
        self.assertEqual((len(runs), len(self.travel)), (2, 2))
        self.assertIs(self.warm(pd, warm=0.), True)
        board.live = False
        self.assertIs(self.warm(pd), False)
        self.assertEqual(len(runs), 2)

    def test_cancelled_passes_dont_count_as_firing(self):
        pd, board, runs, moves = self.make()
        pd.last_fire = 99.
        pd.cmd_POLYDYE_CANCEL(self.gcmd())
        self.assertEqual((self.cancels, pd.last_fire), ([[0]], None))

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


class Prewarm(unittest.TestCase):
    """POLYDYE_PREWARM: a burst scheduled with the moves, and what it means for the next pass's warm-up."""

    make = AutoWarm.make
    warm = AutoWarm.warm

    def gcmd(self, **params):
        def get(name, default=None, **kw):
            return params.get(name, default)
        return types.SimpleNamespace(get=get, get_float=get, get_int=get, respond_info=self.said.append,
                                     error=RuntimeError)

    def board(self, pd, board):
        """The board's burst commands: what was sent, and the last burst's state (clocks at 10 MHz)."""
        self.sent, self.callbacks = [], []
        pd.prewarm_cmd = types.SimpleNamespace(send=lambda args, reqclock=0: self.sent.append((args, reqclock)))
        board.burst = {'state': 0, 'start': 0, 'end': 0, 'columns': 0}
        pd.query_prewarm_cmd = types.SimpleNamespace(send=lambda args: dict(board.burst))
        pd.mcu = types.SimpleNamespace(estimated_print_time=lambda eventtime: board.print_time,
                                       print_time_to_clock=lambda t: int(round(t * 1e7)),
                                       clock_to_print_time=lambda clock: clock / 1e7)
        self.objects['toolhead'].register_lookahead_callback = self.callbacks.append
        pd.warm_idle_time = 5.

    def test_a_burst_goes_with_the_moves_and_turns_live_ink_on(self):
        pd, board, runs, moves = self.make()
        self.board(pd, board)
        self.objects['toolhead'].dwell = lambda t: self.fail("no pause in the moves for the boost")
        pd.cmd_POLYDYE_PREWARM(self.gcmd())
        self.assertEqual((board.boost, board.live), (True, True))
        # Sent once the moves before it are planned, for when they end: 6 s (prewarm_seconds) of 1-word pulses every
        # 254 us.
        self.assertEqual(self.sent, [])
        self.callbacks[0](123.4)
        self.assertEqual(self.sent, [([0, 1234000000, 23622, 254], 1234000000)])
        self.assertEqual(pd.prewarm_sent['seconds'], 23622 * 254 / 1e6)
        pd.cmd_POLYDYE_PREWARM(self.gcmd(SECONDS=3.))
        self.callbacks[1](500.)  # the clock wraps at 32 bits
        self.assertEqual(self.sent[-1], ([0, 5000000000 & 0xffffffff, 11811, 254], 5000000000))
        # The boost was off: the moves went on, but the next pass waits for it to settle (0.15 s after it came on).
        self.assertEqual(pd.boost_ready, 100.15)
        dwells = []
        self.objects['toolhead'].dwell = dwells.append
        board.queued_until = 100.05
        pd._wait_for_boost()
        self.assertEqual([round(d, 6) for d in dwells], [0.1])
        board.queued_until = 101.
        pd._wait_for_boost()
        self.assertEqual(len(dwells), 1)
        with self.assertRaisesRegex(RuntimeError, "SECONDS=20 is too long: a burst is at most 65535 columns, 16.6 s"):
            pd.cmd_POLYDYE_PREWARM(self.gcmd(SECONDS=20.))
        self.assertEqual(len(self.callbacks), 2)
        # Cancelled, or live ink off, before its moves were planned: the burst isn't sent.
        for cancel in (lambda: pd.cmd_POLYDYE_CANCEL(self.gcmd()),
                       lambda: pd.cmd_POLYDYE_ENABLE(self.gcmd(LIVE=0))):
            sent = len(self.sent)
            pd.cmd_POLYDYE_PREWARM(self.gcmd())
            cancel()
            self.callbacks[-1](600.)
            self.assertEqual(len(self.sent), sent)
        pd.cmd_POLYDYE_PREWARM(self.gcmd())
        self.callbacks[-1](700.)
        self.assertEqual(self.sent[-1][0][1], 7000000000 & 0xffffffff)
        pd.prewarm_cmd = None
        with self.assertRaisesRegex(RuntimeError, "POLYDYE_PREWARM: needs newer PolyDye board firmware"):
            pd.cmd_POLYDYE_PREWARM(self.gcmd())

    def test_a_pass_after_a_burst_needs_no_warm_up_of_its_own(self):
        pd, board, runs, moves = self.make()
        self.board(pd, board)
        board.live, board.print_time = True, 100.
        # No burst sent yet: a cold head, warmed at the warm spot.
        self.warm(pd)
        self.assertEqual(len(runs), 1)
        pd.last_fire, pd.prewarm_sent = None, {'start': 190., 'end': 200., 'seconds': 10., 'clock': int(190e7)}
        board.print_time = 200.
        for state, start, end, columns, warms in (
                (1, 190., 200., 0, False),        # queued: the pass cuts it short
                (2, 190., 200., 0, False),        # firing: likewise
                (2, 160., 170., 0, True),         # firing, but due to end 30 s before the pass: cold by then
                (3, 187., 197., 39370, False),    # done 3 s before the pass
                (4, 190., 199., 35000, False),    # made way for it
                (3, 160., 170., 39370, True),     # done 30 s before: cold again
                (4, 199.99, 199.99, 0, True),     # made way before a column
                (5, 195., 195., 0, True)):        # skipped (live ink was off, say)
            board.burst = {'state': state, 'start': int(start * 1e7), 'end': int(end * 1e7), 'columns': columns}
            before, pd.last_fire = len(runs), None
            self.warm(pd)
            self.assertEqual(len(runs) > before, warms, (state, end))
        self.assertIn("the head won't have fired for 30 s when the pass starts", self.said[-3])
        self.assertIn("the head hasn't fired since Klipper started", self.said[-1])
        # The board's 32-bit clocks are read near the burst's own, not now's: a burst that ended 430 s ago (more than
        # half the 32-bit clock's 429.5 s round at 10 MHz) is still long ago.
        pd.last_fire, pd.prewarm_sent = None, {'start': 500., 'end': 510., 'seconds': 10., 'clock': int(500e7)}
        board.burst = {'state': 3, 'start': int(500e7) & 0xffffffff, 'end': int(510e7) & 0xffffffff, 'columns': 39370}
        board.print_time, before = 940., len(runs)
        self.warm(pd)
        self.assertEqual(len(runs), before + 1)
        self.assertIn("the head won't have fired for 430 s when the pass starts", self.said[-1])

    def test_the_burst_in_status(self):
        pd, board, runs, moves = self.make()
        self.board(pd, board)
        pd.warm_period_us = 254
        self.assertEqual(pd._prewarm_text(), "prewarm burst: none since Klipper started")
        self.assertEqual((pd._prewarm_status(), pd._head_idle_s(0.)),
                         ({'available': True, 'state': 'none', 'seconds': None}, None))
        pd.prewarm_sent = {'start': 90., 'end': 100., 'seconds': 10., 'clock': int(90e7)}
        board.print_time = 104.
        board.burst = {'state': 4, 'start': int(90e7), 'end': int(99e7), 'columns': 35433}
        self.assertEqual(pd._prewarm_text(), "prewarm burst: made way after 9.0 s, 5 s ago")
        self.assertEqual((pd._prewarm_status(), pd._head_idle_s(0.)),
                         ({'available': True, 'state': 'made way', 'seconds': 9.0}, 5.0))
        board.burst['state'] = 5
        self.assertIn("prewarm burst: skipped", pd._prewarm_text())
        pd.prewarm_cmd = None
        self.assertEqual(pd._prewarm_text(), "prewarm bursts: need newer board firmware")

    def test_warm_up_columns_keep_to_the_boards_limit(self):
        # The board's rule is columns x period_us <= 30,000,000 in whole us: 30 s at 1016 us is 29527 columns, not
        # 29528 (30.000448 s, refused with a shutdown on 2026-11-25).
        self.assertEqual(polydye.warm_columns(30., 1016, 30.), 29527)
        self.assertEqual(polydye.warm_columns(10., 254, 30.), 39370)
        self.assertEqual(polydye.warm_columns(10., 254, 10.), 39370)
        self.assertEqual(polydye.warm_columns(0.0001, 254, 30.), 1)
        for period in range(254, 3000, 7):
            self.assertLessEqual(polydye.warm_columns(30., period, 30.) * period, 30000000)
        pd, board, runs, moves = self.make()
        self.assertEqual(pd._warm_plan(self.gcmd(), "POLYDYE_WARM", 30., 1, 1016), (1, 1016, 29527))


class Prime(unittest.TestCase):
    """POLYDYE_PRIME: a short stock-pattern spit over the wipe sponge, for an ink layer whose colours have been idle."""

    make = AutoWarm.make

    def gcmd(self, **params):
        def get(name, default=None, **kw):
            return params.get(name, default)
        return types.SimpleNamespace(get=get, get_float=get, respond_info=self.said.append, error=RuntimeError)

    def prime(self, **params):
        pd, board, runs, moves = self.make()
        pd.spit_run_cmd = types.SimpleNamespace(send=lambda args: (runs.append(args), board.sent.append('run')))
        self.dwells, board.queued_until = [], 100.  # the moves queued end now
        self.objects['toolhead'].dwell = lambda t: (self.dwells.append(round(t, 6)), board.sent.append('dwell'))
        return pd, board, runs, moves

    def test_a_prime_spits_over_the_sponge_and_comes_back(self):
        pd, board, runs, moves = self.prime()
        # The warm-up spot is the pass's (warm_position pass), but a prime is always over the sponge: its gap is fixed,
        # and spits from higher up mist.
        pd.cmd_POLYDYE_PRIME(self.gcmd())
        # 0.3 s (prime_seconds) of the stock pattern every 508 us, no warm-up, in one board run.
        self.assertEqual(runs, [[0, 591, polydye.SPIT_PATTERNS['stock'], 0, 508, 0, 0, 0]])
        # The boost was off: live ink on, then the moves to the sponge wait for it to settle; up by POLYDYE_WIPE's
        # z_hop, over the sponge, spit, then back across and down.
        self.assertEqual(moves, [('mode', 1, 1), 'dwell', "SAVE_GCODE_STATE NAME=polydye_warm",
                                 "G90\nG1 Z1.300 F900\nG1 X245.000 F9000", 'run',
                                 "G1 X10.000 Y20.000 F9000\nG1 Z0.300 F900", "RESTORE_GCODE_STATE NAME=polydye_warm"])
        self.assertEqual((self.travel, board.live, self.dwells), ([], True, [0.15]))
        self.assertIn("priming, 0.3 s of the stock pattern (591 columns every 508 us) over the wipe sponge (X=245.0)",
                      self.said[0])
        # It fired: a pass right after it needs no warm-up of its own.
        self.assertEqual(pd.last_fire, 100.)
        board.print_time, pd.warm_idle_time = 102., 5.
        self.assertIs(AutoWarm.warm(self, pd), True)
        self.assertEqual(len(runs), 1)
        # SECONDS sets its length; with live ink already on, nothing waits for the boost.
        pd.cmd_POLYDYE_PRIME(self.gcmd(SECONDS=0.1))
        self.assertEqual((runs[-1][1], self.dwells), (197, [0.15]))

    def test_refused_before_anything_moves_or_fires(self):
        pd, board, runs, moves = self.prime()
        pd.spit_patterns = ('all',)  # board firmware without the stock pattern
        with self.assertRaisesRegex(RuntimeError, "POLYDYE_PRIME: needs newer PolyDye board firmware"):
            pd.cmd_POLYDYE_PRIME(self.gcmd())
        pd.spit_patterns = tuple(polydye.SPIT_PATTERNS)
        board.template = 'black_b'
        with self.assertRaisesRegex(RuntimeError, "stock pattern, for a colour cartridge template, not black_b"):
            pd.cmd_POLYDYE_PRIME(self.gcmd())
        board.template = 'color_b_late'
        del self.objects['gcode_macro POLYDYE_WIPE']
        with self.assertRaisesRegex(RuntimeError, "no POLYDYE_WIPE macro to say where it is"):
            pd.cmd_POLYDYE_PRIME(self.gcmd())
        self.objects['gcode_macro POLYDYE_WIPE'] = types.SimpleNamespace(variables={'x': 245, 'z_hop': 1})
        # Passes still running 5 s after the moves before it are done.
        pd._fresh_status = pd._query_status = lambda: dict(board.status(), running=True)
        with self.assertRaisesRegex(RuntimeError, "still busy with passes, so the nozzles can't be primed"):
            pd.cmd_POLYDYE_PRIME(self.gcmd())
        self.assertEqual((runs, moves, board.live, pd.last_fire), ([], [], False, None))

    def test_a_print_that_ends_while_it_waits_gets_no_prime(self):
        pd, board, runs, moves = self.prime()
        board.print_state = 'printing'

        def wait_moves():  # the print is cancelled meanwhile
            board.print_state = 'cancelled'
        self.objects['toolhead'].wait_moves = wait_moves
        with self.assertRaisesRegex(RuntimeError, "the print ended while it waited for the passes before it"):
            pd.cmd_POLYDYE_PRIME(self.gcmd())
        self.assertEqual((runs, moves, board.live), ([], [], False))
        # Outside a print it primes as usual.
        board.print_state = 'standby'
        pd.cmd_POLYDYE_PRIME(self.gcmd())
        self.assertEqual(len(runs), 1)

    def test_a_prime_the_board_cant_be_seen_to_finish_stays_over_the_sponge(self):
        pd, board, runs, moves = self.prime()
        board.running = False
        pd._fresh_status = pd._query_status = lambda: dict(board.status(), running=board.running)
        pd.spit_run_cmd = types.SimpleNamespace(send=lambda args: (runs.append(args), setattr(board, 'running', True)))
        with self.assertRaisesRegex(RuntimeError, "didn't finish the prime"):
            pd.cmd_POLYDYE_PRIME(self.gcmd())
        self.assertIn("stays over the warm-up spot", self.said[-1])
        self.assertFalse(any(m.startswith("G1 X10.000") for m in moves if isinstance(m, str)))
        self.assertEqual((moves[-1], pd.last_fire), ("RESTORE_GCODE_STATE NAME=polydye_warm", None))


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
        pd = bare_polydye()
        pd.oid, pd.cartridge_template, pd.pulse_words = 0, template, pulse_words
        pd.spit_patterns = pd.templates = ()
        pd.spit_run_cmd = pd.spit_random_cmd = None
        pd.warm_max_words, pd.warm_max_us, pd.warm_max_s = polydye.WARM_OLD_WORDS, polydye.WARM_OLD_US, polydye.WARM_OLD_S
        pd.warm_duty_ppm = None
        pd.set_clock_cmd = pd.query_clock_cmd = None
        pd.bus_hz_check = None
        pd.boost_idle_s = None
        pd.warm_seconds, pd.warm_pulse, pd.warm_period_us, pd.warm_idle_time, pd.warm_position = 10., 1, 254, 60., 'pass'
        pd.warm_clearance, pd.warm_prespit = 10., 0.
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
        pd.registration, pd.nozzle_dpi, pd.cfg_saves = (0, 0, 0, 0), 300., 0
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
                          pd.warm_clearance, pd.warm_prespit), (6., 1, 254, 5., 'sponge', 10., 0.))
        pd = self.load(warm_seconds='0', warm_position=' 5, 200.5 ')
        self.assertEqual((pd.warm_seconds, pd.warm_position), (0., (5., 200.5)))
        self.assertEqual(self.load(warm_position='Pass').warm_position, 'pass')
        self.assertIn("warm_position must be sponge (the default), X, Y", self.load(warm_position='here'))
        self.assertIn("heater duty", self.load(warm_pulse='8', warm_period_us='400'))
        self.assertIn("warm_position must be sponge (the default), X, Y", self.load(warm_position='5'))
        # A burst is at most 65535 columns: 16.6 s at 254 us.
        self.assertIn("prewarm_seconds 17 is too long: a burst is at most 65535 columns, 16.6 s at warm_period_us 254",
                      self.load(prewarm_seconds='17'))
        self.assertEqual(self.load(prewarm_seconds='16.5').prewarm_seconds, 16.5)
        self.assertEqual((pd.prime_seconds, self.load(prime_seconds='0.5').prime_seconds), (0.3, 0.5))



def decode(data, columns):
    """The (column, colour, position) dots of pass data."""
    out = []
    for j in range(columns):
        col = data[j * polydye.NOZDATA_SIZE:(j + 1) * polydye.NOZDATA_SIZE]
        for c in range(3):
            for q in range(14):
                b = col[polydye.COLOR_BYTE_ORDER[c][q] + 14 * c]
                out.extend((j, c, q + 14 * bit) for bit in range(8) if b >> bit & 1)
    return out


def land(built, direction, along=(0, 0, 0), across=(0, 0, 0), reverse=0.):
    """Where a pass's dots land in drawing columns and positions (built: pass_data()'s result), on a cartridge whose
    colours land `along` bed columns (+pass) and `across` positions off magenta's, and whose reverse passes land
    `reverse` columns further along +pass."""
    data, columns, first = built
    return [(first + j * direction + along[c] + (reverse if direction < 0 else 0), c, p + across[c])
            for j, c, p in decode(data, columns)]


def runs(columns):
    """Centres of the runs of consecutive columns."""
    out, run = [], []
    for i in sorted(set(columns)):
        if run and i != run[-1] + 1:
            out.append(sum(run) / float(len(run)))
            run = []
        run.append(i)
    return out + ([sum(run) / float(len(run))] if run else [])


def single_fires(data, columns):
    """Fires of pass data that are on their own: the nozzle fires neither the column before nor after."""
    dots = set(decode(data, columns))
    return [(j, c, p) for j, c, p in dots if (j - 1, c, p) not in dots and (j + 1, c, p) not in dots]


class CalibrationDrawing(unittest.TestCase):
    def test_no_nozzle_fires_one_column_on_its_own(self):
        # A single fire doesn't eject (the P-box walls, 2026-11-25): the calibration prints' numbers, bars, lines and
        # priming blocks fire every nozzle at least two columns running, at any dpi and with the registration.
        for dpi in (300., 600., 847.):
            drawings = [(d, direction) for _, d, direction in polydye.align_drawings()]
            drawings += [(d, 1) for d in polydye.offset_drawings(dpi / 25.4)]
            for d, direction in drawings:
                data, columns, _ = polydye.pass_data(d, direction, (-34, 1, 27, 0))
                self.assertEqual(single_fires(data, columns), [], dpi)

    def test_registration_is_fixed_on_the_bed(self):
        d = polydye.PassDrawing(0, 199)
        d.dot(10, polydye.CYAN, 40)
        d.dot(10, polydye.MAGENTA, 40)
        d.dot(100, polydye.YELLOW, 111)
        for direction in (1, -1):
            built = polydye.pass_data(d, direction, (-34, 1, 27, 1))
            # Cyan 34 columns back along +pass in either direction, one position up; yellow moved off the nozzle row is
            # left out; the pass covers the drawing and the shifted dot.
            self.assertEqual(sorted(land(built, direction)), [(-24, polydye.CYAN, 41), (10, polydye.MAGENTA, 40)])
            self.assertEqual(built[1:], (224, -24) if direction > 0 else (224, 199))

    def test_priming_block_goes_first(self):
        d = polydye.PassDrawing(0, 9)
        d.prime(-polydye.PRIME_COLUMNS)
        data, columns, first = polydye.pass_data(d, 1)
        self.assertEqual((columns, first), (polydye.PRIME_COLUMNS + 10, -polydye.PRIME_COLUMNS))
        self.assertEqual(data[:polydye.NOZDATA_SIZE], polydye.random_column(0, polydye.STOCK_RANDOM))

    def test_text_reads_along_the_pass_or_across_it(self):
        d = polydye.PassDrawing()
        d.text("1", polydye.MAGENTA, 0, 60, 1, 1)
        # FONT['1']'s top row "00100": column 2, position 60 (the top); its bottom row "01110" 6 positions lower.
        self.assertIn((2, polydye.MAGENTA, 60), d.dots)
        self.assertEqual(sorted(i for i, c, p in d.dots if p == 54), [1, 2, 3])
        d = polydye.PassDrawing()
        d.text("1", polydye.MAGENTA, 100, 60, 1, 1, rotated=True)
        self.assertIn((100, polydye.MAGENTA, 58), d.dots)
        self.assertEqual(sorted(p for i, c, p in d.dots if i == 94), [57, 58, 59])

    def test_plaque_grooves(self):
        rects = polydye.plaque_rects(100., 50.)
        inside = lambda x, y: any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in rects)
        # The grooves: along Y up to 9.3 mm above the centre, along X up to 7.35 mm right of it; plaque beyond.
        for x, y in ((100., 45.), (100., 58.), (95., 50.), (107., 50.)):
            self.assertFalse(inside(x, y), (x, y))
        for x, y in ((100., 60.), (108., 50.), (99., 49.), (101., 51.), (90.3, 29.6), (116.3, 66.9)):
            self.assertTrue(inside(x, y), (x, y))
        # The plaque reaches 6.5 mm beyond every drop of ink, the priming blocks included, at nozzle pitches up to
        # 1/270": ink that far off still lands on plastic.
        pitch, widest = 25.4 / 847, 25.4 / 270
        xs, ys = [polydye.pass_data(d, 1) for d in polydye.offset_drawings(847 / 25.4)]
        ink = [(-(p - polydye.OFFSET_REF_POSITION) * widest * s + dx, i * pitch)
               for (data, n, first), dx in ((xs, 0.), (ys, polydye.OFFSET_Y_SCALE_X))
               for i, c, p in land((data, n, first), 1) for s in (1., 0.)]
        size = polydye.OFFSET_PLAQUE
        self.assertLessEqual(-size[0] + 6.5, min(x for x, y in ink))
        self.assertGreaterEqual(size[1] - 6.5, max(x for x, y in ink))
        self.assertLessEqual(-size[2] + 6.5, min(y for x, y in ink))
        self.assertGreaterEqual(size[3] - 6.5, max(y for x, y in ink))

    def test_plaque_gcode(self):
        rects = [(10., 20., 15., 30.)]
        lines = polydye.plaque_gcode([rects, rects], 0.2, 0.45, 2.405, 0.5)
        moves = [l for l in lines if l.startswith("G1 X") and " E" in l]
        # Prime line 3 mm left of the plaque, then every extruding move within the rectangle.
        self.assertTrue(moves[0].startswith("G1 X7.000 Y30.000 E"))
        for l in moves[1:]:
            x, y = float(l.split()[1][1:]), float(l.split()[2][1:])
            self.assertTrue(10. <= x <= 15. and 20. <= y <= 30., l)
        # The first perimeter edge: 4.5 mm at 0.5 x 0.2 mm of a 2.405 mm2 filament.
        first_edge = moves[1].split()
        self.assertAlmostEqual(float(first_edge[3][1:]), 4.5 * 0.5 * 0.2 / 2.405, 4)
        fan = lines.index("M106 S255")  # from the second layer (Z 0.4) on
        self.assertFalse(any("Z0.400" in l for l in lines[:fan]))
        self.assertIn("G1 Z0.400 F600", lines[fan:])
        self.assertEqual(lines[-2:], ["G1 E-2 F2700", "M106 S0"])

    def test_cfg_with(self):
        text = "\n".join(["[mcu polydye]", "serial: /dev/x", "[polydye]", "offset_x: 1.5  # measured",
                          "#direction_trim: 0", "offset_y: 2", "offset_y: 3", "[gcode_macro A]", "offset_x: 9"])
        out = polydye.cfg_with(text, {'offset_x': '1.25', 'direction_trim': '-0.41', 'offset_y': '0',
                                      'cyan_columns': '-34'})
        self.assertEqual(out.split("\n"), [
            "[mcu polydye]", "serial: /dev/x", "[polydye]", "cyan_columns: -34", "offset_x: 1.25  # measured",
            "direction_trim: -0.41", "offset_y: 0", "#offset_y: 3", "[gcode_macro A]", "offset_x: 9"])
        self.assertEqual(polydye.cfg_value(out, 'offset_y'), '0')
        self.assertIsNone(polydye.cfg_value(text, 'direction_trim'))
        with self.assertRaisesRegex(ValueError, r"no \[polydye\] section"):
            polydye.cfg_with("[printer]\n", {'offset_x': '1'})
        self.assertEqual([polydye.cfg_number(v) for v in (58.6, -0.0, 10., -0.41, 1. / 3)],
                         ['58.6', '0', '10', '-0.41', '0.3333'])

    def test_paced_upload_sends_the_whole_frame(self):
        written = []
        reply = b"PD" + struct.pack("<BBI", 0x80, 7, 2) + b"{}"
        reply += struct.pack("<I", zlib.crc32(b"{}", zlib.crc32(reply[2:8])) & 0xffffffff)

        class Serial:
            def __init__(self, *a, **kw):
                self.left = bytearray(reply)

            def write(self, b):
                written.append(bytes(b))

            def read(self, n):
                out, self.left[:n] = bytes(self.left[:n]), b""
                return out

            flush = close = lambda self: None
        payload = bytes(range(256)) * 200
        with mock.patch.dict(sys.modules, serial=types.SimpleNamespace(Serial=Serial)), \
                mock.patch.object(polydye.time, "sleep") as sleep:
            # A chunk a second.
            self.assertEqual(polydye.data_port_request("/dev/x", 7, 0x10, payload, rate=polydye.DATA_CHUNK), {})
        self.assertEqual([len(w) for w in written[:-1]], [polydye.DATA_CHUNK] * (len(written) - 1))
        self.assertEqual(b"".join(written)[8:-4], payload)
        self.assertEqual(len(written), 4)
        self.assertEqual(sleep.call_count, 3)
        # The clock doesn't move while sleep is mocked, so the waits add up.
        self.assertEqual([round(call[0][0], 1) for call in sleep.call_args_list], [1., 2., 3.])


class CalibrationReadings(unittest.TestCase):
    """The calibration prints' readings, entered as printed, correct a simulated cartridge: a reprint reads 0."""

    def make(self):
        pd = bare_polydye()
        pd.pass_axis, pd.dpi, pd.nozzle_dpi = 'y', 847., 300.
        pd.offset_x, pd.offset_y, pd.direction_trim, pd.registration = 58.6, -8.7, 0., (0, 0, 0, 0)
        pd._save = mock.Mock()
        return pd

    def gcmd(self, **params):
        def get(name, default=None, **kw):
            return params.get(name, default)
        return types.SimpleNamespace(get=get, get_float=get, get_int=get, respond_info=lambda msg: None,
                                     error=RuntimeError)

    def read_page(self, pd, along, across, reverse):
        """The block number where each row of the alignment page lines up, printed with pd's registration and
        direction trim on that cartridge (land())."""
        pitch = 25.4 / pd.dpi
        page = dict((row, land(polydye.pass_data(d, direction, pd.registration), direction, along, across,
                               reverse + (pd.direction_trim / pitch if direction < 0 else 0.)))
                    for row, d, direction in polydye.align_drawings())
        ks = range(-polydye.ALIGN_K, polydye.ALIGN_K + 1)
        readings = {}
        for row, colour, dots in (('C', polydye.CYAN, page['C']), ('Y', polydye.YELLOW, page['Y']),
                                  ('D', polydye.MAGENTA, page['D-'])):
            # The bar on positions 50-79 sitting on a block's magenta bar.
            bars = runs(i for i, c, p in dots if c == colour and 50 <= p <= 79)
            readings[row] = [k for k in ks if any(abs(b - (polydye.align_centre(k) - 0.5)) < 1 for b in bars)]
        for row, colour, n in (('CX', polydye.CYAN, 0), ('YX', polydye.YELLOW, 2)):
            # The block whose colour line lies on the magenta line (positions 49-50), along shift aside.
            shift = pd.registration[n] + along[colour]
            line = lambda k: set(p for i, c, p in page[row] if c == colour and
                                 abs(i - shift - polydye.align_centre(k)) <= 20)
            readings[row] = [k for k in ks if line(k) == {49, 50}]
        return readings

    def test_alignment_page(self):
        pd = self.make()
        cartridge = dict(along=(33, 0, -27), across=(-1, 0, 0), reverse=14)
        self.assertEqual(self.read_page(pd, **cartridge), {'C': [-11], 'Y': [9], 'D': [-7], 'CX': [1], 'YX': [0]})
        pd.cmd_POLYDYE_ALIGN(self.gcmd(CYAN=-11., YELLOW=9., DIRECTION=-7., CYAN_X=1., YELLOW_X=0.))
        self.assertEqual(pd.registration, (-33, 1, 27, 0))
        self.assertAlmostEqual(pd.direction_trim, -14 * 25.4 / 847, 4)
        pd._save.assert_called_once()
        self.assertEqual(self.read_page(pd, **cartridge), {'C': [0], 'Y': [0], 'D': [0], 'CX': [0], 'YX': [0]})

    def test_alignment_readings_are_checked(self):
        pd = self.make()
        with self.assertRaisesRegex(RuntimeError, "give the alignment page's readings"):
            pd.cmd_POLYDYE_ALIGN(self.gcmd())
        pd.registration = (-290, 0, 0, 0)
        with self.assertRaisesRegex(RuntimeError, r"cyan_columns would be -320, beyond \+-300"):
            pd.cmd_POLYDYE_ALIGN(self.gcmd(CYAN=-10.))
        self.assertEqual((pd.registration, pd.direction_trim), ((-290, 0, 0, 0), 0.))
        pd.cmd_POLYDYE_ALIGN(self.gcmd(CYAN_X=-1.4, SAVE=0))
        self.assertEqual(pd.registration, (-290, -1, 0, 0))
        pd._save.assert_not_called()
        # Stored before they apply: if polydye.cfg can't be written nothing changes, so entering the readings again
        # doesn't apply them twice.
        pd._save.side_effect = RuntimeError("couldn't store the values")
        for command, params in ((pd.cmd_POLYDYE_ALIGN, dict(CYAN=2., DIRECTION=1.)),
                                (pd.cmd_POLYDYE_OFFSET, dict(X_TICK=2., Y_TICK=-1.))):
            with self.assertRaisesRegex(RuntimeError, "couldn't store"):
                command(self.gcmd(**params))
            self.assertEqual((pd.registration, pd.direction_trim, pd.offset_x, pd.offset_y),
                             ((-290, -1, 0, 0), 0., 58.6, -8.7))

    def test_offset_print(self):
        pd = self.make()
        pitch, npitch = 25.4 / pd.dpi, 25.4 / pd.nozzle_dpi

        def read(ex, ey):
            """The tick in each slot's middle with the ink landing (ex, ey) mm off: the X scale's lines along Y, at
            bed X = slot - (position - reference) x nozzle pitch, the Y scale's along X at bed Y = slot + column."""
            xs, ys = [polydye.pass_data(d, 1, pd.registration) for d in polydye.offset_drawings(pd.dpi / 25.4)]
            x_lines = set(p for i, c, p in land(xs, 1) if 0 <= i < polydye.OFFSET_SLOT_END[1] / pitch)
            near = min(x_lines, key=lambda p: abs(-(p - polydye.OFFSET_REF_POSITION) * npitch + ex))
            y_lines = runs(i for i, c, p in land(ys, 1) if p == 85 and i >= -polydye.OFFSET_Y_TICKS * 8 - 2)
            self.assertEqual(polydye.offset_y_tick(pd.dpi / 25.4), 8)
            mid = min(y_lines, key=lambda i: abs(i * pitch + ey))
            return ((polydye.OFFSET_REF_POSITION - near) / float(polydye.OFFSET_X_TICK),
                    mid / 8.)

        self.assertEqual(read(0., 0.), (0., 0.))
        # The ink lands 1.3 mm towards +X and 0.75 mm towards -Y of where it should (the offsets are that far off).
        x_tick, y_tick = read(1.3, -0.75)
        self.assertEqual((x_tick, y_tick), (-5., 3.))
        pd.cmd_POLYDYE_OFFSET(self.gcmd(X_TICK=x_tick, Y_TICK=y_tick))
        pd._save.assert_called_once()
        ex = 1.3 + (58.6 - pd.offset_x)
        ey = -0.75 + (-8.7 - pd.offset_y)
        self.assertLess(abs(ex), npitch * polydye.OFFSET_X_TICK / 2.)
        self.assertLess(abs(ey), pitch * 8 / 2.)

    def test_offset_reading_checks(self):
        pd = self.make()
        with self.assertRaisesRegex(RuntimeError, "give X or X_TICK, not both"):
            pd.cmd_POLYDYE_OFFSET(self.gcmd(X=1., X_TICK=2.))
        pd.pass_axis = 'x'
        with self.assertRaisesRegex(RuntimeError, "for pass_axis: y"):
            pd.cmd_POLYDYE_OFFSET(self.gcmd(Y_TICK=2.))
        # Plain offsets aren't stored unless asked.
        pd._respond_offsets = mock.Mock()
        pd.cmd_POLYDYE_OFFSET(self.gcmd(X=1., CYAN_COLUMNS=-30))
        self.assertEqual((pd.offset_x, pd.registration), (1., (-30, 0, 0, 0)))
        pd._save.assert_not_called()


class CalibrationPrints(unittest.TestCase):
    """POLYDYE_ALIGN_PRINT and POLYDYE_OFFSET_PRINT: checked before anything moves, stored, printed, put back."""

    def make(self, homed='xyz'):
        pd, board = TestCleanup.make(TestCleanup(), fail_on="NOTHING")
        pd.offset_x, pd.offset_y, pd.direction_trim, pd.registration = 0., 0., 0., (-34, 1, 27, 0)
        pd.dpi, pd.nozzle_dpi, pd.pass_speed, pd.extra_run_up, pd.settle_time = 847., 300., 59.5, 1., 0.03
        pd.data_port, pd.data_seq, pd.cfg_saves, pd.travel_speed = '/dev/serial/by-id/polydye-if02', 0, 0, 150.
        self.waits = []
        th = {'homed_axes': homed, 'max_accel': 3000., 'max_velocity': 300., 'minimum_cruise_ratio': 0.,
              'axis_minimum': types.SimpleNamespace(x=0., y=0., z=0.),
              'axis_maximum': types.SimpleNamespace(x=250., y=235., z=250.)}
        self.extruder = {'target': 210., 'temperature': 210.}
        self.objects = {
            'toolhead': types.SimpleNamespace(wait_moves=lambda: self.waits.append(len(board.sent)),
                                              get_status=lambda eventtime: th),
            'gcode_move': types.SimpleNamespace(base_position=[0., 0., 0., 0.], get_status=lambda eventtime: {
                'gcode_position': types.SimpleNamespace(z=1.), 'position': types.SimpleNamespace(z=1.)}),
            'gcode_macro POLYDYE_WIPE': types.SimpleNamespace(variables={'x': 245, 'z_hop': 1}),
            'extruder': types.SimpleNamespace(get_status=lambda eventtime: self.extruder),
            'configfile': types.SimpleNamespace(get_status=lambda eventtime: {'settings': {'extruder': {
                'filament_diameter': 1.75, 'nozzle_diameter': 0.4, 'min_extrude_temp': 170.}}})}
        lookup = pd.printer.lookup_object
        pd.printer.lookup_object = lambda name, default=None: (self.objects[name] if name in self.objects
                                                               else lookup(name, default))
        self.uploads = []

        def request(path, seq, ftype, payload=b"", timeout=3., rate=None):
            self.uploads.append((ftype, payload[:8], len(payload), rate))
            return {'cart': 'color'} if ftype == polydye.DATA_HELLO else {}
        patcher = mock.patch.object(polydye, "data_port_request", side_effect=request)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.said = []
        return pd, board

    def gcmd(self, **params):
        def get(name, default=None, **kw):
            return params.get(name, default)
        return types.SimpleNamespace(get=get, get_float=get, get_int=get, respond_info=self.said.append,
                                     error=RuntimeError)

    def test_alignment_page(self):
        pd, board = self.make()
        pd.cmd_POLYDYE_ALIGN_PRINT(self.gcmd(X=60., Y=50., Z=1.))
        puts = [u for u in self.uploads if u[0] == polydye.DATA_PUT]
        self.assertEqual([struct.unpack("<HHBBH", u[1]) for u in puts],
                         [(polydye.CAL_LAYER, n, 0, 0, 3760) for n in range(7)])
        self.assertEqual(set(u[3] for u in puts), {polydye.CAL_UPLOAD_RATE})
        passes = [s for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_PASS")]
        pitch = 25.4 / 847
        start, end = 50. - 400 * pitch, 50. + 3759 * pitch
        self.assertEqual(passes, [
            "POLYDYE_PASS LAYER=65000 PASS=%d X=%.3f Y=%.3f DIR=%d WARM=0" % (n, x, y, d) for n, x, y, d in (
                (0, 60., start, 1), (1, 68., start, 1), (2, 76., start, 1), (3, 84., start, 1),
                (4, 84., end, -1), (5, 92., start, 1), (6, 100., start, 1), (0, 108., start, 1))])
        # In order: G-code state saved, Z, live ink, the passes (waiting after 4: the board queues at most 8), then
        # live ink off and the G-code state back.
        script = [s for s in board.sent if isinstance(s, str)]
        self.assertEqual(script[:3], ["SAVE_GCODE_STATE NAME=polydye_cal", "G90\nG1 Z1.000 F900",
                                      "POLYDYE_ENABLE LIVE=1"])
        self.assertEqual(script[-1], "RESTORE_GCODE_STATE NAME=polydye_cal")
        self.assertIn(board.sent.index(passes[4]), self.waits)
        self.assertEqual((board.boost, board.live), (False, False))
        self.assertIn("POLYDYE_ALIGN CYAN=<C> YELLOW=<Y> DIRECTION=<D> CYAN_X=<CX> YELLOW_X=<YX>", self.said[-1])
        # Where the paper goes: the rows across X (the nozzle row's half width at 10 % more than nozzle_dpi either
        # side), the page with its priming blocks along Y.
        self.assertIn("Its ink spans X 57-111, Y 38-163: the paper must cover that with 5 mm to spare, at least 70 x "
                      "140 mm", [s for s in self.said if s.startswith("PolyDye alignment page, live ink")][0])
        # With a G-code offset the ink lands that much further on the bed; the reverse pass's ink moves by
        # direction_trim, which can make the page too long for the paper.
        pd, board = self.make()
        self.objects['gcode_move'].base_position = [2., -10., 0., 0.]
        pd.direction_trim = -3.
        pd.cmd_POLYDYE_ALIGN_PRINT(self.gcmd(X=60., Y=60., Z=1.))
        self.assertIn("Its ink spans X 59-113, Y 38-160",
                      [s for s in self.said if s.startswith("PolyDye alignment page, live ink")][-1])
        pd, board = self.make()
        pd.direction_trim = 10.
        with self.assertRaisesRegex(RuntimeError, r"the page would need 65 x 145 mm of paper"):
            pd.cmd_POLYDYE_ALIGN_PRINT(self.gcmd(X=60., Y=50., Z=1.))
        self.assertEqual(board.sent, [])

    def test_refused_before_anything_is_sent(self):
        for homed, params, setup, error in (
                ('', {}, None, "home XY first"),
                ('xy', dict(Z=1.), None, "home Z first"),
                ('xyz', {}, lambda pd, board: setattr(board, 'template', 'black_a'), "for the colour cartridge"),
                ('xyz', {}, lambda pd, board: setattr(pd, 'data_port', None), "set data_port"),
                ('xyz', dict(X=60., Y=150.), None, r"would take the toolhead to X=60.0 Y=2\d\d.\d, beyond the axis"),
                ('xyz', {}, lambda pd, board: setattr(board, 'print_state', 'paused'), "not during a print"),
                ('xyz', dict(SPACING=12.), None, r"at SPACING 12 and dpi 847 the page would need 89 x 135 mm of paper"),
                ('xyz', {}, lambda pd, board: setattr(pd, 'dpi', 600.), r"dpi 600 the page would need 65 x 186 mm")):
            pd, board = self.make(homed)
            if setup:
                setup(pd, board)
            with self.assertRaisesRegex(RuntimeError, error):
                pd.cmd_POLYDYE_ALIGN_PRINT(self.gcmd(**params))
            self.assertEqual((board.sent, self.uploads), ([], []), error)

    def test_a_failed_pass_still_turns_off_and_restores(self):
        pd, board = self.make()
        objects = pd.printer.lookup_object('gcode')
        run = objects.run_script_from_command

        def failing(script):
            run(script)
            if script.startswith("POLYDYE_PASS LAYER=65000 PASS=2"):
                raise RuntimeError("Move out of range")
        objects.run_script_from_command = failing
        with self.assertRaisesRegex(RuntimeError, "out of range"):
            pd.cmd_POLYDYE_ALIGN_PRINT(self.gcmd())
        self.assertEqual(board.sent[-1], "RESTORE_GCODE_STATE NAME=polydye_cal")
        self.assertEqual((board.boost, board.live), (False, False))

    def test_the_head_warms_over_the_sponge_not_at_warm_position(self):
        # With the automatic warm-up on and the board able to: up by the wipe's Z hop, over the sponge (POLYDYE_WIPE's
        # X), warmed with the automatic warm-up's recipe, back over the first pass and down; the passes don't warm
        # again.
        pd, board = self.make()
        pd.spit_run_cmd, pd.warm_duty_ppm, pd.warm_max_s = object(), 5800, 30.
        pd.offset_x, pd.offset_y = 58.6, -8.7
        pd.cmd_POLYDYE_ALIGN_PRINT(self.gcmd(X=100., Y=50.))
        script = [s for s in board.sent if isinstance(s, str)]
        first = 50. - 400 * 25.4 / 847
        self.assertEqual(script[1:5], ["G90\nG1 Z2.000 F900\nG1 X245.000 F9000",
                                       "POLYDYE_WARM SECONDS=10 WARM_PULSE=1 WARM_PERIOD_US=254 CONFIRM=1",
                                       "G1 X%.3f Y%.3f F9000\nG1 Z1.000 F900" % (100. - 58.6, first + 8.7),
                                       "POLYDYE_ENABLE LIVE=1"])
        self.assertTrue(all(s.endswith(" WARM=0") for s in script if s.startswith("POLYDYE_PASS")))
        # No POLYDYE_WIPE to say where the sponge is, or its X at the axis maximum (the hard stop POLYDYE_WIPE refuses
        # too): refused before anything is sent.
        for wipe, error in ((None, "no POLYDYE_WIPE macro"), ({'x': 250}, "must be within the X axis, below its max")):
            pd, board = self.make()
            pd.spit_run_cmd, pd.warm_duty_ppm = object(), 5800
            if wipe is None:
                del self.objects['gcode_macro POLYDYE_WIPE']
            else:
                self.objects['gcode_macro POLYDYE_WIPE'].variables = wipe
            with self.assertRaisesRegex(RuntimeError, error):
                pd.cmd_POLYDYE_ALIGN_PRINT(self.gcmd(X=100., Y=50.))
            self.assertEqual((board.sent, self.uploads), ([], []))
        # Warm-up off: none.
        pd, board = self.make()
        pd.spit_run_cmd, pd.warm_duty_ppm, pd.warm_seconds = object(), 5800, 0.
        pd.cmd_POLYDYE_ALIGN_PRINT(self.gcmd(X=100., Y=50.))
        self.assertFalse([s for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_WARM")])

    def test_offset_print(self):
        pd, board = self.make()
        pd.cmd_POLYDYE_OFFSET_PRINT(self.gcmd(X=120., Y=100., BED_TEMP=60.))
        script = [s for s in board.sent if isinstance(s, str)]
        setup = script[1].split("\n")
        # Heated (the hotend to its current target), the plaque at relative extrusion, then up to the plaque's top
        # plus the gap with the hotend off; the ink scales after that.
        self.assertEqual(setup[:7], ["M140 S60", "M190 S60", "M109 S210", "G90", "M83", "M220 S100", "M221 S100"])
        self.assertEqual(setup[-2:], ["G1 Z2.000 F600", "M104 S0"])
        # Two solid layers (one rectangle: a travel down to it for the perimeter and one for the fill; the first layer
        # also comes down for the prime line), then three with the groove (six rectangles).
        zs = [float(l.split()[1][1:]) for prev, l in zip(setup, setup[1:])
              if prev.endswith(" F9000") and l.startswith("G1 Z")]
        self.assertEqual([sum(abs(z - n * 0.2) < 1e-6 for z in zs) for n in range(1, 6)], [3, 2, 12, 12, 12])
        self.assertEqual(script[2:4], ["G90\nG1 Z2.000 F900", "POLYDYE_ENABLE LIVE=1"])
        pitch = 25.4 / 847
        self.assertEqual([s for s in script if s.startswith("POLYDYE_PASS")], [
            "POLYDYE_PASS LAYER=65000 PASS=0 X=120.000 Y=%.3f DIR=1 WARM=0" % (100. - 367 * pitch,),
            "POLYDYE_PASS LAYER=65000 PASS=1 X=126.500 Y=%.3f DIR=1 WARM=0" % (100. - 467 * pitch,)])
        self.assertEqual(script[-2:], ["M104 S0\nM106 S0", "RESTORE_GCODE_STATE NAME=polydye_cal"])
        self.assertIn("POLYDYE_OFFSET X_TICK=<X scale> Y_TICK=<Y scale>", self.said[-1])

    def test_a_failed_plaque_turns_the_hotend_and_fan_off(self):
        pd, board = self.make()
        gcode = pd.printer.lookup_object('gcode')
        run = gcode.run_script_from_command

        def failing(script):
            run(script)
            if script.startswith("M140"):
                raise RuntimeError("Heater extruder not heating at expected rate")
        gcode.run_script_from_command = failing
        with self.assertRaisesRegex(RuntimeError, "not heating"):
            pd.cmd_POLYDYE_OFFSET_PRINT(self.gcmd(X=120., Y=100., BED_TEMP=60.))
        script = [s for s in board.sent if isinstance(s, str)]
        self.assertEqual(script[-2:], ["M104 S0\nM106 S0", "RESTORE_GCODE_STATE NAME=polydye_cal"])

    def test_offset_print_checks(self):
        pd, board = self.make()
        self.extruder['target'] = 0.
        with self.assertRaisesRegex(RuntimeError, "heat the hotend for PLA first"):
            pd.cmd_POLYDYE_OFFSET_PRINT(self.gcmd())
        with self.assertRaisesRegex(RuntimeError, r"the plaque \(X -5.8-23.4, Y -18.6-19.0 with its prime line\)"):
            pd.cmd_POLYDYE_OFFSET_PRINT(self.gcmd(X=7., Y=2., EXTRUDER_TEMP=215.))
        # The scales keep their size in mm at another dpi (600 fits), but at dpi 100 a column is 0.25 mm: the Y scale's
        # ticks, a column apart, would leave the plaque.
        x0, y0, x1, y1 = polydye.offset_ink_extent(600., 300.)
        size = polydye.OFFSET_PLAQUE
        self.assertTrue(-size[0] + 6.5 <= x0 and x1 <= size[1] - 6.5 and -size[2] + 6.5 <= y0 and y1 <= size[3] - 6.5)
        pd.dpi = 100.
        with self.assertRaisesRegex(RuntimeError, "at dpi 100 and nozzle_dpi 300 the scales would reach .* beyond the "
                                                  "plaque less its 6.5 mm margin"):
            pd.cmd_POLYDYE_OFFSET_PRINT(self.gcmd(X=120., Y=100., EXTRUDER_TEMP=215.))
        pd.dpi = 847.
        # With the plaque the ink goes GAP above it: a Z that could be lower is refused.
        with self.assertRaisesRegex(RuntimeError, "Z goes with PLAQUE=0"):
            pd.cmd_POLYDYE_OFFSET_PRINT(self.gcmd(X=120., Y=100., EXTRUDER_TEMP=215., Z=0.2))
        self.assertEqual((board.sent, self.uploads), ([], []))
        # Only the ink: no plaque, nothing heated, Z as given.
        pd.cmd_POLYDYE_OFFSET_PRINT(self.gcmd(X=120., Y=100., PLAQUE=0, Z=1.2))
        script = [s for s in board.sent if isinstance(s, str)]
        self.assertEqual(script[:3], ["SAVE_GCODE_STATE NAME=polydye_cal", "G90\nG1 Z1.200 F900",
                                      "POLYDYE_ENABLE LIVE=1"])
        pd.pass_axis = 'x'
        with self.assertRaisesRegex(RuntimeError, "needs pass_axis: y"):
            pd.cmd_POLYDYE_OFFSET_PRINT(self.gcmd())

    def test_save(self):
        import tempfile
        pd, board = self.make()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "polydye.cfg")
            with open(cfg, "w") as f:
                f.write("[polydye]\npass_axis: y\noffset_x: 58.6\noffset_y: -8.7\nyellow_positions: 2\n")
            os.chmod(cfg, 0o640)
            pd.printer.get_start_args = lambda: {'config_file': os.path.join(tmp, "printer.cfg")}
            pd.offset_x, pd.direction_trim, pd.registration = 58.25, -0.41, (-34, 1, 0, 0)
            with mock.patch.object(polydye.os, "sync", create=True):
                pd.cmd_POLYDYE_SAVE(self.gcmd())
            with open(cfg) as f:
                text = f.read()
            # Registration only where it isn't zero or polydye.cfg has it already.
            self.assertEqual(text, "[polydye]\ndirection_trim: -0.41\ncyan_columns: -34\ncyan_positions: 1\n"
                                   "pass_axis: y\noffset_x: 58.25\noffset_y: 0\nyellow_positions: 0\n")
            self.assertEqual(os.stat(cfg).st_mode & 0o777, 0o640)
            self.assertEqual((pd.cfg_saves, os.listdir(tmp)), (1, ["polydye.cfg"]))
            # The nozzle skew once it's set: POLYDYE_OFFSET SKEW= with SAVE=1.
            pd._respond_offsets = mock.Mock()
            with mock.patch.object(polydye.os, "sync", create=True):
                pd.cmd_POLYDYE_OFFSET(self.gcmd(SKEW=0.03, SAVE=1))
            with open(cfg) as f:
                self.assertIn("\nnozzle_skew: 0.03\n", f.read())
            self.assertEqual(pd.nozzle_skew, 0.03)
            os.remove(cfg)
            with self.assertRaisesRegex(RuntimeError, "couldn't store the values in"):
                pd.cmd_POLYDYE_SAVE(self.gcmd())


def job_planes(dots, columns):
    """polydye_post.py's colour planes for (column, colour, position) dots: (colours, data)."""
    colours, planes = '', []
    for c, ch in enumerate('cmy'):
        plane = bytearray(columns * 14)
        for i, cc, p in dots:
            if cc == c:
                plane[i * 14 + p % 14] |= 1 << (p // 14)
        if any(plane):
            colours += ch
            planes.append(bytes(plane))
    return colours, b''.join(planes)


def write_job(path, passes, dpi=847., nozzle_dpi=300., positions=(16, 83), layers=2, prewarms=0, primes=0,
              ink_accel=0., ink_z=0., prewarm_s=0.):
    """A G-code file with polydye_post.py's job of `passes`: (layer, pass, object, x, y, dir, columns, dots)."""
    ink = []
    for n, (layer, number, obj, x, y, direction, columns, dots) in enumerate(passes):
        colours, data = job_planes(dots, columns)
        p = polydye_post.InkPass(layer, 1, obj, x, y, columns, colours, zlib.compress(data))
        p.n, p.number, p.dir = n, number, direction
        ink.append(p)
    opts = types.SimpleNamespace(dpi=dpi, nozzle_dpi=nozzle_dpi, positions=positions)
    with open(path, "w") as f:
        f.write("; generated by OrcaSlicer\nPOLYDYE_JOB\nG1 X10 Y10\n; EXECUTABLE_BLOCK_END\n\n")
        f.writelines(line + "\n" for line in polydye_post.job_block(ink, opts, layers, prewarms, primes, ink_accel,
                                                                    ink_z, prewarm_s))
        f.write("\n; CONFIG_BLOCK_START\n; filament_colour = #FFFFFF;#FF00FF\n; CONFIG_BLOCK_END\n")


def bar(c, columns, p0, p1):
    return [(i, c, p) for i in range(columns) for p in range(p0, p1 + 1)]


def capped(built, direction):
    """pass_data()'s result with the heater cap job passes get (cap_heaters())."""
    data, n, first = built
    cols = [data[i * polydye.NOZDATA_SIZE:(i + 1) * polydye.NOZDATA_SIZE] for i in range(n)]
    if direction < 0:
        cols.reverse()
    buf = bytearray(b"".join(cols))
    polydye.cap_heaters(buf, first if direction > 0 else first - n + 1)
    cols = [bytes(buf[i * polydye.NOZDATA_SIZE:(i + 1) * polydye.NOZDATA_SIZE]) for i in range(n)]
    if direction < 0:
        cols.reverse()
    return b"".join(cols), n, first


def pulse_load(data):
    """Per column of nozzle data: the most heaters on one fire pulse (a packet, one fire line, across the colours)."""
    out = []
    for i in range(len(data) // polydye.NOZDATA_SIZE):
        col = data[i * polydye.NOZDATA_SIZE:(i + 1) * polydye.NOZDATA_SIZE]
        out.append(max(sum(bin(col[14 * c + k] & mask).count('1') for c in range(3))
                       for k in range(14) for mask in (0x0f, 0xf0)))
    return out


def singles(data):
    """The fires with neither neighbour column firing the same heater: (column, byte, bit)."""
    n, size = len(data) // polydye.NOZDATA_SIZE, polydye.NOZDATA_SIZE
    at = lambda i, b: data[i * size + b] if 0 <= i < n else 0
    return [(i, b, bit) for i in range(n) for b in range(size) for bit in range(8)
            if at(i, b) >> bit & 1 and not (at(i - 1, b) | at(i + 1, b)) >> bit & 1]


class HeaterCap(unittest.TestCase):
    """At most 4 heaters per fire pulse in job passes, shared by an even rotation, and no fire on its own."""

    def build(self, dots, columns, registration=(0, 0, 0, 0), direction=1):
        colours, data = job_planes(dots, columns)
        return polydye.job_pass_data(data, colours, columns, direction, registration)

    def test_one_colour_at_full_width_is_left_alone(self):
        for c in range(3):
            dots = bar(c, 40, 16, 83)
            built = self.build(dots, 40)
            drawing = polydye.PassDrawing(0, 39)
            for i, cc, p in dots:
                drawing.dot(i, cc, p)
            self.assertEqual(built, polydye.pass_data(drawing, 1))
            self.assertLessEqual(max(pulse_load(built[0])), 4)

    def test_colours_together_share_four_per_pulse_evenly(self):
        for colours in ((polydye.CYAN, polydye.YELLOW), (polydye.CYAN, polydye.MAGENTA, polydye.YELLOW)):
            for registration in ((0, 0, 0, 0), (-34, 1, 27, 0)):  # yellow 27 columns on: its pairs straddle ours
                dots = [d for c in colours for d in bar(c, 120, 16, 83)]
                data, n, first = self.build(dots, 120, registration)
                self.assertEqual(max(pulse_load(data)), 4, (colours, registration))
                self.assertEqual(singles(data), [], (colours, registration))
                # Every colour keeps a fair share of its dots.
                kept = [sum(bin(data[i * 42 + 14 * c + k]).count('1') for i in range(n) for k in range(14))
                        for c in colours]
                self.assertLess(max(kept) / min(kept), 1.2, (colours, registration, kept))
        # Unshifted, each heater fires in runs of whole column pairs, as often as the others in its fire pulse.
        data, n, first = self.build([d for c in (0, 2) for d in bar(c, 120, 16, 83)], 120)
        runs = {}
        for b in range(42):
            for bit in range(8):
                fires = [i for i in range(n) if data[i * 42 + b] >> bit & 1]
                if fires:
                    self.assertTrue(all(i % 2 == 0 and i + 1 in fires for i in fires if i - 1 not in fires),
                                    (b, bit))
                    runs[(b % 14, bit // 4, b // 14 * 8 + bit)] = len(fires)
        for group in set(k[:2] for k in runs):
            counts = [v for k, v in runs.items() if k[:2] == group]
            self.assertLessEqual(max(counts) - min(counts), 4, (group, counts))

    def test_reverse_passes_drop_the_same_dots(self):
        dots = [d for c in range(3) for d in bar(c, 61, 16, 83)]
        fwd, n, first = self.build(dots, 61, (3, 0, -27, 2))
        back, n2, last = self.build(dots, 61, (3, 0, -27, 2), -1)
        self.assertEqual((n2, last), (n, first + n - 1))
        self.assertEqual(back, b"".join(fwd[i * 42:(i + 1) * 42] for i in range(n - 1, -1, -1)))


class InkJobData(unittest.TestCase):
    def test_registration_matches_the_drawn_passes(self):
        # The same dots as a job pass and as a drawing: the same nozzle data and first column, in both directions,
        # whatever the registration (dots moved off the nozzles left out).
        import random
        rnd = random.Random(7)
        dots = [(rnd.randrange(300), rnd.randrange(3), rnd.randrange(14, 86)) for _ in range(2000)]
        dots += [(0, polydye.YELLOW, 110), (299, polydye.CYAN, 1)]
        colours, data = job_planes(dots, 300)
        drawing = polydye.PassDrawing(10 ** 9, -10 ** 9)
        for i, c, p in dots:
            drawing.dot(i, c, p)
        for registration, skew in (((0, 0, 0, 0), 0.), ((-34, 1, 27, 0), 0.), ((5, -3, -2, 4), 0.03),
                                   ((0, 0, 0, -20), -0.1), ((-34, 1, 27, 0), 0.046)):
            shifts = polydye.skew_shifts(skew, 847., 300.)
            for direction in (1, -1):
                self.assertEqual(polydye.job_pass_data(data, colours, 300, direction, registration, shifts),
                                 capped(polydye.pass_data(drawing, direction, registration, shifts), direction),
                                 (registration, skew, direction))

    def test_skew_puts_a_line_across_the_pass_straight(self):
        # nozzle_skew 0.03: position p lands 0.03 (p - 50) nozzle pitches (2.82 columns each at 847/300) further along
        # the pass, so its dots go that many columns back, rounded: +3 columns at 14, none at 50, -3 at 85.
        shifts = polydye.skew_shifts(0.03, 847., 300.)
        self.assertEqual((shifts[14], shifts[50], shifts[85], len(shifts)), (3, 0, -3, 112))
        self.assertIsNone(polydye.skew_shifts(0., 847., 300.))
        self.assertIsNone(polydye.skew_shifts(0.001, 847., 300.))  # under half a column everywhere
        # A 2-column bar across positions 14-85 lands straight, in both directions: each dot within half a column of
        # where it was drawn.
        colours, data = job_planes(bar(polydye.MAGENTA, 2, 14, 85), 2)
        for direction in (1, -1):
            out, n, first = polydye.job_pass_data(data, colours, 2, direction, skew=shifts)
            self.assertEqual(n, 8)
            for k in range(n):
                i = first + k * direction  # the bed column it fires at
                col = out[k * 42:(k + 1) * 42]
                for p in range(14, 86):
                    byte, bit = polydye.COLOR_BYTE_ORDER[polydye.MAGENTA][p % 14] + 14, p // 14
                    if col[byte] >> bit & 1:
                        self.assertLessEqual(abs(i + 0.03 * (p - 50) * 847 / 300 - 0.5), 1.0, (direction, k, p))

    def test_plane_shift_drops_dots_off_the_nozzles(self):
        colours, data = job_planes([(0, 0, 0), (0, 0, 13), (0, 0, 14), (0, 0, 111), (1, 0, 50)], 2)
        shifted = polydye.plane_shift(data, 1)
        _, expect = job_planes([(0, 0, 1), (0, 0, 14), (0, 0, 15), (1, 0, 51)], 2)
        self.assertEqual(shifted, expect)
        _, expect = job_planes([(0, 0, 0), (0, 0, 97), (1, 0, 36)], 2)
        self.assertEqual(polydye.plane_shift(data, -14), expect)

    def test_loading_refuses_a_missing_or_broken_job(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "print.gcode")
        with open(path, "w") as f:
            f.write("G1 X10\n" * 1000)
        with self.assertRaisesRegex(polydye.JobError, "no PolyDye ink job in the G-code"):
            polydye.job_load(path)
        write_job(path, [(0, 0, '-', 100., 100., 1, 4, bar(1, 4, 40, 41))])
        header, records = polydye.job_load(path)
        self.assertEqual((header['dpi'], header['nozzle_dpi'], header['positions'], header['passes']),
                         (847., 300., (16, 83), 1))
        self.assertEqual({k: records[0][k] for k in ('n', 'layer', 'pass', 'object', 'dir', 'x', 'y', 'columns',
                                                     'colours')},
                         {'n': 0, 'layer': 0, 'pass': 0, 'object': '-', 'dir': 1, 'x': 100., 'y': 100., 'columns': 4,
                          'colours': 'm'})
        text = open(path).read()
        for broken, error in ((text.replace("version=1", "version=2"), "version 2, this add-on reads version 1"),
                              (text.replace("passes=1", "passes=2"), "says 2 passes but has 1"),
                              (text.replace("positions=16-83", "positions=10-83"), "beyond the 14-85"),
                              (re.sub(r";PDD \S+", ";PDD AAAA", text), "data is corrupt")):
            with open(path, "w") as f:
                f.write(broken)
            with self.assertRaisesRegex(polydye.JobError, error):
                header, records = polydye.job_load(path)
                with open(path, "rb") as fb:
                    polydye.job_record_data(fb, records[0])


class InkJobs(unittest.TestCase):
    """POLYDYE_JOB, POLYDYE_JOB_PASS and POLYDYE_JOB_END with the job's uploads in their real thread."""

    PASSES = [(0, 0, 'A', 120., 100., 1, 40, bar(1, 40, 30, 33)),
              (0, 1, 'A', 125.76, 100.5, -1, 60, bar(0, 60, 20, 21) + bar(2, 50, 70, 70)),
              (0, 2, 'B', 140., 100., 1, 30, bar(1, 30, 50, 52)),
              (1, 0, 'B', 140., 101., 1, 30, bar(1, 30, 50, 52)),
              (1, 1, 'A', 120., 100., -1, 20, bar(0, 20, 40, 41)),
              (1, 2, 'A', 125.76, 100., 1, 20, bar(2, 20, 60, 61))]

    def make(self, passes=None, fail=None, **job):
        pd, board = CalibrationPrints.make(self)
        reactor = types.SimpleNamespace(monotonic=time.monotonic, pause=lambda t: time.sleep(0.002))
        pd.printer.get_reactor = lambda: reactor
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "print.gcode")
        write_job(self.path, self.PASSES if passes is None else passes, **job)
        self.frames, self.hook, self.hello = [], None, {'cart': 'color', 'free': 8000000}

        def request(path, seq, ftype, payload=b"", timeout=3., rate=None):
            if self.hook is not None:
                self.hook(ftype, payload)
            self.frames.append((ftype, payload, rate))
            if fail is not None and ftype == fail:
                raise polydye.DataPortError("no reply from the PolyDye data port")
            return dict(self.hello) if ftype == polydye.DATA_HELLO else {}
        patcher = mock.patch.object(polydye, "data_port_request", side_effect=request)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: pd._stop_job(None))
        self.excluded = []
        self.objects['toolhead'].get_last_move_time = lambda: 0.  # passes end before the print time now
        self.objects['exclude_object'] = types.SimpleNamespace(
            get_status=lambda eventtime: {'excluded_objects': self.excluded})
        return pd, board

    def puts(self):
        return [f for f in self.frames if f[0] == polydye.DATA_PUT]

    def wait_for(self, what, timeout=5.):
        end = time.time() + timeout
        while not what():
            if time.time() > end:
                self.fail("timed out")
            time.sleep(0.01)

    def test_a_job_streams_and_prints_with_the_registration(self):
        pd, board = self.make()
        pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
        self.assertIn("PolyDye ink job: 6 passes on 2 layers", self.said[-1])
        # Hello and the earlier passes cleared, then every pass in order, each deleted once printed, at the
        # calibration's rate.
        self.assertEqual([f[0] for f in self.frames[:2]], [polydye.DATA_HELLO, polydye.DATA_CLEAR])
        self.wait_for(lambda: len(self.puts()) == 6)
        pitch = 25.4 / 847
        for n, (ftype, payload, rate) in enumerate(self.puts()):
            layer, number, _, _, x, y, direction, columns, dots = (self.PASSES[n][:2] + (None, None) +
                                                                   self.PASSES[n][3:])
            colours, data = job_planes(dots, columns)
            built = polydye.job_pass_data(data, colours, columns, direction, (-34, 1, 27, 0))
            self.assertEqual(payload, struct.pack("<HHBBH", layer, number, 0, polydye.PASS_FLAG_ONCE, built[1]) +
                             built[0])
            self.assertEqual(rate, polydye.JOB_UPLOAD_RATE)
        # A pass prints where its drawing's first column lands, the registration moving cyan 34 columns back.
        for n in range(6):
            pd.cmd_POLYDYE_JOB_PASS(self.gcmd(N=n))
        script = [s for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_PASS")]
        firsts = (0, 76, 0, 0, 19 - 34, 27)  # cyan 34 columns back, yellow 27 on
        self.assertEqual(script, ["POLYDYE_PASS LAYER=%d PASS=%d X=%.4f Y=%.5f DIR=%d"
                                  % (p[0], p[1], p[3], p[4] + f * pitch, p[5]) for p, f in zip(self.PASSES, firsts)])
        # Waiting for the moves after every 4 passes: the board queues at most 8.
        self.assertEqual(self.waits, [board.sent.index(script[4])])
        pd.cmd_POLYDYE_JOB_END(self.gcmd())
        self.assertEqual(self.said[-1], "PolyDye ink job done: 6 of 6 passes printed")
        self.assertIsNone(pd.job)
        with self.assertRaisesRegex(RuntimeError, "no PolyDye ink job is loaded"):
            pd.cmd_POLYDYE_JOB_PASS(self.gcmd(N=0))

    def test_an_excluded_objects_passes_are_skipped_and_deleted(self):
        pd, board = self.make()
        pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
        self.wait_for(lambda: len(self.puts()) == 6)
        self.excluded = ['B']
        for n in range(6):
            pd.cmd_POLYDYE_JOB_PASS(self.gcmd(N=n))
        self.assertEqual([s.split(" X=")[0] for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_PASS")],
                         ["POLYDYE_PASS LAYER=0 PASS=0", "POLYDYE_PASS LAYER=0 PASS=1", "POLYDYE_PASS LAYER=1 PASS=1",
                          "POLYDYE_PASS LAYER=1 PASS=2"])
        self.assertIn("PolyDye: pass 2 skipped: object B is excluded", self.said)
        self.wait_for(lambda: len([f for f in self.frames if f[0] == polydye.DATA_DELETE]) == 2)
        self.assertEqual([struct.unpack("<HH", f[1]) for f in self.frames if f[0] == polydye.DATA_DELETE],
                         [(0, 2), (1, 0)])
        pd.cmd_POLYDYE_JOB_END(self.gcmd())
        self.assertEqual(self.said[-1], "PolyDye ink job done: 4 of 6 passes printed, 2 skipped (objects excluded)")

    def test_a_new_job_stops_the_old_ones_uploads_before_it_clears_the_board(self):
        # The first job's thread is still making its first pass's data when a second job is loaded and clears the
        # board: that pass never goes up, so the board has only the second job's passes after the clear.
        pd, board = self.make()
        making, release = threading.Event(), threading.Event()
        real = polydye.job_pass_data

        def slow(*args, **kw):
            if not making.is_set():
                making.set()
                release.wait(5.)
            return real(*args, **kw)
        second = os.path.join(self.tmp, "second.gcode")
        write_job(second, [(7, 0, 'A', 120., 100., 1, 40, bar(1, 40, 30, 33))])
        with mock.patch.object(polydye, "job_pass_data", slow):
            pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
            self.assertTrue(making.wait(5.))
            pd.cmd_POLYDYE_JOB(self.gcmd(FILE=second))
            self.wait_for(lambda: (7, 0) in [struct.unpack("<HH", p[:4]) for t, p, _ in self.frames
                                             if t == polydye.DATA_PUT])
            release.set()
            time.sleep(0.3)
        clear = max(n for n, f in enumerate(self.frames) if f[0] == polydye.DATA_CLEAR)
        self.assertEqual([struct.unpack("<HH", p[:4]) for t, p, _ in self.frames[clear:] if t == polydye.DATA_PUT],
                         [(7, 0)])
        self.assertIsNone(pd.job['streamer'].status()['error'])

    def test_the_room_on_the_board_is_what_the_clear_leaves(self):
        # An earlier job's passes fill the board: after the clear there's room again.
        pd, board = self.make()
        self.hello['free'] = 2000
        self.hook = lambda ftype, payload: ftype == polydye.DATA_CLEAR and self.hello.update(free=8000000)
        pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
        self.assertEqual([f[0] for f in self.frames[:3]], [polydye.DATA_HELLO, polydye.DATA_CLEAR, polydye.DATA_HELLO])
        pd._stop_job(None)
        # Still too little: refused, nothing uploaded.
        self.hook, self.frames[:] = None, []
        self.hello['free'] = 2000
        with self.assertRaisesRegex(RuntimeError, r"pass 1 needs \d+ kB on the board, which has 1 kB free"):
            pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
        self.assertEqual([f[0] for f in self.frames], [polydye.DATA_HELLO, polydye.DATA_CLEAR, polydye.DATA_HELLO])

    def test_the_next_pass_must_fit_in_what_may_be_ahead(self):
        # Room for the largest pass (121 columns with the registration's spread) and no more: the next pass goes up
        # only once the one before it is printed.
        pd, board = self.make()
        self.hello['free'] = 2 * 121 * 42
        pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
        self.wait_for(lambda: len(self.puts()) == 1)
        time.sleep(0.2)
        self.assertEqual(len(self.puts()), 1)
        # Pass 0 is scheduled to end at print time 200: until then it's still on the board, so pass 1 waits.
        self.objects['toolhead'].get_last_move_time = lambda: 200.
        pd.cmd_POLYDYE_JOB_PASS(self.gcmd(N=0))
        time.sleep(0.3)
        self.assertEqual(len(self.puts()), 1)
        # Pass 1, waiting for its room, lets the moves run (print time 250): pass 0 has printed, pass 1 goes up.
        self.objects['toolhead'].wait_moves = lambda: setattr(board, 'print_time', 250.)
        pd.cmd_POLYDYE_JOB_PASS(self.gcmd(N=1))
        self.assertEqual(len(self.puts()), 2)
        self.assertEqual([s.split(" X=")[0] for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_PASS")],
                         ["POLYDYE_PASS LAYER=0 PASS=0", "POLYDYE_PASS LAYER=0 PASS=1"])

    def test_the_registration_and_skew_stay_while_a_job_is_loaded(self):
        pd, board = self.make()
        pd._save, pd._respond_offsets = mock.Mock(), mock.Mock()
        pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
        for command, params in ((pd.cmd_POLYDYE_OFFSET, dict(CYAN_COLUMNS=-30)),
                                (pd.cmd_POLYDYE_OFFSET, dict(SKEW=0.03)), (pd.cmd_POLYDYE_ALIGN, dict(CYAN=1.))):
            with self.assertRaisesRegex(RuntimeError, "an ink job is loaded, and its passes are made with the colour "
                                                      "registration and nozzle skew in use"):
                command(self.gcmd(**params))
        self.assertEqual((pd.registration, pd.nozzle_skew), ((-34, 1, 27, 0), 0.))
        # The offsets still change; once the job is unloaded, the registration too.
        pd.cmd_POLYDYE_OFFSET(self.gcmd(X=1., SAVE=0))
        self.assertEqual(pd.offset_x, 1.)
        pd.cmd_POLYDYE_JOB_END(self.gcmd())
        pd.cmd_POLYDYE_OFFSET(self.gcmd(CYAN_COLUMNS=-30, SAVE=0))
        self.assertEqual(pd.registration, (-30, 1, 27, 0))

    def test_uploads_stay_a_few_passes_ahead(self):
        with mock.patch.object(polydye, "JOB_AHEAD_PASSES", 2):
            pd, board = self.make()
            pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
            self.wait_for(lambda: len(self.puts()) == 2)
            time.sleep(0.2)
            self.assertEqual(len(self.puts()), 2)
            pd.cmd_POLYDYE_JOB_PASS(self.gcmd(N=0))
            self.wait_for(lambda: len(self.puts()) == 3)
            time.sleep(0.2)
            self.assertEqual(len(self.puts()), 3)

    def test_a_failed_upload_stops_the_pass(self):
        with mock.patch.object(polydye, "JOB_RETRIES", 1):
            pd, board = self.make(fail=polydye.DATA_PUT)
            pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
            with self.assertRaisesRegex(RuntimeError, "the ink job's upload to the board failed: no reply"):
                pd.cmd_POLYDYE_JOB_PASS(self.gcmd(N=0))
        self.assertFalse([s for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_PASS")])

    def test_the_print_ending_stops_its_job(self):
        pd, board = self.make()
        sdcard = {'active': False}
        self.objects['virtual_sdcard'] = types.SimpleNamespace(file_path=lambda: self.path,
                                                               is_active=lambda: sdcard['active'])
        with self.assertRaisesRegex(RuntimeError, "goes at the start of a print's G-code"):
            pd.cmd_POLYDYE_JOB(self.gcmd())  # a file loaded, but no print running
        sdcard['active'], board.print_state = True, 'printing'
        pd.cmd_POLYDYE_JOB(self.gcmd())
        streamer = pd.job['streamer']
        pd._check_job_end(0.)
        self.assertIsNotNone(pd.job)
        sdcard['active'], board.print_state = False, 'cancelled'
        pd._check_job_end(0.)
        self.assertIsNone(pd.job)
        self.assertTrue(streamer.stopping.is_set())
        # A job loaded from a file outside a print stays until POLYDYE_JOB_END.
        pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
        pd._check_job_end(0.)
        self.assertIsNotNone(pd.job)

    def test_a_short_pass_has_the_longest_run_up(self):
        # minimum_cruise_ratio 0.6 at 500 mm/s^2: a 2-column magenta pass at Y=226.2 needs 8.84 mm of run-out, to
        # Y=235.07 on a 235 mm axis. The registration's spread (61 columns) made it look 63 columns long, with a shorter
        # run-out; it's refused now.
        pd, board = self.make([(0, 0, '-', 120., 226.2, 1, 2, bar(1, 2, 30, 33))], ink_accel=500.)
        self.objects['toolhead'].get_status(0.)['minimum_cruise_ratio'] = 0.6
        with self.assertRaisesRegex(RuntimeError, r"layer 0's ink at X=120.0 Y=225.2 would take the toolhead to "
                                                  r"X=120.0 Y=235.9, beyond the axis limits"):
            pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
        # 1.5 mm further in, it fits.
        pd, board = self.make([(0, 0, '-', 120., 224.7, 1, 2, bar(1, 2, 30, 33))], ink_accel=500.)
        self.objects['toolhead'].get_status(0.)['minimum_cruise_ratio'] = 0.6
        pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))

    def test_a_pass_speed_over_max_velocity_is_refused_at_the_start(self):
        pd, board = self.make()
        self.objects['toolhead'].get_status(0.)['max_velocity'] = 50.
        with self.assertRaisesRegex(RuntimeError, r"the passes go at pass_speed 59.5 mm/s, over max_velocity 50.0"):
            pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
        self.assertEqual(self.frames, [])

    def test_object_names_and_burst_lengths(self):
        # A non-ASCII object name (Orca keeps the model's) still matches the excluded object's.
        pd, board = self.make([(0, 0, 'pièce_id_0_copy_0', 120., 100., 1, 40, bar(1, 40, 30, 33))])
        pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
        self.assertEqual(pd.job['records'][0]['object'], 'pièce_id_0_copy_0')
        self.excluded = ['PIÈCE_ID_0_COPY_0']
        pd.cmd_POLYDYE_JOB_PASS(self.gcmd(N=0))
        self.assertIn("PolyDye: pass 0 skipped: object pièce_id_0_copy_0 is excluded", self.said)
        pd._stop_job(None)
        # Bursts longer than the board takes at warm_period_us 200 (65535 columns: 13.1 s) are refused at the start.
        pd, board = self.make(prewarms=3, prewarm_s=16.)
        pd.prewarm_cmd, pd.warm_period_us, pd.warm_max_s = object(), 200, 30.
        with self.assertRaisesRegex(RuntimeError, r"the job's prewarm bursts are 16 s, longer than the board takes at "
                                                  r"warm_period_us 200 \(13.1 s\): slice again with --prewarm 13.1"):
            pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
        self.assertEqual(self.frames, [])
        pd.warm_period_us = 254
        pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))

    def test_refused_before_anything_is_sent(self):
        far = [(0, 0, '-', 5., 100., 1, 40, bar(1, 40, 30, 33))]
        for passes, job, setup, error in (
                (None, dict(dpi=600.), None, "sliced for dpi 600, but polydye.cfg has 847: slice it again with "
                                             "polydye_post.py --dpi 847"),
                (None, dict(nozzle_dpi=330.), None, "polydye_post.py --nozzle-dpi 300"),
                (None, dict(positions=(16, 85)), None, r"the colour registration \(cyan_positions 1, yellow_positions "
                                                       r"0\) moves dots beyond the 14-85 .* --positions 16-84"),
                (far, {}, lambda pd: setattr(pd, 'offset_x', 59.), r"layer 0's ink at X=5.0 Y=99.0 would take the "
                                                                   r"toolhead to X=-54.0"),
                (None, {}, lambda pd: setattr(pd, 'pass_axis', 'x'), "drawn for pass_axis: y"),
                (None, {}, lambda pd: setattr(pd, 'data_port', None), "set data_port"),
                # The G-code's acceleration for the ink: at 10 mm/s^2 a run-up to 59.5 mm/s is 177 mm, off the bed.
                (None, dict(ink_accel=10.), None, r"layer 0's ink at X=120.0 .* beyond the axis limits"),
                (None, dict(ink_z=250.5), None, r"the ink goes up to Z=250.50 \(the job's --gap above the layer\), "
                                               r"beyond the Z axis maximum"),
                (None, dict(prewarms=3), None, r"prewarm bursts \(POLYDYE_PREWARM\) need newer PolyDye board"),
                (None, dict(primes=1), lambda pd: setattr(pd, 'spit_patterns', ('all',)),
                 r"primes \(POLYDYE_PRIME\) need newer PolyDye board"),
                (None, dict(primes=1), lambda pd: (setattr(pd, 'spit_run_cmd', object()),
                                                   self.objects.pop('gcode_macro POLYDYE_WIPE')),
                 "the job primes the nozzles over the wipe sponge, but there's no POLYDYE_WIPE macro")):
            pd, board = self.make(passes, **job)
            if setup:
                setup(pd)
            with self.assertRaisesRegex(RuntimeError, error):
                pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
            self.assertEqual((self.frames, pd.job), ([], None), error)
        pd, board = self.make()
        with open(self.path, "w") as f:
            f.write("G1 X1\n")
        with self.assertRaisesRegex(RuntimeError, "POLYDYE_JOB: no PolyDye ink job in the G-code"):
            pd.cmd_POLYDYE_JOB(self.gcmd(FILE=self.path))
        self.assertEqual(self.frames, [])

    gcmd = CalibrationPrints.gcmd


if __name__ == "__main__":
    unittest.main()
