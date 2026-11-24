#!/usr/bin/env python3
"""Checks this add-on's files against git's record of them.

An unsafe shutdown (the Pi freezing or losing power) soon after an update can leave freshly written files empty or
full of zeros. `git status` doesn't notice: it trusts a file whose size and time match what it recorded. This hashes
every tracked file instead and compares it with the index.

  polydye_files.py [REPO]     (default: the repo this script is in)

Prints one line per file that differs: "corrupt PATH" (missing, empty or zero-filled, or a text file with NUL bytes:
damage, not an edit) or "changed PATH" (any other difference, e.g. a local edit). The last line is "result: ok",
"result: corrupt", "result: changed" or "result: unknown (why)". Callers must look for that line: a damaged copy of
this script prints nothing.

The Klipper service's start hook (polydye-toggle.sh) and the Moonraker component (the PolyDye page) run it.

This file is part of polydye-klipper (GPLv3).
"""
import os
import subprocess
import sys

# Files that aren't text: corrupt only if missing, empty or all zeros, or not starting like an ESP32 image.
BINARY_SUFFIXES = (".bin", ".png", ".jpg", ".jpeg", ".gif", ".ico")


class CheckError(Exception):
    pass


def _git(repo, *args, data=None):
    try:
        r = subprocess.run(["git", "-C", repo] + list(args), input=data, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CheckError("can't run git: %s" % (e,))
    if r.returncode:
        raise CheckError("git %s failed: %s" % (args[0], r.stderr.decode(errors="replace").strip()))
    return r.stdout


def looks_corrupt(path, data):
    """Damage an unsafe shutdown leaves (empty or zero-filled blocks), not an edit."""
    if not data or not data.strip(b"\0"):
        return True
    if path.endswith(".bin"):
        return data[0] != 0xE9  # every ESP32 firmware image starts with this byte
    return not path.endswith(BINARY_SUFFIXES) and b"\0" in data


def check(repo):
    """Returns {"corrupt": [paths], "changed": [paths]}. Raises CheckError if this isn't a git checkout or git
    can't be run."""
    if not os.path.exists(os.path.join(repo, ".git")):
        raise CheckError("not a git checkout")
    entries = []
    for rec in _git(repo, "ls-files", "-s", "-z").split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, sha = meta.split()[:2]
        if mode.startswith(b"100"):  # regular files; not links or submodules
            entries.append((path.decode(), sha.decode()))
    corrupt, changed, present = [], [], []
    for path, sha in entries:
        if os.path.isfile(os.path.join(repo, path)):
            present.append((path, sha))
        else:
            corrupt.append(path)
    if present:
        # One git process for every file; it applies the same line-ending rules as when it checked them out.
        out = _git(repo, "hash-object", "--stdin-paths", data="".join(p + "\n" for p, _ in present).encode())
        hashes = out.decode().split()
        if len(hashes) != len(present):
            raise CheckError("git hash-object gave %d hashes for %d files" % (len(hashes), len(present)))
        for (path, sha), got in zip(present, hashes):
            if got == sha:
                continue
            with open(os.path.join(repo, path), "rb") as f:
                data = f.read()
            (corrupt if looks_corrupt(path, data) else changed).append(path)
    return {"corrupt": corrupt, "changed": changed}


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        result = check(repo)
    except CheckError as e:
        print("result: unknown (%s)" % (e,))
        return 0
    for kind in ("corrupt", "changed"):
        for path in result[kind]:
            print("%s %s" % (kind, path))
    print("result: %s" % ("corrupt" if result["corrupt"] else "changed" if result["changed"] else "ok"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
