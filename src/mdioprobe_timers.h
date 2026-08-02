/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <stdint.h>

/**
 * Initialize TIM3 as the per-frame MDC edge counter.
 *
 * After this call:
 *   - TI1 listens internally to comp7_out (MDIO digital), falling edge.
 *   - ETR is internally clocked by comp1_out (MDC digital), rising edge.
 *   - On each MDIO 1→0 (= Clause-22 ST=0 frame start) the one-pulse
 *     counter runs for `ARR` rising-MDC edges, then OPM stops it.
 *   - TRGO carries CNT_EN; its falling edge (the OPM stop) is the
 *     NSS-release event consumed by the HRTIM latch.
 *
 * TIM3 drives no GPIO pin — it is a purely internal counter feeding
 * `mdioprobe_hrtim_nss_init()`'s latch.
 *
 * Default counter range = 32 MDC ticks (one Clause-22 data field).
 */
void mdioprobe_tim3_count_init(void);

/**
 * Initialize the HRTIM SR-latch that generates SPI NSS.
 *
 * HRTIM1 Timer B output CHB1 (PA10, AF13, PCB trace → PB12) is an active-low
 * latch:
 *   - SET on EEV5 = comp7_out falling (frame Start)  → NSS LOW.
 *   - RST on EEV3 = tim3_trgo falling (32nd MDC tick) → NSS HIGH.
 *
 * Requires `mdioprobe_tim3_count_init()` to have configured TRGO.
 */
void mdioprobe_hrtim_nss_init(void);

/**
 * Reprogram the counter range (NSS LOW pulse width, in MDC ticks).
 * `arr` = number of rising-MDC edges the one-pulse counter spans before
 * OPM releases NSS. Clamped to a minimum of 1.
 */
void mdioprobe_tim3_set_pulse(uint32_t arr);

/** Read the current ARR — reported through the API's diagnostics. */
uint32_t mdioprobe_tim3_get_arr(void);
