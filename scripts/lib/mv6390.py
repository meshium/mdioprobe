"""88E6390 / 88E6390X / 88E6190 / 88E6190X / 88E6290.

Load with `use mv6390`. Copy to /flash/lib on the probe.

**VERIFIED** on an 88E6390 rev 1 on 2026-08-21: identification, port status,
PHYDetect read and written, the SMI PHY window on both functions, `probe`
across all 32 addresses on all three passes, `smimap`, `extbus` both ways and
a software reset. Where silicon disagreed with the documents it is said so
below, next to the claim it replaced.
"""

# Written from the documents and then run against an 88E6390 rev 1 on
# 2026-08-21. Sources:
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
# fails until `ta relaxed` — deliberately not automatic, because with the
# check off a collision starts looking like data.
#
# Confirmed on silicon 2026-08-21 against an 88E6390 rev 1: nothing whatever
# is driven in the turnaround, and with the check off every read is exact —
# `mv6390 id` reports 88E6390 rev 1 correctly. That last part needs firmware
# from 2026-08-21 or later; before it, the turnaround zero also fixed the
# position of the data field, so tolerating a missing one returned every
# value shifted a bit left.
#
# The size limit is no longer the compiler. Helpers ship as .mpy — `./build.sh`
# cross-compiles them into build/lib — so the old "keep the source under about
# 9 KB, MicroPython compiles it on the device" rule is gone. What replaced it
# is the heap at load time: this file at 11.7 KB of .mpy takes nearly all of
# the ~35 KB free after a soft reset, so exactly one helper fits at a time and
# switching parts needs `use drop` first. `use` prints the free heap when it
# refuses.

import time

from cli import marvell
from cli import cmd_probe
from cli import mvport
from cli.cmd_mdio import apply_modify
from cli.parser import ranged, spec, u16
from cli.registry import CommandError, command

VERIFIED = True

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

# The SERDES answer here, but **not to Clause 22**, which is all `probe` and
# `phy` send. Measured 2026-08-21 on an 88E6390 rev 1: a Clause-22 read of
# 0x09 or 0x0A returns nothing, while the same address reached with SMIMode
# clear (Table 262 bit 12: 0 = Clause 45) and RegAddr = 4 as the device class
# answers. There DevAddr is the port rather than an SMI address, which is
# what the same table says in one line and is easy to read past.
#
#   0x2000-0x2005  a Clause-22 image: 0x1940 0x0149 0x0141 0x0c00 0x0020 0
#   0xf002         0x0003
#   0xf010         0x0500   PCS control 1
#
# So the identifier is 0x01410c00 — the same OUI and model as the internal
# copper PHYs, which the documents we have give nowhere. The BMSR image is
# what tells them apart: 0x0149 here against 0x7949 on copper.
INTERNAL_SERDES = (9, 10)

# 6390X and 6190X break each 10G SERDES into four lanes, and the extra three
# of each appear at their own internal SMI addresses (FS p.184): port 9 lanes
# 1-3 at 0x12-0x14, port 10 lanes 1-3 at 0x15-0x17.
#
# This used to say that nothing answers there on a plain 6390. It is not so.
# Measured 2026-08-21 on a part that identifies as 0x3901 — a 6390, not an X —
# all six lane addresses answer over Clause 45 with the same contents as 0x09
# and 0x0A. So the lanes are present on the die whether or not the part is
# sold as an X, and their silence cannot be used to tell the two apart.
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

# FS Table 44, bit 12, type RWR: set by the device at the end of its init
# routine if SMI register 2 or 3 at this port's address answers with anything
# but all ones, through the Global2 window. Two consequences, both measured
# on 2026-08-21:
#
#   * it survives a software reset, because that does not re-run init;
#   * a port whose external PHY was unreachable at hardware-reset time reads
#     clear for good. Port 0 here has a live RTL8211F on it and reads "no PHY
#     detected", because MDC_PHY/MDIO_PHY were routed to GPIO when the switch
#     came out of reset, so the init routine could not see it.
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

# Clause 45 through the same window. Clearing SMIMode (Table 262 bit 12) does
# more than change the framing: it re-purposes both address fields. DevAddr
# stops being an SMI address and becomes the *port*, RegAddr stops being a
# register and becomes the device class, and the register number itself goes
# through the data register in a separate address cycle. So a read is two
# commands where Clause 22 is one, and the opcodes mean different things.
_OP_C45_ADDR = 0x0000    # write the address register
_OP_C45_WRITE = 0x0400   # write the data register
_OP_C45_READ = 0x0800    # read the data register

# The SERDES answer on one device class and no other: a sweep of 0..7 found
# 4 alone, on both ports, measured 2026-08-21. Inside it the block presents a
# Clause-22 image at 0x2000 — 0x2002 and 0x2003 are the identifier — which is
# why the probe window below offsets by it and can then be handed to the same
# `identify` as any ordinary PHY.
SERDES_DEVAD = 4
SERDES_C22_IMAGE = 0x2000

# Which port has an RGMII interface, and this is the whole of what stays
# here: Table 48 names bits 15:14 "valid on Port 0 only", where the 88E6321
# has them on ports 2, 5 and 6 and the 88E6240 on 5 and 6. Everything else
# about those bits — the numbers, the rule that the link must be down first —
# all three specifications state identically, so it lives in cli.mvport.
#
# One neighbouring bit does *not* generalise and is deliberately untouched:
# bit 13 is ForcedSpd here, where the 6321 and 6240 force the speed by writing
# something other than 0b11 into bits 1:0. Nothing below sets a speed, and
# that difference is the reason.
RGMII_PORTS = (0,)

# Table 45, the Interface Configuration Matrix. Same shape as the 88E6321's
# Table 66 and the 88E6240's Table 60, and different again: C_Mode 0x8 is
# Reserved here where the other two have 100BASE-FX, 0x3 is the internal
# CPU's own port, and 0xB to 0xD are speeds neither of the others reaches.
_PX = "px_enable"
_PPU = "ppu"
CMODE = (
    (("FD MII", _PX), ("FD MII", _PX)),                            # 0x0
    (("MII PHY", _PX), ("MII PHY", _PX)),                          # 0x1
    (("MII MAC", _PX), ("MII to PHY", _PPU)),                      # 0x2
    (("GMII to the internal CPU", "cpu"),
     ("GMII to the internal CPU", "cpu")),                         # 0x3
    (("RMII PHY", _PX), ("RMII to PHY", _PPU)),                    # 0x4
    (("RMII MAC", _PX), ("RMII to PHY", _PPU)),                    # 0x5
    (("xMII tristate", "none"), ("xMII tristate", "none")),        # 0x6
    (("RGMII", _PX), ("RGMII to PHY", _PPU)),                      # 0x7
    None,                                                          # 0x8
    (("1000BASE-X", "serdes"), ("1000BASE-X", "serdes")),          # 0x9
    (("SGMII", "serdes+ppu"), ("SGMII", "serdes+ppu")),            # 0xA
    (("2500BASE-X", "serdes"), ("2500BASE-X", "serdes")),          # 0xB
    (("10 Gb XAUI", "serdes"), ("10 Gb XAUI", "serdes")),          # 0xC
    (("10 Gb RXAUI", "serdes"), ("10 Gb RXAUI", "serdes")),        # 0xD
    None,                                                          # 0xE
    (("PHY", "phy"), ("PHY", "phy")),                              # 0xF
)

# Ports 9 and 10 carry the SERDES, at their own port numbers — but reached
# over Clause 45, device class 4, inside the Clause-22 image at 0x2000, which
# is the only way they answer at all. Table 45 also allows C_Mode 0x9 on
# ports 2 to 7, on the X parts, and where those blocks live is not in the
# document here: such a port reports its SERDES as unreachable rather than
# having one invented for it.
SERDES_PORTS = (9, 10)

# How to spell a deliberate write to the external PHY at address 0.
# Not `mv phy write`: this family's window needs the function field.
PHY_WRITE_HINT = "mv6390 phy write {sw} ext 0 0"

# Only an address on the *external* MDIO bus can carry a broadcast echo.
# An internal PHY answers through the switch's own Global2 window, where
# nothing else can reply, so address 0 there means port 0 and nothing
# more — which is the ordinary case on an 88E6240, whose internal PHYs
# are 0x00 to 0x04. Saying otherwise was wrong, and was found to be so
# on an 88E6240 rev 1 on 2026-08-22.
BCAST_NOTE = (", and address 0 is also the broadcast address that Realtek and "
               "Motorcomm parts answer on by default, so this may be an echo")

_PORT_USAGE = ("usage: mv6390 port <sw> [ports] "
               "[detect on|off | up|down|auto | state <s> | rgmii <m>]")

_USAGE = """usage:
  mv6390 id     <sw>
  mv6390 port   <sw> [ports] [detect on|off]
  mv6390 port   <sw> <ports> up|down|auto
  mv6390 port   <sw> <ports> state disabled|blocking|learning|forwarding
  mv6390 port   <sw> <ports> rgmii none|rx|tx|both
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


def phy_read_c45(sw, func, port, devad, reg):
    """One Clause-45 read through the window: address cycle, then data.

    `port` goes where an SMI address goes on Clause 22 and `devad` where a
    register number goes — that is Table 262's doing, not ours. Nothing is
    written to the target; the write here is to the switch's own data
    register, which is how Clause 45 carries the register number.
    """
    _phy_wait(sw)
    sw.write(marvell.GLOBAL2, marvell.G2_PHY_DATA, reg & 0xFFFF)
    sw.write(marvell.GLOBAL2, marvell.G2_PHY_CMD,
             _BUSY | (func << 13) | _OP_C45_ADDR |
             ((port & 0x1F) << 5) | (devad & 0x1F))
    _phy_wait(sw)
    sw.write(marvell.GLOBAL2, marvell.G2_PHY_CMD,
             _BUSY | (func << 13) | _OP_C45_READ |
             ((port & 0x1F) << 5) | (devad & 0x1F))
    _phy_wait(sw)
    return sw.read(marvell.GLOBAL2, marvell.G2_PHY_DATA)


def phy_write_c45(sw, func, port, devad, reg, value):
    """One Clause-45 write through the window: address cycle, then data.

    The mirror of phy_read_c45, and needed because this family's SERDES have
    no Clause-22 path at all — a port operation cannot power one without it.
    Same re-purposing of the address fields: `port` goes where an SMI address
    goes on Clause 22 and `devad` where a register number goes.
    """
    _phy_wait(sw)
    sw.write(marvell.GLOBAL2, marvell.G2_PHY_DATA, reg & 0xFFFF)
    sw.write(marvell.GLOBAL2, marvell.G2_PHY_CMD,
             _BUSY | (func << 13) | _OP_C45_ADDR |
             ((port & 0x1F) << 5) | (devad & 0x1F))
    _phy_wait(sw)
    sw.write(marvell.GLOBAL2, marvell.G2_PHY_DATA, value & 0xFFFF)
    sw.write(marvell.GLOBAL2, marvell.G2_PHY_CMD,
             _BUSY | (func << 13) | _OP_C45_WRITE |
             ((port & 0x1F) << 5) | (devad & 0x1F))
    _phy_wait(sw)


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

    if not VERIFIED:
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
    # PHYDetect and the InitState precondition are this family's business;
    # the rest of `port` is cli.mvport's. Table 62 footnote 5 is worth having
    # in mind while reading the output: with the NO_CPU pin low at reset the
    # ports come up Disabled *and* the internal PHYs and SERDES come up
    # powered down, so a board strapped for a CPU that has none looks dead in
    # two registers at once.
    if not args:
        raise CommandError(_PORT_USAGE)

    sw = _switch(args[0])
    rest, action, value = mvport.take_verb(args[1:])
    ports = mvport.port_list(rest, action, PORTS, _PORT_USAGE)

    p = _ports_of(sw)

    if action == "detect":
        if value not in ("on", "off"):
            raise CommandError("detect takes on or off")
        # FS Table 44: must not be written while InitState says initialising.
        if not (sw.read(marvell.GLOBAL1, G1_STATUS) & G1_STATUS_INIT_POLLING):
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
    """The PHY that belongs to port `n`, in the shape cli.mvport wants.

    The one part of a port operation this family cannot share. Two SMI buses
    answer at the same address here — that is what the function field in
    Table 262 is for — so the PHY has to be looked for on both: the internal
    PHYs of ports 1-8 on function 0, and an external PHY, the only thing port
    0, 9 or 10 can have, on function 1. Internal first, because that is the
    cheaper and the more common answer.
    """
    for func in (SMI_FUNC_INTERNAL, SMI_FUNC_EXTERNAL):
        try:
            bmcr = phy_read(sw, func, n, mvport.BMCR)
        except (OSError, CommandError):
            continue
        if bmcr == 0xFFFF:
            continue
        # Only a PHY found on the external function can be a broadcast
        # answer; the internal window is the switch's own and nothing else
        # replies there.
        if n == 0 and func == SMI_FUNC_EXTERNAL:
            mvport.refuse_broadcast(n, "external PHY 0", "mv6390", sw.addr,
                                    PHY_WRITE_HINT)
        return (n, bmcr,
                lambda v, f=func: phy_write(sw, f, n, mvport.BMCR, v),
                "{} PHY {}".format(
                    "internal" if func == SMI_FUNC_INTERNAL else "external",
                    n))
    return None


def _serdes_at(sw, n):
    """The SERDES that serves port `n`, if this helper can reach one.

    Ports 9 and 10, and nothing else: their blocks answer only over Clause 45
    on device class 4, where they keep a Clause-22 image at 0x2000 — so the
    BMCR is at SERDES_C22_IMAGE + 0 and behaves like any other. Measured on
    an 88E6390 rev 1: 0x1940 out of reset, which is powerdown set, exactly as
    Table 62 footnote 5 says it should be on a board strapped for a CPU.

    The address handed back is the port number, which is what the window
    wants there — and it is not 0 for either of them, so the broadcast rule
    in cli.mvport has nothing to catch here.
    """
    if n not in SERDES_PORTS:
        return None
    try:
        bmcr = phy_read_c45(sw, SMI_FUNC_INTERNAL, n, SERDES_DEVAD,
                            SERDES_C22_IMAGE + mvport.BMCR)
    except (OSError, CommandError):
        return None
    if bmcr == 0xFFFF:
        return None
    return (n, bmcr,
            lambda v: phy_write_c45(sw, SMI_FUNC_INTERNAL, n, SERDES_DEVAD,
                                    SERDES_C22_IMAGE + mvport.BMCR, v),
            "SERDES {} (clause 45, devad {})".format(n, SERDES_DEVAD))


def _ports_of(sw):
    return mvport.Ports(sw, "mv6390", PORT_BASE, PORTS, RGMII_PORTS, SPEED,
                        CMODE, lambda n: _phy_at(sw, n),
                        lambda n: _serdes_at(sw, n), wide=True)


# --- reset ------------------------------------------------------------

def _reset(ctx, args):
    """Software reset — and it resets far less than the name suggests.

    FS, Global1 0x04 bit 15: "causes the QC and the MAC state machines in the
    switch to be reset (i.e., the switch datapath). Register values are not
    modified. The EEPROM will not be re-read. The ATU, VTU, MIBs, PHYs etc.
    are not effected by this bit."

    So this is not a way to undo a configuration mistake. Measured 2026-08-21:
    PHYDetect cleared on port 1 by hand stayed clear across it, which is the
    documented behaviour — the bit is RWR, and it is set at the end of the
    *init* routine, which a software reset does not re-run. Only the hardware
    RESETn does that.
    """
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
        return "SERDES lane"
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
                             _role(func, addr) +
                             _polled(sw, func, addr) +
                             (BCAST_NOTE
                              if addr == 0 and
                              func == SMI_FUNC_EXTERNAL else ""))
            found += 1

    # Third pass, Clause 45 on the internal function, because the SERDES do
    # not answer the other two at all — measured 2026-08-21, and it is why
    # 0x09 and 0x0A used to be reported silent on a part that had them.
    #
    # A presence test first, then `identify` only where something answered.
    # A full identify at every address would double an already slow sweep for
    # nothing: an empty Clause-45 address here returns 0x0000 rather than
    # failing, so one register is enough to tell it from a live one.
    #
    # Only the internal function is swept this way. An external Clause-45
    # PHY is possible and would need a fourth pass; none has been seen, and
    # guessing at one is not worth doubling the wait again.
    for addr in range(32):
        try:
            if not _c45_answers(sw, SMI_FUNC_INTERNAL, addr):
                continue
            what = cmd_probe.identify(_SerdesWindow(sw, SMI_FUNC_INTERNAL, addr))
        except OSError as exc:
            ctx.out.line("c45 {:02x}: read failed, errno {}", addr,
                         exc.args[0] if exc.args else "?")
            continue
        if what is None:
            continue
        cmd_probe.report(ctx, "c45 {:02x}".format(addr), what,
                         _role(SMI_FUNC_INTERNAL, addr))
        found += 1

    ctx.out.line("{} address(es) answered", found)


def _c45_answers(sw, func, addr):
    """Cheap presence test on the Clause-45 side.

    An address with nothing behind it reads 0x0000 here, not 0xffff and not
    an error, so both of those are treated as "nothing" too: 0xffff is the
    idle line and cannot be an identifier.
    """
    ident = phy_read_c45(sw, func, addr, SERDES_DEVAD, SERDES_C22_IMAGE + 2)
    return ident not in (0x0000, 0xFFFF)


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


class _SerdesWindow:
    """A SERDES behind this switch, as if it were an ordinary Clause-22 PHY.

    The offset is what makes that true: the block carries a Clause-22 image
    at 0x2000, so register `r` of the image is 0x2000 + r, and `identify`
    then reads 2 and 3 and gets a PHY identifier without knowing any of this.
    Read only — a probe has no business writing into a datapath block.
    """

    def __init__(self, sw, func, port):
        self._sw = sw
        self._func = func
        self._port = port

    def read(self, reg):
        return phy_read_c45(self._sw, self._func, self._port, SERDES_DEVAD,
                            SERDES_C22_IMAGE + (reg & 0x1F))


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

VERIFIED on an 88E6390 rev 1, identifier 0x3901, on 2026-08-21, and the
port operations on 2026-08-22. The 88E6190, 88E6290 and the X parts are
still only read about.

Port registers start at 0x00, not 0x10. The SMI PHY command register has a
function field in bits 14:13 that other families leave Reserved: 0 reaches
internal PHYs and SERDES, 1 the external bus, and the two may carry the same
addresses — hence `mv6390 phy ... int|ext`, since plain `mv phy` sends 0.

NormalSMI is scratch index 0x02; index 0x63 is a GPIO direction register
here. There is no `solo` — no PPU enable is documented on this family.

Rev A0 errata 3.13: the switch may not drive MDIO low after the address
cycle, which our master reads as "nobody answered". Reads fail until
`ta relaxed`. Measured on a rev 1 part: it drives nothing at all there.

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

`state` writes PortState in Port Control (offset 0x04), encoded exactly as on
the other two families. Worth knowing why so many ports start Disabled here:
Table 62 footnote 5 says the ports come up Forwarding *unless* the NO_CPU pin
was low at reset, in which case they come up Disabled and the internal PHYs
and SERDES come up powered down. A board strapped for a CPU that has none
therefore looks dead in two registers at once. On this family the SERDES of
ports 9 and 10 are reached over Clause 45 — they answer nothing else — so
`up` there goes through the Clause-22 image the block keeps at 0x2000.

`rgmii none|rx|tx|both` writes bits 15:14 of Physical Control, always both, so
the setting is stated rather than accumulated. It refuses on any port but 0 —
Table 48 says those bits are valid there and nowhere else on this family —
and while the port's link is up, because the same table says the change is
disruptive and must be made with the link down.

Anything that writes wants an explicit port list. `port <sw> state forwarding`
with no list would mean all eleven.

`probe` sweeps all 32 addresses **three times**: once on each SMI function
with Clause 22, then once more on the internal function with Clause 45,
which is the only way the SERDES answer at all. Every line says which pass
found it — `int`, `ext` or `c45`. The release notes say plainly that an
external device may share an address with an internal PHY or SERDES, so
merging the sweeps would turn two parts into one wrong line.

It refuses with the PPU stopped: the window then answers 0xffff to
everything instead of failing, which reads as an empty switch.

Nothing is written. PHYDetect is reported, not set — and on ports 9 and 10 it
is not a polling hint at all but a 1000BASE-X/SGMII selector, which is one
more reason to leave it alone.

Verified on an 88E6390 rev 1 on 2026-08-21. There `probe` found eight
internal PHYs, the two SERDES, all six lane addresses — the lanes answer on a
plain 6390, whatever the specification's X-only wording suggests — and, once
`extbus on` had routed the pins, an RTL8211F on the external bus at 0x00.
""")
def cmd_mv6390(ctx, args):
    if not args:
        raise CommandError(_USAGE)

    group = _GROUPS.get(args[0])
    if group is None:
        raise CommandError(_USAGE)
    group(ctx, args[1:])
