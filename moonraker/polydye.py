# PolyDye Moonraker component: serves the PolyDye page (opened from Mainsail's sidebar) and updates the board's
# firmware over USB.
#
# install.sh links this file into Moonraker's components folder and adds a [polydye] section to moonraker.conf.
# The page itself talks to Moonraker's normal websocket API, like Mainsail does.
#
# Firmware API (authorised like the rest of Moonraker's API):
#   GET  /server/polydye/firmware          -> {"bundled": {version, size, sha256} | null, "updating": bool}
#   POST /server/polydye/firmware/update   [file=<path under the config root>] [force=true]
#        Updates from the given .bin, or from the firmware bundled with this add-on. Progress is sent to websocket
#        clients as notify_polydye_firmware {"state": "running"|"done"|"error", "message", "progress"}.
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

from ..common import RequestType, TransportType

# Raspberry Pi firmware's throttle flags (same bits as `vcgencmd get_throttled`); absent on other boards.
THROTTLED_PATH = "/sys/devices/platform/soc/soc:firmware/get_throttled"


def power_problem(path: str = THROTTLED_PATH):
    """Why a USB firmware transfer isn't safe on this Pi right now, or None."""
    try:
        with open(path) as f:
            bits = int(f.read().strip(), 16)
    except (OSError, ValueError):
        return None
    if bits & 0x1:
        return "the Pi is under-volted right now"
    if bits & 0x10000:
        return "the Pi has been under-volted since it started"
    return None

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

        self.uploader_path = os.path.join(repo_dir, "scripts", "polydye_upload.py")
        spec = importlib.util.spec_from_file_location("polydye_upload", self.uploader_path)
        self.uploader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.uploader)
        self.bundled_path = os.path.join(repo_dir, "firmware", "polydye.bin")
        self.updating = False
        self.server.register_notification("polydye:polydye_firmware")
        self.server.register_endpoint("/server/polydye/firmware", RequestType.GET, self._handle_firmware_info)
        self.server.register_endpoint("/server/polydye/firmware/update", RequestType.POST,
                                      self._handle_firmware_update)

    def _make_handler(self, filename: str):
        path = os.path.join(self.web_dir, filename)

        async def handler(web_request) -> str:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

        return handler

    # ---- firmware updates ----------------------------------------------------------------------------------

    def _image_info(self, path: str):
        with open(path, "rb") as f:
            return self.uploader.image_info(f.read())

    async def _handle_firmware_info(self, web_request):
        bundled = None
        if os.path.isfile(self.bundled_path):
            try:
                bundled = self._image_info(self.bundled_path)
            except ValueError as e:
                logging.warning(f"polydye: bundled firmware unusable: {e}")
        return {"bundled": bundled, "updating": self.updating, "power_problem": power_problem()}

    def _resolve_upload(self, rel: str) -> str:
        config_dir = self.server.lookup_component("file_manager").get_directory("config")
        path = os.path.realpath(os.path.join(config_dir, rel))
        if os.path.commonpath([path, os.path.realpath(config_dir)]) != os.path.realpath(config_dir):
            raise self.server.error("firmware file must be inside the config folder", 400)
        if not os.path.isfile(path):
            raise self.server.error(f"firmware file not found: {rel}", 404)
        return path

    async def _handle_firmware_update(self, web_request):
        if self.updating:
            raise self.server.error("a firmware update is already running", 409)
        why = power_problem()
        if why:
            # A Pi 3 short of power loses USB data and can lock up during the transfer or the board's restart.
            raise self.server.error(f"{why}: fix its power supply first (a Pi 3 needs 5.1 V, 2.5 A)", 409)
        rel = web_request.get_str("file", None)
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
        klippy_apis = self.server.lookup_component("klippy_apis")
        try:
            status = await klippy_apis.query_objects({"print_stats": ["state"]})
            if status.get("print_stats", {}).get("state") in ("printing", "paused"):
                raise self.server.error("not during a print", 409)
        except self.server.error as e:
            if e.status_code == 409:
                raise
        self.updating = True
        force = web_request.get_boolean("force", False)
        self.server.get_event_loop().create_task(self._run_update(ports[0], path, force))
        return {"started": True, "version": info["version"]}

    def _notify(self, state: str, message: str, progress=None) -> None:
        self.server.send_event("polydye:polydye_firmware",
                               {"state": state, "message": message, "progress": progress})

    async def _run_update(self, port: str, path: str, force: bool) -> None:
        ok, rebooted, last = False, False, ""
        try:
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
                    rebooted = rebooted or "rebooting the board" in last
                    logging.info(f"polydye firmware: {last}")
                    self._notify("running", last)
            ok = await proc.wait() == 0
        except Exception as e:
            last = f"error: {e}"
            logging.exception("polydye: firmware update failed")
        finally:
            self.updating = False
        if rebooted:
            # Klipper lost the board while it rebooted: reconnect.
            try:
                await self.server.lookup_component("klippy_apis").do_restart("FIRMWARE_RESTART")
            except Exception as e:
                logging.warning(f"polydye: FIRMWARE_RESTART after the update failed: {e}")
        if ok:
            self._notify("done", last or "firmware updated", 100)
        else:
            self._notify("error", re.sub(r"^error: ", "", last) or "the update failed")


def load_component(config) -> PolyDye:
    return PolyDye(config)
