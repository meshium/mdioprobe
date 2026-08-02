"""Clause-22 register access."""

from cli import access, transport
from cli.parser import integer, phy, reg, spec, u16
from cli.registry import CommandError, command

# Addressing modes, and what each needs before the register itself.
#
#   plain      read <phy> [regs]
#   page       read page <phy> <page> [regs]          select via 0x1F
#   mvpage     read mvpage <phy> <page> [regs]        select via 22
#   extpage    read extpage <phy> <ext_page> [regs]   0x1F=7 then 0x1E
#   mmd        read mmd <phy> <devad> <regs>          0x0D/0x0E, 16-bit regs
#   rdb        read rdb <phy> <regs>                  0x1E/0x1F window
#   extreg     read extreg <phy> <regs>               0x1E/0x1F window
#
# rdb and extreg are the same shape and differ only by name; both are kept
# because that is what the respective datasheets call them, and a person
# reading a Broadcom sheet should be able to type what it says.
#
# `page` and `mvpage` differ only in which register selects, and that is
# exactly why they are separate words rather than one word that guesses.
# Guessing wrong here does not return a wrong value — it *writes* a page
# number into whatever register the other vendor put there.
_MODES = ("page", "mvpage", "extpage", "mmd", "rdb", "extreg")

# Registers are 5-bit in the plain and paged spaces, 16-bit behind an
# indirection.
_WIDE = ("mmd", "rdb", "extreg")

# Marvell's page select. IEEE gives a PHY 32 registers; this family pages the
# space with register 22 bits 7:0 (88E6321 spec §8.10.2, Table 56). Register
# 22 itself is never paged, and the value survives a software reset.
MV_PAGE_SELECT = 22


def take_mode(args):
    """Peel a leading mode keyword. Returns (mode, rest); mode may be None."""
    if args and args[0] in _MODES:
        return args[0], args[1:]
    return None, args


def wrap(mode, bus, args):
    """Apply `mode` to an already-built base bus, consuming its arguments.

    Split out from _open() so the same six modes work over anything that has
    read/write — in practice a PHY on the wire, or a PHY behind a switch.
    Returns (mode_name, wrapped_bus, remaining_args).
    """
    if mode is None:
        return "plain", bus, args

    if mode in ("page", "mvpage"):
        if not args:
            raise CommandError("page number missing")
        select = MV_PAGE_SELECT if mode == "mvpage" else access.Paged.SELECT
        return mode, access.Paged(bus, integer(args[0], "page"),
                                  select_reg=select), args[1:]
    if mode == "extpage":
        if not args:
            raise CommandError("extension page missing")
        return mode, access.ExtPage(bus, integer(args[0], "page")), args[1:]
    if mode == "mmd":
        if not args:
            raise CommandError("device address (devad) missing")
        from cli.parser import ranged
        return mode, access.Mmd(bus, ranged(args[0], 0, 31, "devad")), args[1:]

    # rdb and extreg
    return mode, access.Window(bus), args


def _open(args, needs_regs):
    """Work out the mode, build the accessor, return (accessor, remaining).

    `args` starts at the first token after the command name.
    """
    mode, args = take_mode(args)

    if not args:
        raise CommandError("which PHY?")

    bus = access.PhyBus(phy(args[0]))
    return wrap(mode, bus, args[1:])


def _regs(mode, args, required):
    if not args:
        if required:
            raise CommandError("which register?")
        return range(32)
    high = 0xFFFF if mode in _WIDE else 31
    return spec(args[0], 0, high, "register")


def _label(mode, target):
    return "{}{}".format(mode if mode != "plain" else "reg",
                         "" if mode == "plain" else " reg")


def dump(ctx, registers, read, label, width=2, vwidth=4):
    """Read and print a set of registers, surviving individual failures.

    One register that will not answer must not cost the other thirty-one.
    A dump is usually how you find out *which* register is the problem, and
    aborting at the first one hides exactly that.
    """
    for r in registers:
        try:
            ctx.out.line("{} 0x{:0{}x} = 0x{:0{}x}", label, r, width,
                         read(r), vwidth)
        except OSError as exc:
            ctx.out.line("{} 0x{:0{}x} = ERROR (errno {})", label, r, width,
                         exc.args[0] if exc.args else "?")


def _dump(ctx, bus, registers, mode):
    dump(ctx, registers, bus.read, _label(mode, None),
         4 if mode in _WIDE else 2)


def bitmask(args, what="mask"):
    """Parse the bit-or-mask argument of `modify`. Returns (mask, remaining).

    Three spellings, and the bare one is always a mask:

        0x8000      mask
        mask 0x8000 mask, said out loud
        bit 15      bit index

    The previous project also read a bare decimal 0..15 as a bit index, so
    `8` meant bit 8 while `0x8` meant bit 3. That is not a convenience, it is
    a way to set the wrong bit in a live PHY, so only the explicit form is
    accepted here.
    """
    if not args:
        raise CommandError("which bit or mask?")

    if args[0] == "bit":
        if len(args) < 2:
            raise CommandError("`bit` needs a bit number")
        from cli.parser import ranged
        return 1 << ranged(args[1], 0, 15, "bit number"), args[2:]
    if args[0] == "mask":
        if len(args) < 2:
            raise CommandError("`mask` needs a value")
        return u16(args[1]), args[2:]
    return u16(args[0]), args[1:]


def apply_modify(action, read, write, register, mask):
    """Read-modify-write one register. Returns (before, after)."""
    before = read(register)
    after = (before | mask) if action == "set" else (before & ~mask & 0xFFFF)

    write(register, after)
    return before, after


@command("read", category="mdio",
         syntax="read [page|mvpage|extpage|mmd|rdb|extreg] <phy> [...] [regs]",
         summary="read registers, directly or through an addressing mode",
         detail="""
`regs` accepts a list and ranges: 1, 0-7, 0,2-4,7. Numbers are decimal
unless prefixed 0x. Omitting them reads all 32 Clause-22 registers; the
indirect modes address 16 bits, so there they are required.

    read 6 0x02-0x03            PHY ID
    read page 2 0x0a43 0x1a     paged register, select via 0x1F
    read mvpage 3 2 0x00-0x1f   paged register, select via 22 (Marvell)
    read mmd 2 7 0x003d         EEE link partner ability
    read extpage 2 0x2c 0x1a
    read rdb 2 0x02a0-0x02a3

A register that will not answer is reported in place and the rest of the
list still gets read.
""")
def cmd_read(ctx, args):
    mode, bus, rest = _open(args, True)
    registers = _regs(mode, rest, mode in _WIDE)

    _dump(ctx, bus, registers, mode)


@command("write", category="mdio",
         syntax="write [page|mvpage|extpage|mmd|rdb|extreg] <phy> [...] <reg> <value>",
         summary="write one register, directly or through an addressing mode",
         detail="""
    write 6 0x00 0x8000         PHY reset
    write mmd 2 1 0x0000 0x1234
    write page 2 0x0007 0x10 0x0001
""")
def cmd_write(ctx, args):
    mode, bus, rest = _open(args, True)

    if len(rest) != 2:
        raise CommandError("need a register and a value")

    high = 0xFFFF if mode in _WIDE else 31
    from cli.parser import ranged
    register = ranged(rest[0], 0, high, "register")
    value = u16(rest[1])

    bus.write(register, value)
    ctx.out.line("{} 0x{:0{}x} <- 0x{:04x}", _label(mode, register), register,
                 4 if mode in _WIDE else 2, value)


@command("modify", category="mdio",
         syntax="modify set|clear [mode] <phy> [...] <reg> <bit n|mask v>",
         detail="""
The register is read, changed and written back, and both values are shown so
a write that did not take is obvious. Every addressing mode `read` accepts
works here too.

A bare value is a mask, and there are explicit spellings for both:

    modify set 6 0x00 0x8000        mask
    modify set 6 0x00 mask 0x8000   the same, said out loud
    modify set 6 0x00 bit 15        bit number

There is deliberately no rule that reads a bare decimal as a bit number:
that would make `8` and `0x8` mean different bits.
""",
         summary="set or clear bits, leaving the rest alone")
def cmd_modify(ctx, args):
    if not args or args[0] not in ("set", "clear"):
        raise CommandError(
            "usage: modify set|clear [mode] <phy> [...] <reg> <bit n|mask v>")

    action = args[0]
    mode, bus, rest = _open(args[1:], True)

    if not rest:
        raise CommandError("need a register and a bit or mask")

    high = 0xFFFF if mode in _WIDE else 31
    from cli.parser import ranged
    register = ranged(rest[0], 0, high, "register")
    mask, rest = bitmask(rest[1:])

    if rest:
        raise CommandError("unexpected extra arguments: {}".format(" ".join(rest)))

    before, after = apply_modify(action, bus.read, bus.write, register, mask)

    ctx.out.line("{} 0x{:0{}x}: 0x{:04x} -> 0x{:04x}  ({} 0x{:04x})",
                 _label(mode, register), register, 4 if mode in _WIDE else 2,
                 before, after, action, mask)


@command("scan", category="mdio", syntax="scan [reg]",
         summary="find devices that answer, across all 32 addresses",
         detail="""
Reads one register from every address and lists those that reply. Register 0
by default. An address that does not answer leaves the bus idle, which reads
back as 0xffff — those are reported as silent, not as devices.
""")
def cmd_scan(ctx, args):
    register = reg(args[0]) if args else 0
    found = 0

    for address in range(32):
        try:
            value = transport.read(address, register)
        except OSError:
            continue
        if value == 0xFFFF:
            continue
        ctx.out.line("phy {:02d}: 0x{:04x}", address, value)
        found += 1

    ctx.out.line("{} device(s) answered on register 0x{:02x}", found, register)


@command("trace", category="mdio", syntax="trace [on|off]",
         summary="echo every bus transaction")
def cmd_trace(ctx, args):
    if args:
        if args[0] not in ("on", "off"):
            raise CommandError("usage: trace [on|off]")
        transport.set_trace(args[0] == "on")
    ctx.out.line("trace: {}", "on" if transport.tracing() else "off")


@command("clock", category="mdio", syntax="clock [khz]",
         summary="MDC rate the master clocks at",
         detail="""
Range 10..2500 kHz, default 1500. Three different limits are in play: the bit
loop tops out near 4000, Clause-22 allows at most 2500, and the 88E6321 on the
development bench stopped answering above about 1800. The default leaves room
under the last of those, because it is the one that varies with your wiring.

Wind it down for long or lightly pulled-up wiring: MDIO is open-drain, so a
'1' is a pull-up charging the line, and at 1500 kHz a bit is only 667 ns.

`diag` shows what is actually delivered, which is at or just below what was
asked for - never above.

If a target answers nothing but 0xffff, check for a pull-up on MDIO before
touching this: not every v2 board carries one, the switch's internal weak
pull-ups are not enough, and the symptom is identical.
""")
def cmd_clock(ctx, args):
    import mdioprobe

    if args:
        khz = integer(args[0], "rate")
        try:
            mdioprobe.c22_clock(khz)
        except OSError:
            raise CommandError("rate must be 10..2500 kHz")

    khz = mdioprobe.c22_clock()
    ctx.out.line("MDC {} kHz requested ({} ns per bit); `diag` shows delivered",
                 khz, 1000000 // khz)
