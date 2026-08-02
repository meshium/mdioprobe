/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * MicroPython configuration for the MDIO probe.
 *
 * Starts from the upstream Zephyr port's configuration and then takes things
 * away. Two different reasons to remove something, worth keeping apart:
 *
 *   - size. The image also carries the capture chain, the vreg loop and USB,
 *     and the GC heap is a static array competing for the same 128 KB.
 *
 *   - safety. `machine.Pin`, `machine.SPI` and friends would hand a script
 *     direct control of pins this firmware is actively using: PB6/PB7/PB8
 *     belong to the bitbang master, PB12-PB15 to the SPI capture slave,
 *     PA0/PA8/PA10 carry comparator outputs into it. A script that reassigns
 *     one of those does not fail loudly — it silently breaks capture. The
 *     probe's own API is the only sanctioned way to touch the hardware, so
 *     the generic peripheral modules are removed rather than left as a trap.
 */

#include "ports/zephyr/mpconfigport.h"

/* VFS stays on. It is what gives `import` and `open()` over the FAT volume,
 * and it is also the condition under which the port runs `_boot.py` at all
 * (see MICROPY_VFS && MICROPY_MODULE_FROZEN_MPY in ports/zephyr/main.c). */

/* The CLI reads its command line with input(), which is not in the ROM
 * feature level the port defaults to. It routes through the same readline
 * the REPL uses, so enabling it also buys cursor movement, history and
 * Ctrl-C handling — which is why there is no hand-rolled line editor here. */
#undef MICROPY_PY_BUILTINS_INPUT
#define MICROPY_PY_BUILTINS_INPUT   (1)

/* Line history. Upstream defaults to 8, which is short for a session spent
 * stepping through registers. The cost is 4 bytes per slot in BSS (the array
 * is a GC root in mp_state_ctx) plus the lines themselves on the GC heap, in
 * 16-byte blocks — so 24 slots of typical commands is well under a kilobyte
 * of a 32 KB heap.
 *
 * Note the history slots are *not* cleared on a soft reset on this port:
 * ports/zephyr/main.c never calls readline_init0(), unlike every other port,
 * so the pointers would survive into a re-initialised heap. _boot.py calls
 * mdioprobe.history_reset() to close that. */
#undef MICROPY_READLINE_HISTORY_SIZE
#define MICROPY_READLINE_HISTORY_SIZE (24)

/* --- safety: no generic peripheral access from Python --- */
#undef MICROPY_PY_MACHINE
#define MICROPY_PY_MACHINE          (0)
#undef MICROPY_PY_MACHINE_I2C
#define MICROPY_PY_MACHINE_I2C      (0)
#undef MICROPY_PY_MACHINE_I2C_TARGET
#define MICROPY_PY_MACHINE_I2C_TARGET (0)
#undef MICROPY_PY_MACHINE_SOFTI2C
#define MICROPY_PY_MACHINE_SOFTI2C  (0)
#undef MICROPY_PY_MACHINE_SPI
#define MICROPY_PY_MACHINE_SPI      (0)
#undef MICROPY_PY_MACHINE_SOFTSPI
#define MICROPY_PY_MACHINE_SOFTSPI  (0)
#undef MICROPY_PY_MACHINE_UART
#define MICROPY_PY_MACHINE_UART     (0)
#undef MICROPY_PY_MACHINE_WDT
#define MICROPY_PY_MACHINE_WDT      (0)
#undef MICROPY_PY_MACHINE_PWM
#define MICROPY_PY_MACHINE_PWM      (0)
#undef MICROPY_PY_MACHINE_ADC
#define MICROPY_PY_MACHINE_ADC      (0)

/* --- size: nothing on this board uses these --- */
#undef MICROPY_PY_SOCKET
#define MICROPY_PY_SOCKET           (0)
#undef MICROPY_PY_BLUETOOTH
#define MICROPY_PY_BLUETOOTH        (0)
#undef MICROPY_PY_ZSENSOR
#define MICROPY_PY_ZSENSOR          (0)

/* The `zephyr` module stays: zephyr.FileSystem is how the FAT volume gets
 * mounted into the VFS. */
