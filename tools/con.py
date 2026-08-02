#!/usr/bin/env python3
"""Minimal CDC ACM console driver for the mdioprobe_v2 bench.

Waits for the device node, opens it (which asserts DTR — the firmware has
CONFIG_SHELL_BACKEND_SERIAL_CHECK_DTR=y, so the boot log is held back until
then), dumps everything with timestamps, and optionally sends commands.
"""
import argparse
import glob
import re
import sys
import time

import serial

# The shell redraws its prompt with colour codes on every log line; strip the
# escapes and the leading prompt so the transcript stays readable.
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PROMPT = re.compile(r"^(uart:~\$ )+")


def clean(text):
    return PROMPT.sub("", ANSI.sub("", text)).rstrip("\r")


def find_port(pattern, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
        time.sleep(0.1)
    return None


def drain(ser, seconds, tag=""):
    """Read for `seconds`, echoing timestamped lines. Returns raw text."""
    out = bytearray()
    line = bytearray()
    t0 = time.time()
    deadline = t0 + seconds
    while time.time() < deadline:
        try:
            chunk = ser.read(4096)
        except (serial.SerialException, OSError) as exc:
            # Expected on `kernel reboot`: the reset tears USB enumeration down.
            print("[%7.3f]%s -- port went away: %s" % (time.time() - t0, tag, exc))
            break
        if not chunk:
            continue
        out += chunk
        line += chunk
        while b"\n" in line:
            one, _, line = line.partition(b"\n")
            text = clean(one.decode("utf-8", "replace"))
            if text:
                print("[%7.3f]%s %s" % (time.time() - t0, tag, text))
                sys.stdout.flush()
    if line:
        text = clean(line.decode("utf-8", "replace"))
        if text:
            print("[%7.3f]%s %s" % (time.time() - t0, tag, text))
    return out.decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/cu.usbmodem*")
    ap.add_argument("--wait", type=float, default=15.0,
                    help="seconds to wait for the device node")
    ap.add_argument("--banner", type=float, default=3.0,
                    help="seconds to read before sending anything")
    ap.add_argument("--settle", type=float, default=1.5,
                    help="seconds to read after each command")
    ap.add_argument("cmd", nargs="*")
    args = ap.parse_args()

    port = find_port(args.port, args.wait)
    if not port:
        print("!! no device matching %s after %.1f s" % (args.port, args.wait))
        return 1
    print("== port: %s" % port)

    # A freshly enumerated CDC ACM node can need a moment before open() works.
    ser = None
    for _ in range(40):
        try:
            ser = serial.Serial(port, 115200, timeout=0.2)
            break
        except (serial.SerialException, OSError) as exc:
            last = exc
            time.sleep(0.25)
    if ser is None:
        print("!! open failed: %s" % last)
        return 1

    print("== opened, DTR asserted; reading boot output")
    drain(ser, args.banner)

    for cmd in args.cmd:
        print("== send: %s" % cmd)
        ser.write((cmd + "\r\n").encode())
        ser.flush()
        drain(ser, args.settle, tag=" >")

    ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
