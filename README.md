# mdioprobe v2

An MDIO probe on a custom STM32G474CET6 board: it watches a Clause-22 bus
without touching it, and it can drive one.

- **Passive capture.** The analogue MDIO and MDC lines go through comparators
  with a DAC threshold; TIM3 counts 32 MDC edges from the start bit, an HRTIM
  latch turns that into chip-select for SPI2 in slave mode, and the CPU only
  decodes finished frames. No per-frame software setup, so a busy bus does
  not outrun it — 284350 consecutive frames from an 88E6321 PPU at 6.25 MHz
  MDC with no decode errors, ring overruns or DMA faults.
- **Active master.** Bit-banged Clause 22 up to 2.5 MHz through a tri-state
  buffer powered from a programmable supply, so MDC comes out at the target's
  own logic level. Reads go back through the same comparator front end, which
  is why no receive-side translator is needed.
- **Level-agnostic.** The target's rail is measured, not assumed: 1.8, 2.5 and
  3.3 V buses are detected and every threshold derives from that one number.
  Until a rail is known, capture and the master both refuse to run.
- **Chip identification.** Registers 2 and 3 are looked up in a table
  extracted from the Linux kernel sources — 302 PHYs and 120 DSA switches,
  frozen into the firmware, so it works on a blank volume.

The command interface is a MicroPython CLI over USB CDC ACM, with history,
TAB completion, output redirection to files and a QSPI NOR volume for scripts
and logs.

```
mdio /flash> probe
phy 05: 4f51e91a  YT8531S Gigabit Ethernet
1 device(s) answered with an identifier

mdio /flash> read 5 2-3
reg 0x02 = 0x4f51
reg 0x03 = 0xe91a
```

## Building

Zephyr, with the board definition in-tree. The MicroPython submodule is
needed first:

```
git submodule update --init --recursive
./build.sh              # build
./build.sh flash        # build and flash over SWD
```

`build.sh` is the only supported entry point. It sources the Zephyr
environment, pins a toolchain and calls `west`, and it takes nothing on trust
from the machine it runs on:

| | |
| :-- | :-- |
| `ZEPHYR_BASE` | the Zephyr tree; defaults to `~/zephyrproject/zephyr` |
| `MDIOPROBE_SDK` | which SDK; defaults to the newest `~/zephyr-sdk-*` |
| `MDIOPROBE_VENV` | virtualenv to activate, and only when `west` is not already on `PATH` |

If your Zephyr environment is already active, the script uses it and changes
nothing. If a path is missing it says which one, rather than failing deep
inside CMake.

## Console

There is no VCP on the board. The log and the CLI share **USB CDC ACM**
(`/dev/tty.usbmodem*`); SWD on PA13/PA14/PB3 is for debugging. `tools/con.py`
is the bench driver for the console.

`help` lists the commands and `help <command>` explains one. USB is either the
console or a mass-storage device, never both at once: `msd` hands the volume
to the host and takes the console away until the probe is unplugged.

## Layout

```
src/            firmware; the public surface is include/mdioprobe_api.h alone
src/python/     the CLI, frozen into the image
boards/         the board definition (HWMv2)
hardware/       KiCad project: schematic, PCB, gerbers
docs/CAPTURE.md   how a frame is captured, end to end
docs/HARDWARE.md  pinout, connector, and what the schematic actually does
tools/con.py    console driver for the bench
tools/upload.py put files on the volume without unplugging
```

Zephyr APIs are used for ADC, GPIO, flash, UART, I2C and USB; the comparators,
TIM3, HRTIM, the SPI2 slave ring and the bit-bang master are driven through
STM32Cube LL and direct register writes, because Zephyr does not expose the
COMP-to-timer crossbars and its STM32 SPI slave driver races its own DMA.

## Status

Pre-release. Bring-up is complete and verified on hardware, including capture
against a real external master. Not yet exercised: the `uart` and `i2c`
connector personalities, and the short-circuit latch on the programmable
supply.

## Licence

Two objects, licensed separately, which is the usual practice for open
hardware and the only one that reads sensibly — a software licence does not
define what "object form" or "linking" mean for a printed circuit board.

- **Firmware and everything else: Apache-2.0**, text in `LICENSE`. The image
  is a static link, so a binary release carries Zephyr, MicroPython, the
  STM32Cube LL drivers, FatFs and picolibc with it; `NOTICE` lists each
  component and its terms and belongs with the binary, not only with the
  source.
- **The board, under `hardware/`: CERN-OHL-P-2.0**, text in
  `hardware/LICENSE`. Permissive, to match the firmware, and written for
  hardware — unlike a Creative Commons licence it grants patent rights, which
  for a circuit board is the part that matters.
