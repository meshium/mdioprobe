# mdioprobe v2 — board

SPDX-License-Identifier: CERN-OHL-P-2.0

KiCad 8 project for the probe: schematic, PCB, the footprints that are not in
the standard libraries, and a gerber set.

```
mdioprobe.kicad_sch     schematic
mdioprobe.kicad_pcb     board
mdioprobe.netlist       netlist
footprints.pretty/      footprints not in the KiCad libraries
gbr/                    gerbers and drill files
```

The KiCad footprint cache (`fp-info-cache`) and per-user project settings
(`*.kicad_prl`) are not tracked: KiCad rebuilds the first from the libraries
on demand, and the second is local to whoever opened the project.

## Licence

Everything in this directory is under the **CERN Open Hardware Licence
Version 2 — Permissive** (`LICENSE` here, SPDX `CERN-OHL-P-2.0`).

**The firmware is not.** The rest of the repository is Apache-2.0, with its
own `LICENSE` and `NOTICE` at the top level. Hardware and software are
different objects in law and are licensed separately, which is the usual
practice and the only one that makes sense: a software licence has nothing to
say about what "object form" or "linking" mean for a printed circuit board.

CERN-OHL-P rather than a Creative Commons licence, for two reasons. It is
written for hardware — it defines Source as the design files, Product as the
thing you build from them, and it covers making and having made, none of
which a licence written for creative works addresses. And it grants patent
rights (§6.1), which CC 4.0 explicitly does not (§2(b)(2): "Patent and
trademark rights are not licensed"). For hardware that gap is in exactly the
place the risk lives — the design files would be yours to copy while the
right to build the thing was not.

Permissive rather than the reciprocal variant (CERN-OHL-S): it matches the
firmware's Apache-2.0, and the point of publishing this board is that people
can use it, including inside something they do not open.

There is no warranty and no liability, per sections 5 and 8 of the licence.
This is a bench instrument, not a certified one.

## What it is

STM32G474CET6 in LQFP48. Comparators with a DAC threshold on the MDIO and MDC
inputs, a tri-state buffer on the MDC output powered from a programmable
soft-LDO so the probe drives at the target's own logic level, QSPI NOR for
storage, and USB.

`docs/HARDWARE.md` in the repository root is the pinout, the connector, and
the parts of the schematic worth knowing about before changing anything —
including what the next board revision needs.
