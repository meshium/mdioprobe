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
 * HSI48 trimming for USB. The board has no crystal, so USB FS runs off the
 * internal 48 MHz RC — which on its own drifts far outside the ±0.25 % USB
 * requires. The CRS peripheral closes that loop against the host's SOF
 * packets. Zephyr's G4 clock driver has no CRS support, so this module
 * sets it up from LL at boot (SYS_INIT).
 */

/** True once the CRS has locked onto USB SOF (i.e. the host is talking). */
bool mdioprobe_usb_crs_synced(void);

/** Raw CRS ISR flags — diagnostic only. */
uint32_t mdioprobe_usb_crs_flags(void);

/** Current HSI48 trim value the CRS has settled on. */
uint32_t mdioprobe_usb_crs_trim(void);
