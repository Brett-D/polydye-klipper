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


def printer_status(macros, homed="xyz", pos=(50., 60., 2., 0.), axis_max=(250., 208., 250.)):
    status = {
        "toolhead": {"homed_axes": homed, "axis_maximum": dict(zip("xyz", axis_max)),
                     "axis_minimum": {"x": 0., "y": -22., "z": 0.}},
        "gcode_move": {"homing_origin": {"x": 0., "y": 0., "z": 0., "e": 0.},
                       "gcode_position": dict(zip("xyze", pos))},
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
        self.assertEqual(lines, ["G28", "_POLYDYE_WIPE_MOVES X=235.0 LENGTH=25.0 STROKES=1 SPEED=50.0 Z_HOP=1.0 "
                                        "TRAVEL=150.0"])
        self.assertEqual(len(run(self.macros, "POLYDYE_WIPE", homed="xyz")), 1)

    def test_refuses_x_at_or_beyond_the_axis_maximum(self):
        for params, axis_max in (("X=250", 250.), ("", 235.), ("X=260", 250.)):
            with self.assertRaisesRegex(MacroError, "X axis maximum"):
                run(self.macros, "POLYDYE_WIPE", params, homed="", axis_max=(axis_max, 208., 250.))
        with self.assertRaisesRegex(MacroError, "LENGTH"):
            run(self.macros, "POLYDYE_WIPE", "X=10 LENGTH=25")

    def test_strokes_then_return(self):
        lines = run(self.macros, "_POLYDYE_WIPE_MOVES", "X=235 LENGTH=25 STROKES=3 SPEED=50 Z_HOP=1 TRAVEL=150")
        self.assertEqual(lines[:3], ["SAVE_GCODE_STATE NAME=polydye_wipe", "G90", "G1 Z3.0 F900"])
        self.assertEqual(x_moves(lines), [235., 210., 235., 210., 235., 210., 235., 50.])
        self.assertEqual(lines[-1], "RESTORE_GCODE_STATE NAME=polydye_wipe MOVE=1 MOVE_SPEED=15")

    def test_z_limit_and_speed(self):
        lines = run(self.macros, "_POLYDYE_WIPE_MOVES", "X=230 LENGTH=10 STROKES=1 SPEED=20 Z_HOP=1 TRAVEL=150",
                    pos=(50., 60., 249.5, 0.))
        self.assertIn("G1 Z250.0 F900", lines)
        self.assertEqual(x_moves(lines), [230., 220., 230., 50.])
        self.assertIn("G1 X220.0 F1200.0", lines)


if __name__ == "__main__":
    unittest.main()
