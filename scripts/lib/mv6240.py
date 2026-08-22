"""88E6352 / 88E6240 / 88E6176 / 88E6172.

Load with `use mv6240`. Copy to /flash/lib on the probe.

**VERIFIED** on an 88E6240 rev 1: on 2026-08-04 identification, port status,
PHYDetect read and written, the SMI PHY window, `probe` across all 32
addresses, the SERDES page and a software reset; on 2026-08-22 the C_Mode
table and every port operation, on a part in single-chip mode. Where silicon
disagreed with the documents it is said so below, next to the claim it
replaced.

What 2026-08-22 added, all measured on the part:

  * the C_Mode table checks out — ports 0 to 4 report 0xF (PHY) and ports 5
    and 6 report 0x7 (RGMII), which is exactly the family's own map and the
    two ports Table 62 gives the RGMII delay bits to;
  * `up` and `down` drive the internal PHYs' BMCR, and with a patch cord
    between ports 0 and 3 the link comes up within about six seconds of `up`;
  * the SERDES page dance is not optional. Through it, 0x0F register 0 reads
    0x1940 — powerdown set, as the NO_CPU strap implies — and the same
    register read without selecting page 1 reads 0x0000. So a helper that
    skipped the page would conclude the SERDES was absent;
  * the broadcast refusal must *not* apply here. This family's internal PHYs
    are 0x00 to 0x04, so port 0's PHY is legitimately at SMI address 0,
    behind the switch's own window where no external part can answer. An
    earlier version refused `port 0 up` outright.
Sources:

    FS   88E6352/88E6240/88E6176/88E6172 Functional Specification
    DS   the matching datasheet

What differs from the 88E6321:

  * five internal PHYs on ports 0-4, at SMI device addresses 0x00-0x04, and
    a single SERDES at 0x0F — not two at 0x0C/0x0D (FS Figure 60, p. 211;
    FS Table 60, p. 216). That 0x0F was read off a figure nothing could be
    extracted from, and it is now confirmed: 0x0F is the address that
    answers with model 0x2a, the SERDES number. The fibre registers live on
    page 1, so register 22 has to be set to 0x1 first (FS Table 374,
    p. 472) — and it really does read 0x0000 out of a software reset, so
    that write is required rather than defensive.
  * external PHYs attach to ports 5 and 6 only, and their SMI addresses must
    equal the port number for the PPU to poll them (DS p. 52).
  * Global3 exists at device address 0x1D (TCAM, 6352/6240 only).
  * Global1 0x00 bits 15:12 are Reserved in the document (FS Table 94,
    p. 262) — and are not zero on the part. An 88E6240 rev 1 reads 0xc800
    in that register, unchanged across a software reset, so those four bits
    are 0b1100 and bit 15 reads one. On the 88E6321 that same bit is
    PPUState, where one means "PPU polling", which is what this looks like.
    Do not poll it expecting a zero.
  * Global1 0x04 bit 14 is plain "Reserved for future use" (FS Table 98,
    p. 266) and no PPUEn field exists in the specification — but it reads as
    a one, 0x4001 measured, the way the 88E6390 documents for its own. That
    is why `reset` writes it back as it found it.

Two absences worth stating out loud, because both look like missing
features until you check:

**There is no `extbus`.** This family has no NormalSMI bit. The external
MDIO_PHY/MDC_PHY pins are selected by the P5_MODE straps at the rising edge
of RESETn (DS p. 23), and no register moves them afterwards — the GPIO Pin
Control registers do not offer MDIO as an alternate function either. So
there is nothing for such a command to write. Note also that scratch index
0x63, which is NormalSMI on the 88E6321, is a **GPIO direction** register
here (FS Table 215, p. 340): do not write the 6321 value into it.

**There is no PPU precondition on PHY access.** The datasheet is explicit
(DS §2.2.7, p. 52): "software can access all of the PHY registers at any
time by using the SMI Command and Data registers (Global 2, offsets 0x18 and
0x19)". The 88E6321 behaves the opposite way, which is why `mv phy` no
longer refuses on its own.
"""

import time

from cli import marvell
from cli import cmd_probe
from cli import mvport
from cli.cmd_mdio import apply_modify
from cli.parser import ranged, spec
from cli.registry import CommandError, command

VERIFIED = True

# FS Table 64, p. 221, plus one number no document here prints: an 88E6240
# rev 1 identifies as 0x2401, so product 0x240, read off the part on
# 2026-08-04. It is in the table on that authority alone, which is also why
# _id() still lets an unrecognised number through — see there.
PARTS = {
    0x352: "88E6352",
    0x240: "88E6240",
    0x176: "88E6176",
    0x172: "88E6172",
}

PORT_BASE = 0x10
PORTS = 7

INTERNAL_PHY = (0, 1, 2, 3, 4)
INTERNAL_SERDES = (0x0F,)
EXTERNAL_PHY = (5, 6)

GLOBAL3 = 0x1D            # TCAM, 6352/6240 only

G1_STATUS = 0x00
G1_CONTROL = 0x04
G1_STATUS_INIT_READY = 0x0800
G1_CONTROL_SW_RESET = 0x8000

PORT_STATUS_PHY_DETECT = 0x1000

# FS Table 59. Same encoding as the 6321; 0x3 is reserved on this family.
SPEED = ("10 Mbps", "100/200 Mbps", "1000 Mbps", "reserved")

# Which ports have an RGMII interface, and this is the whole of what stays
# here: Table 62 names bits 15:14 "valid on Port 5 and Port 6 only", where the
# 88E6321 has them on ports 2, 5 and 6 and the 88E6390 on port 0 alone.
# Everything else about those bits — the numbers, the rule that the link must
# be down first — all three specifications state identically, so it lives in
# cli.mvport.
RGMII_PORTS = (5, 6)

# Table 60, the Interface Configuration Matrix. Same shape as the 88E6321's
# Table 66 and the 88E6390's Table 45, different contents: C_Mode 0x0 is
# Reserved here where the 6321 has FD MII, and the SERDES modes belong to
# ports 4 and 5 rather than 0 and 1.
_PX = "px_enable"
_PPU = "ppu"
CMODE = (
    None,                                                          # 0x0
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

# One SERDES at 0x0F, and the S_MODE strap decides whether it serves port 4
# or port 5 (FS Table 60 and the text above it). Both are listed and C_Mode
# picks: cli.mvport only asks for the SERDES on a port whose mode says the
# link comes from one, so the port that is not in a SERDES mode never sees it.
SERDES_AT = {4: 0x0F, 5: 0x0F}

# Only an address on the *external* MDIO bus can carry a broadcast echo.
# An internal PHY answers through the switch's own Global2 window, where
# nothing else can reply, so address 0 there means port 0 and nothing
# more — which is the ordinary case on an 88E6240, whose internal PHYs
# are 0x00 to 0x04. Saying otherwise was wrong, and was found to be so
# on an 88E6240 rev 1 on 2026-08-22.
BCAST_NOTE = (", and address 0 is also the broadcast address that Realtek and "
               "Motorcomm parts answer on by default, so this may be an echo")

_PORT_USAGE = ("usage: mv6240 port <sw> [ports] "
               "[detect on|off | up|down|auto | state <s> | rgmii <m>]")

_USAGE = """usage:
  mv6240 id    <sw>
  mv6240 port  <sw> [ports] [detect on|off]
  mv6240 port  <sw> <ports> up|down|auto
  mv6240 port  <sw> <ports> state disabled|blocking|learning|forwarding
  mv6240 port  <sw> <ports> rgmii none|rx|tx|both
  mv6240 reset <sw>"""


def _port(n):
    return PORT_BASE + n


def _switch(token, check=True):
    sw = marvell.Switch(ranged(token, 0, 31, "switch address"))

    if check:
        ident = sw.read(_port(0), 0x03)
        if ident in (0x0000, 0xFFFF):
            raise CommandError(
                "identifier reads 0x{:04x} — nothing answered".format(ident))
    return sw


def _id(ctx, args):
    """Identify, and stay useful on a part no table names.

    The specification lists product numbers for the 6352, 6176 and 6172 but
    not for the 6240 itself; 0x240 is known here because it was read off a
    part, not because a document gives it. Whatever else shares this register
    map is in the same position, so an unrecognised number is reported as
    unrecognised and the other commands are still allowed — the map is shared
    across the document, which is the whole reason these four are in one
    specification.
    """
    if len(args) != 1:
        raise CommandError("usage: mv6240 id <sw>")

    sw = _switch(args[0], check=False)
    ident = sw.read(_port(0), 0x03)
    part = PARTS.get(ident >> 4)

    if part is None:
        ctx.out.line("switch 0x{:02x}: identifier 0x{:04x} — not a number "
                     "this helper knows", sw.addr, ident)
        ctx.out.line("  it knows 0x352x, 0x240x, 0x176x and 0x172x, and the "
                     "specification prints all but the second, so this may "
                     "still be the right family; check the part marking")
    else:
        ctx.out.line("switch 0x{:02x}: {} rev {}  (id 0x{:04x})",
                     sw.addr, part, ident & 0x0F, ident)

    status = sw.read(marvell.GLOBAL1, G1_STATUS)
    ctx.out.line("Global1 status 0x{:04x}: init {}  (bits 15:12 are Reserved "
                 "in the document and not zero on the part — see the module "
                 "comment)", status,
                 "ready" if status & G1_STATUS_INIT_READY else "not ready")

    ctx.out.line("{} ports, registers at 0x{:02x}+n, Global3 at 0x{:02x}",
                 PORTS, PORT_BASE, GLOBAL3)
    ctx.out.line("PHY addresses: {} internal, {} SERDES (fibre regs on page "
                 "1), {} external slots",
                 ",".join(str(a) for a in INTERNAL_PHY),
                 ",".join("0x{:02x}".format(a) for a in INTERNAL_SERDES),
                 ",".join(str(a) for a in EXTERNAL_PHY))


def _ports(ctx, args):
    # PHYDetect, and port 4's Auto-Media side effect, are this family's
    # business; the rest of `port` is cli.mvport's.
    if not args:
        raise CommandError(_PORT_USAGE)

    sw = _switch(args[0])
    rest, action, value = mvport.take_verb(args[1:])
    ports = mvport.port_list(rest, action, PORTS, _PORT_USAGE)

    if action is not None:
        # The loose identifier check in `_switch` exists because this
        # family's own product number is in no document here, so an
        # unrecognised one may still be the right part. That reasoning was
        # written when every command only read. A verb that writes deserves
        # the stricter test the other two helpers apply up front: on a part
        # with a different map, PortState and the delay bits land somewhere
        # else entirely.
        ident = sw.read(_port(0), 0x03)
        if (ident >> 4) not in PARTS:
            raise CommandError(
                "identifier 0x{:04x} is not a number this helper knows, and "
                "`{}` writes. Reading is allowed on an unknown number here "
                "because this family's own is in no document; writing into "
                "the wrong register map is not".format(ident, action))

    p = _ports_of(sw)

    if action == "detect":
        if value not in ("on", "off"):
            raise CommandError("detect takes on or off")
        for n in ports:
            # Port 4 is Auto-Media between its GPHY and the SERDES, and
            # clearing its PHYDetect turns that off (FS p. 215) — a side
            # effect the 6321 has no equivalent of.
            if n == 4 and value == "off":
                ctx.out.line("port 4: clearing PHYDetect also disables "
                             "Auto-Media between the PHY and the SERDES")
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

    On this family the SMI address of a port's PHY is the port number, for
    the internal five and the two external slots alike, and one window
    reaches them all. No PPU precondition, unlike the 88E6321: "software can
    access all of the PHY registers at any time by using the SMI Command and
    Data registers" (DS p. 52).
    """
    try:
        bmcr = sw.phy_read(n, mvport.BMCR)
    except OSError:
        return None
    if bmcr == 0xFFFF:
        return None
    # No broadcast refusal here, and that is the point: this family's
    # internal PHYs are SMI 0x00 to 0x04, so port 0's PHY is *at* address 0
    # legitimately, behind the switch's own window where no external part can
    # answer. Its external slots are 5 and 6 and never 0.
    return (n, bmcr,
            lambda v: sw.phy_write(n, mvport.BMCR, v),
            "PHY {}".format(n))


def _serdes_page(sw, addr, value=None):
    """Read or write the SERDES BMCR with the fibre page selected.

    Unlike the 88E6321, this family's SERDES does not come up on page 1: the
    page register reads 0x0000 out of reset and has to be written, measured
    on an 88E6240 rev 1. So every access is page-select, access, page-back —
    and the page is put back even if the access raises, because leaving a
    SERDES pointed at page 1 makes every later plain read of it look wrong.
    """
    bus = marvell.SwitchPhy(sw, addr)
    bus.write(PAGE_SELECT, PAGE_FIBER)
    try:
        if value is None:
            return bus.read(mvport.BMCR)
        bus.write(mvport.BMCR, value)
        return None
    finally:
        bus.write(PAGE_SELECT, 0x0000)


def _serdes_at(sw, n):
    """The SERDES that serves port `n`, if the port has one."""
    addr = SERDES_AT.get(n)
    if addr is None:
        return None
    try:
        bmcr = _serdes_page(sw, addr)
    except OSError:
        return None
    if bmcr == 0xFFFF:
        return None
    return (addr, bmcr,
            lambda v: _serdes_page(sw, addr, v),
            "SERDES 0x{:02x} page 1".format(addr))


def _ports_of(sw):
    return mvport.Ports(sw, "mv6240", PORT_BASE, PORTS, RGMII_PORTS, SPEED,
                        CMODE, lambda n: _phy_at(sw, n),
                        lambda n: _serdes_at(sw, n))


# --- reset ----------------------------------------------------------

def _reset(ctx, args):
    if len(args) != 1:
        raise CommandError("usage: mv6240 reset <sw>")

    sw = _switch(args[0])
    ctl = sw.read(marvell.GLOBAL1, G1_CONTROL)

    if ctl == 0xFFFF:
        raise CommandError("Global1 control reads 0xffff — no answer from the "
                           "switch, refusing to write a guess into it")

    # Bit 14 left as read: Reserved on this family, and not a PPU enable —
    # and it reads as a one (0x4001 on an 88E6240 rev 1), so writing the
    # register back from a constant would clear something the part holds set.
    # The specification asks for all ports to be disabled and 2 ms of quiet
    # before the reset; that is left to the operator rather than done here,
    # because disabling ports is a bigger decision than a reset command
    # should take on its own.
    sw.write(marvell.GLOBAL1, G1_CONTROL, ctl | G1_CONTROL_SW_RESET)

    last = 0
    for _ in range(100):
        last = sw.read(marvell.GLOBAL1, G1_STATUS)
        if last != 0xFFFF and (last & G1_STATUS_INIT_READY):
            ctx.out.line("Global1 status 0x{:04x}: init ready", last)
            return
        time.sleep_ms(10)
    raise CommandError(
        "switch did not report init-ready (status 0x{:04x})".format(last))


# --- probe ------------------------------------------------------------

# Register 3 bits 9:4 are a model number, and on this family they are
# published and different for the two blocks: the copper PHY reads "Always
# 101011" (FS p.438, Table 316) and the SERDES "Always 101010" (FS p.477,
# Table 379). Nowhere else in the Link Street line does the identifier say
# what kind of block answered. Both hold: the five internal PHYs read
# 0x01410eb1 and the SERDES 0x01410ea1.
MODEL_COPPER = 0x2B
MODEL_SERDES = 0x2A

# Fibre/SERDES registers live on page 1 and, unlike the 88E6321, the page
# register does not come up there: "the Page Address (Reg 22) must first be
# set to 0x1" (FS p.472, Table 374). Confirmed — register 22 reads 0x0000
# straight out of a software reset, and reads of 2 and 3 on page 0 return
# zeros rather than an identifier. Write exactly 0x0001: bits 15:14 are
# the Ignore PHYAD broadcast bits, and catching one of those sends the write
# to every port at once.
PAGE_SELECT = 22
PAGE_FIBER = 0x0001


def _kind(ident):
    model = (ident >> 4) & 0x3F

    if (ident >> 16) != 0x0141:
        return ""
    if model == MODEL_COPPER:
        return "copper PHY (model 0x2b)"
    if model == MODEL_SERDES:
        return "SERDES (model 0x2a)"
    return "Marvell, model 0x{:02x}".format(model)


def _polled(sw, addr):
    """Whether the PPU visits the port this address belongs to.

    Reported, never written. On an 88E6321 clearing PHYDetect made no
    difference to reading registers 2 and 3 through the window, and an
    88E6240 agrees: the RTL8211Fs on its ports 5 and 6 both answered the
    window with the bit clear.

    Setting it is not cosmetic, though, which is why it is not done here.
    Measured on port 5: the status register went from 0x0e07 to 0x1007, link
    up to link down, because with the bit set the register reports the PHY
    the PPU polls rather than the forced state — and that PHY had no cable.
    `port <sw> detect on` is where that decision lives.
    """
    if addr >= PORTS:
        return ""

    try:
        status = sw.read(_port(addr), 0x00)
    except OSError:
        return ""
    return "" if status & PORT_STATUS_PHY_DETECT else ", PPU not polling port"


def _probe(ctx, args):
    # All 32 addresses: an external PHY strapped to an address that does not
    # match its port number is invisible to the PPU but perfectly readable
    # through the window.
    #
    # No PPU check here, and that is the one place this family contradicts
    # the 88E6321 outright -- "software can access all of the PHY registers
    # at any time by using the SMI Command and Data registers" (DS p.52).
    if len(args) != 1:
        raise CommandError("usage: mv6240 probe <sw>")

    sw = _switch(args[0])
    found = 0

    for addr in range(32):
        try:
            what = cmd_probe.identify(marvell.SwitchPhy(sw, addr))
        except OSError as exc:
            ctx.out.line("addr {:02x}: read failed, errno {}", addr,
                         exc.args[0] if exc.args else "?")
            continue
        if what is None:
            continue
        cmd_probe.report(ctx, "addr {:02x}".format(addr), what,
                         _kind(what[0]) + _polled(sw, addr) +
                         (BCAST_NOTE if addr == 0 and
                          addr in EXTERNAL_PHY else ""))
        found += 1

    found += _probe_fiber(ctx, sw)
    ctx.out.line("{} address(es) answered", found)


def _probe_fiber(ctx, sw):
    # Only the addresses the map calls SERDES get the page written. Setting
    # page 1 on all 32 to look for a SERDES somewhere unexpected would mean
    # writing a page register into whatever happens to answer, which is not
    # a thing to do to an unidentified part.
    found = 0

    for addr in INTERNAL_SERDES:
        bus = marvell.SwitchPhy(sw, addr)
        try:
            bus.write(PAGE_SELECT, PAGE_FIBER)
            what = cmd_probe.identify(bus)
        finally:
            bus.write(PAGE_SELECT, 0x0000)

        if what is None:
            continue
        cmd_probe.report(ctx, "addr {:02x}".format(addr), what,
                         "{}, page 1".format(_kind(what[0])))
        found += 1

    return found


_GROUPS = {
    "id": _id,
    "port": _ports,
    "probe": _probe,
    "reset": _reset,
}


@command("mv6240", category="mdio", syntax="mv6240 id|port|reset <sw> ...",
         summary="88E6352/6240/6176/6172: identify, port status and control, reset",
         detail=_USAGE + """

VERIFIED on an 88E6240 rev 1, identifier 0x2401 — identification, port status
and `probe` on 2026-08-04, and the port operations on 2026-08-22 against a
part in single-chip mode with a patch cord between ports 0 and 3.

Ports are at device address 0x10+n as on the 88E6321, but the PHY map is
different: five internal PHYs on ports 0-4 at SMI addresses 0x00-0x04, one
SERDES at 0x0F whose fibre registers are on page 1, and external PHYs on
ports 5 and 6 only.

Three of the `port` verbs are port operations rather than register edits, and
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

`rgmii none|rx|tx|both` writes bits 15:14 of Physical Control, always both, so
the setting is stated rather than accumulated. It refuses on any port but 5
and 6 — Table 62 says those bits are valid there and nowhere else on this
family — and while the port's link is up, because the same table says the
change is disruptive and must be made with the link down.

Anything that writes wants an explicit port list. `port <sw> state
forwarding` with no list would mean all seven.

Global3 lives at 0x1D. All of the map above was read off a part rather than
only off the documents.

There is no `extbus`, and that is not an omission. This family has no
NormalSMI bit: the external MDIO_PHY/MDC_PHY pins are selected by the
P5_MODE straps at reset and no register moves them afterwards. Scratch index
0x63 — NormalSMI on the 88E6321 — is a GPIO direction register here, so do
not carry that command across.

There is also no PPU precondition. The datasheet says software may use the
Global2 SMI PHY window at any time, which is the opposite of the 88E6321,
and is why `mv phy` no longer enforces anything on its own. Every read here
went through that window with nothing done about the PPU, which is as far as
the claim can be tested: this family exposes no way to stop the PPU, so the
failing case cannot be produced to compare against.

Global1 0x00 is documented as having no PPUState field, so `id` reports only
init-ready — but bits 15:12 are not zero on the part and bit 15 reads one.
The module comment has the measurement.

`probe` names every PHY behind the switch through the SMI PHY window, over
all 32 addresses rather than only the mapped ones: a PHY strapped to an
address that does not match its port number is invisible to the PPU and
perfectly readable here. There is no PPU check, for the reason above.

Register 3 carries a published model number on this family, so the output
says whether a copper PHY (0x2b) or a SERDES (0x2a) answered — nowhere else
in the Link Street line does the identifier tell you that. The SERDES is read
a second time with register 22 set to page 1, which is where its registers
live; only the mapped SERDES address gets that write, and it is put back.
PHYDetect is read and reported, never set.
""")
def cmd_mv6240(ctx, args):
    if not args:
        raise CommandError(_USAGE)

    group = _GROUPS.get(args[0])
    if group is None:
        raise CommandError(_USAGE)
    group(ctx, args[1:])
