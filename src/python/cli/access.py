"""Clause-22 addressing modes.

A PHY register is not always reachable by its number. Vendors layer indirect
schemes on top of the 32 Clause-22 registers — a page select, a device
address, an address/data window — and each is just a fixed sequence of plain
reads and writes.

Each mode here is an object with the same two methods, `read(reg)` and
`write(reg, value)`, wrapped around something that already has them. That is
what lets a mode compose: `Mmd(SwitchPhy(switch, 3), 7)` reads an MMD
register of a PHY behind a switch, and neither class knows about the other.
"""


class PhyBus:
    """Plain Clause-22 access to one PHY on the wire."""

    def __init__(self, phy):
        from cli import transport

        self._t = transport
        self.phy = phy

    def read(self, reg):
        return self._t.read(self.phy, reg)

    def write(self, reg, value):
        self._t.write(self.phy, reg, value)


class Paged:
    """Realtek-style pages: select through register 0x1F, then access normally.

    The page is left selected. That is how the parts behave and how the
    previous project drove them — restoring it would cost a write per access
    and would surprise anyone stepping through registers on one page.
    """

    SELECT = 0x1F

    def __init__(self, bus, page, select_reg=SELECT):
        self._bus = bus
        self._page = page
        self._select = select_reg

    def _arm(self):
        self._bus.write(self._select, self._page & 0xFFFF)

    def read(self, reg):
        self._arm()
        return self._bus.read(reg)

    def write(self, reg, value):
        self._arm()
        self._bus.write(reg, value)


class ExtPage:
    """Extension pages: 0x1F selects the extension space, 0x1E the page.

    Unlike `Paged` this one puts 0x1F back to 0 afterwards. Leaving the
    extension space selected changes what every ordinary register access
    means, so a later plain read would quietly return something else.
    """

    EXTENSION = 0x0007

    def __init__(self, bus, ext_page):
        self._bus = bus
        self._page = ext_page

    def _arm(self):
        self._bus.write(0x1F, self.EXTENSION)
        self._bus.write(0x1E, self._page & 0x00FF)

    def _release(self):
        try:
            self._bus.write(0x1F, 0x0000)
        except OSError:
            pass

    def read(self, reg):
        self._arm()
        try:
            return self._bus.read(reg)
        finally:
            self._release()

    def write(self, reg, value):
        self._arm()
        try:
            self._bus.write(reg, value)
        finally:
            self._release()


class Mmd:
    """Clause-45 registers reached indirectly through Clause 22 (0x0D/0x0E).

    0x0D takes the device address, 0x0E the register address, then 0x0D is
    rewritten with bit 14 set to turn 0x0E into a data port. Registers are
    16-bit, so the address space is far larger than Clause 22's 32.
    """

    ADDR = 0x0D
    DATA = 0x0E
    DATA_MODE = 0x4000   # data port, no post-increment

    def __init__(self, bus, devad):
        self._bus = bus
        self._devad = devad & 0x1F

    def _arm(self, reg):
        self._bus.write(self.ADDR, self._devad)
        self._bus.write(self.DATA, reg & 0xFFFF)
        self._bus.write(self.ADDR, self._devad | self.DATA_MODE)

    def read(self, reg):
        self._arm(reg)
        return self._bus.read(self.DATA)

    def write(self, reg, value):
        self._arm(reg)
        self._bus.write(self.DATA, value & 0xFFFF)


class Window:
    """An address/data register pair — write the address, then use the data port.

    Broadcom's RDB and Motorcomm's extended registers are both this shape,
    differing only in which registers form the pair.
    """

    def __init__(self, bus, addr_reg=0x1E, data_reg=0x1F):
        self._bus = bus
        self._addr = addr_reg
        self._data = data_reg

    def read(self, reg):
        self._bus.write(self._addr, reg & 0xFFFF)
        return self._bus.read(self._data)

    def write(self, reg, value):
        self._bus.write(self._addr, reg & 0xFFFF)
        self._bus.write(self._data, value & 0xFFFF)
