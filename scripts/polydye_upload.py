#!/usr/bin/env python3
"""Upload ink passes to the PolyDye board over its USB data port.

The board appears as two serial ports; the data port is the second one
(/dev/serial/by-id/usb-PolyDye_PolyDye_ink_controller_<MAC>-if02 on Linux).

  polydye_upload.py info
  polydye_upload.py list
  polydye_upload.py clear
  polydye_upload.py delete LAYER PASS
  polydye_upload.py put --layer 0 --pass 0 --columns 600 --pattern all [--cart color] [--once]
  polydye_upload.py job FILE.pdj
  polydye_upload.py make-test-job FILE.pdj --layers 1 --passes 2 --columns 600 --pattern alternate
  polydye_upload.py firmware-info FILE.bin
  polydye_upload.py firmware FILE.bin [--force]

`firmware` updates the board over USB: it writes the image to the board's spare firmware slot, reboots the board
into it, waits for it to come back and confirms it. If the new firmware doesn't come back working, the board returns
to the previous one by itself. The board leaves USB and comes back when it reboots, so stop Klipper first
(`sudo systemctl stop klipper`) and start it again afterwards: on Linux this refuses to start while a program has the
board's Klipper port open, because a Raspberry Pi 3 has frozen when the board came back with ports open. The PolyDye
page in Mainsail does both for you.

Job files (.pdj, version 0): b"PDJ0", u32 pass count, then per pass a u32 length and a PUT payload
(layer u16, pass u16, cart u8, flags u8, columns u16, columns * 42 bytes of nozzle data). Integers are
little-endian. The wire protocol is described in README.md ("Data port").

This file is part of polydye-klipper (GPLv3).
"""
import argparse
import glob
import hashlib
import json
import math
import os
import struct
import sys
import time
import zlib

NOZDATA_SIZE = 42
CARTS = {"color": 0, "black": 1}
FLAG_ONCE = 1
T_HELLO, T_PUT, T_DELETE, T_CLEAR, T_LIST, T_OK, T_ERR = 0x01, 0x10, 0x11, 0x12, 0x13, 0x80, 0xFF
T_FW_BEGIN, T_FW_DATA, T_FW_END, T_FW_ABORT, T_FW_CONFIRM, T_REBOOT = 0x20, 0x21, 0x22, 0x23, 0x24, 0x25
FW_CHUNK = 4096
# Firmware goes at about the FT232 flashing rate: full USB speed for a whole image upset a Raspberry Pi 3's USB
# (lost data, then a lock-up when the board restarted). An update takes about 25 s.
FW_RATE = 48 * 1024
FW_PROJECT = "polydye"
ESP32S3_CHIP_ID = 9


class BoardError(Exception):
    pass


class DataPort:
    def __init__(self, port, timeout=10.):
        import serial  # pyserial
        self.ser = serial.Serial(port, 115200, timeout=timeout)  # opening raises DTR, which the board needs
        self.seq = 0

    def close(self):
        try:
            self.ser.close()
        except OSError:
            pass  # the board may already have dropped off USB (after REBOOT)

    def request(self, ftype, payload=b"", retries=0, log=None):
        """Sends one request and returns the reply's JSON. On a timeout, resends it up to `retries` times; only
        use retries for requests the board treats as idempotent (the firmware update ones)."""
        self.seq = (self.seq + 1) & 0xff
        head = struct.pack("<BBI", ftype, self.seq, len(payload))
        crc = zlib.crc32(payload, zlib.crc32(head))
        frame = b"PD" + head + payload + struct.pack("<I", crc)
        for attempt in range(retries + 1):
            self.ser.write(frame)
            self.ser.flush()
            try:
                return self._reply()
            except BoardError as e:
                if "timed out" not in str(e) or attempt == retries:
                    raise
                # The board drops a frame it doesn't receive in full within 3 s; by now it has, so resend.
                say(log, "no reply from the board; resending (USB data lost?)")
                self.ser.reset_input_buffer()

    def _read(self, n):
        data = self.ser.read(n)
        if len(data) != n:
            raise BoardError("timed out waiting for the board")
        return data

    def _reply(self):
        # A reply for an earlier request (e.g. the late original after a resend) is stale: skip it, within the
        # usual timeout.
        deadline = time.time() + (self.ser.timeout or 10.)
        while True:
            if time.time() > deadline:
                raise BoardError("timed out waiting for the board")
            prev = b""
            while True:
                c = self._read(1)
                if prev == b"P" and c == b"D":
                    break
                prev = c
            head = self._read(6)
            ftype, seq, length = struct.unpack("<BBI", head)
            payload = self._read(length)
            (crc,) = struct.unpack("<I", self._read(4))
            if zlib.crc32(payload, zlib.crc32(head)) != crc:
                raise BoardError("reply CRC mismatch")
            if seq == self.seq:
                break
        body = json.loads(payload.decode() or "{}")
        if ftype == T_ERR:
            raise BoardError(body.get("error", "error"))
        return body

    def hello(self):
        return self.request(T_HELLO)

    def put(self, layer, pass_num, cart, flags, data):
        if len(data) % NOZDATA_SIZE:
            raise ValueError("pass data must be a whole number of %d-byte columns" % NOZDATA_SIZE)
        columns = len(data) // NOZDATA_SIZE
        return self.request(T_PUT, struct.pack("<HHBBH", layer, pass_num, cart, flags, columns) + data)

    def put_payload(self, payload):
        return self.request(T_PUT, payload)


def pattern_data(name, columns):
    full, empty = b"\xff" * NOZDATA_SIZE, b"\x00" * NOZDATA_SIZE
    if name == "all":
        return full * columns
    if name == "none":
        return empty * columns
    if name == "alternate":
        return b"".join(full if i % 2 == 0 else empty for i in range(columns))
    raise ValueError("unknown pattern %r (all, none, alternate)" % name)


def read_job(path):
    with open(path, "rb") as f:
        blob = f.read()
    if blob[:4] != b"PDJ0":
        raise ValueError("%s is not a PolyDye job file (version 0)" % path)
    (count,) = struct.unpack_from("<I", blob, 4)
    pos, records = 8, []
    for _ in range(count):
        (length,) = struct.unpack_from("<I", blob, pos)
        records.append(blob[pos + 4:pos + 4 + length])
        pos += 4 + length
    return records


def write_job(path, records):
    with open(path, "wb") as f:
        f.write(b"PDJ0" + struct.pack("<I", len(records)))
        for r in records:
            f.write(struct.pack("<I", len(r)) + r)


def image_info(blob):
    """Version and checks of an ESP-IDF app image (the firmware's polydye.bin). Raises ValueError if unusable."""
    # Image header (24 bytes), first segment header (8), then esp_app_desc_t: magic, secure_version, 2 reserved
    # words, version[32], project_name[32], ...
    if len(blob) < 112 or blob[0] != 0xE9:
        raise ValueError("not an ESP32 firmware image")
    (chip_id,) = struct.unpack_from("<H", blob, 12)
    if chip_id != ESP32S3_CHIP_ID:
        raise ValueError("firmware image is for another chip (id %d, the board is an ESP32-S3)" % chip_id)
    (magic,) = struct.unpack_from("<I", blob, 32)
    if magic != 0xABCD5432:
        raise ValueError("firmware image has no app description")
    version = blob[48:80].split(b"\0", 1)[0].decode(errors="replace")
    project = blob[80:112].split(b"\0", 1)[0].decode(errors="replace")
    if project != FW_PROJECT:
        raise ValueError("not a PolyDye firmware image (project %r)" % project)
    return {"version": version, "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}


def say(log, text):
    if log:
        log(text)


def port_present(port):
    if os.path.exists(port):
        return True
    from serial.tools import list_ports  # Windows COM ports aren't files
    return any(p.device == port for p in list_ports.comports())


def klipper_port(data_port):
    """The board's Klipper port (its -if00 serial port) for its data port (-if02), or None if that can't be told
    (only /dev/serial/by-id names, or a tty one of them points at, can be matched)."""
    if data_port.endswith("-if02"):
        return data_port[:-len("02")] + "00"
    try:
        target = os.path.realpath(data_port)
        for p in glob.glob("/dev/serial/by-id/*PolyDye*-if02"):
            if os.path.realpath(p) == target:
                return p[:-len("02")] + "00"
    except OSError:
        pass
    return None


def port_holders(port, proc="/proc"):
    """[(pid, program name)] of the processes that have `port` open, as far as /proc shows (Linux; processes of
    other users aren't visible). Empty if `port` doesn't exist or there's no /proc."""
    if not port or not os.path.exists(port):
        return []
    target = os.path.realpath(port)
    holders = []
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        fd_dir = os.path.join(proc, pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(os.path.join(fd_dir, fd)) != target:
                    continue
            except OSError:
                continue
            try:
                with open(os.path.join(proc, pid, "comm")) as f:
                    name = f.read().strip()
            except OSError:
                name = "?"
            holders.append((int(pid), name))
            break
    return holders


def sync_disks():
    """Writes everything pending to disk (POSIX), before a step that has frozen a Raspberry Pi 3."""
    if hasattr(os, "sync"):
        os.sync()


def update_firmware(port, blob, force=False, log=print, confirm=True, rate=FW_RATE):
    """Writes `blob` to the board, reboots into it and confirms it. Returns the new version."""
    if not (isinstance(rate, (int, float)) and math.isfinite(rate) and rate > 0):
        raise ValueError("the transfer rate must be a positive number of bytes per second")
    info = image_info(blob)
    holders = port_holders(klipper_port(port))
    if holders:
        raise BoardError("%s has the board's Klipper port open: stop Klipper first (sudo systemctl stop klipper). "
                         "The board leaves USB and comes back when it restarts into the new firmware"
                         % ", ".join("%s (pid %d)" % (name, pid) for pid, name in holders))
    board = DataPort(port)
    try:
        hello = board.hello()
        if hello.get("protocol", 1) < 2:
            raise BoardError("the board's firmware (%s) can't update over USB yet; flash it once with the FT232"
                             % hello.get("firmware"))
        if hello.get("app_state") == "pending":
            board.request(T_FW_CONFIRM, retries=2, log=log)  # left unconfirmed by an interrupted update
        if hello["firmware"] == info["version"] and not force:
            say(log, "the board already runs %s" % info["version"])
            return info["version"]
        say(log, "updating the board from %s to %s (%d kB)" % (hello["firmware"], info["version"],
                                                               len(blob) // 1024))
        board.ser.timeout = 60.  # erasing the slot takes a few seconds
        target = board.request(T_FW_BEGIN, struct.pack("<I", len(blob)) + bytes.fromhex(info["sha256"]),
                               retries=2, log=log)
        board.ser.timeout = 5.  # more than the board's 3 s frame timeout, so a resend finds it ready
        try:
            last = -1
            due = time.time()
            for off in range(0, len(blob), FW_CHUNK):
                if due > time.time():
                    time.sleep(due - time.time())
                # Blocks start at least one slot apart; time lost to a resend is not made up with a burst.
                due = max(due, time.time()) + FW_CHUNK / float(rate)
                board.request(T_FW_DATA, struct.pack("<I", off) + blob[off:off + FW_CHUNK], retries=3, log=log)
                pct = (off + FW_CHUNK) * 100 // len(blob)
                if pct // 5 != last // 5:
                    say(log, "progress: %d%%" % min(pct, 100))
                    last = pct
            done = board.request(T_FW_END, retries=2, log=log)
        except BaseException:
            try:
                board.request(T_FW_ABORT)
            except Exception:
                pass
            raise
        say(log, "written to %s and checked; rebooting the board" % target["partition"])
        sync_disks()
        try:
            board.request(T_REBOOT)
        except (BoardError, OSError) as e:  # pyserial's SerialException is an OSError
            if isinstance(e, BoardError) and "timed out" not in str(e):
                raise
            # The board can restart (and drop off USB) before its reply is read; see whether it comes back.
    finally:
        board.close()

    # The board comes back as the same USB device. If the new firmware doesn't work, it returns to the old one
    # within about two minutes.
    deadline = time.time() + 180.
    while port_present(port) and time.time() < deadline - 170.:
        time.sleep(0.2)
    hello = None
    while time.time() < deadline:
        time.sleep(1.)
        if not port_present(port):
            continue
        try:
            board = DataPort(port, timeout=3.)
        except Exception:
            continue
        try:
            hello = board.hello()
            if hello["firmware"] != done["version"] or hello.get("partition") != target["partition"]:
                break
            if not confirm:
                say(log, "the board runs %s, NOT confirmed: it goes back to the previous firmware within %d s"
                    % (done["version"], 120))
                return done["version"]
            board.request(T_FW_CONFIRM, retries=2, log=log)
            say(log, "the board runs %s; confirmed" % done["version"])
            return done["version"]
        except Exception:
            continue
        finally:
            board.close()
    if hello is None:
        raise BoardError("the board didn't come back after the update; check its USB cable and power")
    raise BoardError("the new firmware didn't start properly; the board went back to %s (%s)"
                     % (hello["firmware"], hello.get("partition")))


def default_port():
    ports = sorted(glob.glob("/dev/serial/by-id/*PolyDye*-if02"))
    if not ports:
        raise SystemExit("no PolyDye data port found; pass --port")
    return ports[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="data port (default: the PolyDye -if02 port under /dev/serial/by-id)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    sub.add_parser("list")
    sub.add_parser("clear")
    d = sub.add_parser("delete")
    d.add_argument("layer", type=int)
    d.add_argument("pass_num", metavar="pass", type=int)
    p = sub.add_parser("put", help="upload a test pattern as one pass")
    p.add_argument("--layer", type=int, required=True)
    p.add_argument("--pass", dest="pass_num", type=int, required=True)
    p.add_argument("--columns", type=int, required=True)
    p.add_argument("--pattern", default="all")
    p.add_argument("--cart", choices=CARTS, default="color")
    p.add_argument("--once", action="store_true", help="delete the pass after it has been printed")
    j = sub.add_parser("job", help="upload every pass in a .pdj job file")
    j.add_argument("file")
    m = sub.add_parser("make-test-job", help="write a .pdj file of test-pattern passes (no board needed)")
    m.add_argument("file")
    m.add_argument("--layers", type=int, default=1)
    m.add_argument("--passes", type=int, default=1)
    m.add_argument("--columns", type=int, default=600)
    m.add_argument("--pattern", default="alternate")
    m.add_argument("--cart", choices=CARTS, default="color")
    fi = sub.add_parser("firmware-info", help="show a firmware image's version (no board needed)")
    fi.add_argument("file")
    fw = sub.add_parser("firmware", help="update the board's firmware over USB")
    fw.add_argument("file")
    fw.add_argument("--force", action="store_true", help="update even if the board already runs this version")
    fw.add_argument("--no-confirm", action="store_true", help=argparse.SUPPRESS)  # tests the automatic rollback
    fw.add_argument("--rate", type=float, default=FW_RATE / 1024., help="transfer rate in kB/s (default %(default)g)")
    args = ap.parse_args()

    if args.cmd == "firmware-info":
        try:
            with open(args.file, "rb") as f:
                print(json.dumps(image_info(f.read()), indent=2))
        except (ValueError, OSError) as e:
            print("error: %s" % e, file=sys.stderr)
            return 1
        return 0
    if args.cmd == "firmware":
        try:
            with open(args.file, "rb") as f:
                blob = f.read()
            update_firmware(args.port or default_port(), blob, args.force, log=lambda s: print(s, flush=True),
                            confirm=not args.no_confirm, rate=args.rate * 1024.)
        except (BoardError, ValueError, OSError) as e:
            print("error: %s" % e, file=sys.stderr, flush=True)
            return 1
        return 0

    if args.cmd == "make-test-job":
        data = pattern_data(args.pattern, args.columns)
        records = [struct.pack("<HHBBH", layer, n, CARTS[args.cart], 0, args.columns) + data
                   for layer in range(args.layers) for n in range(args.passes)]
        write_job(args.file, records)
        print("wrote %d passes to %s" % (len(records), args.file))
        return 0

    board = DataPort(args.port or default_port())
    try:
        if args.cmd == "info":
            print(json.dumps(board.hello(), indent=2))
        elif args.cmd == "list":
            passes = board.request(T_LIST)["passes"]
            print("%d passes" % len(passes))
            for layer, pass_num, columns, cart, flags in passes:
                print("  layer %d pass %d: %d columns, %s%s" % (layer, pass_num, columns,
                      "black" if cart else "color", ", once" if flags & FLAG_ONCE else ""))
        elif args.cmd == "clear":
            print("deleted %d passes" % board.request(T_CLEAR)["deleted"])
        elif args.cmd == "delete":
            board.request(T_DELETE, struct.pack("<HH", args.layer, args.pass_num))
            print("deleted")
        elif args.cmd == "put":
            r = board.put(args.layer, args.pass_num, CARTS[args.cart], FLAG_ONCE if args.once else 0,
                          pattern_data(args.pattern, args.columns))
            print("stored layer %d pass %d (%d columns)" % (r["stored"][0], r["stored"][1], r["columns"]))
        elif args.cmd == "job":
            records = read_job(args.file)
            start, total = time.time(), 0
            for rec in records:
                r = board.put_payload(rec)
                total += len(rec)
                print("stored layer %d pass %d (%d columns)" % (r["stored"][0], r["stored"][1], r["columns"]))
            dt = time.time() - start
            print("%d passes, %.1f kB in %.1f s (%.0f kB/s)" % (len(records), total / 1024., dt,
                                                               total / 1024. / max(dt, 1e-3)))
    except BoardError as e:
        print("board: %s" % e, file=sys.stderr)
        return 1
    finally:
        board.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
