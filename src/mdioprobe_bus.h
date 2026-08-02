/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

/*
 * Target detection by MDIO idle voltage.
 *
 * A background thread watches the level on the sniffed MDIO line and
 * classifies it into one of the standard logic rails. Everything that
 * touches the wire depends on knowing that rail: the comparator thresholds
 * are derived from it, and so is the translator supply for the master's MDC
 * output. Until it is known — detected or declared by the operator — capture
 * and the master stay blocked.
 *
 * Ported from ~/src/mdioprobe/src/mdioprobe_mdio_link.c.
 */

struct mdioprobe_bus_status {
	bool     target_present; /* level classified into a known rail */
	bool     level_forced;   /* operator declared the rail by hand */
	int32_t  nominal_mv;     /* 1800 / 2500 / 3300, or -1 if unknown */
	int32_t  measured_mv;    /* last measured idle level */
	uint32_t samples_used;   /* samples that clustered at the rail */
	uint32_t samples_total;
};

/** Start the detector thread. Idempotent. */
void mdioprobe_bus_start(void);

void mdioprobe_bus_get_status(struct mdioprobe_bus_status *out);

/** True when the rail is known, whether detected or forced. */
bool mdioprobe_bus_ready(void);

/** Rail in millivolts, or -1 when unknown. */
int32_t mdioprobe_bus_nominal_mv(void);

/**
 * Declare the rail by hand, for a bench with no target attached.
 *
 * @param nominal_mv 1800, 2500 or 3300; 0 returns to auto-detection.
 * @return 0 on success, -EINVAL for any other value.
 */
int mdioprobe_bus_force_level(int32_t nominal_mv);

/**
 * Tell the detector the wire is ours right now.
 *
 * The master drives PB8 hard while a transaction runs, so a level sampled
 * then measures the probe, not the target. The master brackets its
 * transactions with these so the detector skips those windows instead of
 * concluding the target vanished.
 */
void mdioprobe_bus_inhibit(bool on);
