#!/usr/bin/env python3
"""Tests for config/polydye_macros.cfg: renders the macros with Jinja the way Klipper does (no Klipper needed).

This file is part of polydye-klipper (GPLv3).
"""
import ast
import configparser
import os
import unittest

try:
    import jinja2
except ImportError:
    jinja2 = None

MACROS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "polydye_macros.cfg")


def load_macros():
    # Klipper's config parser settings (klippy/configfile.py).
    cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(';', '#'))
    with open(MACROS) as f:
        cp.read_file(f)
    env = jinja2.Environment('{%', '%}', '{', '}')
    macros = {}
    for section in cp.sections():
        if section.startswith("gcode_macro "):
            name = section.split(None, 1)[1]
            variables = {k[len("variable_"):]: ast.literal_eval(v) for k, v in cp.items(section)
                         if k.startswith("variable_")}
            macros[name] = (env.from_string(cp.get(section, "gcode")), variables)
    return macros


class MacroError(Exception):
    pass


def raise_error(msg):
    raise MacroError(msg)


def printer_status(macros, homed="xyz", pos=(50., 60., 2., 0.), axis_max=(250., 208., 250.), g92_x=0.,
                   print_state="standby"):
    # g92_x: a G92 shift in X, so machine position = G-code position + g92_x (homing origin stays 0).
    status = {
        "print_stats": {"state": print_state},
        "toolhead": {"homed_axes": homed, "axis_maximum": dict(zip("xyz", axis_max)),
                     "axis_minimum": {"x": 0., "y": -22., "z": 0.}},
        "gcode_move": {"homing_origin": {"x": 0., "y": 0., "z": 0., "e": 0.},
                       "gcode_position": dict(zip("xyze", pos)),
                       "position": dict(zip("xyze", (pos[0] + g92_x,) + tuple(pos[1:])))},
    }
    for name, (_, variables) in macros.items():
        status["gcode_macro " + name] = variables
    return status


def run(macros, name, params="", **status_args):
    # Like Klipper: the macro's own variables are plain names, plus printer, params and rawparams.
    template, variables = macros[name]
    params_dict = dict(p.split("=", 1) for p in params.split())
    out = template.render(dict(variables), printer=printer_status(macros, **status_args), params=params_dict,
                          rawparams=params, action_raise_error=raise_error)
    return [line.strip() for line in out.splitlines() if line.strip()]


def x_moves(lines):
    return [float(word[1:]) for line in lines if line.startswith("G1 ") for word in line.split()
            if word.startswith("X")]


@unittest.skipUnless(jinja2, "needs jinja2 (part of klippy's environment)")
class Wipe(unittest.TestCase):
    def setUp(self):
        self.macros = load_macros()

    def test_homes_first_only_when_needed_and_passes_checked_values(self):
        lines = run(self.macros, "POLYDYE_WIPE", "STROKES=1", homed="")
        self.assertEqual(lines, ["G28", "_POLYDYE_WIPE_MOVES X=245.0 LENGTH=25.0 STROKES=1 SPEED=50.0 Z_HOP=1.0 "
                                        "TRAVEL=150.0"])
        self.assertEqual(len(run(self.macros, "POLYDYE_WIPE", homed="xyz")), 1)

    def test_refuses_x_at_or_beyond_the_axis_maximum(self):
        for params, axis_max in (("X=250", 250.), ("", 245.), ("X=260", 250.)):
            with self.assertRaisesRegex(MacroError, "X axis maximum"):
                run(self.macros, "POLYDYE_WIPE", params, homed="", axis_max=(axis_max, 208., 250.))
        with self.assertRaisesRegex(MacroError, "LENGTH"):
            run(self.macros, "POLYDYE_WIPE", "X=10 LENGTH=25")

    def test_g92_shift_counts_when_homed_not_before_g28(self):
        # Homed at machine X=15 after G92 X0: G-code X=245 would be machine X=260, past the 250 stop.
        with self.assertRaisesRegex(MacroError, "X axis maximum"):
            run(self.macros, "POLYDYE_WIPE", homed="xyz", pos=(0., 60., 2., 0.), g92_x=15.)
        # Not homed: G28 clears the shift, so the default is fine.
        self.assertEqual(run(self.macros, "POLYDYE_WIPE", homed="", pos=(0., 60., 2., 0.), g92_x=15.)[0], "G28")

    def test_strokes_then_return(self):
        lines = run(self.macros, "_POLYDYE_WIPE_MOVES", "X=245 LENGTH=25 STROKES=3 SPEED=50 Z_HOP=1 TRAVEL=150")
        self.assertEqual(lines[:3], ["SAVE_GCODE_STATE NAME=polydye_wipe", "G90", "G1 Z3.0 F900"])
        self.assertEqual(x_moves(lines), [245., 220., 245., 220., 245., 220., 245., 50.])
        self.assertEqual(lines[-1], "RESTORE_GCODE_STATE NAME=polydye_wipe MOVE=1 MOVE_SPEED=15")

    def test_z_limit_and_speed(self):
        lines = run(self.macros, "_POLYDYE_WIPE_MOVES", "X=230 LENGTH=10 STROKES=1 SPEED=20 Z_HOP=1 TRAVEL=150",
                    pos=(50., 60., 249.5, 0.))
        self.assertIn("G1 Z250.0 F900", lines)
        self.assertEqual(x_moves(lines), [230., 220., 230., 50.])
        self.assertIn("G1 X220.0 F1200.0", lines)


@unittest.skipUnless(jinja2, "needs jinja2 (part of klippy's environment)")
class FireTest(unittest.TestCase):
    def setUp(self):
        self.macros = load_macros()

    def test_homes_only_when_needed_then_sets_z_and_passes_the_stripes(self):
        lines = run(self.macros, "PD_FIRE_TEST", "X=90 STRIPES=black_a:6,color_b_late:8 PATTERN=ALL", homed="")
        # The Z move and the G-code state are POLYDYE_FIRE_TEST's, so they're restored even if it fails.
        self.assertEqual(lines, ["G28", "POLYDYE_FIRE_TEST X=90.0 Y=60.0 Z=0.5 SPACING=18.0 PATTERN=all "
                                        "STRIPES=black_a:6,color_b_late:8"])
        # Without STRIPES, POLYDYE_FIRE_TEST prints its default (the stock firmware's settings); the pattern is stock's.
        lines = run(self.macros, "PD_FIRE_TEST", homed="xyz")
        self.assertEqual(lines, ["POLYDYE_FIRE_TEST X=70.0 Y=60.0 Z=0.5 SPACING=18.0 PATTERN=stock"])

    def test_passes_the_random_pattern_knobs(self):
        # The knobs go with the stock pattern, the default (POLYDYE_FIRE_TEST refuses them with another).
        lines = run(self.macros, "PD_FIRE_TEST", "DENSITY=50 COLORS=m PER_PULSE=2 COLUMNS=300", homed="xyz")
        self.assertEqual(lines, ["POLYDYE_FIRE_TEST X=70.0 Y=60.0 Z=0.5 SPACING=18.0 PATTERN=stock COLUMNS=300 "
                                 "DENSITY=50 COLORS=m PER_PULSE=2"])
        lines = run(self.macros, "PD_FIRE_TEST", "PATTERN=Stock REPEAT=3 FIRST=20 LAST=40", homed="xyz")
        self.assertEqual(lines, ["POLYDYE_FIRE_TEST X=70.0 Y=60.0 Z=0.5 SPACING=18.0 PATTERN=stock REPEAT=3 "
                                 "FIRST=20 LAST=40"])
        lines = run(self.macros, "PD_FIRE_TEST", "PATTERN=all WARM=10 WARM_PULSE=8 WARM_PERIOD_US=508", homed="xyz")
        self.assertEqual(lines, ["POLYDYE_FIRE_TEST X=70.0 Y=60.0 Z=0.5 SPACING=18.0 PATTERN=all WARM=10 "
                                 "WARM_PULSE=8 WARM_PERIOD_US=508"])

    def test_refuses_during_a_print_before_homing(self):
        for state in ("printing", "paused"):
            with self.assertRaisesRegex(MacroError, "not during a print"):
                run(self.macros, "PD_FIRE_TEST", homed="", print_state=state)

    def test_refuses_z_below_0_2(self):
        with self.assertRaisesRegex(MacroError, "Z must be"):
            run(self.macros, "PD_FIRE_TEST", "Z=0")



@unittest.skipUnless(jinja2, "needs jinja2 (part of klippy's environment)")
class CalibrationPrints(unittest.TestCase):
    def setUp(self):
        self.macros = load_macros()

    def test_home_only_when_needed_and_pass_the_values(self):
        self.assertEqual(run(self.macros, "PD_ALIGN_PRINT", "X=60 SPACING=9", homed="xy"),
                         ["G28", "POLYDYE_ALIGN_PRINT Z=1.0 X=60.0 SPACING=9.0"])
        self.assertEqual(run(self.macros, "PD_ALIGN_PRINT", homed="xyz"), ["POLYDYE_ALIGN_PRINT Z=1.0"])
        self.assertEqual(run(self.macros, "PD_OFFSET_PRINT", "LAYERS=3 EXTRUDER_TEMP=215 BED_TEMP=60", homed=""),
                         ["G28", "POLYDYE_OFFSET_PRINT LAYERS=3 EXTRUDER_TEMP=215.0 BED_TEMP=60.0"])
        self.assertEqual(run(self.macros, "PD_OFFSET_PRINT", homed="xyz"), ["POLYDYE_OFFSET_PRINT"])
        self.assertEqual(run(self.macros, "PD_TEST_PAGE", "X=120 PRIME=0.5", homed="xy"),
                         ["G28", "POLYDYE_TEST_PAGE Z=1.0 X=120.0 PRIME=0.5"])
        self.assertEqual(run(self.macros, "PD_TEST_PAGE", "Z=1.5", homed="xyz"), ["POLYDYE_TEST_PAGE Z=1.5"])

    def test_refused_during_a_print_before_homing(self):
        for name in ("PD_ALIGN_PRINT", "PD_OFFSET_PRINT", "PD_TEST_PAGE"):
            with self.assertRaisesRegex(MacroError, "not during a print"):
                run(self.macros, name, homed="", print_state="paused")
        for name in ("PD_ALIGN_PRINT", "PD_TEST_PAGE"):
            with self.assertRaisesRegex(MacroError, "Z must be"):
                run(self.macros, name, "Z=0.1")


if __name__ == "__main__":
    unittest.main()
