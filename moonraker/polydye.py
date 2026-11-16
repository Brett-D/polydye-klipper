# PolyDye Moonraker component: serves the PolyDye page (opened from Mainsail's sidebar).
#
# install.sh links this file into Moonraker's components folder and adds a [polydye] section to moonraker.conf.
# The page itself talks to Moonraker's normal websocket API, like Mainsail does.
#
# Copyright (C) 2026  polydye-klipper contributors
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import logging
import os

from ..common import RequestType, TransportType

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

    def _make_handler(self, filename: str):
        path = os.path.join(self.web_dir, filename)

        async def handler(web_request) -> str:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

        return handler


def load_component(config) -> PolyDye:
    return PolyDye(config)
