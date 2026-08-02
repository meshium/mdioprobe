"""88E6352 / 88E6240 / 88E6176 / 88E6172.

Load with `use mv6240`. Copy to /flash/lib on the probe.

**NOT VERIFIED.** Written from the documents; nothing here has been executed
against the silicon. Sources:

    FS   88E6352/88E6240/88E6176/88E6172 Functional Specification
    DS   the matching datasheet

What differs from the 88E6321:

  * five internal PHYs on ports 0-4, at SMI device addresses 0x00-0x04, and
    a single SERDES at 0x0F — not two at 0x0C/0x0D (FS Figure 60, p. 211;
    FS Table 60, p. 216). The fibre registers on that SERDES live on page 1,
    so register 22 has to be set to 0x1 first (FS Table 374, p. 472).
  * external PHYs attach to ports 5 and 6 only, and their SMI addresses must
    equal the port number for the PPU to poll them (DS p. 52).
  * Global3 exists at device address 0x1D (TCAM, 6352/6240 only).
  * Global1 0x00 bits 15:12 are Reserved — there is no PPUState here at all
    (FS Table 94, p. 262), so a poll on bit 15 reads a constant zero.
  * Global1 0x04 bit 14 is plain "Reserved for future use" (FS Table 98,
    p. 266). No PPUEn field exists in the specification.

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
from cli.cmd_mdio import apply_modify
from cli.parser import ranged, spec
from cli.registry import CommandError, command

VERIFIED = False

# FS Table 64, p. 221. The 88E6240's own product number is not printed in
# either document — see _id() for what that costs.
PARTS = {
    0x352: "88E6352",
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

_USAGE = """usage:
  mv6240 id    <sw>
  mv6240 port  <sw> [ports] [detect on|off]
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
    """Identify, and be honest about the one part this cannot name.

    The specification lists product numbers for the 6352, 6176 and 6172 but
    not for the 6240 itself. Refusing everything unrecognised would make a
    helper called mv6240 useless on an 88E6240, so an unknown number is
    reported as unknown and the other commands are still allowed — the
    register map is shared across the document, which is the whole reason
    these four are in one specification.
    """
    if len(args) != 1:
        raise CommandError("usage: mv6240 id <sw>")

    sw = _switch(args[0], check=False)
    ident = sw.read(_port(0), 0x03)
    part = PARTS.get(ident >> 4)

    ctx.out.line("this helper is written from the documents and has never "
                 "been run against the silicon")

    if part is None:
        ctx.out.line("switch 0x{:02x}: identifier 0x{:04x} — not one of the "
                     "numbers the specification prints", sw.addr, ident)
        ctx.out.line("  it lists 0x352x, 0x176x and 0x172x and never gives "
                     "the 88E6240's own, so this may still be the right "
                     "family; check the part marking")
    else:
        ctx.out.line("switch 0x{:02x}: {} rev {}  (id 0x{:04x})",
                     sw.addr, part, ident & 0x0F, ident)

    status = sw.read(marvell.GLOBAL1, G1_STATUS)
    ctx.out.line("Global1 status 0x{:04x}: init {}  (no PPUState on this "
                 "family — bits 15:12 are Reserved)", status,
                 "ready" if status & G1_STATUS_INIT_READY else "not ready")

    ctx.out.line("{} ports, registers at 0x{:02x}+n, Global3 at 0x{:02x}",
                 PORTS, PORT_BASE, GLOBAL3)
    ctx.out.line("PHY addresses: {} internal, {} SERDES (fibre regs on page "
                 "1), {} external slots",
                 ",".join(str(a) for a in INTERNAL_PHY),
                 ",".join("0x{:02x}".format(a) for a in INTERNAL_SERDES),
                 ",".join(str(a) for a in EXTERNAL_PHY))


def _ports(ctx, args):
    if not args:
        raise CommandError("usage: mv6240 port <sw> [ports] [detect on|off]")

    sw = _switch(args[0])
    args = args[1:]
    want = None

    if len(args) >= 2 and args[-2] == "detect":
        if args[-1] not in ("on", "off"):
            raise CommandError("detect takes on or off")
        want = args[-1] == "on"
        args = args[:-2]

    if len(args) > 1:
        raise CommandError("usage: mv6240 port <sw> [ports] [detect on|off]")
    ports = spec(args[0], 0, PORTS - 1, "port") if args else range(PORTS)

    if want is not None:
        for n in ports:
            # Port 4 is Auto-Media between its GPHY and the SERDES, and
            # clearing its PHYDetect turns that off (FS p. 215) — a side
            # effect the 6321 has no equivalent of.
            if n == 4 and not want:
                ctx.out.line("port 4: clearing PHYDetect also disables "
                             "Auto-Media between the PHY and the SERDES")
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


def _reset(ctx, args):
    if len(args) != 1:
        raise CommandError("usage: mv6240 reset <sw>")

    sw = _switch(args[0])
    ctl = sw.read(marvell.GLOBAL1, G1_CONTROL)

    if ctl == 0xFFFF:
        raise CommandError("Global1 control reads 0xffff — no answer from the "
                           "switch, refusing to write a guess into it")

    # Bit 14 left as read: Reserved on this family, and not a PPU enable.
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
# what kind of block answered.
MODEL_COPPER = 0x2B
MODEL_SERDES = 0x2A

# Fibre/SERDES registers live on page 1 and, unlike the 88E6321, the page
# register does not come up there: "the Page Address (Reg 22) must first be
# set to 0x1" (FS p.472, Table 374). Write exactly 0x0001 -- bits 15:14 are
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

    Reported, never written. Measured on an 88E6321 — the family this one is
    written against, not run against — clearing PHYDetect made no difference
    to reading registers 2 and 3 through the window. The bit gates the PPU's
    poll routine, and `port <sw> detect on` is where setting it lives.
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
                         _kind(what[0]) + _polled(sw, addr))
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
         summary="88E6352/6240/6176/6172: identify, port status, reset",
         detail=_USAGE + """

WRITTEN FROM THE DOCUMENTS, NEVER RUN. Nothing here has touched silicon.

Ports are at device address 0x10+n as on the 88E6321, but the PHY map is
different: five internal PHYs on ports 0-4 at SMI addresses 0x00-0x04, one
SERDES at 0x0F whose fibre registers are on page 1, and external PHYs on
ports 5 and 6 only. Global3 lives at 0x1D.

There is no `extbus`, and that is not an omission. This family has no
NormalSMI bit: the external MDIO_PHY/MDC_PHY pins are selected by the
P5_MODE straps at reset and no register moves them afterwards. Scratch index
0x63 — NormalSMI on the 88E6321 — is a GPIO direction register here, so do
not carry that command across.

There is also no PPU precondition. The datasheet says software may use the
Global2 SMI PHY window at any time, which is the opposite of the 88E6321,
and is why `mv phy` no longer enforces anything on its own.

Global1 0x00 has no PPUState field on this family, so `id` reports only
init-ready.

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
