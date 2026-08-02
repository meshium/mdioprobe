"""`mv` — Marvell Link Street indirect register access.

Addressing only. What the registers mean lives in helper scripts loaded with
`use`; see cli/marvell.py for why the line is drawn
here.
"""

from cli import marvell, monitor
from cli.cmd_mdio import _WIDE, apply_modify, bitmask, dump, take_mode, wrap
from cli.parser import ranged, spec
from cli.registry import CommandError, command

# Repeated verbatim in the command's `detail` below rather than concatenated
# into it. A concatenation is evaluated at import and its result lives on the
# GC heap for the life of the session; two separate literals both stay in
# flash with the frozen module. That mattered — `help mv` used to fail with
# MemoryError.
_USAGE = """usage:
  mv reg     read|write|modify|monitor <sw> <dev> ...     switch registers
  mv phy     read|write|modify|monitor [mode] <sw> <addr> ...
  mv scratch read|write|modify|monitor <sw> ...           one byte each"""

_ACTIONS = ("read", "write", "modify", "monitor")


def _switch(token):
    return marvell.Switch(ranged(token, 0, 31, "switch address"))


def _take_action(args):
    """Peel the action, and for `modify` an optional set|clear right after it.

    `modify set|clear` reads the way the plain command spells it; putting
    set|clear after the register reads the way this command's own arguments
    run. Both work, because insisting on one of them is a rule to remember
    for no gain.
    """
    if not args or args[0] not in _ACTIONS:
        raise CommandError(_USAGE)

    action, args = args[0], args[1:]
    sub = None

    if action == "modify" and args and args[0] in ("set", "clear"):
        sub, args = args[0], args[1:]
    return action, sub, args


def _run(ctx, action, sub, target, args, high=31, awidth=2, vwidth=4,
         default=None, what="register", hint_idle=False):
    """read / write / modify / monitor over one register space.

    All four differ only in what they do with an address and a value, so the
    argument shapes are settled once here rather than four times per group.
    `target` is anything with read(addr) and write(addr, value).
    """
    seconds, args = monitor.take_timeout(args)

    if action in ("read", "monitor"):
        if args:
            registers = spec(args[0], 0, high, what)
            args = args[1:]
        elif default is None:
            raise CommandError("which {}?".format(what))
        else:
            registers = default
        if args:
            raise CommandError("unexpected extra arguments: {}".format(" ".join(args)))

        if action == "read":
            _dump_with_hint(ctx, registers, target, awidth, vwidth, hint_idle)
        else:
            monitor.run(ctx, registers, target.read, target.label, awidth,
                        seconds, vwidth)
        return

    if not args:
        raise CommandError("which {}?".format(what))
    address = ranged(args[0], 0, high, what)
    args = args[1:]

    if action == "write":
        if len(args) != 1:
            raise CommandError("need a value")
        value = ranged(args[0], 0, (1 << (4 * vwidth)) - 1, "value")
        target.write(address, value)
        ctx.out.line("{} 0x{:0{}x} <- 0x{:0{}x}", target.label, address, awidth,
                     value, vwidth)
        return

    # modify
    if sub is None:
        if not args or args[0] not in ("set", "clear"):
            raise CommandError("modify needs set or clear")
        sub, args = args[0], args[1:]
    what_to_do = sub
    mask, args = bitmask(args)
    if args:
        raise CommandError("unexpected extra arguments: {}".format(" ".join(args)))

    before, after = apply_modify(what_to_do, target.read, target.write,
                                 address, mask)
    ctx.out.line("{} 0x{:0{}x}: 0x{:0{}x} -> 0x{:0{}x}  ({} 0x{:04x})",
                 target.label, address, awidth, before, vwidth, after, vwidth,
                 what_to_do, mask)


def _dump_with_hint(ctx, registers, target, awidth, vwidth, hint_idle):
    """Read a set of registers, and say something when every one was 0xffff.

    That is what an idle bus reads as, so a whole dump of it means nothing
    answered rather than that every register happens to be all ones. The
    cause is chip-specific — on some parts the SMI PHY window only works
    while the PPU runs — so the note describes the symptom and points at the
    possibility rather than asserting it. This used to be a hard refusal
    based on the 88E6321; the 88E6240 datasheet says the opposite in as many
    words, which is why it is now a remark.
    """
    idle = [0]
    seen = [0]

    def read(reg):
        value = target.read(reg)
        seen[0] += 1
        if value == 0xFFFF:
            idle[0] += 1
        return value

    dump(ctx, registers, read, target.label, awidth, vwidth)

    if hint_idle and seen[0] and idle[0] == seen[0]:
        ctx.out.line("  (every read came back 0xffff — an idle bus reads that "
                     "way; on some parts this window needs the PPU running)")


class _Target:
    """A register space plus how to name it in the output."""

    def __init__(self, read, write, label):
        self.read = read
        self.write = write
        self.label = label


def _reg_group(ctx, args):
    action, sub, args = _take_action(args)

    if len(args) < 2:
        raise CommandError(_USAGE)
    sw = _switch(args[0])
    dev = ranged(args[1], 0, 31, "device address")

    target = _Target(lambda r: sw.read(dev, r),
                     lambda r, v: sw.write(dev, r, v),
                     "sw 0x{:02x} dev 0x{:02x} reg".format(sw.addr, dev))
    _run(ctx, action, sub, target, args[2:], 31, 2, 4, range(32))


def _phy_group(ctx, args):
    action, sub, args = _take_action(args)
    mode, args = take_mode(args)

    if len(args) < 2:
        raise CommandError(_USAGE)
    sw = _switch(args[0])
    addr = ranged(args[1], 0, 31, "PHY address")

    mode, bus, args = wrap(mode, marvell.SwitchPhy(sw, addr), args[2:])
    wide = mode in _WIDE
    label = "sw 0x{:02x} phy {:02d} {}reg".format(
        sw.addr, addr, "" if mode == "plain" else mode + " ")

    _run(ctx, action, sub, _Target(bus.read, bus.write, label), args,
         0xFFFF if wide else 31, 4 if wide else 2, 4,
         None if wide else range(32), hint_idle=True)


def _scratch_group(ctx, args):
    action, sub, args = _take_action(args)

    if not args:
        raise CommandError(_USAGE)
    sw = _switch(args[0])
    target = _Target(sw.scratch_read, sw.scratch_write,
                     "sw 0x{:02x} scratch".format(sw.addr))

    _run(ctx, action, sub, target, args[1:], 0xFF, 2, 2, range(0x80), "index")


_GROUPS = {
    "reg": _reg_group,
    "phy": _phy_group,
    "scratch": _scratch_group,
}

@command("mv", category="mdio", syntax="mv reg|phy|scratch ...",
         summary="Marvell Link Street indirect register access",
         detail="""usage:
  mv reg     read|write|modify|monitor <sw> <dev> ...     switch registers
  mv phy     read|write|modify|monitor [mode] <sw> <addr> ...
  mv scratch read|write|modify|monitor <sw> ...           one byte each

Three layers of indirection and nothing else. This command knows how to
reach a register; it does not know what any register means, and that is
deliberate — the meanings differ enough between Link Street families to be
dangerous. Scratch index 0x63 is the external-bus mux on an 88E6321 and a
GPIO *direction* register on the 88E6240 and 88E6390; port registers start
at device address 0x10 on the first two and at 0x00 on the third.

So anything with a meaning — identifying the part, resetting it, reading
port status, moving the external MDIO bus — lives in a helper script named
after the family and loaded with `use`:

    use mv6321
    mv6321 id 6

`use` lists what is currently loaded.

Switch address 0 means single-chip addressing, where the switch's device
blocks answer as ordinary MDIO addresses. Any other address means
multi-chip: the whole switch sits at that one address and everything goes
through a command/data pair there.

Every addressing mode `read` accepts works after `mv phy`, and the mode word
goes before the switch address:

    mv phy read 6 3 [regs]                  plain, regs 0..31
    mv phy read mvpage  6 3 <page> [regs]   page select via register 22
    mv phy read page    6 3 <page> [regs]   page select via 0x1F
    mv phy read extpage 6 3 <page> [regs]   0x1F=7, then 0x1E
    mv phy read mmd     6 3 <devad> <regs>  via 0x0D/0x0E, regs 0..0xffff
    mv phy read rdb     6 3 <regs>          0x1E/0x1F window
    mv phy read extreg  6 3 <regs>          0x1E/0x1F window

`write`, `modify` and `monitor` take the same shape, with the value, the
bit/mask or `for <sec>` after the register.

The last four are other vendors' schemes, kept because `mv phy` also reaches
external PHYs. On a Marvell PHY use `mvpage` — those page through register
22, so `page` would write a page number into register 31 instead. `mmd`
works, but only while register 22 holds 0..7; above that the spec says the
MMD registers are not reachable.
""")
def cmd_mv(ctx, args):
    if not args:
        raise CommandError(_USAGE)

    group = _GROUPS.get(args[0])
    if group is None:
        raise CommandError(_USAGE)
    group(ctx, args[1:])
