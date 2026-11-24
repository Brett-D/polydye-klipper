#!/usr/bin/env python3
"""Tests for the firmware image checks in scripts/polydye_upload.py (no board needed).

This file is part of polydye-klipper (GPLv3).
"""
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import polydye_upload  # noqa: E402


def fake_image(version=b"03e9e89", project=b"polydye", chip_id=9, desc_magic=0xABCD5432):
    blob = bytearray(4096)
    blob[0] = 0xE9
    struct.pack_into("<H", blob, 12, chip_id)
    struct.pack_into("<I", blob, 32, desc_magic)
    blob[48:48 + len(version)] = version
    blob[80:80 + len(project)] = project
    return bytes(blob)


class ImageInfo(unittest.TestCase):
    def test_reads_version(self):
        info = polydye_upload.image_info(fake_image())
        self.assertEqual(info["version"], "03e9e89")
        self.assertEqual(info["size"], 4096)
        self.assertEqual(len(info["sha256"]), 64)

    def test_refuses_other_images(self):
        for blob, why in ((b"\x00" * 4096, "not an ESP32"), (fake_image(chip_id=0), "another chip"),
                          (fake_image(desc_magic=0), "no app description"),
                          (fake_image(project=b"other"), "not a PolyDye")):
            with self.assertRaisesRegex(ValueError, why):
                polydye_upload.image_info(blob)


class UpdateFirmware(unittest.TestCase):
    def test_bad_rate_refused_before_touching_the_board(self):
        # The port doesn't exist: reaching it would raise OSError instead.
        for rate in (0, -1., float("nan"), float("inf")):
            with self.assertRaisesRegex(ValueError, "transfer rate"):
                polydye_upload.update_firmware("/nonexistent/port", fake_image(), rate=rate, log=None)

    def test_refused_while_klipper_has_the_board_open(self):
        # The board leaves USB and comes back when it restarts into the new firmware: not with Klipper's port open.
        port = "/dev/serial/by-id/usb-PolyDye_PolyDye_ink_controller_E4B063-if02"
        with mock.patch.object(polydye_upload, "port_holders", return_value=[(812, "klippy")]) as holders:
            with self.assertRaisesRegex(polydye_upload.BoardError, r"klippy \(pid 812\).*stop Klipper first"):
                polydye_upload.update_firmware(port, fake_image(), log=None)
        holders.assert_called_once_with(port[:-2] + "00")


class ConfirmAfterReboot(unittest.TestCase):
    def confirm(self, hello, present=(True, False, True), timeout=180.):
        """The board still there, gone, then back; it answers HELLO with `hello`. Returns (result, requests)."""
        present, requests = iter(present), []

        class Board:
            def __init__(self, port, timeout=10.):
                pass

            def hello(self):
                return hello

            def request(self, ftype, *args, **kw):
                requests.append(ftype)

            def close(self):
                pass
        with mock.patch.object(polydye_upload, "port_present", lambda port: next(present, True)), \
                mock.patch.object(polydye_upload, "DataPort", Board), \
                mock.patch.object(polydye_upload.time, "sleep", lambda s: None):
            try:
                return polydye_upload.confirm_after_reboot("/dev/x-if02", "9ba5def", "ota_1", log=None,
                                                           timeout=timeout), requests
            except polydye_upload.BoardError as e:
                return e, requests

    def test_confirms_the_new_firmware(self):
        self.assertEqual(self.confirm({"firmware": "9ba5def", "partition": "ota_1"}),
                         ("9ba5def", [polydye_upload.T_FW_CONFIRM]))

    def test_rolled_back(self):
        err, requests = self.confirm({"firmware": "cd4926d", "partition": "ota_0"})
        self.assertIsInstance(err, polydye_upload.RolledBack)
        self.assertIn("went back to cd4926d (ota_0)", str(err))
        self.assertEqual(requests, [])

    def test_not_back(self):
        err, _ = self.confirm({}, present=[False] * 1000000, timeout=0.05)
        self.assertNotIsInstance(err, polydye_upload.RolledBack)
        self.assertIn("didn't come back", str(err))


class KlipperPort(unittest.TestCase):
    def test_from_the_data_port(self):
        self.assertEqual(polydye_upload.klipper_port("/dev/serial/by-id/usb-PolyDye_x-if02"),
                         "/dev/serial/by-id/usb-PolyDye_x-if00")
        self.assertIsNone(polydye_upload.klipper_port("/nonexistent/ttyACM9"))

    def test_holders_from_proc(self):
        # A fake /proc: pid 812 (klippy) has the port open, pid 900 something else, pid 950 can't be read.
        proc = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, proc)
        port = os.path.join(proc, "ttyACM0")
        open(port, "w").close()
        for pid, name, target in (("812", "klippy", port), ("900", "bash", "/dev/null"), ("950", None, None)):
            os.makedirs(os.path.join(proc, pid, "fd") if name else os.path.join(proc, pid))
            if name:
                with open(os.path.join(proc, pid, "comm"), "w") as f:
                    f.write(name + "\n")
                os.symlink(target, os.path.join(proc, pid, "fd", "3"))
        self.assertEqual(polydye_upload.port_holders(port, proc=proc), [(812, "klippy")])
        self.assertEqual(polydye_upload.port_holders(os.path.join(proc, "gone"), proc=proc), [])
        self.assertEqual(polydye_upload.port_holders(None, proc=proc), [])

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "needs Linux /proc")
    def test_finds_this_process(self):
        with tempfile.NamedTemporaryFile() as f:
            self.assertIn(os.getpid(), [pid for pid, _ in polydye_upload.port_holders(f.name)])


def frame(ftype, seq, body):
    payload = body.encode()
    head = struct.pack("<BBI", ftype, seq, len(payload))
    return b"PD" + head + payload + struct.pack("<I", zlib.crc32(payload, zlib.crc32(head)))


class FakeSerial:
    timeout = 1.

    def __init__(self, data):
        self.data = data

    def read(self, n):
        out, self.data = self.data[:n], self.data[n:]
        return out


class Replies(unittest.TestCase):
    def test_stale_reply_after_a_resend_is_skipped(self):
        port = polydye_upload.DataPort.__new__(polydye_upload.DataPort)
        port.seq = 7
        port.ser = FakeSerial(frame(polydye_upload.T_OK, 6, '{"old":1}') + frame(polydye_upload.T_OK, 7, '{"new":1}'))
        self.assertEqual(port._reply(), {"new": 1})


if __name__ == "__main__":
    unittest.main()
