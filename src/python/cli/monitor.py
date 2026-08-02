"""Polling a set of registers and reporting what changes.

Kept apart from the command that spells it because two commands want it:
`monitor` over a PHY on the wire, and `mv ... monitor` over a switch. Both
end up with something that has read(reg), which is all this needs.
"""

import time

_POLL_MS = 100

# How long to watch when no duration was given. Not unlimited, because a
# running command cannot be interrupted on this firmware: the console path
# never scans input for the interrupt character, so Ctrl-C is only read
# between commands. An unbounded watch would mean a power cycle.
_DEFAULT_SECONDS = 60


def take_timeout(args):
    """Peel a trailing `for <seconds>`. Returns (seconds_or_None, rest).

    Spelled with a keyword on purpose. The previous project put the timeout
    last as a bare number, next to an optional register list that is also
    numbers, and resolved the clash by trying to parse a register spec first
    and falling back to a timeout — so `monitor 6 30` quietly meant register
    30 rather than thirty seconds.
    """
    if len(args) >= 2 and args[-2] == "for":
        from cli.parser import integer
        from cli.registry import CommandError

        seconds = integer(args[-1], "duration")
        if seconds <= 0:
            raise CommandError("duration must be positive")
        return seconds, args[:-2]
    return None, args


def run(ctx, registers, read, label, width=2, seconds=None, vwidth=4):
    """Poll for a while, printing every change.

    The first sweep is silent: it establishes what "unchanged" means. A
    register that fails to read is skipped rather than reported, because at
    ten sweeps a second a target that has gone away would otherwise bury the
    changes that did happen under thousands of identical errors.
    """
    if seconds is None:
        seconds = _DEFAULT_SECONDS
    previous = {}

    for r in registers:
        try:
            previous[r] = read(r)
        except OSError:
            pass

    started = time.ticks_ms()
    changes = 0

    ctx.out.line("watching {} register(s) for {} s", len(registers), seconds)
    stopped = None

    try:
        while True:
            if time.ticks_diff(time.ticks_ms(), started) >= seconds * 1000:
                break
            if ctx.out.out_of_room():
                stopped = "the volume is nearly full"
                break

            for r in registers:
                try:
                    value = read(r)
                except OSError:
                    continue

                if r not in previous:
                    previous[r] = value
                elif previous[r] != value:
                    ctx.out.line("{} 0x{:0{}x}: 0x{:0{}x} -> 0x{:0{}x}",
                                 label, r, width, previous[r], vwidth,
                                 value, vwidth)
                    previous[r] = value
                    changes += 1

            time.sleep_ms(_POLL_MS)
    except KeyboardInterrupt:
        ctx.out.line("")

    elapsed = time.ticks_diff(time.ticks_ms(), started)
    ctx.out.line("{} change(s) in {}.{} s{}", changes, elapsed // 1000,
                 (elapsed % 1000) // 100,
                 " (stopped: {})".format(stopped) if stopped else "")
