#!/usr/bin/env python3
"""Tests for the Moonraker component (moonraker/polydye.py): the order of a firmware update's steps, and reporting
damaged add-on files. Runs without Moonraker: its few imports are faked.

This file is part of polydye-klipper (GPLv3).
"""
import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA_PORT = "/dev/serial/by-id/usb-PolyDye_PolyDye_ink_controller_E4B063-if02"
KLIPPER_PORT = DATA_PORT[:-2] + "00"


def load_component():
    """moonraker/polydye.py as Moonraker loads it, with a fake `..common`."""
    pkg, comps, common = (types.ModuleType(n) for n in ("mr", "mr.components", "mr.common"))
    pkg.__path__ = comps.__path__ = []
    common.KlippyState = types.SimpleNamespace(READY="ready")
    common.RequestType = types.SimpleNamespace(GET="GET", POST="POST")
    common.TransportType = types.SimpleNamespace(HTTP="HTTP")
    sys.modules.update({"mr": pkg, "mr.components": comps, "mr.common": common})
    spec = importlib.util.spec_from_file_location("mr.components.polydye", os.path.join(ROOT, "moonraker", "polydye.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pd = load_component()


class FakeLoop:
    async def run_in_thread(self, fn, *args):
        return fn(*args)


class FakeMachine:
    def __init__(self, events, state, fail=None):
        self.events, self.state, self.fail = events, state, fail

    def get_system_info(self):
        return {"service_state": {"klipper": {"active_state": self.state}}}

    async def do_service_action(self, action, service):
        self.events.append((action, service))
        if action == self.fail:
            raise RuntimeError("Service Actions Not Available")
        if action == "stop" and self.fail == "hang":
            await asyncio.Event().wait()  # systemd still working on it


class FakeServer:
    def __init__(self, events, state="active", fail=None):
        self.events, self.warnings, self.loop = events, {}, FakeLoop()
        self.machine = FakeMachine(events, state, fail)

    def lookup_component(self, name, default=None):
        return {"machine": self.machine}[name]

    def get_event_loop(self):
        return self.loop

    def send_event(self, name, data):
        self.events.append(("notify", data["state"], data["message"]))

    def add_warning(self, warning, warn_id=None):
        self.warnings[warn_id] = warning


class FakeUploader:
    class RolledBack(Exception):
        pass

    def __init__(self, events, holders, settle=None):
        self.events, self.holders, self.settle = events, list(holders), settle

    def confirm_after_reboot(self, port, version, partition, log=None, timeout=0.):
        self.events.append(("confirm", version, partition))
        if self.settle:
            raise self.settle
        return version

    def klipper_port(self, port):
        return port[:-2] + "00"

    def port_holders(self, port):
        self.events.append(("holders", port))
        return self.holders.pop(0) if self.holders else []


class FakeProc:
    """The uploader. With hang=True it keeps running (waiting for the board) until terminated."""

    def __init__(self, events, lines, rc, hang=False):
        self.events, self.lines, self.rc, self.hang = events, [line.encode() + b"\n" for line in lines], rc, hang
        self.stdout, self.returncode, self.ended = self, None, asyncio.Event()

    async def readline(self):
        if self.lines:
            return self.lines.pop(0)
        if self.hang:
            await self.ended.wait()
        return b""

    async def wait(self):
        if self.hang:
            await self.ended.wait()
        self.returncode = self.rc
        return self.rc

    def terminate(self):
        self.events.append("terminate")
        self.rc = -15
        self.ended.set()


class Update(unittest.TestCase):
    def run_update(self, state="active", fail=None, rc=0, lines=("progress: 50%", "the board runs 9ba5def; confirmed"),
                   holders=([(812, "klippy")],), scenario=None, settle=None):
        events = []
        comp = object.__new__(pd.PolyDye)
        comp.server = FakeServer(events, state, fail)
        comp.klipper_service = "klipper"
        comp.uploader = FakeUploader(events, holders, settle)
        comp.uploader_path = "/home/pi/polydye-klipper/scripts/polydye_upload.py"
        comp.update_task = None

        async def fake_exec(*args, **kw):
            events.append(("upload", args[-2] if args[-1] == "--force" else args[-1]))
            return FakeProc(events, lines, rc, hang=scenario is not None)
        with mock.patch.object(pd.os, "sync", side_effect=lambda: events.append("sync")), \
                mock.patch.object(pd.asyncio, "create_subprocess_exec", fake_exec):
            asyncio.run(scenario(comp, events) if scenario else comp._run_update_steps(DATA_PORT, "/fw.bin", False,
                                                                                       "9ba5def"))
        return [e for e in events if e[0] != "notify"], [e for e in events if e[0] == "notify"]

    def test_sync_stop_klipper_wait_for_its_port_update_sync_start(self):
        steps, notes = self.run_update()
        self.assertEqual(steps, ["sync", ("stop", "klipper"), ("holders", KLIPPER_PORT), ("holders", KLIPPER_PORT),
                                 ("upload", "/fw.bin"), "sync", ("start", "klipper")])
        self.assertEqual(notes[-1], ("notify", "done", "the board runs 9ba5def; confirmed"))

    def test_klipper_not_running_is_left_stopped(self):
        steps, notes = self.run_update(state="inactive", holders=())
        self.assertEqual(steps, ["sync", ("holders", KLIPPER_PORT), ("upload", "/fw.bin"), "sync"])
        self.assertEqual(notes[-1][1], "done")

    def test_failed_update_still_syncs_and_starts_klipper(self):
        steps, notes = self.run_update(rc=1, lines=("error: the board didn't come back after the update",))
        self.assertEqual(steps[-2:], ["sync", ("start", "klipper")])
        self.assertEqual(notes[-1], ("notify", "error", "the board didn't come back after the update"))

    def test_no_update_if_klipper_cant_be_stopped(self):
        with self.assertLogs(level="ERROR"):
            steps, notes = self.run_update(fail="stop")
        # Started again in case systemd took the request after all (a running service stays as it is).
        self.assertEqual(steps, ["sync", ("stop", "klipper"), "sync", ("start", "klipper")])
        self.assertEqual(notes[-1][1], "error")
        self.assertIn("Service Actions Not Available", notes[-1][2])

    @staticmethod
    def shut_down_after(event):
        """Moonraker shutting down (on_exit) once `event` has happened."""
        async def scenario(comp, events):
            comp.update_task = asyncio.get_running_loop().create_task(
                comp._run_update_steps(DATA_PORT, "/fw.bin", False, "9ba5def"))
            while event not in events:
                await asyncio.sleep(0)
            await comp.on_exit()
            assert comp.update_task.cancelled()
        return scenario

    def test_moonraker_shutting_down_mid_update_ends_the_uploader_then_starts_klipper(self):
        steps, _ = self.run_update(scenario=self.shut_down_after(("upload", "/fw.bin")))
        self.assertEqual(steps[-4:], [("upload", "/fw.bin"), "terminate", "sync", ("start", "klipper")])

    def test_moonraker_shutting_down_while_klipper_stops_still_starts_it(self):
        steps, _ = self.run_update(fail="hang", scenario=self.shut_down_after(("stop", "klipper")))
        self.assertEqual(steps, ["sync", ("stop", "klipper"), "sync", ("start", "klipper")])

    def test_after_an_interrupted_reboot_the_new_firmware_is_confirmed_before_klipper_starts(self):
        rebooting = ("progress: 100%", "written to ota_1 and checked; rebooting the board")
        after_reboot = self.shut_down_after(("notify", "running", rebooting[1]))
        steps, _ = self.run_update(lines=rebooting, scenario=after_reboot)
        self.assertEqual(steps[-4:], ["terminate", ("confirm", "9ba5def", "ota_1"), "sync", ("start", "klipper")])
        # Gone back to the old firmware: settled too.
        with self.assertLogs(level="WARNING"):
            steps, _ = self.run_update(lines=rebooting, scenario=after_reboot,
                                       settle=FakeUploader.RolledBack("went back to cd4926d"))
        self.assertEqual(steps[-1], ("start", "klipper"))
        # Not back: Klipper stays stopped rather than have the board rejoin USB under it.
        with self.assertLogs(level="ERROR"):
            steps, _ = self.run_update(lines=rebooting, scenario=after_reboot,
                                       settle=RuntimeError("the board didn't come back"))
        self.assertEqual(steps[-2:], [("confirm", "9ba5def", "ota_1"), "sync"])

    def test_uploader_killed_by_systemd_after_the_reboot(self):
        # systemd stops Moonraker's whole service: the uploader may end by a signal before on_exit runs.
        steps, notes = self.run_update(rc=-15, lines=("written to ota_0 and checked; rebooting the board",))
        self.assertEqual(steps[-3:], [("confirm", "9ba5def", "ota_0"), "sync", ("start", "klipper")])
        self.assertEqual(notes[-1][1], "error")

    def test_no_update_while_something_keeps_the_klipper_port_open(self):
        with mock.patch.object(pd, "PORT_FREE_TIMEOUT", 0.), self.assertLogs(level="ERROR"):
            steps, notes = self.run_update(holders=([(812, "klippy")],) * 3)
        self.assertNotIn(("upload", "/fw.bin"), steps)
        self.assertEqual(steps[-2:], ["sync", ("start", "klipper")])
        self.assertIn("klippy (pid 812) still has the board's Klipper port open", notes[-1][2])


class Files(unittest.TestCase):
    def component(self, checker=None, error=None):
        comp = object.__new__(pd.PolyDye)
        comp.server = FakeServer([])
        comp.repo_dir = "/home/pi/polydye-klipper"
        comp.files_checker, comp.files_error, comp.files = checker, error, {}
        return comp

    def test_damaged_files_raise_a_moonraker_warning(self):
        checker = types.SimpleNamespace(check=lambda repo: {"corrupt": ["klipper/polydye.py"], "changed": []})
        comp = self.component(checker)
        asyncio.run(comp.component_init())
        self.assertIn("add-on files damaged (klipper/polydye.py)", comp.server.warnings["polydye_files"])
        self.assertIn("Hard Recovery", comp.server.warnings["polydye_files"])
        self.assertEqual(comp.files["corrupt"], ["klipper/polydye.py"])

    def test_edits_and_copied_installs_raise_none(self):
        for check in (lambda repo: {"corrupt": [], "changed": ["README.md"]},
                      mock.Mock(side_effect=RuntimeError("not a git checkout"))):
            comp = self.component(types.SimpleNamespace(check=check))
            asyncio.run(comp.component_init())
            self.assertEqual(comp.server.warnings, {})

    def test_a_damaged_checker_is_reported_and_doesnt_stop_the_page(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "polydye_files.py")
        with open(path, "wb") as f:
            f.write(b"\0" * 64)  # zero-filled, as an unsafe shutdown leaves it
        module, error = pd.load_module("polydye_files", path, "check")
        self.assertIsNone(module)
        comp = self.component(None, error)
        asyncio.run(comp.component_init())
        self.assertIn("scripts/polydye_files.py, so the others couldn't be checked: polydye_files.py",
                      comp.server.warnings["polydye_files"])
        self.assertEqual(comp.files["corrupt"], ["scripts/polydye_files.py"])  # the page shows its recovery banner


if __name__ == "__main__":
    unittest.main()
