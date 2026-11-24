#!/usr/bin/env python3
"""Tests for scripts/polydye_files.py: finding add-on files an unsafe shutdown damaged.

This file is part of polydye-klipper (GPLv3).
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import polydye_files  # noqa: E402

# A crash leaves the times git recorded; the tests' rewrites can't keep the change time, so git ignores it.
GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "core.autocrlf=false",
       "-c", "core.trustctime=false"]
FIRMWARE = b"\xe9" + b"\x00" * 100 + b"firmware"


def git(repo, *args):
    return subprocess.run(GIT + ["-C", repo] + list(args), check=True, stdout=subprocess.PIPE).stdout.decode()


class Check(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)
        os.makedirs(os.path.join(self.repo, "klipper"))
        os.makedirs(os.path.join(self.repo, "firmware"))
        self.write("klipper/polydye.py", b"import logging\n\ndef load_config(config):\n    pass\n")
        self.write("README.md", b"PolyDye\n")
        self.write("firmware/polydye.bin", FIRMWARE)
        # Older than the index, as after a real checkout: git then trusts their size and time instead of re-reading
        # them ("racily clean" files written in the index's second are always re-read).
        for rel in ("klipper/polydye.py", "README.md", "firmware/polydye.bin"):
            os.utime(self.path(rel), (1e9, 1e9))
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "add-on")

    def path(self, rel):
        return os.path.join(self.repo, rel)

    def write(self, rel, data):
        with open(self.path(rel), "wb") as f:
            f.write(data)

    def zero_fill(self, rel):
        """What an unsafe shutdown leaves: the size and time git recorded, but zeros instead of the content."""
        st = os.stat(self.path(rel))
        self.write(rel, b"\0" * st.st_size)
        os.utime(self.path(rel), ns=(st.st_atime_ns, st.st_mtime_ns))

    def test_intact(self):
        self.assertEqual(polydye_files.check(self.repo), {"corrupt": [], "changed": []})

    def test_zero_filled_file_git_status_calls_clean(self):
        self.zero_fill("klipper/polydye.py")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")  # why git status isn't enough
        self.assertEqual(polydye_files.check(self.repo), {"corrupt": ["klipper/polydye.py"], "changed": []})

    def test_empty_missing_and_bad_firmware_are_corrupt(self):
        self.write("klipper/polydye.py", b"")
        os.remove(self.path("README.md"))
        self.write("firmware/polydye.bin", b"\x00\x01" + FIRMWARE[2:])
        self.assertEqual(sorted(polydye_files.check(self.repo)["corrupt"]),
                         ["README.md", "firmware/polydye.bin", "klipper/polydye.py"])

    def test_edits_are_changed_not_corrupt(self):
        self.write("klipper/polydye.py", b"# a local edit\n")
        self.write("firmware/polydye.bin", FIRMWARE + b"!")
        self.assertEqual(polydye_files.check(self.repo),
                         {"corrupt": [], "changed": ["firmware/polydye.bin", "klipper/polydye.py"]})

    def test_text_with_nul_bytes_is_corrupt(self):
        self.write("README.md", b"Poly\0\0\0\n")
        self.assertEqual(polydye_files.check(self.repo)["corrupt"], ["README.md"])

    def test_not_a_git_checkout(self):
        shutil.rmtree(self.path(".git"))
        with self.assertRaisesRegex(polydye_files.CheckError, "not a git checkout"):
            polydye_files.check(self.repo)

    def test_command_line_ends_with_the_result(self):
        self.zero_fill("README.md")
        self.write("klipper/polydye.py", b"# edited\n")
        out = io.StringIO()
        sys.argv = ["polydye_files.py", self.repo]
        with redirect_stdout(out):
            self.assertEqual(polydye_files.main(), 0)
        self.assertEqual(out.getvalue().splitlines(),
                         ["corrupt README.md", "changed klipper/polydye.py", "result: corrupt"])
        shutil.rmtree(self.path(".git"))
        out = io.StringIO()
        with redirect_stdout(out):
            polydye_files.main()
        self.assertEqual(out.getvalue(), "result: unknown (not a git checkout)\n")


if __name__ == "__main__":
    unittest.main()
