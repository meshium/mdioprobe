"""88E6390 / 88E6390X / 88E6190 / 88E6190X / 88E6290.

Load with `use mv6390`. Copy to /flash/lib on the probe. NOT VERIFIED.
"""

# NOT VERIFIED. Not one line of this has been executed against the silicon —
# it is written from the documents, and the first person with one of these
# parts should treat every command as a hypothesis. Sources:
#
#   FS  Link Street 88E6390X/88E6390/88E6290/88E6190X/88E6190 Functional
#       Specification, Doc. TD-001000 Rev. 1
#   RN  MV-S302664-00, 88E6390/88E6290/88E6190 Rev A0 release notes
#
# What differs from the 88E6321, and why this file exists at all:
#
#  * port registers start at device address 0x00, not 0x10 (FS Table 42,
#    p. 185; corroborated by the Port Based VLAN Map text, "Port0 (SMI
#    Device Address 0x00)"). The 6321 formula lands in reserved space.
#  * the SMI PHY command register has a function field in bits 14:13 that
#    the other families leave Reserved (FS Table 262, p. 367): 0 internal
#    access, 1 external, 2 SMI setup. `mv phy` always sends 0, so it reaches
#    internal PHYs and SERDES only.
#  * NormalSMI is scratch index 0x02, not 0x63, and it moves P0_COL/P0_CRS
#    rather than P5_ (FS Table 270, p. 372; RN §4.6). Index 0x63 here is
#    GPIO Direction [15:8] (FS Table 306, p. 395) — writing the 6321 value
#    into it would reconfigure GPIO pins as outputs.
#  * there is no PPU enable. Global1 0x04 bit 14 is "Reserved ... read as a
#    one and must be written as a one" (FS Table 151, p. 294). No `solo`.
#
# And one erratum to read first (RN §3.13, Rev A0, all three parts): after a
# read or a write the switch may not drive MDIO low after the address cycle.
# The data is correct, only the acknowledgement is missing, and the release
# note's own remedy is for the master to tolerate it. Our master treats a
# missing turnaround as "nobody answered", so on an affected part every read
# fails until mdioprobe.c22_strict_ta(False) — deliberately not automatic,
# because with the check off a collision starts looking like data.
#
# Keep helpers under about 9 KB of code: MicroPython compiles them on the
# device and a larger one exhausts the GC heap. `./build.sh` cross-compiles
# the helpers into build/lib, which removes that limit.

import time

from cli import marvell
from cli import cmd_probe
from cli.cmd_mdio import apply_modify
from cli.parser import ranged, spec, u16
from cli.registry import CommandError, command

VERIFIED = False

# FS Table 61, p. 204. The 88E6290 is absent from that table and from the
# datasheet, so its number is genuinely unknown rather than omitted here.
PARTS = {
    0x390: "88E6390",
    0x190: "88E6190",
    0x0A1: "88E6390X",
    0x0A0: "88E6190X",
}

PORT_BASE = 0x00
PORTS = 11
CPU_PORT = 0x1E          # FS Table 42: internal CPU (IMP) port

# FS §8.3, p. 184. Reachable only through the Global2 window — there is no
# direct SMI path to them.
INTERNAL_PHY = (1, 2, 3, 4, 5, 6, 7, 8)
INTERNAL_SERDES = (9, 10)

# 6390X and 6190X break each 10G SERDES into four lanes, and the extra three
# of each appear at their own internal SMI addresses (FS p.184): port 9 lanes
# 1-3 at 0x12-0x14, port 10 lanes 1-3 at 0x15-0x17. On a plain 6390 nothing
# answers there, which is exactly what a sweep should show.
SERDES_LANES = (0x12, 0x13, 0x14, 0x15, 0x16, 0x17)
EXTERNAL_PHY = (0, 9, 10)

G1_STATUS = 0x00
G1_CONTROL = 0x04
G1_STATUS_INIT_POLLING = 0x8000   # bit 15 InitState: 1 = PPU polling
G1_STATUS_INIT_READY = 0x0800
G1_CONTROL_SW_RESET = 0x8000

# FS Table 270 / RN §4.6.
SCRATCH_MISC_CFG = 0x02
SCRATCH_MISC_NORMAL_SMI = 0x80
SCRATCH_CONFIG1 = 0x71
SCRATCH_CONFIG1_NO_CPU = 0x04

PORT_STATUS_PHY_DETECT = 0x1000

# FS Table 44, p. 187. Unlike the 6321, 0x3 is a real speed here; which of
# the two alternates applies is selected by the port's AltSpeed bit.
SPEED = ("10 Mbps", "100/200 Mbps", "1000 Mbps", "2500 Mbps or 10 Gb")

# SMI PHY Command, Global2 0x18 (FS Table 262). Bit 15 busy, bits 14:13
# function, bit 12 SMIMode (1 = Clause 22), bits 11:10 operation.
SMI_FUNC_INTERNAL = 0x0
SMI_FUNC_EXTERNAL = 0x1
SMI_FUNC_SETUP = 0x2
_BUSY = 0x8000
_C22 = 0x1000
_OP_WRITE = 0x0400
_OP_READ = 0x0800

_USAGE = """usage:
  mv6390 id     <sw>
  mv6390 port   <sw> [ports] [detect on|off]
  mv6390 phy    read|write <sw> int|ext <addr> <reg> [value]
  mv6390 probe  <sw>
  mv6390 smimap <sw>                       SMI address translation table
  mv6390 reset  <sw>
  mv6390 extbus <sw> [on|off]"""


def _port(n):
    return PORT_BASE + n


def _switch(token, check=True):
    sw = marvell.Switch(ranged(token, 0, 31, "switch address"))

    if check:
        ident = sw.read(_port(0), 0x03)
        if (ident >> 4) not in PARTS:
            raise CommandError(
                "identifier 0x{:04x} is not a part this helper knows; the "
                "88E6290's number is absent from the spec, so it lands here "
                "too. `mv reg` for raw access".format(ident))
    return sw


# --- the SMI PHY window, with the function field ----------------------
#
# Built here rather than using marvell.Switch.phy_read, because that always
# sends function 0. Composing it out of sw.read/sw.write on Global2 is the
# whole point of keeping the frozen layer free of chip knowledge: a family
# that needs a different command word does not need a firmware change.

def _phy_wait(sw):
    for _ in range(16):
        if (sw.read(marvell.GLOBAL2, marvell.G2_PHY_CMD) & _BUSY) == 0:
            return
        time.sleep_ms(1)
    raise CommandError("Global2 PHY command stayed busy")


def phy_read(sw, func, addr, reg):
    _phy_wait(sw)
    sw.write(marvell.GLOBAL2, marvell.G2_PHY_CMD,
             _BUSY | (func << 13) | _C22 | _OP_READ |
             ((addr & 0x1F) << 5) | (reg & 0x1F))
    _phy_wait(sw)
    return sw.read(marvell.GLOBAL2, marvell.G2_PHY_DATA)


def phy_write(sw, func, addr, reg, value):
    _phy_wait(sw)
    sw.write(marvell.GLOBAL2, marvell.G2_PHY_DATA, value & 0xFFFF)
    sw.write(marvell.GLOBAL2, marvell.G2_PHY_CMD,
             _BUSY | (func << 13) | _C22 | _OP_WRITE |
             ((addr & 0x1F) << 5) | (reg & 0x1F))
    _phy_wait(sw)


def _phy(ctx, args):
    if len(args) < 4:
        raise CommandError(
            "usage: mv6390 phy read|write <sw> int|ext <addr> <reg> [value]")

    action = args[0]
    if action not in ("read", "write"):
        raise CommandError("mv6390 phy takes read or write")

    sw = _switch(args[1])
    if args[2] not in ("int", "ext"):
        raise CommandError("say int or ext: the function field picks which "
                           "SMI bus, and both can carry the same address")
    func = SMI_FUNC_INTERNAL if args[2] == "int" else SMI_FUNC_EXTERNAL
    addr = ranged(args[3], 0, 31, "PHY address")
    rest = args[4:]

    if action == "read":
        registers = spec(rest[0], 0, 31, "register") if rest else range(32)
        for r in registers:
            ctx.out.line("sw 0x{:02x} {} phy {:02d} reg 0x{:02x} = 0x{:04x}",
                         sw.addr, args[2], addr, r, phy_read(sw, func, addr, r))
        return

    if len(rest) != 2:
        raise CommandError("need a register and a value")
    reg = ranged(rest[0], 0, 31, "register")
    value = u16(rest[1])
    phy_write(sw, func, addr, reg, value)
    ctx.out.line("sw 0x{:02x} {} phy {:02d} reg 0x{:02x} <- 0x{:04x}",
                 sw.addr, args[2], addr, reg, value)


def _smimap(ctx, args):
    # Read the SMI Device Address Translation Table. FS Table 264, p. 369:
    # with the function field set to SMI Setup the low ten bits become a
    # pointer, and 0x000-0x01F select this table. It decides which SMI
    # address the PPU polls for each port; External-Access bypasses it.
    #
    # Read only, deliberately. Changing an entry also requires restarting
    # the PPU, and the documented sequence goes through the hidden register
    # pair at Port 4 / Port 5 offset 0x1A (RN §4.3):
    #
    #     Write Global 1 Register 0x4 = 0x0001
    #     Write Port 5 Register 0x1A = 0xA42E
    #     Write Port 4 Register 0x1A = 0xC3E6
    #     Write Global 1 Register 0x4 = 0x4001
    #
    # The release notes warn that "access to incorrect registers can disrupt
    # the operation of the switch". Writing them blind, into a part nobody
    # here has ever seen, is not a good trade for saving four `mv reg write`
    # commands.
    if len(args) != 1:
        raise CommandError("usage: mv6390 smimap <sw>")

    sw = _switch(args[0])

    for n in range(PORTS):
        _phy_wait(sw)
        sw.write(marvell.GLOBAL2, marvell.G2_PHY_CMD,
                 _BUSY | (SMI_FUNC_SETUP << 13) | _OP_READ | n)
        _phy_wait(sw)
        value = sw.read(marvell.GLOBAL2, marvell.G2_PHY_DATA)
        ctx.out.line("port {:2d} -> SMI address 0x{:02x}{}", n, value & 0x1F,
                     "  (identity)" if (value & 0x1F) == n else "")


# --- identify ---------------------------------------------------------

def _id(ctx, args):
    if len(args) != 1:
        raise CommandError("usage: mv6390 id <sw>")

    sw = _switch(args[0], check=False)
    ident = sw.read(_port(0), 0x03)
    part = PARTS.get(ident >> 4)

    ctx.out.line("written from the documents; never run against silicon")

    if part is None:
        ctx.out.line("switch 0x{:02x}: identifier 0x{:04x} — unknown here; "
                     "expected 0x390x, 0x190x, 0x0A1x or 0x0A0x",
                     sw.addr, ident)
        return

    ctx.out.line("switch 0x{:02x}: {} rev {}  (id 0x{:04x})",
                 sw.addr, part, ident & 0x0F, ident)

    status = sw.read(marvell.GLOBAL1, G1_STATUS)
    ctx.out.line("Global1 status 0x{:04x}: PPU {}, init {}", status,
                 "polling" if status & G1_STATUS_INIT_POLLING else "initialising",
                 "ready" if status & G1_STATUS_INIT_READY else "not ready")

    ctx.out.line("{} ports at 0x{:02x}+n, CPU port 0x{:02x}",
                 PORTS, PORT_BASE, CPU_PORT)
    ctx.out.line("PHY: {} internal, {} SERDES, {} external",
                 ",".join(str(a) for a in INTERNAL_PHY),
                 ",".join(str(a) for a in INTERNAL_SERDES),
                 ",".join(str(a) for a in EXTERNAL_PHY))


# --- port status ------------------------------------------------------

def _ports(ctx, args):
    if not args:
        raise CommandError("usage: mv6390 port <sw> [ports] [detect on|off]")

    sw = _switch(args[0])
    args = args[1:]
    want = None

    if len(args) >= 2 and args[-2] == "detect":
        if args[-1] not in ("on", "off"):
            raise CommandError("detect takes on or off")
        want = args[-1] == "on"
        args = args[:-2]

    if len(args) > 1:
        raise CommandError("usage: mv6390 port <sw> [ports] [detect on|off]")
    ports = spec(args[0], 0, PORTS - 1, "port") if args else range(PORTS)

    if want is not None:
        # FS Table 44: must not be written while InitState says initialising.
        if not (sw.read(marvell.GLOBAL1, G1_STATUS) & G1_STATUS_INIT_POLLING):
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

        ctx.out.line("port {:2d}: 0x{:04x}  {}, {}{}{}", n, v,
                     "link up" if v & 0x0800 else "link down",
                     "PHY detected" if v & PORT_STATUS_PHY_DETECT
                     else "no PHY detected",
                     ", {}".format(SPEED[(v >> 8) & 3]) if v & 0x0800 else "",
                     ", full duplex" if (v & 0x0800) and (v & 0x0400) else
                     (", half duplex" if v & 0x0800 else ""))


# --- reset ------------------------------------------------------------

def _reset(ctx, args):
    if len(args) != 1:
        raise CommandError("usage: mv6390 reset <sw>")

    sw = _switch(args[0])
    ctl = sw.read(marvell.GLOBAL1, G1_CONTROL)

    if ctl == 0xFFFF:
        raise CommandError("Global1 control reads 0xffff — no answer from the "
                           "switch, refusing to write a guess into it")

    # Bit 14 is left exactly as read: the specification says it must be
    # written as a one, and unlike the 6321 it is not a PPU enable here.
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


# --- external PHY bus -------------------------------------------------

def _routed(cfg, config1):
    """FS Table 270: NormalSMI set gives MDIO_PHY/MDC_PHY on P0_COL/P0_CRS
    if the NO_CPU strap was one; clearing the bit inverts that."""
    return bool(cfg & SCRATCH_MISC_NORMAL_SMI) == bool(config1 &
                                                       SCRATCH_CONFIG1_NO_CPU)


def _extbus(ctx, args):
    if not args or len(args) > 2:
        raise CommandError("usage: mv6390 extbus <sw> [on|off]")

    sw = _switch(args[0])

    if len(args) == 2:
        if args[1] not in ("on", "off"):
            raise CommandError(
                "usage: mv6390 extbus <sw> [on|off]; no `solo` on this "
                "family, no PPU enable is documented")
        want = args[1] == "on"

        cfg = sw.scratch_read(SCRATCH_MISC_CFG)
        config1 = sw.scratch_read(SCRATCH_CONFIG1)
        no_cpu = bool(config1 & SCRATCH_CONFIG1_NO_CPU)
        bit = SCRATCH_MISC_NORMAL_SMI if (want == no_cpu) else 0
        new = (cfg & ~SCRATCH_MISC_NORMAL_SMI) | bit

        if new != cfg:
            sw.scratch_write(SCRATCH_MISC_CFG, new)
            ctx.out.line("pins now {}",
                         "MDIO_PHY/MDC_PHY" if want else "GPIO")

    cfg = sw.scratch_read(SCRATCH_MISC_CFG)
    config1 = sw.scratch_read(SCRATCH_CONFIG1)

    ctx.out.line("scratch 0x02 = 0x{:02x} (NormalSMI {})", cfg,
                 1 if cfg & SCRATCH_MISC_NORMAL_SMI else 0)
    ctx.out.line("scratch 0x71 = 0x{:02x} (NO_CPU {})", config1,
                 1 if config1 & SCRATCH_CONFIG1_NO_CPU else 0)
    ctx.out.line("pins:  {}", "MDIO_PHY/MDC_PHY (P0_COL/P0_CRS)"
                 if _routed(cfg, config1) else "GPIO[7:8]")


# --- probe ------------------------------------------------------------

def _role(func, addr):
    if func == SMI_FUNC_EXTERNAL:
        return "external" if addr in EXTERNAL_PHY else "external, off the map"
    if addr in INTERNAL_PHY:
        return "internal PHY"
    if addr in INTERNAL_SERDES:
        return "SERDES"
    if addr in SERDES_LANES:
        return "SERDES lane (6390X/6190X)"
    return "off the map"


def _polled(sw, func, addr):
    """Whether the PPU visits the port this address belongs to.

    Reported, never written. On ports 9 and 10 the bit is not a polling hint
    at all — it selects 1000BASE-X against SGMII (FS p.191) — which is one
    more reason for a probe to read it and leave it alone.
    """
    if func != SMI_FUNC_INTERNAL or addr >= PORTS:
        return ""

    try:
        status = sw.read(_port(addr), 0x00)
    except OSError:
        return ""
    return "" if status & PORT_STATUS_PHY_DETECT else ", PPU not polling port"


def _probe(ctx, args):
    # Two sweeps, not one, and the function is printed on every line. The
    # release notes are explicit that this is deliberate: "This allows
    # external devices to have the same SMI address internal PHYs and
    # SERDES" (§4.2). Merge the two and an internal PHY at 0x09 and an
    # external one at 0x09 become a single wrong line.
    if len(args) != 1:
        raise CommandError("usage: mv6390 probe <sw>")

    sw = _switch(args[0])

    if not (sw.read(marvell.GLOBAL1, G1_CONTROL) & 0x4000):
        raise CommandError(
            "Global1 0x04 bit 14 is clear. The spec calls it Reserved and "
            "requires it written as one; the release notes stop and restart "
            "the PPU with it (§4.3), and with the PPU stopped the SMI window "
            "answers 0xffff to everything rather than failing")

    if not _routed(sw.scratch_read(SCRATCH_MISC_CFG),
                   sw.scratch_read(SCRATCH_CONFIG1)):
        ctx.out.line("note: the external bus is on GPIO — the external sweep "
                     "cannot answer. `mv6390 extbus <sw> on`")

    found = 0

    for func, tag in ((SMI_FUNC_INTERNAL, "int"),
                      (SMI_FUNC_EXTERNAL, "ext")):
        for addr in range(32):
            try:
                what = cmd_probe.identify(_Window(sw, func, addr))
            except OSError as exc:
                ctx.out.line("{} {:02x}: read failed, errno {}", tag,
                             addr, exc.args[0] if exc.args else "?")
                continue
            if what is None:
                continue
            cmd_probe.report(ctx, "{} {:02x}".format(tag, addr), what,
                             _role(func, addr) + _polled(sw, func, addr))
            found += 1

    ctx.out.line("{} address(es) answered", found)


class _Window:
    """A PHY behind this switch, on the bus the function field selects.

    `marvell.SwitchPhy` cannot be used: it goes through
    `marvell.Switch.phy_read`, which always sends function 0 and so never
    leaves the internal bus.
    """

    def __init__(self, sw, func, addr):
        self._sw = sw
        self._func = func
        self._addr = addr

    def read(self, reg):
        return phy_read(self._sw, self._func, self._addr, reg)

    def write(self, reg, value):
        phy_write(self._sw, self._func, self._addr, reg, value)


_GROUPS = {
    "id": _id,
    "port": _ports,
    "phy": _phy,
    "probe": _probe,
    "smimap": _smimap,
    "reset": _reset,
    "extbus": _extbus,
}


@command("mv6390", category="mdio",
         syntax="mv6390 id|port|phy|probe|smimap|reset|extbus <sw> ...",
         summary="88E6390/6290/6190: identify, ports, PHY window, external bus",
         detail=_USAGE + """

WRITTEN FROM THE DOCUMENTS, NEVER RUN. Check it against your part.

Port registers start at 0x00, not 0x10. The SMI PHY command register has a
function field in bits 14:13 that other families leave Reserved: 0 reaches
internal PHYs and SERDES, 1 the external bus, and the two may carry the same
addresses — hence `mv6390 phy ... int|ext`, since plain `mv phy` sends 0.

NormalSMI is scratch index 0x02; index 0x63 is a GPIO direction register
here. There is no `solo` — no PPU enable is documented on this family.

Rev A0 errata 3.13: the switch may not drive MDIO low after the address
cycle, which our master reads as "nobody answered". Reads fail until
mdioprobe.c22_strict_ta(False) from the REPL.

`probe` sweeps all 32 addresses **twice**, once on each SMI function, and
prints which one answered. The release notes say plainly that an external
device may share an address with an internal PHY or SERDES, so merging the
two sweeps would turn two parts into one wrong line.

It refuses with the PPU stopped: the window then answers 0xffff to
everything instead of failing, which reads as an empty switch.

Nothing is written. PHYDetect is reported, not set — and on ports 9 and 10 it
is not a polling hint at all but a 1000BASE-X/SGMII selector, which is one
more reason to leave it alone.

Written from the specification and the release notes, not from silicon.
""")
def cmd_mv6390(ctx, args):
    if not args:
        raise CommandError(_USAGE)

    group = _GROUPS.get(args[0])
    if group is None:
        raise CommandError(_USAGE)
    group(ctx, args[1:])
