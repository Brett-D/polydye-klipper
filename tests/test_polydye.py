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
        pd.querying, pd.last_status, pd.oid, pd.query_level_cmd = True, {}, 0, None

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
        self.assertEqual(polydye.parse_stripes(polydye.FIRE_TEST_STRIPES),
                         [('color_b', 6), ('color_b', 8), ('color_b', 10), ('color_a', 6), ('color_a', 10)])
        self.assertEqual(polydye.parse_stripes(" Black_A:12 , "), [('black_a', 12)])
        for bad in ("", "color_c:6", "color_a", "color_a:x", "color_a:5", "color_a:13"):
            with self.assertRaises(ValueError):
                polydye.parse_stripes(bad)

    def test_positions_side_by_side_across_the_pass_axis(self):
        self.assertEqual(polydye.stripe_positions('y', 80., 60., 20., 3), [(80., 60.), (100., 60.), (120., 60.)])
        self.assertEqual(polydye.stripe_positions('x', 80., 60., 20., 2), [(80., 60.), (80., 80.)])


class FakeBoard:
    """Just enough of the board for POLYDYE_FIRE_TEST / POLYDYE_SPIT: tracks mode, template and pulse."""

    def __init__(self):
        self.boost = self.live = False
        self.template, self.pulse = 'color_a', 8
        self.sent = []

    def status(self):
        return {'boost': self.boost, 'live': self.live, 'running': False, 'queued': 0, 'faults': 0,
                'cartridge_template': self.template, 'pulse_words': self.pulse, 'pulse_us': self.pulse * .3}


class TestCleanup(unittest.TestCase):
    """A failing stripe must still leave PolyDye off with the template and pulse put back."""

    def make(self, fail_on):
        board = FakeBoard()
        pd = object.__new__(polydye.PolyDye)
        pd.oid, pd.pass_axis = 0, 'y'
        pd.query_cartridge_cmd = object()

        def mode(args):
            board.boost, board.live = bool(args[1]), bool(args[2])
            board.sent.append(('mode', args[1], args[2]))
        pd.mode_cmd = types.SimpleNamespace(send=mode)
        pd.set_template_cmd = types.SimpleNamespace(
            send=lambda args: (setattr(board, 'template', polydye.TEMPLATES[args[1]]), board.sent.append('tpl')))
        pd.set_pulse_cmd = types.SimpleNamespace(
            send=lambda args: (setattr(board, 'pulse', args[1]), board.sent.append('pulse')))
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
            elif script == "POLYDYE_ENABLE LIVE=1":
                board.boost = board.live = True
            elif script == "POLYDYE_DISABLE":
                board.boost = board.live = False
        reactor = types.SimpleNamespace(monotonic=lambda: 0., pause=lambda t: None)
        objects = {'gcode': types.SimpleNamespace(run_script_from_command=run_script),
                   'toolhead': types.SimpleNamespace(wait_moves=lambda: None)}
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
            pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b:10", X=80., Y=60., SPACING=20., COLUMNS=600,
                                               UPLOAD=1, LAYER=0, PASS=0))
        self.assertIn("POLYDYE_UPLOAD_TEST_PASS LAYER=0 PASS=0 COLUMNS=600 PATTERN=all", board.sent)
        self.assertEqual((board.boost, board.live, board.template, board.pulse), (False, False, 'color_a', 8))

    def test_fire_test_restores_after_success(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.cmd_POLYDYE_FIRE_TEST(self.gcmd(STRIPES="color_b:10,color_b:12", X=80., Y=60., SPACING=20.,
                                           COLUMNS=600, UPLOAD=1, LAYER=0, PASS=0))
        passes = [s for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_PASS")]
        self.assertEqual(passes, ["POLYDYE_PASS LAYER=0 PASS=0 X=80.000 Y=60.000 DIR=1",
                                  "POLYDYE_PASS LAYER=0 PASS=0 X=100.000 Y=60.000 DIR=1"])
        self.assertEqual(sum(1 for s in board.sent if isinstance(s, str) and s.startswith("POLYDYE_UPLOAD")), 1)
        self.assertEqual((board.boost, board.template, board.pulse), (False, 'color_a', 8))

    def test_spit_needs_confirm(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append(('spit', args[1])))
        with self.assertRaisesRegex(RuntimeError, "CONFIRM=1"):
            pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=2.))
        self.assertEqual(board.sent, [])

    def test_spit_sends_columns_then_turns_off_and_restores(self):
        pd, board = self.make(fail_on="NOTHING")
        pd.spit_cmd = types.SimpleNamespace(send=lambda args: board.sent.append(('spit', args[1])))
        pd.cmd_POLYDYE_SPIT(self.gcmd(SECONDS=1.5, PULSE=12, CONFIRM=1))
        self.assertIn(('spit', 1500), board.sent)
        self.assertLess(board.sent.index("POLYDYE_CARTRIDGE PULSE=12"), board.sent.index(('mode', 1, 1)))
        self.assertEqual((board.boost, board.live, board.pulse), (False, False, 8))


if __name__ == "__main__":
    unittest.main()
