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


if __name__ == "__main__":
    unittest.main()
