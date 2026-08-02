"""Marvell Link Street addressing — the indirection, and nothing above it.

Three layers, each a fixed sequence of Clause-22 accesses:

    switch registers   SMI command/data at the switch's own MDIO address
    switch PHYs        Global2 SMI PHY command/data
    scratch registers  Global2 scratch window, one byte at a time

What is deliberately absent is any knowledge of what those registers *mean*.
That knowledge turned out to differ enough between families to be dangerous:
scratch index 0x63 is NormalSMI on the 88E6321 and a GPIO **direction**
register on both the 88E6240 and the 88E6390, so the same write configures
pins as outputs on two parts out of three. Port registers start at device
address 0x10 on the first two and at 0x00 on the third. The 6321 needs the
PPU running before this window answers; the 6240 datasheet says the opposite
in as many words.

So chip semantics live in helper scripts on the volume (`use mv6321`), and
what stays here is what the 6321, 6240 and 6390 specifications agree on word
for word: the command/data pair at 0x00/0x01 and its bit layout, Global1 at
0x1B, Global2 at 0x1C, the SMI PHY window at 0x18/0x19, and the scratch
window protocol at 0x1A. What the registers mean is not here: that differs
between families and lives in the chip helpers on the volume.

Everything here is protocol. It goes through cli.transport like the rest, so
it works over whatever that is pointed at.
"""

import time

from cli import transport
from cli.registry import CommandError

# Multi-chip indirection, at the switch's own MDIO address.
#
# Layout of the command register, identical on all three families (88E6321
# Table 63, 88E6240 Table 57, 88E6390 Table 40): bit 15 SMIBusy, bit 12
# SMIMode (1 = generate Clause 22 frames), bits 11:10 SMIOp (01 write,
# 10 read), bits 9:5 device address, bits 4:0 register address.
SMI_CMD = 0x00
SMI_DATA = 0x01
SMI_BUSY = 0x8000
SMI_OP_C22_WRITE = 0x9400
SMI_OP_C22_READ = 0x9800

# Device addresses inside the switch. Ports are *not* here: their base is
# family-specific and belongs to the helper.
GLOBAL1 = 0x1B
GLOBAL2 = 0x1C

# Global2 windows.
G2_PHY_CMD = 0x18
G2_PHY_DATA = 0x19
G2_SCRATCH = 0x1A
G2_PHY_BUSY = 0x8000
G2_PHY_OP_C22_READ = 0x9800
G2_PHY_OP_C22_WRITE = 0x9400

# There is no "external PHY" bit in this register, and an earlier version of
# this file wrongly defined one as 0x0400 — that is the low bit of SMIOp, so
# setting it would have turned a read (10) into 11, which is Reserved.
#
# On the 6390 family bits 14:13 are a function field (0 internal access,
# 1 external, 2 SMI setup) where the other two leave them Reserved, so
# reaching an external PHY there needs a command word this module does not
# build. That is on purpose: a helper composes it out of sw.read/sw.write on
# Global2, which is exactly the case the extension surface has to support.

_POLL_TRIES = 16


def _wait(read_busy, what):
    for _ in range(_POLL_TRIES):
        if (read_busy() & 0x8000) == 0:
            return
        time.sleep_ms(1)
    raise CommandError("{} stayed busy — is this a Marvell switch?".format(what))


class Switch:
    """One switch, addressed either directly or through multi-chip indirection.

    Address 0 means single-chip: the switch's devices answer as ordinary MDIO
    addresses. Any other address means multi-chip, where the whole switch
    occupies one MDIO address and everything goes through a command/data pair
    there.

    This used to reject odd addresses as typos. That was invented rather than
    read: the 88E6321 spec says only that multi-chip mode is entered when the
    ADDR[4:0] pins "carry a non-zero value", and the 6390 datasheet shows a
    worked example of strapping a switch to address 0x01. The bench switch
    happens to sit at 6, which is not a rule.
    """

    def __init__(self, addr):
        if addr < 0 or addr > 31:
            raise CommandError("switch address out of range 0..31: {}".format(addr))
        self.addr = addr

    # --- switch registers ---------------------------------------------

    def read(self, dev, reg):
        dev &= 0x1F
        reg &= 0x1F

        if self.addr == 0:
            return transport.read(dev, reg)

        _wait(lambda: transport.read(self.addr, SMI_CMD), "SMI")
        transport.write(self.addr, SMI_CMD, SMI_OP_C22_READ | (dev << 5) | reg)
        _wait(lambda: transport.read(self.addr, SMI_CMD), "SMI")
        return transport.read(self.addr, SMI_DATA)

    def write(self, dev, reg, value):
        dev &= 0x1F
        reg &= 0x1F

        if self.addr == 0:
            transport.write(dev, reg, value)
            return

        _wait(lambda: transport.read(self.addr, SMI_CMD), "SMI")
        transport.write(self.addr, SMI_DATA, value & 0xFFFF)
        transport.write(self.addr, SMI_CMD, SMI_OP_C22_WRITE | (dev << 5) | reg)
        _wait(lambda: transport.read(self.addr, SMI_CMD), "SMI")

    # --- PHYs behind the switch ---------------------------------------

    def _phy_wait(self):
        _wait(lambda: self.read(GLOBAL2, G2_PHY_CMD), "Global2 PHY command")

    def phy_read(self, phy, reg):
        self._phy_wait()
        self.write(GLOBAL2, G2_PHY_CMD,
                   G2_PHY_OP_C22_READ | ((phy & 0x1F) << 5) | (reg & 0x1F))
        self._phy_wait()
        return self.read(GLOBAL2, G2_PHY_DATA)

    def phy_write(self, phy, reg, value):
        self._phy_wait()
        self.write(GLOBAL2, G2_PHY_DATA, value & 0xFFFF)
        self.write(GLOBAL2, G2_PHY_CMD,
                   G2_PHY_OP_C22_WRITE | ((phy & 0x1F) << 5) | (reg & 0x1F))
        self._phy_wait()

    # --- scratch and misc, one byte per index -------------------------
    #
    # Protocol only. Which index means what is family-specific and lives in
    # the helper: on the 6321 index 0x63 is NormalSMI, on the other two it is
    # a GPIO direction register.

    def scratch_read(self, index):
        self.write(GLOBAL2, G2_SCRATCH, (index & 0xFF) << 8)
        return self.read(GLOBAL2, G2_SCRATCH) & 0xFF

    def scratch_write(self, index, value):
        self.write(GLOBAL2, G2_SCRATCH,
                   0x8000 | ((index & 0xFF) << 8) | (value & 0xFF))


class SwitchPhy:
    """A PHY behind a switch, shaped like any other bus.

    This is what lets the addressing modes in cli.access work through a
    switch without knowing one is there: Mmd(SwitchPhy(sw, 3), 7) is an MMD
    register of a PHY the switch manages.
    """

    def __init__(self, switch, phy):
        self._sw = switch
        self._phy = phy

    def read(self, reg):
        return self._sw.phy_read(self._phy, reg)

    def write(self, reg, value):
        self._sw.phy_write(self._phy, reg, value)
