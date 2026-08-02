"""Name what is on the bus.

Reading an identifier is two register reads; the work is in saying what the
number means. `cli/chipdb.py` holds the table, generated from the Linux
sources by `scripts/gen_chipdb.py` and frozen into the firmware, so both the
lookup and the answer survive an empty volume.

Read-only on purpose. Everything here is a Clause-22 read, and which extra
addresses get read for switches comes out of the table rather than being
written in here — the database already records, per part, the address, the
register and whether reaching it needs writes. Reaching further means writing
to the bus, and writing blind to something that has not identified itself is
how you reset the PHY you meant to look at. The `mmd` form is the one
exception, and it says so.
"""

from cli import access
from cli.parser import ranged
from cli.registry import CommandError, command

# Field offsets in a chipdb.SWITCH record. Fixed width so a lookup can slice
# out what it needs without splitting the line or the table.
SW_ID, SW_PROD, SW_REV = 0, 9, 18
SW_ADDR, SW_REG, SW_ACCESS, SW_NAME = 27, 30, 33, 49

_CHIPDB = None


def chipdb():
    """Import the table on first use.

    Frozen, this costs about the module's own dictionary and nothing for the
    strings themselves, but there is no reason to pay even that on a boot
    where nobody asks.
    """
    global _CHIPDB

    if _CHIPDB is None:
        from cli import chipdb as table
        _CHIPDB = table
    return _CHIPDB


def decode(reg2, reg3):
    """(identifier, oui, model, revision) from Clause-22 registers 2 and 3.

    The OUI is 22 bits split across both registers: bits 3..18 are the whole
    of register 2, bits 19..24 the top six of register 3.
    """
    ident = ((reg2 << 16) | reg3) & 0xFFFFFFFF

    return ident, (reg2 << 6) | (reg3 >> 10), (reg3 >> 4) & 0x3F, reg3 & 0x0F


def _walk(table):
    """Record boundaries in a fixed-width table, without splitting it.

    `table.split("\\n")` would build a three-hundred-element list of strings
    off a constant already sitting in flash, which is the whole reason the
    database is a string and not a dict.
    """
    pos = 0
    end = len(table)

    while pos < end:
        stop = table.find("\n", pos)
        if stop < 0:
            stop = end
        if stop > pos:
            yield pos, stop
        pos = stop + 1


def _popcount(value):
    bits = 0

    while value:
        bits += value & 1
        value >>= 1
    return bits


def lookup_phy(ident):
    """The most specific record matching `ident`, or None.

    Most specific, not first: a part can be covered by both an exact record
    and a vendor-wide one, and the exact one is the answer. Linux gets the
    same effect out of driver registration order, which we do not have.
    """
    table = chipdb().PHY
    best = None
    best_bits = -1

    for start, stop in _walk(table):
        entry = int(table[start:start + 8], 16)
        mask = int(table[start + 9:start + 17], 16)

        if (ident ^ entry) & mask:
            continue

        bits = _popcount(mask)
        if bits > best_bits:
            best, best_bits = table[start + 18:stop], bits

    return best


def switch_probes():
    """The (address, register) pairs a plain read can identify a switch at.

    Read out of the table rather than written down here, so adding a family
    to the database is enough to have it looked for.
    """
    table = chipdb().SWITCH
    pairs = []

    for start, _stop in _walk(table):
        if table[start + SW_ACCESS:start + SW_ACCESS + 4].rstrip() != "c22":
            continue

        addr = table[start + SW_ADDR:start + SW_ADDR + 2]
        reg = table[start + SW_REG:start + SW_REG + 2]
        if "-" in addr or "-" in reg:
            continue

        pair = (int(addr, 16), int(reg, 16))
        if pair not in pairs:
            pairs.append(pair)

    return pairs


def lookup_switch(value, address, register):
    """Switch records that `value`, read at this address and register, fits."""
    table = chipdb().SWITCH
    at = "{:02x}".format(address)
    of = "{:02x}".format(register)
    found = []

    for start, stop in _walk(table):
        if table[start + SW_ACCESS:start + SW_ACCESS + 4].rstrip() != "c22":
            continue
        if table[start + SW_ADDR:start + SW_ADDR + 2] != at:
            continue
        if table[start + SW_REG:start + SW_REG + 2] != of:
            continue

        entry = int(table[start:start + 8], 16)
        prod = int(table[start + SW_PROD:start + SW_PROD + 8], 16)
        if (value & prod) != entry:
            continue

        rev_mask = int(table[start + SW_REV:start + SW_REV + 8], 16)
        found.append((table[start + SW_NAME:stop],
                      value & rev_mask if rev_mask else None))

    return found


def read_identity(bus):
    """Registers 2 and 3, or None if the address is silent."""
    try:
        reg2 = bus.read(0x02)
        reg3 = bus.read(0x03)
    except OSError:
        return None

    if reg2 == 0xFFFF and reg3 == 0xFFFF:
        return None
    return reg2, reg3


# errno 19, ENODEV: the probe refuses to drive the wire while it does not
# know the target's rail. Measured on the bench, a long burst of traffic can
# make it briefly not know: the detector samples the line every poll period,
# and a sample taken while the line is still held low reads 29 mV instead of
# 3293, which classifies as no rail. It recovers on the next sample, ~200 ms.
RAIL_LOST = 19


def identify(bus, tries=3):
    """`(identifier, name)` through anything with `read(reg)`, or None.

    The whole of what a chip helper needs from this module: it supplies the
    bus — a plain address, a PHY behind a switch, a paged SerDes — and gets
    back the number and, if the database knows it, the part. `name` is None
    for a part that answered but is not in the table, which is a different
    answer from silence and worth keeping separate.

    Unlike `read_identity` this does **not** swallow errors. A sweep that
    quietly turned "the probe would not talk to the bus" into "nothing is
    there" is how a switch with four live PHYs got reported as having two —
    measured, and the reason this function exists rather than the caller
    looping over `read_identity`. The one error it does absorb is the rail
    blinking out under its own traffic, and only by waiting for it.
    """
    import time

    while True:
        try:
            reg2 = bus.read(0x02)
            reg3 = bus.read(0x03)
            break
        except OSError as exc:
            if tries > 1 and exc.args and exc.args[0] == RAIL_LOST:
                tries -= 1
                time.sleep_ms(250)
                continue
            raise

    # All ones is an idle bus. All zeros is not a device either: no OUI of
    # zero was ever assigned, and measured on an 88E6321, registers 0 to 3 of
    # a PHY read exactly 0x0000 whenever its page register points somewhere
    # other than the page the identifier lives on. Reporting that as "a part
    # answered, identifier 0x00000000" would be a sweep inventing hardware.
    if (reg2, reg3) in ((0xFFFF, 0xFFFF), (0x0000, 0x0000)):
        return None

    ident = ((reg2 << 16) | reg3) & 0xFFFFFFFF
    return ident, lookup_phy(ident)


def report(ctx, label, found, note=""):
    """One line about one address, in the same shape as `probe` itself.

    Exists so a helper does not have to reinvent the formatting, and so the
    output of `mv6321 probe` reads like the output of `probe`.
    """
    if found is None:
        ctx.out.line("{}: silent", label)
        return

    ident, name = found
    ctx.out.line("{}: {:08x}  {}{}", label, ident,
                 name if name else "not in the database",
                 "   {}".format(note) if note else "")


def _switch_pass(ctx):
    """Read the handful of places a switch can be named without writing."""
    shown = 0

    for address, register in switch_probes():
        try:
            value = access.PhyBus(address).read(register)
        except OSError:
            continue

        if value in (0xFFFF, 0x0000):
            continue

        for name, revision in lookup_switch(value, address, register):
            if not shown:
                ctx.out.line("")
                ctx.out.line("switch candidates, by plain read:")
            ctx.out.line("  {:02x}.{:02x} = 0x{:04x}  {}{}", address, register,
                         value, name,
                         "" if revision is None else
                         " rev {}".format(revision))
            shown += 1

    return shown


def _sweep(ctx):
    found = 0
    mute = []

    for address in range(32):
        bus = access.PhyBus(address)
        pair = read_identity(bus)

        if pair is None:
            # An address with no identifier is not necessarily empty. Ask
            # register 0 as well, because that is the difference between an
            # empty bus and a switch in multi-chip mode -- and reporting the
            # first when it is the second sends someone looking for a wiring
            # fault that is not there.
            try:
                if bus.read(0x00) != 0xFFFF:
                    mute.append(address)
            except OSError:
                pass
            continue

        ident = decode(*pair)[0]
        name = lookup_phy(ident)

        ctx.out.line("phy {:02d}: {:08x}  {}", address, ident,
                     name if name else "not in the database")
        found += 1

    ctx.out.line("{} device(s) answered with an identifier", found)

    if mute:
        ctx.out.line("{} answered register 0 but no identifier: {}",
                     len(mute), ", ".join(str(a) for a in mute))
        ctx.out.line("that is what a Marvell switch in multi-chip mode looks "
                     "like — `probe <addr>` says more")
    elif not found:
        ctx.out.line("nothing on the bus, or no pull-up on MDIO — see "
                     "docs/HARDWARE.md")

    if not _switch_pass(ctx) and not mute:
        ctx.out.line("no switch named itself by a plain read")


def _detail(ctx, address):
    bus = access.PhyBus(address)
    pair = read_identity(bus)

    ctx.out.line("phy {:02d}", address)

    if pair is None:
        # Not necessarily silence. A Marvell switch in multi-chip mode
        # decodes registers 0 and 1 and nothing else, so the identifier
        # registers fail while the part is very much there -- which is
        # exactly what this bench looks like. Say which it was.
        try:
            control = bus.read(0x00)
        except OSError:
            control = None

        if control is None or control == 0xFFFF:
            ctx.out.line("  nothing answered here")
        else:
            ctx.out.line("  reg 0x00    0x{:04x}", control)
            ctx.out.line("  no identifier: registers 2 and 3 did not answer, "
                         "but register 0 did")
            ctx.out.line("  a Marvell switch in multi-chip mode looks like "
                         "this — it decodes 0 and 1 only, and naming it "
                         "means writing to SMI_CMD")
    else:
        reg2, reg3 = pair
        ident, oui, model, revision = decode(reg2, reg3)
        name = lookup_phy(ident)

        ctx.out.line("  reg 0x02    0x{:04x}", reg2)
        ctx.out.line("  reg 0x03    0x{:04x}", reg3)
        ctx.out.line("  identifier  0x{:08x}", ident)
        ctx.out.line("  oui         0x{:06x}   bits 3..24", oui)
        ctx.out.line("  model       0x{:02x}", model)
        ctx.out.line("  revision    0x{:x}", revision)
        ctx.out.line("  name        {}", name if name else
                     "not in the database (Linux {})".format(chipdb().KERNEL))

    for at, register in switch_probes():
        if at != address:
            continue

        try:
            value = bus.read(register)
        except OSError:
            continue

        for switch, rev in lookup_switch(value, address, register):
            ctx.out.line("  switch      {}{}   from reg 0x{:02x} = 0x{:04x}",
                         switch, "" if rev is None else " rev {}".format(rev),
                         register, value)


def _mmd(ctx, args):
    if not args:
        raise CommandError("usage: probe mmd <phy> [devad]")

    address = ranged(args[0], 0, 31, "address")
    devads = ([ranged(args[1], 0, 31, "device address")] if len(args) > 1
              else (1, 3, 7))

    ctx.out.line("phy {:02d}: through registers 13 and 14 — this writes to "
                 "the target", address)

    bus = access.PhyBus(address)

    for devad in devads:
        mmd = access.Mmd(bus, devad)
        try:
            reg2 = mmd.read(0x02)
            reg3 = mmd.read(0x03)
        except OSError:
            ctx.out.line("  devad {:2d}   no answer", devad)
            continue

        if (reg2, reg3) in ((0xFFFF, 0xFFFF), (0x0000, 0x0000)):
            ctx.out.line("  devad {:2d}   empty", devad)
            continue

        ident = ((reg2 << 16) | reg3) & 0xFFFFFFFF
        name = lookup_phy(ident)
        ctx.out.line("  devad {:2d}   0x{:08x}  {}", devad, ident,
                     name if name else "not in the database")


@command("probe", category="mdio", syntax="probe [<phy> | mmd <phy> [devad]]",
         summary="identify what answers on the bus",
         detail="""
Reads registers 2 and 3, turns them into a 32-bit identifier and looks that up
in a table extracted from the Linux kernel sources. Without an argument it
does this for all 32 addresses; with one it prints the full breakdown for that
address — OUI, model number, revision — so the command is still worth
something on a part the table has never heard of.

Matching uses the mask the kernel carries for each part rather than a fixed
one. That matters more than it sounds: Motorcomm registers the YT8531S and
the YT8531 as exact identifiers one nibble apart, so rounding the revision
away names the wrong chip.

Nothing is written to the bus. Switches are looked for only where the
database says a plain read reaches them — a Marvell part in single-chip mode,
an 88E6060, a LAN9303 — which is around a third of the switches it knows and
almost entirely Marvell. Everything else, including any Marvell switch in
multi-chip mode, needs a write to a command register first, and this command
will not do that on its own. `mv` and the chip helpers will.

Those are candidates rather than findings. One of the places a switch
answers is address 0x00 register 0x03, which on a plain PHY is its
identifier's low half, so a PHY there can look like a switch product code by
coincidence. The raw value is printed next to the name, and the same address
appears in the PHY listing above, so the two readings can be compared.

    probe mmd <phy> [devad]

is the exception and does write: registers 13 and 14 of the target are the
Clause-45 indirection window, and using it means loading an address into
them. Without a device address it tries 1, 3 and 7. The window is left
pointing at the last register read — here and in Linux both — so a later
plain read of register 14 returns that MMD register again rather than
register 14.

The table records which kernel it came from, and `probe <phy>` prints that
when it cannot identify a part, so it is clear what was searched.
""")
def cmd_probe(ctx, args):
    if args and args[0] == "mmd":
        _mmd(ctx, args[1:])
        return

    if not args:
        _sweep(ctx)
        return

    if len(args) > 1:
        raise CommandError("usage: probe [<phy> | mmd <phy> [devad]]")

    _detail(ctx, ranged(args[0], 0, 31, "address"))
