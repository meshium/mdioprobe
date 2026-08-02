"""`monitor` — watch a register set and report changes."""

from cli import monitor
from cli.cmd_mdio import _MODES, _WIDE, _label, _open, _regs
from cli.registry import command


@command("monitor", category="mdio",
         syntax="monitor [mode] <phy> [...] [regs] [for <sec>]",
         summary="watch registers and report every change",
         detail="""
Takes the same addressing modes as `read`: page, mvpage, extpage, mmd, rdb,
extreg. Without a register list it watches all 32 Clause-22 registers.

    monitor 6 0x01                  link status, for the default 60 s
    monitor 6 0x01,0x05 for 30      thirty seconds, then stop
    monitor mvpage 3 0 0x11 for 10  a paged register

The duration is written `for <sec>` rather than as a trailing number,
because a bare number there is ambiguous with the register list. There is
always a duration: a running command cannot be interrupted on this
firmware, so an unbounded watch would need a power cycle to leave.

The first sweep is silent — it only establishes the baseline. Output goes
through the usual redirection, so `monitor 6 0x01 for 60 > log.txt` works.
""")
def cmd_monitor(ctx, args):
    seconds, args = monitor.take_timeout(args)
    mode, bus, rest = _open(args, True)
    registers = _regs(mode, rest, mode in _WIDE)

    monitor.run(ctx, registers, bus.read, _label(mode, None),
                4 if mode in _WIDE else 2, seconds)
