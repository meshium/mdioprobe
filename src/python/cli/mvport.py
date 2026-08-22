"""Port operations the three Link Street families spell identically.

A deliberate exception to the rule in `cli.marvell`, and worth stating plainly
because it moves a line that was drawn on purpose.

`cli.marvell` keeps addressing and refuses to know what any register means,
because the meanings differ between families in ways that do not merely fail —
scratch index 0x63 is NormalSMI on an 88E6321 and a GPIO *direction* register
on the other two. That rule stands. What it was protecting against is a
*difference*, though, and the port registers below are the case where the
three specifications say the same thing word for word:

    port offset 0x00  Port Status      link 11, duplex 10, PHYDetect 12,
                                       C_Mode 3:0
    port offset 0x01  Physical Control RGMII Rx 15, Tx 14, LinkValue 5,
                                       ForcedLink 4
    port offset 0x04  Port Control     PortState 1:0, 00 Disabled .. 11
                                       Forwarding

Checked in all three: 88E6321/6320 FS Rev. 0.05 Tables 65, 67 and 71; 88E6352/
6240/6176/6172 FS Tables 59 and 62; 88E6390X et al. FS TD-001000 Rev. 1
Tables 44, 48 and 62. Same offsets, same bit numbers, same encodings.

What is *not* here is everything that differs, and it arrives as a parameter:
where the port block starts and how many ports there are, which ports have an
RGMII interface at all, what each C_Mode value means on this family, where a
port's PHY and SERDES are reached, and what speed code 0x3 means.

The test to apply before adding anything else here: can the claim be quoted
from all three specifications in the same words? If it needs a "except on the
88E..." it belongs in the helper.

WHAT IS IN A PORT'S PATH

The Interface Configuration Matrix — Table 66 on the 6321, Table 60 on the
6240, Table 45 on the 6390 — is the same table three times: for each C_Mode,
and sometimes for each value of PHYDetect, it names where the port's link,
speed and duplex come from. That is exactly what a port operation needs to
know, so the helper hands its own copy of the table over and this module acts
on the answer:

    phy          C_Mode 0xF, an internal PHY on this port's own SMI address
    ppu          an xMII mode with PHYDetect set: an external PHY, polled
    px_enable    an xMII mode without it: nothing on the wire answers for
                 this port, the link follows a pin
    serdes       100BASE-FX, 1000BASE-X, 2500BASE-X, XAUI: the SERDES alone
    serdes+ppu   SGMII: the SERDES *and* a PHY behind it, both needed
    cpu, none    tristate, or the internal CPU port: nothing to power

The practical consequence, and the reason this is not decoration: a port in a
SERDES mode has no PHY in its path at all, so powering "the PHY at the port's
address" there is powering something else that happens to share a number.
"""

from cli.parser import spec
from cli.registry import CommandError

# Port Status, port offset 0x00.
STATUS = 0x00
ST_PHY_DETECT = 0x1000
ST_LINK = 0x0800
ST_DUPLEX = 0x0400
ST_CMODE = 0x000F

# Physical Control, port offset 0x01.
PHYS_CTRL = 0x01
PC_RGMII_RX = 0x8000
PC_RGMII_TX = 0x4000
PC_LINK_VALUE = 0x0020    # bit 5, inert unless ForcedLink is set
PC_FORCED_LINK = 0x0010   # bit 4

# Port Control, port offset 0x04.
PORT_CTRL = 0x04
PORT_STATE = 0x0003
STATES = ("disabled", "blocking", "learning", "forwarding")

# The PHY's own register 0, reached through whichever window the helper hands
# over. Clause 22 is Clause 22 on every part there has ever been.
BMCR = 0x00
BMCR_ANEG = 0x1000
BMCR_PDOWN = 0x0800
BMCR_RESTART = 0x0200

RGMII_MODES = {
    "none": 0,
    "rx": PC_RGMII_RX,
    "tx": PC_RGMII_TX,
    "both": PC_RGMII_RX | PC_RGMII_TX,
}

# What each link source means for `up` and `down`. See the module comment.
WANTS_PHY = ("phy", "ppu", "serdes+ppu")
WANTS_SERDES = ("serdes", "serdes+ppu")

# Verbs the `port` subcommand takes after its port list: the first group
# stands alone, the second takes one word after it.
BARE_VERBS = ("up", "down", "auto")
VALUED_VERBS = ("detect", "state", "rgmii")


def take_verb(args):
    """Split a trailing verb off `port`'s arguments.

    Returns (rest, action, value). The verb goes last because `detect on|off`
    already did, and one shape for all of them beats two.
    """
    args = list(args)

    if args and args[-1] in BARE_VERBS:
        return args[:-1], args[-1], None
    if len(args) >= 2 and args[-2] in VALUED_VERBS:
        return args[:-2], args[-2], args[-1]
    return args, None, None


def port_list(rest, action, count, usage):
    """The port list, insisting on one for anything that writes.

    Defaulting to every port is right for a status read and a trap for
    everything else: `port <sw> state forwarding` with no list would mean the
    whole switch. `detect` is exempt only because it behaved that way before
    there was anything else to be consistent with.
    """
    if len(rest) > 1:
        raise CommandError(usage)
    if rest:
        return spec(rest[0], 0, count - 1, "port")
    if action in (None, "detect"):
        return range(count)
    raise CommandError("say which ports: `{}` changes them, and with no list "
                       "that would be all {}".format(action, count))


def refuse_broadcast(port, label, cmd, sw_addr, phy_write_hint):
    """SMI address 0 is not a per-port address on the external bus.

    Realtek and Motorcomm parts answer at address 0 as well as at their
    strapped one, by default and in addition — that is what their documents
    call the broadcast address. So a read at 0 may be any PHY on that bus,
    and a write at 0 reaches *every* one of them at once, which is not what
    `port 0 up` looks like it does.

    Nothing here can tell an echo from a PHY genuinely strapped to 0, and the
    bit that turns the broadcast response off is a vendor register this layer
    has no business knowing. So refuse, and name the two ways through.

    **Only the helper may call this, and only for a PHY on the external MDIO
    bus.** An internal PHY is reached through the switch's own Global2 window
    and nothing else can answer there, so address 0 means exactly port 0 —
    which is the ordinary case on an 88E6240, whose internal PHYs are 0 to 4.
    Refusing there was a real bug, found on an 88E6240 rev 1 on 2026-08-22.
    """
    raise CommandError(
        "port {} reaches {} at SMI address 0 on the external bus, and that is "
        "the broadcast address: Realtek and Motorcomm PHYs answer there by "
        "default as well as at their strapped address, so a write would reach "
        "every one of them at once. Turn the broadcast response off in the "
        "PHY first, or write the register deliberately with `{} <value>`. "
        "`{} probe {}` shows which addresses answer".format(
            port, label, phy_write_hint.format(sw=sw_addr), cmd, sw_addr))


class Ports:
    """The port block of one switch, with the family's own facts supplied.

    `cmode` is the family's Interface Configuration Matrix: sixteen entries,
    each None where the value is Reserved, or a pair of `(name, source)`
    indexed by PHYDetect — `((name, source), (name, source))` — because on
    several xMII modes that bit is what decides whether a PHY is in the path.

    `phy_at(n)` and `serdes_at(n)` return what is reachable for port `n` as
    `(addr, bmcr, write, label)`, where `write(value)` puts a value back and
    `label` names the thing for the operator, or None when it is not there.
    Those are the pieces that cannot be shared: a PHY is one Clause-22 read
    through one window on two families and a search across two SMI functions
    on the third, and a SERDES is at a fixed SMI address on two of them and
    behind Clause 45 on the other.
    """

    def __init__(self, sw, cmd, base, count, rgmii_ports, speed, cmode,
                 phy_at, serdes_at=None, wide=False):
        self.sw = sw
        self.cmd = cmd
        self.base = base
        self.count = count
        self.rgmii_ports = rgmii_ports
        self.speed = speed
        self.cmode = cmode
        self.phy_at = phy_at
        self.serdes_at = serdes_at or (lambda n: None)
        self.wide = wide

    # --- plumbing -----------------------------------------------------

    def dev(self, n):
        return self.base + n

    def read(self, n, reg):
        return self.sw.read(self.dev(n), reg)

    def write(self, n, reg, value):
        self.sw.write(self.dev(n), reg, value)

    def name(self, n):
        # The 6390 has eleven ports, so its columns only line up at width 2.
        return "port {:2d}".format(n) if self.wide else "port {}".format(n)

    def mode(self, n, status=None):
        """(name, source) for this port, from the family's matrix."""
        if status is None:
            status = self.read(n, STATUS)
        entry = self.cmode[status & ST_CMODE]
        if entry is None:
            return ("C_Mode 0x{:x}, reserved on this family".format(
                status & ST_CMODE), "none")
        return entry[1 if status & ST_PHY_DETECT else 0]

    # --- the operations -----------------------------------------------

    def _parts(self, n, source):
        """Everything in this port's path, in the order to power it.

        The SERDES first: on SGMII the PHY sits behind it, and bringing the
        near end up before the far one is the order that needs no second
        thought.

        Returns (parts, missing). `missing` names what the mode says should
        be there and the helper could not reach — an external PHY behind an
        unrouted MDIO bus, a SERDES on a port whose block this helper has no
        map for. That is a different thing from a port with nothing to power,
        and saying so is the difference between a command that did nothing
        and a command that looks as if it worked.
        """
        parts = []
        missing = []

        if source in WANTS_SERDES:
            found = self.serdes_at(n)
            if found is None:
                missing.append("SERDES")
            else:
                # False: autonegotiation is not this command's business on a
                # SERDES. Whether the block negotiates at all is decided by
                # the mode it is in — 100BASE-FX has no autonegotiation to
                # enable, 1000BASE-X does — and that is configured elsewhere.
                # Measured on an 88E6321 rev 2: a 100BASE-FX SERDES reads
                # BMCR 0x2900 out of reset, with bit 12 clear, and setting it
                # as if it were a copper PHY changed a mode bit for no reason.
                # `up` powers things; it does not reconfigure them.
                parts.append((found, False))
        if source in WANTS_PHY:
            found = self.phy_at(n)
            if found is None:
                missing.append("PHY")
            else:
                parts.append((found, True))
        return parts, missing

    def link(self, ctx, ports, what):
        """up / down / auto, at the physical layer.

        What a port has in its path comes from its C_Mode, not from guessing:
        an internal or external PHY, a SERDES, both on SGMII, or nothing at
        all on an xMII port whose link follows a pin. Everything found is
        powered; a port with nothing to power gets the MAC's ForcedLink /
        LinkValue pair instead, which is the only thing up and down can mean
        there, and `auto` releases it.

        The two are never mixed. Forcing the link on a port that has a PHY or
        a SERDES would override the thing that was just powered, so a force
        is only ever *cleared* on such a port, never added.
        """
        for n in ports:
            status = self.read(n, STATUS)
            name, source = self.mode(n, status)
            parts, missing = self._parts(n, source)

            if missing:
                ctx.out.line("{}: {} — expected {} for this port and none "
                             "answers; that is what has to come up, not the "
                             "MAC link", self.name(n), name,
                             " and a ".join(missing))

            for (addr, bmcr, put, label), aneg in parts:
                if what == "down":
                    new = bmcr | BMCR_PDOWN
                elif aneg:
                    new = (bmcr | BMCR_ANEG | BMCR_RESTART) & ~BMCR_PDOWN
                else:
                    new = bmcr & ~BMCR_PDOWN
                put(new)
                ctx.out.line("{}: {} bmcr 0x{:04x} -> 0x{:04x} ({})",
                             self.name(n), label, bmcr, new,
                             "powered down" if what == "down" else
                             ("powered up, autoneg restarted" if aneg
                              else "powered up"))

            v = self.read(n, PHYS_CTRL)
            if what == "auto":
                # Both bits, not just the force: LinkValue has no effect on
                # its own, but leaving it set means the register does not
                # come back to what it was, and the next reader has to know
                # the spec to see that the difference is inert.
                new = v & ~(PC_FORCED_LINK | PC_LINK_VALUE)
            elif what == "up":
                new = v | PC_FORCED_LINK | PC_LINK_VALUE
            else:
                new = (v | PC_FORCED_LINK) & ~PC_LINK_VALUE

            if parts and what != "auto":
                continue
            if new != v:
                self.write(n, PHYS_CTRL, new)

            if not parts:
                ctx.out.line("{}: {} — nothing to power for this port; MAC "
                             "link {} (physical control 0x{:04x} -> "
                             "0x{:04x})", self.name(n), name,
                             "released to normal detection" if what == "auto"
                             else "forced {}".format(what), v, new)
            elif new != v:
                ctx.out.line("{}: MAC link force removed, {} decides again "
                             "(physical control 0x{:04x} -> 0x{:04x})",
                             self.name(n),
                             "the SERDES" if source == "serdes" else "the PPU",
                             v, new)

    def state(self, ctx, ports, name):
        if name not in STATES:
            raise CommandError(
                "state takes one of: {}".format(", ".join(STATES)))
        want = STATES.index(name)

        for n in ports:
            v = self.read(n, PORT_CTRL)
            new = (v & ~PORT_STATE) | want
            if new != v:
                self.write(n, PORT_CTRL, new)
            ctx.out.line("{}: {} (port control 0x{:04x} -> 0x{:04x})",
                         self.name(n), name, v, new)

    def rgmii(self, ctx, ports, mode):
        """Both delay bits, always written together.

        Stating the setting rather than accumulating it means `rgmii rx`
        turns the Tx delay off, which is what a reader of the command
        expects and what a read-modify-write of one bit would not do.
        """
        if mode not in RGMII_MODES:
            raise CommandError("rgmii takes one of: none, rx, tx, both")
        bits = RGMII_MODES[mode]

        for n in ports:
            if n not in self.rgmii_ports:
                raise CommandError(
                    "port {} has no RGMII interface on this family: bits "
                    "15:14 of Physical Control are defined on port(s) {} and "
                    "nowhere else here. The other Link Street families put "
                    "them on different ports — `help {}`".format(
                        n, ", ".join(str(x) for x in self.rgmii_ports),
                        self.cmd))

            if self.read(n, STATUS) & ST_LINK:
                raise CommandError(
                    "port {}'s link is up, and every one of the three "
                    "specifications says a change to these bits is "
                    "disruptive and must be made with the link down. "
                    "`{} port {} {} down` first".format(
                        n, self.cmd, self.sw.addr, n))

            v = self.read(n, PHYS_CTRL)
            new = (v & ~(PC_RGMII_RX | PC_RGMII_TX)) | bits
            if new != v:
                self.write(n, PHYS_CTRL, new)
            ctx.out.line("{}: RGMII delay {} (physical control 0x{:04x} -> "
                         "0x{:04x})", self.name(n),
                         "off" if mode == "none" else mode, v, new)

    # --- what the plain form prints -------------------------------------

    def status(self, ctx, ports):
        """Link as the PPU published it, the state that gates traffic, and
        the C_Mode that says where the link came from.

        C_Mode earns its place: without it a port reads "link down, no PHY
        detected" whether it is a fibre port waiting on a powered-down
        SERDES, an RGMII port whose link follows a pin, or a tristated one
        that will never come up at all. Those need three different actions
        and looked identical before.
        """
        for n in ports:
            try:
                v = self.read(n, STATUS)
                ctl = self.read(n, PORT_CTRL)
            except OSError as exc:
                ctx.out.line("{}: ERROR (errno {})", self.name(n),
                             exc.args[0] if exc.args else "?")
                continue

            name, source = self.mode(n, v)
            up = v & ST_LINK

            ctx.out.line("{}: 0x{:04x}  {}, link {}{}, {}{}",
                         self.name(n), v, name,
                         "up" if up else "down",
                         " {} {}".format(
                             self.speed[(v >> 8) & 3],
                             "full duplex" if v & ST_DUPLEX
                             else "half duplex") if up else "",
                         STATES[ctl & PORT_STATE],
                         ", PHY detected" if v & ST_PHY_DETECT else "")
