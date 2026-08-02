/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * MDIO Probe v2 — entry point.
 *
 * Brings the hardware layer up and gets out of the way. There is no command
 * interface in this image: the shell that used to live here was removed once
 * `mdioprobe_api.h` covered everything it could do, and the CLI that replaces
 * it is the next stage.
 *
 * Until then the board is observable through its log and its indication:
 * the target detector reports the bus rail it classified, and the LED shows
 * connection state on the base layer with activity overlaid.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include "mdioprobe_api.h"
#include "mdioprobe_msd.h"

LOG_MODULE_REGISTER(main, CONFIG_LOG_DEFAULT_LEVEL);

/* MicroPython's Zephyr port. `real_main()` is its entry point — the port
 * normally supplies its own main() in ports/zephyr/src/zephyr_start.c, which
 * we leave out of the build so the hardware is up before the interpreter
 * starts. `mp_console_init()` hands console RX to the interpreter. */
int real_main(void);
int mp_console_init(void);

int main(void)
{
	LOG_INF("MDIO Probe v2 (STM32G474CET6) start");

	int err = mdioprobe_init();

	if (err) {
		LOG_ERR("hardware init failed: %d", err);
		return err;
	}

	/* The volume is automounted by the fstab entry, so by here FatFs has
	 * it and the label can be checked. Idempotent, and a failure is not
	 * fatal — a cosmetic name is not worth refusing to boot over. */
	(void)mdioprobe_msd_label(NULL, 0);

	struct mdioprobe_bus_info bus;

	/* Logged, not printed: the CLI greets the operator itself once a host
	 * opens the port, and it is the one that waits for that. These lines
	 * are for the case where the CLI does not come up at all. */
	mdioprobe_bus_get(&bus);
	if (bus.target_present) {
		LOG_INF("Ready — target on a %d mV bus (measured %d mV, %u/%u "
			"samples at rail)",
			bus.nominal_mv, bus.measured_mv,
			bus.samples_used, bus.samples_total);
	} else {
		LOG_INF("Ready — no target detected (measured %d mV); capture "
			"and the master stay blocked until one is, or until the "
			"rail is declared", bus.measured_mv);
	}

	/* Hand this thread to MicroPython: it runs the frozen _boot.py, which
	 * mounts storage and starts the CLI, then the REPL. It does not
	 * return. */
	(void)mp_console_init();
	return real_main();
}
