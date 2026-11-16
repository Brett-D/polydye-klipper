#!/usr/bin/env python3
"""Tests for the firmware image checks in scripts/polydye_upload.py (no board needed).

This file is part of polydye-klipper (GPLv3).
"""
import os
import struct
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
