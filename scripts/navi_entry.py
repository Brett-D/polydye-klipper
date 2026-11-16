#!/usr/bin/env python3
"""Adds or removes the PolyDye entry in Mainsail's custom navigation file (.theme/navi.json).

  navi_entry.py add|remove <path to navi.json>

Other entries are left alone. On remove, the file is deleted if PolyDye was its only entry.

This file is part of polydye-klipper (GPLv3).
"""
import json
import os
import sys

ENTRY = {
    "title": "PolyDye",
    "href": "/server/polydye/ui",
    "target": "_self",
    "position": 85,
    # A simple ink drop, 24x24 viewBox.
    "icon": "M12 2C12 2 5 10 5 14.5A7 7 0 0 0 19 14.5C19 10 12 2 12 2Z",
}


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("add", "remove"):
        print(__doc__)
        return 2
    action, path = sys.argv[1], sys.argv[2]
    entries = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        entries = json.loads(text) if text else []
        if not isinstance(entries, list):
            print("polydye: %s is not a list of entries; left unchanged" % path)
            return 1
    others = [e for e in entries if not (isinstance(e, dict) and e.get("title") == ENTRY["title"])]
    if action == "add":
        new = others + [ENTRY]
        if new != entries:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(new, f, indent=2)
                f.write("\n")
            print("polydye: added PolyDye to Mainsail's sidebar (%s)" % path)
    elif others != entries:
        if others:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(others, f, indent=2)
                f.write("\n")
        else:
            os.remove(path)
        print("polydye: removed PolyDye from Mainsail's sidebar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
