# PolyDye Moonraker component: serves the PolyDye page (opened from Mainsail's sidebar) and updates the board's
# firmware over USB.
#
# install.sh links this file into Moonraker's components folder and adds a [polydye] section to moonraker.conf.
# The page itself talks to Moonraker's normal websocket API, like Mainsail does.
#
# Firmware API (authorised like the rest of Moonraker's API):
#   GET  /server/polydye/firmware          -> {"bundled": {version, size, sha256} | null, "updating": bool,
#                                              "files": {"corrupt": [...], "changed": [...]} | {"error": ...}, ...}
#   POST /server/polydye/firmware/update   [file=<path under the config root>] [force=true]
#        Updates from the given .bin, or from the firmware bundled with this add-on. Progress is sent to websocket
#        clients as notify_polydye_firmware {"state": "running"|"done"|"error", "message", "progress"}.
#
# The board leaves USB and comes back when it restarts into new firmware, and a Raspberry Pi 3 has frozen when that
# happened with ports open (its dwc_otg USB driver). So an update first writes everything pending to the SD card and
# stops the Klipper service, and starts it again once the board is back.
#
# At startup it also checks the add-on's own files (scripts/polydye_files.py) and shows a Moonraker warning if an
# unsafe shutdown damaged them.
#
# Copyright (C) 2026  polydye-klipper contributors
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import asyncio
import glob
import importlib.util
import logging
import os
import re
import sys
import time

from ..common import KlippyState, RequestType, TransportType

# Raspberry Pi firmware's throttle flags (same bits as `vcgencmd get_throttled`); absent on other boards.
THROTTLED_PATH = "/sys/devices/platform/soc/soc:firmware/get_throttled"


def power_problem(path: str = THROTTLED_PATH, bits: int = 0):
    """Why a USB firmware transfer isn't safe on this Pi right now, or None. `bits` adds throttle flags from
    elsewhere (Moonraker's proc_stats), for kernels without the sysfs file."""
    try:
        with open(path) as f:
            bits |= int(f.read().strip(), 16)
    except (OSError, ValueError):
        pass
    if bits & 0x1:
        return "the Pi is under-volted right now"
    if bits & 0x10000:
        return "the Pi has been under-volted since it started"
    return None

CHECKER = "scripts/polydye_files.py"
FILES_WARNING = ("PolyDye: add-on files damaged ({files}), probably by an unsafe shutdown (the Pi froze or lost power) "
                 "right after an update. Recover them: Update Manager, polydye, Recover, Hard Recovery (or see the "
                 "polydye-klipper README, \"Raspberry Pi 3: freezes and damaged files\").")
# Seconds to wait for Klipper to let go of the board's Klipper port after its service was told to stop.
PORT_FREE_TIMEOUT = 30.


def load_module(name: str, path: str, needed: str):
    """Loads one of the add-on's scripts as a module. Returns (module, None), or (None, why) if it is missing or
    damaged (a zero-filled file loads as an empty module, so check for a function it must have)."""
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        return None, f"{os.path.basename(path)}: {e}"
    if not hasattr(module, needed):
        return None, f"{os.path.basename(path)} is damaged (no {needed})"
    return module, None


# Files the page needs, with their content types. Nothing else in web/ is served.
ASSETS = {
    "ui": ("index.html", "text/html; charset=UTF-8"),
    "polydye.js": ("polydye.js", "application/javascript; charset=UTF-8"),
    "polydye.css": ("polydye.css", "text/css; charset=UTF-8"),
}


class PolyDye:
    def __init__(self, config) -> None:
        self.server = config.get_server()
        # This file is a symlink into the polydye-klipper repo; the page lives next to it in web/.
        repo_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        self.web_dir = config.get("web_path", os.path.join(repo_dir, "web"))
        if not os.path.isdir(self.web_dir):
            raise config.error(f"polydye: web folder not found: {self.web_dir}")
        for name, (filename, content_type) in ASSETS.items():
            self.server.register_endpoint(
                f"/server/polydye/{name}",
                RequestType.GET,
                self._make_handler(filename),
                transports=TransportType.HTTP,
                wrap_result=False,
                content_type=content_type,
                # Static page assets, no printer data; API calls from the page are authorised as usual.
                auth_required=False,
            )
        logging.info(f"polydye: serving the PolyDye page from {self.web_dir} at /server/polydye/ui")

        self.repo_dir = repo_dir
        self.uploader_path = os.path.join(repo_dir, "scripts", "polydye_upload.py")
        # A damaged script must not stop the page from loading: it is how the damage gets reported.
        self.uploader, self.uploader_error = load_module("polydye_upload", self.uploader_path, "update_firmware")
        self.files_checker, self.files_error = load_module("polydye_files", os.path.join(repo_dir, CHECKER), "check")
        self.files = {}
        self.bundled_path = os.path.join(repo_dir, "firmware", "polydye.bin")
        # The service that carries the plug-in toggle hook (install.sh's KLIPPER_SERVICE).
        self.klipper_service = config.get("klipper_service", "klipper")
        self.updating = False
        self.update_task = None
        self.server.register_notification("polydye:polydye_firmware")
        self.server.register_endpoint("/server/polydye/firmware", RequestType.GET, self._handle_firmware_info)
        self.server.register_endpoint("/server/polydye/firmware/update", RequestType.POST,
                                      self._handle_firmware_update)

    async def component_init(self) -> None:
        await self._check_files()

    async def on_exit(self) -> None:
        """Moonraker is shutting down (before it closes its components): end a running firmware update, whose cleanup
        terminates the uploader and starts Klipper again (_run_update_steps)."""
        task = self.update_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task], timeout=30.)

    async def _check_files(self) -> None:
        """Compares the add-on's files with git's record and raises a Moonraker warning (shown by Mainsail) for
        damaged ones."""
        if self.files_checker is None:
            # Most likely damaged by the same shutdown: report it as such, so the page shows the recovery steps too.
            self.files = {"corrupt": [CHECKER], "changed": [], "error": self.files_error}
            self.server.add_warning(FILES_WARNING.format(files=f"{CHECKER}, so the others couldn't be checked: "
                                                               f"{self.files_error}"), warn_id="polydye_files")
            return
        try:
            self.files = await self.server.get_event_loop().run_in_thread(self.files_checker.check, self.repo_dir)
        except Exception as e:  # not a git checkout (a copied install), or git missing: nothing to compare
            self.files = {"error": str(e)}
            logging.info(f"polydye: couldn't check the add-on's files: {e}")
            return
        if self.files["corrupt"]:
            self.server.add_warning(FILES_WARNING.format(files=", ".join(self.files["corrupt"])),
                                    warn_id="polydye_files")
        elif self.files["changed"]:
            logging.info(f"polydye: add-on files that differ from git (local edits?): {self.files['changed']}")

    def _make_handler(self, filename: str):
        path = os.path.join(self.web_dir, filename)

        async def handler(web_request) -> str:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

        return handler

    # ---- firmware updates ----------------------------------------------------------------------------------

    def _image_info(self, path: str):
        if self.uploader is None:
            raise ValueError(f"the add-on's uploader is unusable ({self.uploader_error})")
        with open(path, "rb") as f:
            return self.uploader.image_info(f.read())

    def _power_problem(self):
        # proc_stats polls `vcgencmd get_throttled` and keeps every flag it has seen since Moonraker started.
        stats = self.server.lookup_component("proc_stats", None)
        seen = getattr(stats, "total_throttled", 0) | getattr(stats, "last_throttled", 0)
        return power_problem(bits=seen if isinstance(seen, int) else 0)

    async def _handle_firmware_info(self, web_request):
        bundled = None
        if os.path.isfile(self.bundled_path):
            try:
                bundled = self._image_info(self.bundled_path)
            except ValueError as e:
                logging.warning(f"polydye: bundled firmware unusable: {e}")
        return {"bundled": bundled, "updating": self.updating, "power_problem": self._power_problem(),
                "board_on_usb": bool(glob.glob("/dev/serial/by-id/*PolyDye*-if00")),
                "klipper_service": self.klipper_service, "files": self.files}

    def _resolve_upload(self, rel: str) -> str:
        config_dir = self.server.lookup_component("file_manager").get_directory("config")
        path = os.path.realpath(os.path.join(config_dir, rel))
        if os.path.commonpath([path, os.path.realpath(config_dir)]) != os.path.realpath(config_dir):
            raise self.server.error("firmware file must be inside the config folder", 400)
        if not os.path.isfile(path):
            raise self.server.error(f"firmware file not found: {rel}", 404)
        return path

    async def _handle_firmware_update(self, web_request):
        # Everything that can fail without waiting comes first; the busy flag is then claimed before the only
        # await, so a second request gets 409 rather than racing this one.
        force = web_request.get_boolean("force", False)
        rel = web_request.get_str("file", None)
        if self.updating:
            raise self.server.error("a firmware update is already running", 409)
        if self.uploader is None:
            raise self.server.error(f"the add-on's uploader is unusable ({self.uploader_error}): recover the "
                                    "add-on's files first", 500)
        why = self._power_problem()
        if why:
            # A Pi 3 short of power loses USB data and can lock up during the transfer or the board's restart.
            raise self.server.error(f"{why}: fix its power supply first (a Pi 3 needs 5.1 V, 2.5 A)", 409)
        path = self._resolve_upload(rel) if rel else self.bundled_path
        if not os.path.isfile(path):
            raise self.server.error("this add-on has no bundled firmware", 404)
        try:
            info = self._image_info(path)
        except ValueError as e:
            raise self.server.error(str(e), 400)
        ports = sorted(glob.glob("/dev/serial/by-id/*PolyDye*-if02"))
        if len(ports) != 1:
            raise self.server.error("PolyDye board not found on USB" if not ports
                                    else "more than one PolyDye board is plugged in", 400)
        self.updating = True
        try:
            if self.server.lookup_component("klippy_connection").state == KlippyState.READY:
                status = await self.server.lookup_component("klippy_apis").query_objects(
                    {"print_stats": ["state"]})
                if status.get("print_stats", {}).get("state") in ("printing", "paused"):
                    raise self.server.error("not during a print", 409)
            # Klipper not ready (shut down, error, starting): it can't be printing, so the update may go ahead.
        except Exception as e:
            self.updating = False
            if isinstance(e, self.server.error) and e.status_code == 409:
                raise
            raise self.server.error(f"couldn't check whether the printer is printing: {e}", 503)
        except BaseException:
            self.updating = False
            raise
        self.update_task = self.server.get_event_loop().create_task(self._run_update(ports[0], path, force))
        return {"started": True, "version": info["version"]}

    def _notify(self, state: str, message: str, progress=None) -> None:
        self.server.send_event("polydye:polydye_firmware",
                               {"state": state, "message": message, "progress": progress})

    async def _run_update(self, port: str, path: str, force: bool) -> None:
        # self.updating stays set until the final notification, so no second update can start while Klipper is
        # still reconnecting to the board.
        try:
            await self._run_update_steps(port, path, force)
        finally:
            self.updating = False

    async def _sync(self) -> None:
        """Writes everything pending to the SD card, before and after the step that has frozen a Raspberry Pi 3."""
        await self.server.get_event_loop().run_in_thread(os.sync)

    def _klipper_running(self) -> bool:
        """Whether the Klipper service is running (or starting), so it is started again after the update."""
        try:
            services = self.server.lookup_component("machine").get_system_info().get("service_state", {})
            state = services.get(self.klipper_service, {}).get("active_state")
        except Exception:
            state = None
        if state is None:  # Moonraker doesn't track the service: go by its connection to Klipper
            return self.server.lookup_component("klippy_connection").is_connected()
        return state not in ("inactive", "failed", "deactivating")

    async def _wait_port_free(self, port: str) -> None:
        """Waits until no program has `port` open (systemd's stop returns before Klipper has exited)."""
        deadline = time.monotonic() + PORT_FREE_TIMEOUT
        while True:
            holders = await self.server.get_event_loop().run_in_thread(self.uploader.port_holders, port)
            if not holders:
                return
            if time.monotonic() > deadline:
                names = ", ".join(f"{name} (pid {pid})" for pid, name in holders)
                raise Exception(f"{names} still has the board's Klipper port open; stop it, then try again")
            await asyncio.sleep(0.25)

    async def _run_update_steps(self, port: str, path: str, force: bool) -> None:
        ok, last, proc, start_klipper = False, "", None, False
        machine = self.server.lookup_component("machine")
        try:
            self._notify("running", "writing pending files to the SD card", 0)
            await self._sync()
            if self._klipper_running():
                self._notify("running", f"stopping {self.klipper_service}: the board leaves USB while it restarts", 0)
                await machine.do_service_action("stop", self.klipper_service)
                start_klipper = True
            klipper_port = self.uploader.klipper_port(port)
            if klipper_port:
                await self._wait_port_free(klipper_port)
            self._notify("running", "starting the update", 0)
            args = [sys.executable, self.uploader_path, "--port", port, "firmware", path]
            if force:
                args.append("--force")
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                last = line.decode(errors="replace").strip()
                m = re.match(r"progress: (\d+)%", last)
                if m:
                    self._notify("running", "writing the firmware", int(m.group(1)))
                elif last:
                    logging.info(f"polydye firmware: {last}")
                    self._notify("running", last)
            ok = await proc.wait() == 0
        except Exception as e:  # not a cancellation: that is a BaseException, and goes straight to the cleanup
            last = f"error: {e}"
            logging.exception("polydye: firmware update failed")
        finally:
            # Also after a failure, and when Moonraker shuts down mid-update (on_exit cancels this): the uploader
            # mustn't reboot the board once Klipper has its port again, and Klipper mustn't be left stopped.
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                await proc.wait()
            try:
                await self._sync()
            except Exception:
                logging.exception("polydye: sync after the firmware update failed")
            if start_klipper:
                # The board is back (or gone); Klipper's start hook picks that up.
                try:
                    self._notify("running", f"starting {self.klipper_service}")
                    await machine.do_service_action("start", self.klipper_service)
                except Exception as e:
                    logging.exception(f"polydye: couldn't start {self.klipper_service} after the firmware update")
                    if ok:
                        ok, last = False, f"the board was updated, but {self.klipper_service} didn't start: {e}"
        if ok:
            self._notify("done", last or "firmware updated", 100)
        else:
            self._notify("error", re.sub(r"^error: ", "", last) or "the update failed")

def load_component(config) -> PolyDye:
    return PolyDye(config)
