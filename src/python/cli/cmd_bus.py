"""Bus rail, translator supply, target reset, connector mode."""

import time

import mdioprobe

from cli.parser import integer
from cli.registry import CommandError, command


@command("bus", category="bus", syntax="bus [force <mv> | auto]",
         summary="show the detected bus rail, or declare it",
         detail="""
Capture and the master both refuse to run until the rail is known, because
the comparator thresholds and the translator supply are derived from it.
Normally the detector works it out from the MDIO idle level. `bus force`
declares it instead, for a bench with no target attached; `bus auto` hands
it back to the detector.
""")
def cmd_bus(ctx, args):
    if args:
        if args[0] == "auto":
            mdioprobe.bus_force_level(0)
        elif args[0] == "force":
            if len(args) != 2:
                raise CommandError("usage: bus force <mv>")
            mv = integer(args[1], "rail")
            try:
                mdioprobe.bus_force_level(mv)
            except OSError:
                raise CommandError("rail must be 1800, 2500 or 3300")
        else:
            raise CommandError("usage: bus [force <mv> | auto]")

        # The detector polls every 200 ms. Without this the command would
        # print the state it just replaced, which reads as if nothing
        # happened.
        time.sleep_ms(250)

    info = mdioprobe.bus_get()
    ctx.out.line("target:   {}", "present" if info["target_present"] else "absent")
    ctx.out.line("rail:     {}{}",
                 "unknown" if info["nominal_mv"] < 0 else
                 "{} mV".format(info["nominal_mv"]),
                 " (declared)" if info["level_forced"] else "")
    ctx.out.line("measured: {} mV, {}/{} samples at the rail",
                 info["measured_mv"], info["samples_used"], info["samples_total"])
    ctx.out.line("usable:   {}", "yes" if mdioprobe.bus_ready() else "no")


@command("vreg", category="bus", syntax="vreg [<mv> | off | reset]",
         summary="translator supply for the MDC buffer",
         detail="""
1800..3000 mV is regulated. Above that the pass element is opened fully and
the output follows the board's 3.3 V — there is no headroom left to regulate
with, so a request over 3000 becomes pass-through rather than pretending.

The master raises this by itself before driving, so setting it by hand is
for bench work.

`reset` re-arms the min/max window without touching the rail. The window
otherwise runs from the last target change, so it always contains the
soft-start ramp and the ramp hides everything smaller.
""")
def cmd_vreg(ctx, args):
    if args:
        if args[0] == "off":
            mdioprobe.vreg_off()
        elif args[0] == "reset":
            mdioprobe.vreg_reset_excursion()
        else:
            mv = integer(args[0], "voltage")
            try:
                mdioprobe.vreg_set(mv)
            except OSError:
                raise CommandError("voltage must be at least 1800 mV")

    info = mdioprobe.vreg_get()
    ctx.out.line("state:    {}", info["state"])
    ctx.out.line("target:   {} mV", info["target_mv"])
    ctx.out.line("measured: {} mV", info["measured_mv"])
    ctx.out.line("range:    {}..{} mV since the last reset or retarget",
                 info["min_mv"], info["max_mv"])


@command("rst", category="bus", syntax="rst [ms]",
         summary="pulse the target's reset line",
         detail="""
The line is open-drain with no pull-up on our side — it has to come from the
target. After the pulse the line is read back, and a line that still reads
asserted means no pull-up is present, which looks exactly like a target that
ignores MDIO.
""")
def cmd_rst(ctx, args):
    ms = integer(args[0], "duration") if args else 0

    mdioprobe.target_reset(ms)
    if mdioprobe.target_reset_released():
        ctx.out.line("reset pulsed; line released (target pull-up present)")
    else:
        ctx.out.line("reset pulsed, but the line still reads asserted —")
        ctx.out.line("no pull-up on the target side, or it is holding reset")


@command("mode", category="bus", syntax="mode [mdio|uart|i2c]",
         summary="what PB8/PB9 do on the connector",
         detail="""
UART and I2C reach the connector directly at a fixed 3.3 V with no level
translation, and the MDC buffer is switched off in those modes so it cannot
fight PB9. Leaving MDIO mode stops any capture in progress.
""")
def cmd_mode(ctx, args):
    if args:
        try:
            mdioprobe.mode_set(args[0])
        except ValueError as exc:
            raise CommandError(str(exc))
    ctx.out.line("connector: {}", mdioprobe.mode_get())
