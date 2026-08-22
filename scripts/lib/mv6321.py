"""88E6321 / 88E6320 — the parts of this switch that are not addressing.

Load with `use mv6321`. Copy to /flash/lib on the probe.
"""

# VERIFIED: every register and bit here was exercised against a real 88E6321
# on the bench. Where a value came from a measurement rather than the
# specification — or where the two disagreed — the comment says so.
#
# Reference: 88E6321/88E6320 Functional Specification Rev. 0.05.
#
# The size limit is no longer the compiler. Helpers ship as .mpy — `./build.sh`
# cross-compiles them into build/lib — so the old "keep the source under about
# 9 KB, MicroPython compiles it on the device" rule is gone. What replaced it
# is the heap at load time: this file at 11.7 KB of .mpy takes nearly all of
# the ~35 KB free after a soft reset, so exactly one helper fits at a time and
# switching parts needs `use drop` first. `use` prints the free heap when it
# refuses.

from cli import marvell
from cli import cmd_probe
from cli import mvport
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

# Which ports have an RGMII interface, and this is the whole of what stays
# here: Table 67 names bits 15:14 "valid on Port 2, Port 5 and Port 6 only".
# On the 88E6240 the same two bits are ports 5 and 6, on the 88E6390 port 0
# alone. Everything else about those bits — the numbers, the rule that the
# link must be down first — all three specifications state identically, so it
# lives in cli.mvport.
RGMII_PORTS = (2, 5, 6)

# Table 66, the Interface Configuration Matrix: for each C_Mode, and for each
# value of PHYDetect where that bit changes the answer, where the port's link
# comes from. cli.mvport acts on the second half of each pair; the names are
# for the operator.
#
# This is the one field that says what is actually in a port's path, and the
# reason it matters here: C_Mode 0x8 to 0xA are the SERDES modes of ports 0
# and 1, and a port in one of them has no PHY of its own at all — whatever
# answers on the external bus at address 0 or 1 belongs to something else.
_PX = "px_enable"
_PPU = "ppu"
CMODE = (
    (("FD MII", _PX), ("FD MII", _PX)),                            # 0x0
    (("MII PHY", _PX), ("MII PHY", _PX)),                          # 0x1
    (("MII MAC", _PX), ("MII to PHY", _PPU)),                      # 0x2
    (("GMII", _PX), ("GMII to PHY", _PPU)),                        # 0x3
    (("RMII PHY", _PX), ("RMII to PHY", _PPU)),                    # 0x4
    (("RMII MAC", _PX), ("RMII to PHY", _PPU)),                    # 0x5
    (("xMII tristate", "none"), ("xMII tristate", "none")),        # 0x6
    (("RGMII", _PX), ("RGMII to PHY", _PPU)),                      # 0x7
    (("100BASE-FX", "serdes"), ("100BASE-FX", "serdes")),          # 0x8
    (("1000BASE-X", "serdes"), ("1000BASE-X", "serdes")),          # 0x9
    (("SGMII", "serdes+ppu"), ("SGMII", "serdes+ppu")),            # 0xA
    None, None, None, None,                                        # 0xB-0xE
    (("PHY", "phy"), ("PHY", "phy")),                              # 0xF
)

# Which SERDES serves which port. Ports 0 and 1 only, and the addresses are
# not the port numbers — that is the whole difficulty. FS Figure 60 and
# §9.3; measured on an 88E6321 rev 2, both read BMCR 0x2900 out of reset,
# which is powerdown set with 100BASE-FX speed and duplex.
SERDES_AT = {0: 0x0C, 1: 0x0D}

# How to spell a deliberate write to the PHY at address 0, for the
# refusal that sends the operator there.
PHY_WRITE_HINT = "mv phy write {sw} 0 0"

# Only an address on the *external* MDIO bus can carry a broadcast echo.
# An internal PHY answers through the switch's own Global2 window, where
# nothing else can reply, so address 0 there means port 0 and nothing
# more — which is the ordinary case on an 88E6240, whose internal PHYs
# are 0x00 to 0x04. Saying otherwise was wrong, and was found to be so
# on an 88E6240 rev 1 on 2026-08-22.
BCAST_NOTE = (", and address 0 is also the broadcast address that Realtek and "
               "Motorcomm parts answer on by default, so this may be an echo")

_PORT_USAGE = ("usage: mv6321 port <sw> [ports] "
               "[detect on|off | up|down|auto | state <s> | rgmii <m>]")

_USAGE = """usage:
  mv6321 id     <sw>
  mv6321 port   <sw> [ports] [detect on|off]
  mv6321 port   <sw> <ports> up|down|auto
  mv6321 port   <sw> <ports> state disabled|blocking|learning|forwarding
  mv6321 port   <sw> <ports> rgmii none|rx|tx|both
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
    # PHYDetect is what decides whether the PPU publishes anything into a
    # port's status register at all, and the PPU's own detection does not
    # always find an external PHY — measured, ports 5 and 6 stayed undetected
    # with the bus routed and both PHYs answering. The spec is explicit that
    # software may set the bit and the poll routine follows. That is this
    # family's business; the rest of `port` is cli.mvport's.
    if not args:
        raise CommandError(_PORT_USAGE)

    sw = _switch(args[0])
    rest, action, value = mvport.take_verb(args[1:])
    ports = mvport.port_list(rest, action, PORTS, _PORT_USAGE)

    if action in mvport.BARE_VERBS or action == "rgmii":
        # Without the PPU this window does not fail, it answers 0xffff to
        # everything (FS p.314). Every port would then look as if it had no
        # PHY, and `up` would force the MAC link instead of powering the PHY
        # — the wrong register on the wrong layer.
        if not (sw.read(marvell.GLOBAL1, G1_CONTROL) & G1_CONTROL_PPU_ENABLE):
            raise CommandError(
                "the PPU is disabled, so the SMI PHY window answers 0xffff "
                "for every address — every port would look as if it had no "
                "PHY. `mv reg modify set <sw> 0x1b 0x04 mask 0x4000`")

    p = _ports_of(sw)

    if action == "detect":
        if value not in ("on", "off"):
            raise CommandError("detect takes on or off")
        if not (sw.read(marvell.GLOBAL1, G1_STATUS) & G1_STATUS_PPU_POLLING):
            raise CommandError("the PPU is still initialising; this "
                               "register must not be written then")
        for n in ports:
            apply_modify("set" if value == "on" else "clear",
                         lambda r, n=n: sw.read(_port(n), r),
                         lambda r, v, n=n: sw.write(_port(n), r, v),
                         0x00, PORT_STATUS_PHY_DETECT)
    elif action in mvport.BARE_VERBS:
        p.link(ctx, ports, action)
    elif action == "state":
        p.state(ctx, ports, value)
    elif action == "rgmii":
        p.rgmii(ctx, ports, value)

    p.status(ctx, ports)


def _phy_at(sw, n):
    """The PHY the PPU polls for port `n`, in the shape cli.mvport wants.

    On this family the SMI address of a port's PHY *is* the port number —
    that is the rule the PPU itself works by ("the PPU can perform this job
    only if the SMI address of the external PHY matches the physical port
    number it is connected to", DS 2.2.6) — and one window reaches internal
    and external alike. So there is nothing to search: read it, and let
    0xffff mean there is no PHY there.
    """
    try:
        bmcr = sw.phy_read(n, mvport.BMCR)
    except OSError:
        return None
    if bmcr == 0xFFFF:
        return None
    # Only the external slots can be shadowed by a broadcast answer: an
    # internal PHY lives behind the switch's own window, where nothing else
    # can reply. On this family port 0 is an external slot, so it can.
    if n == 0 and n in EXTERNAL_PHY:
        mvport.refuse_broadcast(n, "PHY 0", "mv6321", sw.addr, PHY_WRITE_HINT)
    return (n, bmcr,
            lambda v: sw.phy_write(n, mvport.BMCR, v),
            "PHY {}".format(n))


def _serdes_at(sw, n):
    """The SERDES that serves port `n`, if the port has one.

    Reached through the same Global2 window as any PHY, just not at the port
    number — 0x0C for port 0, 0x0D for port 1. No page write: on this family
    the SERDES page register comes up at 1 already, which is where the fibre
    registers are, and the 88E6240 is the one that does not (there reg 22 has
    to be written).
    """
    addr = SERDES_AT.get(n)
    if addr is None:
        return None
    try:
        bmcr = sw.phy_read(addr, mvport.BMCR)
    except OSError:
        return None
    if bmcr == 0xFFFF:
        return None
    return (addr, bmcr,
            lambda v: sw.phy_write(addr, mvport.BMCR, v),
            "SERDES 0x{:02x}".format(addr))


def _ports_of(sw):
    return mvport.Ports(sw, "mv6321", PORT_BASE, PORTS, RGMII_PORTS, SPEED,
                        CMODE, lambda n: _phy_at(sw, n),
                        lambda n: _serdes_at(sw, n))


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
            if addr == 0 and addr in EXTERNAL_PHY:
                note += BCAST_NOTE
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
         summary="88E6321/6320: identify, port status and control, probe "
                 "PHYs, reset, external bus",
         detail=_USAGE + """

For this family only; every subcommand checks the identifier first.
Addressing lives in `mv` and works on any Link Street part.

`detect on` sets PHYDetect by hand — the PPU's own detection does not always
find an external PHY.

The other three verbs are port operations rather than register edits, and
each names the register it wrote so the result can be checked:

`up|down|auto` is the physical layer, and what it touches comes from the
port's C_Mode rather than from guessing. The Interface Configuration Matrix
says where each mode takes its link from, and that is what is in the port's
path: an internal PHY, an external PHY the PPU polls, the SERDES, both the
SERDES and a PHY on SGMII, or — on an xMII port whose link follows the
Px_ENABLE pin, and on a tristated one — nothing on the wire at all. Whatever
is there is powered through its own BMCR; a port with nothing to power gets
the MAC's ForcedLink/LinkValue pair instead, which is the only thing up and
down can mean there, and `auto` releases it. The two are never mixed, because
forcing the link on a port that has a PHY or a SERDES would override the very
thing just powered.

On a SERDES only the powerdown bit moves. Whether the block negotiates is
part of the mode it is in — 100BASE-FX has no autonegotiation to enable,
1000BASE-X does — and `up` powers things rather than reconfiguring them.

**SMI address 0 is refused.** Realtek and Motorcomm PHYs answer at address 0
by default in addition to their strapped one — the broadcast address in their
documents — so a read there may be any PHY on the bus and a write reaches
every one of them at once. Nothing here can tell an echo from a PHY genuinely
strapped to 0, so a port operation that would land on address 0 refuses and
names the two ways through: turn the broadcast response off in the PHY, or
write the register deliberately. It is also why `probe` marks address 0.

`state` writes PortState in Port Control (offset 0x04). The encoding is
identical on all three families.

`rgmii none|rx|tx|both` writes bits 15:14 of Physical Control, always both of
them, so the setting is stated rather than accumulated. Two refusals are
deliberate: on a port outside 2, 5 and 6, because those bits are defined for
no other port on this family — and while the port's link is up, because
Table 67 says in as many words that the change is disruptive and must be made
with the link down.

Anything that writes wants an explicit port list. `port <sw> state
forwarding` with no list would mean all seven, which is not a thing to do by
omission.

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
