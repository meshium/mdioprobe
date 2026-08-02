"""Passive capture of somebody else's Clause-22 traffic."""

import mdioprobe

from cli.parser import integer
from cli.registry import CommandError, command

_OP = {1: "W", 2: "R"}

# IEEE 802.3 Clause 22 register names. Only shown on request — see the help.
_NAMES = {
    0x00: "BMCR", 0x01: "BMSR", 0x02: "PHYID1", 0x03: "PHYID2",
    0x04: "ANAR", 0x05: "ANLPAR", 0x06: "ANER", 0x07: "ANNPTR",
    0x08: "ANLPRNP", 0x09: "GBCR", 0x0A: "GBSR",
    0x0D: "MMDCTRL", 0x0E: "MMDDATA", 0x0F: "EXTSTATUS",
}


# Both of these bound the summary's memory, and both are reachable rather
# than theoretical.
#
# A register can carry an unbounded number of distinct values — a Realtek
# firmware load pushes a whole image through one data register, so keeping
# every value seen would be a dictionary the size of the firmware. Past this
# many the count stops being interesting anyway; "more than 8" and "4127"
# say the same thing to a reader.
#
# The number of (phy, register) pairs is bounded by the protocol at 32x32,
# but 1024 entries would not fit in the GC heap, and garbled frames reach
# that spread easily: clocking MDC faster than the wiring allows produces
# plausible-looking frames at scattered addresses, which was measured on
# this bench and not imagined.
_MAX_VALUES = 8
_MAX_REGS = 48


class _Tally:
    """Counts per (phy, register), for the summary.

    Kept instead of the frames themselves: the frames are bounded only by
    the count asked for, and on a bus doing something substantial — a
    firmware load, a full register dump — that is a lot of frames.
    """

    def __init__(self):
        self.by_reg = {}
        self.dropped = 0        # frames on registers past _MAX_REGS

    def add(self, frame):
        key = (frame["phy"], frame["reg"])
        entry = self.by_reg.get(key)

        if entry is None:
            if len(self.by_reg) >= _MAX_REGS:
                self.dropped += 1
                return
            entry = [0, 0, [], False]   # reads, writes, values, overflowed
            self.by_reg[key] = entry

        if frame["op"] == 2:
            entry[0] += 1
        else:
            entry[1] += 1

        values = entry[2]
        data = frame["data"]

        if data not in values:
            if len(values) < _MAX_VALUES:
                values.append(data)
            else:
                entry[3] = True

    def report(self, ctx, decode):
        for key in sorted(self.by_reg):
            phy, reg = key
            reads, writes, values, overflowed = self.by_reg[key]
            counts = []
            if reads:
                counts.append("{} read{}".format(reads, "" if reads == 1 else "s"))
            if writes:
                counts.append("{} write{}".format(writes, "" if writes == 1 else "s"))

            # One value seen many times is the signature of a poll; many is
            # something actually being written through. Worth saying which.
            if overflowed:
                detail = "more than {} distinct values".format(_MAX_VALUES)
            elif len(values) == 1:
                detail = "always 0x{:04x}".format(values[0])
            else:
                detail = "{} distinct values".format(len(values))

            ctx.out.line("  phy {:02d}  reg 0x{:02x}{}  {}, {}",
                         phy, reg,
                         "  {:<9}".format(_NAMES.get(reg, "")) if decode else "",
                         ", ".join(counts), detail)

        if self.dropped:
            ctx.out.line("  {} more frame(s) on registers past the first {} — "
                         "not counted", self.dropped, _MAX_REGS)


@command("cap", category="capture", syntax="cap [count] [decode]",
         summary="capture frames from the bus (default 10)",
         detail="""
Listens only — nothing is driven. Capture needs a known bus rail, so it will
refuse until a target is detected or the rail is declared with `bus force`.

Runs of the same frame are folded into one line with a repeat count, and a
summary at the end says which registers were touched, how often, and whether
the value ever changed. On a polled bus that is most of what you want to
know, and it is what the raw list makes hardest to see.

`decode` adds the IEEE Clause-22 register names. It is off by default
because those names are only right for a device that follows the standard
map: a Marvell switch in multi-chip mode uses registers 0 and 1 as its
command and data pair, and calling them BMCR and BMSR there would be a
confident lie.

Frames arrive faster than this loop can print them: on a bus polled at a few
thousand frames a second the ring will overrun while a large count is being
formatted. Small counts are exact; large ones sample.

A running command cannot be interrupted on this firmware — Ctrl-C is only
read between commands — so the count is the only bound, and a large one on
a busy bus has to be waited out. Redirected output stops on its own when
the volume is nearly full, because filling it is worse than losing the tail
of a capture: the file is closed when the command returns, so an end that
is not clean strands the clusters with nothing pointing at them.
""")
def cmd_cap(ctx, args):
    decode = False

    if args and args[-1] == "decode":
        decode = True
        args = args[:-1]
    if len(args) > 1:
        raise CommandError("usage: cap [count] [decode]")

    count = integer(args[0], "count") if args else 10
    if count < 1:
        raise CommandError("count must be at least 1")

    mdioprobe.capture_stop()
    mdioprobe.capture_start(count)
    try:
        render(ctx, decode)
    finally:
        # Whatever went wrong — Ctrl-C, running out of memory formatting a
        # line — the capture must not be left running. It would keep the
        # activity indication lit and keep filling the ring behind whatever
        # command comes next.
        mdioprobe.capture_stop()


def render(ctx, decode=False):
    """Drain a capture that is already running, and print it.

    Separate from the command so it can be pointed at a ring somebody else
    filled — which is the only way to exercise the formatting on this bench,
    where nothing but the probe itself drives the wire.
    """
    tally = _Tally()
    seen = 0
    bad = 0
    run = None          # the frame being repeated
    run_at = 0          # index of the first of the run
    run_len = 0

    def flush():
        if run is None:
            return
        ctx.out.line("{:5d}  {}  phy {:02d}  reg 0x{:02x}{}  0x{:04x}{}{}",
                     run_at, _OP.get(run["op"], "?"), run["phy"], run["reg"],
                     "  {:<9}".format(_NAMES.get(run["reg"], "")) if decode else "",
                     run["data"],
                     "  x{}".format(run_len) if run_len > 1 else "",
                     "  FRAMING ERROR" if run["err"] else "")

    # KeyboardInterrupt cannot actually arrive here on this firmware — the
    # console path in use never scans input for the interrupt character, so
    # Ctrl-C only takes effect between commands. Caught anyway, because that
    # is a Kconfig away from changing and because losing everything counted
    # so far would be the wrong response to it.
    interrupted = False
    stopped = None

    try:
        while mdioprobe.capture_active():
            frame = mdioprobe.capture_next(1000)
            if frame is None:
                break
            seen += 1
            if frame["err"]:
                bad += 1
            tally.add(frame)

            if run is not None and (frame["op"] == run["op"] and
                                    frame["phy"] == run["phy"] and
                                    frame["reg"] == run["reg"] and
                                    frame["data"] == run["data"] and
                                    frame["err"] == run["err"]):
                run_len += 1
                continue
            flush()
            run, run_at, run_len = frame, seen, 1

            if ctx.out.out_of_room():
                stopped = "the volume is nearly full"
                break
    except KeyboardInterrupt:
        interrupted = True
    flush()

    if seen == 0:
        ctx.out.line("nothing captured — is anything driving the bus?")
        return

    ctx.out.line("")
    ctx.out.line("{} frame{}{}{}", seen, "" if seen == 1 else "s",
                 ", {} with framing errors".format(bad) if bad else "",
                 " (interrupted)" if interrupted else
                 (" (stopped: {})".format(stopped) if stopped else ""))
    tally.report(ctx, decode)


@command("mdc", category="capture", syntax="mdc [timeout_ms]",
         summary="measure the MDC frequency, passively",
         detail="""
Listens only: if nothing else drives MDC this reports that the bus is idle
rather than clocking the line itself, because a probe should not become a
master by accident.

Good to about three significant figures. The board has no crystal, so the
time base is HSI16, and at 170 MHz one MDC period at 6.25 MHz is only 27
timer ticks.
""")
def cmd_mdc(ctx, args):
    timeout = integer(args[0], "timeout") if args else 2000

    try:
        hz = mdioprobe.mdc_freq(timeout)
    except OSError:
        ctx.out.line("bus idle — no MDC seen in {} ms", timeout)
        return
    ctx.out.line("MDC ~ {}.{:03d} MHz", hz // 1000000, (hz // 1000) % 1000)
