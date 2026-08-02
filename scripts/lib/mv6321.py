"""88E6321 / 88E6320 — the parts of this switch that are not addressing.

Load with `use mv6321`. Copy to /flash/lib on the probe.
"""

# VERIFIED: every register and bit here was exercised against a real 88E6321
# on the bench. Where a value came from a measurement rather than the
# specification — or where the two disagreed — the comment says so.
#
# Reference: 88E6321/88E6320 Functional Specification Rev. 0.05.
#
# Keep this file under about 9 KB of source. MicroPython compiles it on the
# device, and a 14 KB version of it ran the GC heap out of memory during
# import; prose belongs in comments, which cost the compiler nothing, rather
# than in docstrings and help text, which do. `./build.sh` cross-compiles
# the helpers into build/lib, which removes that limit.

from cli import marvell
from cli import cmd_probe
from cli.cmd_mdio import apply_modify
from cli.parser import ranged, spec
from cli.registry import CommandError, command

VERIFIED = True

# Product number is bits 15:4 of the Switch Identifier, revision bits 3:0.
PARTS = {
    0x310: "88E6321",
    0x115: "88E6320",
}

PORT_BASE = 0x10
PORTS = 7

# Spec §9.3. The internal PHYs of ports 3 and 4, the internal SERDES of ports
# 0 and 1, and external PHYs which must be strapped to match their port
# number so the PPU can poll them.
INTERNAL_PHY = (3, 4)
INTERNAL_SERDES = (0x0C, 0x0D)
EXTERNAL_PHY = (0, 1, 2, 5, 6)

# Global1.
G1_STATUS = 0x00
G1_CONTROL = 0x04
G1_STATUS_PPU_POLLING = 0x8000
G1_STATUS_INIT_READY = 0x0800
G1_CONTROL_SW_RESET = 0x8000

# Global1 0x04 bit 14 enables the PHY Polling Unit, and the spec says so in
# two places that do not agree in tone. The register table (Table 99) calls
# it "Reserved ... Must be set to 1"; the footnote under the SMI PHY Command
# register says that register "can be used to access the PHY registers only
# when the PPU is enabled (global offset 0x04)". Measured, they are the same
# bit: clearing it makes every read through the SMI PHY window return
# 0xffff, and setting it again restores them.
#
# Note it is *not* PPUState in Global1 0x00 bit 15: measured with the PPU
# disabled, status went 0xc800 -> 0x8800, so bit 15 stayed at "polling" while
# bit 14 followed the enable. Bit 14 of the status register is documented as
# Reserved, so the enable bit itself is what gets read.
G1_CONTROL_PPU_ENABLE = 0x4000

# Scratch and Misc indices. 0x63 bit 7 is NormalSMI: with P5_MODE not 0x1/0x2
# it decides whether P5_COL and P5_CRS are the external MDIO_PHY/MDC_PHY bus
# or plain GPIO, and the NO_CPU strap inverts the sense (Table 208).
#
# This index is a GPIO direction register on the 88E6240 and 88E6390. That
# is the reason this file exists.
SCRATCH_CFG = 0x63
SCRATCH_CFG_NORMAL_SMI = 0x80
SCRATCH_CONFIG1 = 0x71
SCRATCH_CONFIG1_NO_CPU = 0x04

# Port Status, port offset 0x00 (Table 65). Bit 12 PHYDetect is the only
# writable bit in it, and it is what the PPU's poll routine consults to
# decide which ports to visit. A SWReset re-runs PPU init and rewrites the
# register, so a manual setting does not survive one.
PORT_STATUS_PHY_DETECT = 0x1000

SPEED = ("10 Mbps", "100 Mbps", "1000 Mbps", "reserved")

_USAGE = """usage:
  mv6321 id     <sw>
  mv6321 port   <sw> [ports] [detect on|off]
  mv6321 probe  <sw>
  mv6321 reset  <sw>
  mv6321 extbus <sw> [on|off|solo]"""


def _port(n):
    return PORT_BASE + n


def _switch(token, check=True):
    sw = marvell.Switch(ranged(token, 0, 31, "switch address"))

    if check:
        ident = sw.read(_port(0), 0x03)
        if (ident >> 4) not in PARTS:
            raise CommandError(
                "identifier 0x{:04x} is not an 88E6321 or 88E6320; these "
                "commands would write to the wrong registers".format(ident))
    return sw


# --- identify ---------------------------------------------------------

def _id(ctx, args):
    if len(args) != 1:
        raise CommandError("usage: mv6321 id <sw>")

    sw = _switch(args[0], check=False)
    ident = sw.read(_port(0), 0x03)
    part = PARTS.get(ident >> 4)

    if part is None:
        ctx.out.line("switch 0x{:02x}: identifier 0x{:04x} — not this family",
                     sw.addr, ident)
        ctx.out.line("  88E6321 reads 0x310x, 88E6320 reads 0x115x")
        return

    ctx.out.line("switch 0x{:02x}: {} rev {}  (id 0x{:04x})",
                 sw.addr, part, ident & 0x0F, ident)

    status = sw.read(marvell.GLOBAL1, G1_STATUS)
    ctx.out.line("Global1 status 0x{:04x}: PPU {}, init {}", status,
                 "polling" if status & G1_STATUS_PPU_POLLING else "idle",
                 "ready" if status & G1_STATUS_INIT_READY else "not ready")

    ctx.out.line("{} ports, registers at 0x{:02x}+n", PORTS, PORT_BASE)
    ctx.out.line("PHY addresses: {} internal, {} SERDES, {} external slots",
                 ",".join(str(a) for a in INTERNAL_PHY),
                 ",".join("0x{:02x}".format(a) for a in INTERNAL_SERDES),
                 ",".join(str(a) for a in EXTERNAL_PHY))


# --- port status ------------------------------------------------------

def _ports(ctx, args):
    # Link state as the PPU published it, and the bit that decides whether
    # it publishes anything at all. This reads no PHY register: the PPU
    # polls the PHY whose SMI address matches the port number and writes
    # link, speed and duplex into that port's status register, which is what
    # the MAC acts on. `mv phy` is the opposite.
    #
    # PHYDetect is the switch between the two, and the PPU's own detection
    # does not always find an external PHY — measured, ports 5 and 6 stayed
    # undetected with the bus routed and both PHYs answering. The spec is
    # explicit that software may set the bit and the poll routine follows.
    if not args:
        raise CommandError("usage: mv6321 port <sw> [ports] [detect on|off]")

    sw = _switch(args[0])
    args = args[1:]
    want = None

    if len(args) >= 2 and args[-2] == "detect":
        if args[-1] not in ("on", "off"):
            raise CommandError("detect takes on or off")
        want = args[-1] == "on"
        args = args[:-2]

    if len(args) > 1:
        raise CommandError("usage: mv6321 port <sw> [ports] [detect on|off]")
    ports = spec(args[0], 0, PORTS - 1, "port") if args else range(PORTS)

    if want is not None:
        if not (sw.read(marvell.GLOBAL1, G1_STATUS) & G1_STATUS_PPU_POLLING):
            raise CommandError("the PPU is still initialising; this "
                               "register must not be written then")
        for n in ports:
            apply_modify("set" if want else "clear",
                         lambda r, n=n: sw.read(_port(n), r),
                         lambda r, v, n=n: sw.write(_port(n), r, v),
                         0x00, PORT_STATUS_PHY_DETECT)

    for n in ports:
        try:
            v = sw.read(_port(n), 0x00)
        except OSError as exc:
            ctx.out.line("port {}: ERROR (errno {})", n,
                         exc.args[0] if exc.args else "?")
            continue

        ctx.out.line("port {}: 0x{:04x}  {}, {}{}{}", n, v,
                     "link up" if v & 0x0800 else "link down",
                     "PHY detected" if v & PORT_STATUS_PHY_DETECT
                     else "no PHY detected",
                     ", {}".format(SPEED[(v >> 8) & 3]) if v & 0x0800 else "",
                     ", full duplex" if (v & 0x0800) and (v & 0x0400) else
                     (", half duplex" if v & 0x0800 else ""))


# --- probe ------------------------------------------------------------

# Register 16 of a SERDES, bits 1:0 (FS p.494, Table 399), reflecting the
# Px_smode pins latched at reset.
SERDES_MODE = ("100BASE-FX", "1000BASE-X", "SGMII system", "SGMII media")


def _polled(sw, addr):
    """Whether the PPU visits the port this address belongs to.

    Measured on an 88E6321 with the external bus routed: clearing PHYDetect
    on port 5 changes nothing about reading registers 2 and 3 at address 5
    through the SMI PHY window — 0x4f51/0xe91a before, after, and 500 ms
    later. The bit gates the PPU's poll routine, not the window, exactly as
    the spec says. So it is reported and not touched; `port <sw> detect on`
    is where setting it lives.
    """
    if addr >= PORTS:
        return ""

    try:
        status = sw.read(_port(addr), 0x00)
    except OSError:
        return ""
    return "" if status & PORT_STATUS_PHY_DETECT else ", PPU not polling port"


def _role(addr):
    # The identifier cannot tell these apart: register 2 reads 0x0141 on the
    # copper PHYs and on the SERDES alike, and this revision of the spec does
    # not publish either model number. Only the address says what a thing is.
    if addr in INTERNAL_PHY:
        return "internal PHY"
    if addr in INTERNAL_SERDES:
        return "SERDES"
    if addr in EXTERNAL_PHY:
        return "external slot"
    return "off the map — the PPU never polls here"


def _probe(ctx, args):
    # Every one of the 32 addresses, not just the ones in the map. An
    # external PHY strapped to an address that does not match its port number
    # is invisible to the PPU — "the PPU can perform this job only if the SMI
    # address of the external PHY matches the physical port number it is
    # connected to" (DS §2.2.6) — but the window reaches all 32, so this is
    # exactly the case worth a sweep.
    if len(args) != 1:
        raise CommandError("usage: mv6321 probe <sw>")

    sw = _switch(args[0])

    # Without the PPU the window does not fail, it lies: busy clears at once
    # and every read returns 0xffff (FS p.314, Tables 165 and 166). That is
    # indistinguishable from an empty switch, so refuse rather than report
    # one.
    if not (sw.read(marvell.GLOBAL1, G1_CONTROL) & G1_CONTROL_PPU_ENABLE):
        raise CommandError(
            "the PPU is disabled, and the SMI PHY window then returns 0xffff "
            "for every register of every address — that would read as an "
            "empty switch. `mv reg modify set <sw> 0x1b 0x04 mask 0x4000`")

    # Not `cfg & NORMAL_SMI`: the NO_CPU strap inverts what the bit means,
    # which is why `_routed` takes both. Getting this wrong reports a routed
    # bus on a board where the pins are GPIO, and then blames the PHYs.
    if not _routed(sw.scratch_read(SCRATCH_CFG),
                   sw.scratch_read(SCRATCH_CONFIG1)):
        ctx.out.line("note: the external bus is on GPIO, not MDIO_PHY — only "
                     "internal addresses can answer. `mv6321 extbus <sw> on`")

    found = 0

    for addr in range(32):
        # One address that will not read must not end the sweep. The
        # probe drops the target rail for a poll period under its own
        # burst of traffic, and `identify` waits that out,
        # but the extra SERDES read below has no such cover. Report the
        # address and carry on — silently skipping it is what made a
        # switch with seven live PHYs look like it had two.
        try:
            what = cmd_probe.identify(marvell.SwitchPhy(sw, addr))
            if what is None:
                continue

            note = _role(addr)
            if addr in INTERNAL_SERDES:
                note = "{}, {}".format(
                    note, SERDES_MODE[sw.phy_read(addr, 16) & 3])
            note += _polled(sw, addr)
        except OSError as exc:
            ctx.out.line("addr {:02x}: read failed, errno {}", addr,
                         exc.args[0] if exc.args else "?")
            continue

        cmd_probe.report(ctx, "addr {:02x}".format(addr), what, note)
        found += 1

    ctx.out.line("{} address(es) answered", found)


# --- reset ------------------------------------------------------------

def _wait_init_ready(sw, tries=100, delay_ms=10):
    # 0xffff counts as "not yet": during a reset the switch does not answer
    # at all, and an unanswered read looks exactly like every bit set.
    import time

    last = 0
    for _ in range(tries):
        last = sw.read(marvell.GLOBAL1, G1_STATUS)
        if last != 0xFFFF and (last & G1_STATUS_INIT_READY):
            return last
        time.sleep_ms(delay_ms)
    raise CommandError(
        "switch did not report init-ready (status 0x{:04x})".format(last))


def _reset(ctx, args):
    if len(args) != 1:
        raise CommandError("usage: mv6321 reset <sw>")

    import time

    import mdioprobe

    sw = _switch(args[0])

    mdioprobe.target_reset(10)
    ctx.out.line("PHY_RST pulsed; waiting for the switch to come back")
    time.sleep_ms(300)

    ctl = sw.read(marvell.GLOBAL1, G1_CONTROL)
    if ctl == 0xFFFF:
        raise CommandError("Global1 control reads 0xffff — no answer from the "
                           "switch, refusing to write a guess into it")

    sw.write(marvell.GLOBAL1, G1_CONTROL,
             ctl | G1_CONTROL_SW_RESET | G1_CONTROL_PPU_ENABLE)
    status = _wait_init_ready(sw)

    ctx.out.line("Global1 status 0x{:04x}: PPU {}, init ready", status,
                 "polling" if status & G1_STATUS_PPU_POLLING else "idle")

    # The hardware pulse restores the scratch defaults, so anything set
    # through `extbus` is gone — worth saying, because the symptom is every
    # external PHY reading 0xffff again with no obvious cause.
    cfg = sw.scratch_read(SCRATCH_CFG)
    config1 = sw.scratch_read(SCRATCH_CONFIG1)
    if not _routed(cfg, config1):
        ctx.out.line("the reset restored the scratch defaults, so the "
                     "external bus is off again")


# --- external PHY bus -------------------------------------------------

def _routed(cfg, config1):
    # Table 208: with NormalSMI set the pins become the external MDIO bus
    # *if the NO_CPU strap was one*; clearing the bit inverts that. So the
    # two values have to be read together — the bit alone does not say.
    return bool(cfg & SCRATCH_CFG_NORMAL_SMI) == bool(config1 &
                                                      SCRATCH_CONFIG1_NO_CPU)


def _extbus(ctx, args):
    if not args or len(args) > 2:
        raise CommandError("usage: mv6321 extbus <sw> [on|off|solo]")

    sw = _switch(args[0])

    if len(args) == 2:
        if args[1] not in ("on", "off", "solo"):
            raise CommandError("usage: mv6321 extbus <sw> [on|off|solo]")
        want = args[1] in ("on", "solo")

        cfg = sw.scratch_read(SCRATCH_CFG)
        config1 = sw.scratch_read(SCRATCH_CONFIG1)
        no_cpu = bool(config1 & SCRATCH_CONFIG1_NO_CPU)
        bit = SCRATCH_CFG_NORMAL_SMI if (want == no_cpu) else 0
        new = (cfg & ~SCRATCH_CFG_NORMAL_SMI) | bit

        if new != cfg:
            sw.scratch_write(SCRATCH_CFG, new)
            ctx.out.line("pins now {}",
                         "MDIO_PHY/MDC_PHY" if want else "GPIO")

        # The PPU goes off only for `solo`, and comes back for anything else.
        # Read-modify-write, and only bit 14: everything else in this
        # register matters, including bits the table marks Reserved but
        # requires to be one, and a blind write would clear them.
        ctl = sw.read(marvell.GLOBAL1, G1_CONTROL)
        if ctl == 0xFFFF:
            raise CommandError("Global1 control reads 0xffff — no answer from "
                               "the switch, refusing to write a guess")
        ppu_on = args[1] != "solo"
        want_ctl = ((ctl | G1_CONTROL_PPU_ENABLE) if ppu_on
                    else (ctl & ~G1_CONTROL_PPU_ENABLE))
        if want_ctl != ctl:
            sw.write(marvell.GLOBAL1, G1_CONTROL, want_ctl)
            ctx.out.line("PPU {}", "started" if ppu_on else "stopped")

    cfg = sw.scratch_read(SCRATCH_CFG)
    config1 = sw.scratch_read(SCRATCH_CONFIG1)
    routed = _routed(cfg, config1)
    ppu = bool(sw.read(marvell.GLOBAL1, G1_CONTROL) & G1_CONTROL_PPU_ENABLE)

    ctx.out.line("scratch 0x63 = 0x{:02x} (NormalSMI {})", cfg,
                 1 if cfg & SCRATCH_CFG_NORMAL_SMI else 0)
    ctx.out.line("scratch 0x71 = 0x{:02x} (NO_CPU {})", config1,
                 1 if config1 & SCRATCH_CONFIG1_NO_CPU else 0)
    ctx.out.line("pins:  {}", "MDIO_PHY/MDC_PHY" if routed else "GPIO")
    ctx.out.line("PPU:   {}{}", "polling" if ppu else "stopped",
                 " this bus" if (ppu and routed) else "")


_GROUPS = {
    "id": _id,
    "port": _ports,
    "probe": _probe,
    "reset": _reset,
    "extbus": _extbus,
}


@command("mv6321", category="mdio",
         syntax="mv6321 id|port|probe|reset|extbus <sw> ...",
         summary="88E6321/6320: identify, port status, probe PHYs, reset, "
                 "external bus",
         detail=_USAGE + """

For this family only; every subcommand checks the identifier first.
Addressing lives in `mv` and works on any Link Street part.

`detect on` sets PHYDetect by hand — the PPU's own detection does not always
find an external PHY.

`probe` names every PHY behind the switch, through the SMI PHY window rather
than the PPU. It reads all 32 addresses, not only the ones the chip map
expects: an external PHY strapped to an address that does not match its port
number is invisible to the PPU, and that is precisely the case a sweep is
for. Addresses off the map are marked as such.

It refuses to run with the PPU stopped. That is not caution — with the PPU
off the window does not fail, it answers 0xffff to everything, which reads
exactly like a switch with nothing attached.

Nothing is written. PHYDetect is read and reported — an address that answers
while its port's bit is clear is a PHY the PPU is not polling, which is worth
seeing — but setting it stays in `port <sw> detect on`. Measured on this
bench with the external bus routed: clearing the bit on port 5 changed
nothing about reading registers 2 and 3 at address 5, before, after, and
500 ms later. The bit gates the PPU, not the window.

`extbus` moves the external MDIO bus onto P5_COL/P5_CRS. Whether that means
setting or clearing NormalSMI depends on the NO_CPU strap, which inverts it,
so both scratch bytes are shown. `solo` also stops the PPU, which is a master
on that bus; the SMI PHY window stops working while it is off, so `mv phy`
then reads 0xffff until `extbus <sw> off`.

`reset` pulses PHY_RST, which restores the scratch defaults and so takes the
external bus down with it.

The other families differ in ways that do not merely fail — scratch index
0x63 is a GPIO direction register on the 88E6240 and the 88E6390 — which is
why each has its own helper rather than a shared one with a switch on the
part number.
""")
def cmd_mv6321(ctx, args):
    if not args:
        raise CommandError(_USAGE)

    group = _GROUPS.get(args[0])
    if group is None:
        raise CommandError(_USAGE)
    group(ctx, args[1:])
