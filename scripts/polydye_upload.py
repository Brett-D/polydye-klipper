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

Job files (.pdj, version 0): b"PDJ0", u32 pass count, then per pass a u32 length and a PUT payload
(layer u16, pass u16, cart u8, flags u8, columns u16, columns * 42 bytes of nozzle data). Integers are
little-endian. The wire protocol is described in README.md ("Data port").

This file is part of polydye-klipper (GPLv3).
"""
import argparse
import glob
import json
import struct
import sys
import time
import zlib

NOZDATA_SIZE = 42
CARTS = {"color": 0, "black": 1}
FLAG_ONCE = 1
T_HELLO, T_PUT, T_DELETE, T_CLEAR, T_LIST, T_OK, T_ERR = 0x01, 0x10, 0x11, 0x12, 0x13, 0x80, 0xFF


class BoardError(Exception):
    pass


class DataPort:
    def __init__(self, port, timeout=10.):
        import serial  # pyserial
        self.ser = serial.Serial(port, 115200, timeout=timeout)  # opening raises DTR, which the board needs
        self.seq = 0

    def close(self):
        self.ser.close()

    def request(self, ftype, payload=b""):
        self.seq = (self.seq + 1) & 0xff
        head = struct.pack("<BBI", ftype, self.seq, len(payload))
        crc = zlib.crc32(payload, zlib.crc32(head))
        self.ser.write(b"PD" + head + payload + struct.pack("<I", crc))
        self.ser.flush()
        return self._reply()

    def _read(self, n):
        data = self.ser.read(n)
        if len(data) != n:
            raise BoardError("timed out waiting for the board")
        return data

    def _reply(self):
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
        if seq != self.seq:
            raise BoardError("reply for request %d, expected %d" % (seq, self.seq))
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
    args = ap.parse_args()

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
