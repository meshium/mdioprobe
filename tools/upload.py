#!/usr/bin/env python3
"""Put files on the probe's volume without unplugging it.

The sanctioned route is `msd`: the probe hands the volume to the host as a
USB drive, the host copies, and the probe is unplugged to get the console
back. That is the only bulk transfer it has, and it needs a hand on the
cable. This is the other way — drop to the MicroPython REPL over the same
CDC ACM port and write the file from there — which is worth having when the
loop is `./build.sh` and re-test, and worth avoiding otherwise:

  * it is slow. Base64 through a REPL that echoes every byte runs at roughly
    a kilobyte per second, so a helper takes about ten seconds.
  * the console has been seen to wedge on the way back from the REPL, and
    the only cure is a replug — the very thing this avoids. It has not been
    tracked down. If the probe goes silent, unplug it.

So: fine for iterating on a chip helper, not for provisioning a board.

    tools/upload.py build/lib/*.mpy          # onto /flash/lib
    tools/upload.py --dest /flash notes.txt

Only `.mpy` belongs on the volume. Import tries `<name>.py` first and reaches
for `<name>.mpy` only when there is no source beside it, so a stale `.py`
silently cancels the cross-compiled one; this warns when it finds that.
"""
import argparse
import base64
import glob
import os
import re
import sys
import time

import serial

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# What a raised exception looks like coming back from the REPL. It has to be
# looked for: the REPL prints the traceback and then a fresh prompt, so a
# reader waiting for a prompt is satisfied by a command that failed.
FAULT = re.compile(r"Traceback \(most recent call last\)|^\s*\w*Error\b", re.M)

# The REPL prompt, and the CLI's — which one answers says whether the shell
# is still running and has to be left first.
REPL = ">>>"
SHELL = "mdio "

# Base64 per line. The device echoes every byte it receives and its console
# buffer is small, so this is bounded by what one line can be without the
# echo falling behind, not by anything on the host.
B64_CHUNK = 240

# Bytes per write, and the pause after each. Sending a whole line at USB
# speed overruns the input buffer: the symptom is a line coming back with
# runs of '~' in it and the REPL never reaching a prompt.
WIRE_CHUNK = 32
WIRE_PAUSE = 0.02


class DeviceError(Exception):
    """The REPL raised something.

    Nearly always MemoryError. The heap is 48 KB for everything and a loaded
    chip helper holds several of those, so decoding base64 into a fresh
    buffer can fail part-way through a transfer — leaving a short file, a
    prompt, and no other sign that anything went wrong. `use drop` before a
    transfer is the cure; this is how it gets reported instead of guessed.
    """


class Stalled(Exception):
    """The device stopped coming back with a prompt.

    Worth an exception rather than a return value. A silent timeout here does
    not stop the transfer, it desynchronises it: the next line is typed into
    a REPL that is still busy with the last one, the writes that follow land
    somewhere between the two, and what reaches the volume is a file that
    ends early. That failure then surfaces as a checksum mismatch, which
    reads as a corrupt wire rather than as a host that ran ahead.
    """


def read_until(ser, tokens, timeout):
    """Read until one of `tokens` appears, or `timeout` passes."""
    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = ser.read(4096).decode("utf-8", "replace")
        if chunk:
            buf += ANSI.sub("", chunk)
            if any(token in buf for token in tokens):
                return buf
    return buf


def send(ser, line, tokens=(REPL,), timeout=15.0, optional=False):
    # Drop whatever is already buffered before asking for anything. The
    # prompt that ended the last command is still in there, and a reader
    # looking for a prompt would take it for the answer to this one — then
    # return while the device is still busy, so the next line is typed over
    # the top of this one. That desync is silent: every send sees a prompt,
    # every write is acknowledged, and the file still ends early.
    ser.reset_input_buffer()

    raw = (line + "\r\n").encode()
    for i in range(0, len(raw), WIRE_CHUNK):
        ser.write(raw[i:i + WIRE_CHUNK])
        ser.flush()
        time.sleep(WIRE_PAUSE)

    out = read_until(ser, tokens, timeout)
    if not optional and not any(token in out for token in tokens):
        raise Stalled("no prompt after %r; last saw %r"
                      % (line[:40], out[-120:]))

    fault = FAULT.search(out)
    if fault and not optional:
        raise DeviceError(out[fault.start():].strip().splitlines()[0])
    return out


def enter_repl(ser):
    """Get to a REPL prompt from wherever the probe happens to be.

    Ctrl-C first: a previous session may have left the REPL part-way through
    a block, where every line sent is swallowed as more of it.
    """
    ser.write(b"\x03\x03")
    ser.flush()
    time.sleep(0.4)
    ser.read(8192)

    ser.write(b"\r\n")
    ser.flush()
    time.sleep(0.4)
    where = ANSI.sub("", ser.read(8192).decode("utf-8", "replace"))

    if REPL in where:
        return True
    if SHELL in where:
        return REPL in send(ser, "exit", optional=True)
    return False


def leave_repl(ser):
    """Hand the port back to the CLI. Best effort — say so if it fails."""
    return SHELL in send(ser, "import cli.shell; cli.shell.run()",
                         tokens=(SHELL,), timeout=15.0, optional=True)


def put(ser, src, dst):
    data = open(src, "rb").read()
    b64 = base64.b64encode(data).decode()
    # Redrawing one line is for someone watching; in a log it would be a
    # single enormous line, so there it is left to the per-file summary.
    live = sys.stdout.isatty()

    send(ser, "import ubinascii as _b, gc")
    send(ser, "gc.collect()")
    send(ser, "_f = open(%r, 'wb')" % dst)
    for i in range(0, len(b64), B64_CHUNK):
        # Periodically, because each chunk decodes into a fresh buffer and the
        # heap fragments faster than it fills.
        if i and not (i // B64_CHUNK) % 16:
            send(ser, "gc.collect()")
        send(ser, "_f.write(_b.a2b_base64('%s'))" % b64[i:i + B64_CHUNK])
        if live:
            sys.stdout.write("\r   %s  %d%%" %
                             (dst, 100 * min(i + B64_CHUNK, len(b64)) // len(b64)))
            sys.stdout.flush()
    send(ser, "_f.close()")
    if live:
        sys.stdout.write("\r")
    return len(data), sum(data)


def verify(ser, dst, expect):
    """Length and byte sum, computed on the device 256 bytes at a time.

    Reading the file whole would be the obvious check and does not fit: the
    heap is 48 KB for everything, so `open(dst).read()` on an 8 KB helper
    raises MemoryError with the file perfectly intact — a failure that looks
    exactly like a bad transfer.

    So two passes over a generator, which never holds more than one chunk.
    The loop counts chunks rather than reading to end-of-file because
    MicroPython's `iter()` takes one argument: there is no
    `iter(callable, sentinel)` to stop on b'', and asking for it raises
    TypeError inside the generator, where it reads as a mismatch rather than
    as the mistake it is.

    Counting chunks would miss anything past the expected length, so the
    last statement checks that one more byte is not there.
    """
    size, total = expect
    chunks = (size + 255) // 256

    send(ser, "_f = open(%r, 'rb')" % dst)
    send(ser, "_n = sum(len(_f.read(256)) for _ in range(%d))" % chunks)
    send(ser, "_f.close(); _f = open(%r, 'rb')" % dst)
    send(ser, "_s = sum(sum(_f.read(256)) for _ in range(%d))" % chunks)
    out = send(ser, "_t = len(_f.read(1)); _f.close(); print('CHECK', _n, _s, _t)")

    want = "CHECK %d %d 0" % (size, total)
    return want in out, want


def shadowed(ser, dest):
    """Stems on the volume that have both a .py and a .mpy.

    Checked over the whole directory rather than over what was just sent,
    because it goes wrong in both directions: an .mpy landing beside a
    forgotten source, and a source landing beside the .mpy it cancels. Import
    takes `<name>.py` first either way, so the cross-compiled file is simply
    never reached — and nothing about that is visible from the outside.
    """
    out = send(ser, "import os; print('LIST', os.listdir(%r))" % dest)
    match = re.search(r"LIST (\[.*?\])", out)
    if not match:
        return []

    listing = set(re.findall(r"'([^']*)'", match.group(1)))
    return sorted(name[:-4] for name in listing
                  if name.endswith(".mpy") and name[:-4] + ".py" in listing)


def main():
    ap = argparse.ArgumentParser(
        description="copy files onto the probe's volume through the REPL")
    ap.add_argument("--port", default="/dev/cu.usbmodem*")
    ap.add_argument("--dest", default="/flash/lib",
                    help="directory on the probe (default /flash/lib)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the length-and-sum check")
    ap.add_argument("file", nargs="+")
    args = ap.parse_args()

    for src in args.file:
        if not os.path.isfile(src):
            print("!! not a file: %s" % src)
            return 1

    hits = sorted(glob.glob(args.port))
    if not hits:
        print("!! no device matching %s" % args.port)
        return 1
    port = hits[0]

    ser = serial.Serial(port, 115200, timeout=0.2)
    print("== port: %s, %d file(s) -> %s" % (port, len(args.file), args.dest))

    if not enter_repl(ser):
        print("!! no prompt — the console is not answering. Unplug the probe.")
        ser.close()
        return 1

    failed = []
    try:
        for src in args.file:
            name = os.path.basename(src)
            dst = args.dest.rstrip("/") + "/" + name

            expect = put(ser, src, dst)
            if args.no_verify:
                print("   %s  %d bytes, unverified" % (dst, expect[0]))
                continue

            ok, want = verify(ser, dst, expect)
            print("   %s  %d bytes, %s" %
                  (dst, expect[0], "sum ok" if ok else "MISMATCH (wanted " + want + ")"))
            if not ok:
                failed.append(dst)

        send(ser, "del _f", optional=True)
        for stem in shadowed(ser, args.dest):
            print("!! %s/%s.py shadows %s.mpy — import takes the source first, "
                  "and it no longer fits the on-device compiler"
                  % (args.dest.rstrip("/"), stem, stem))
    except Stalled as exc:
        print("!! the probe stopped answering: %s" % exc)
        failed.append("(stalled)")
    except DeviceError as exc:
        print("!! the probe raised: %s" % exc)
        print("   if that is a MemoryError, `use drop` on the probe and retry")
        failed.append("(device error)")
    finally:
        if not leave_repl(ser):
            print("!! could not get back to the CLI; the probe is at the REPL")
        ser.close()

    if failed:
        print("!! %d file(s) did not verify" % len(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
