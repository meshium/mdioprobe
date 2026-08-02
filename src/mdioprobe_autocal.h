/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <stdint.h>

struct mdioprobe_mdc_stats {
	int edges_captured;
	int periods_total;
	int periods_accepted;
	uint32_t avg_period_ticks;
	uint32_t jitter_ticks;
	uint32_t tim_freq_hz;
	/*
	 * Frequency in milli-Hz (Hz × 1000). uint64_t because typical
	 * MDC at 6+ MHz overflows uint32 once scaled by 1000 (6.25 MHz ×
	 * 1000 = 6.25e9 > 2^32).
	 */
	uint64_t freq_milli_hz;
};

/**
 * Measure MDC frequency via TIM2 input-capture from COMP1_OUT.
 *
 * Listens only. If nothing else drives MDC this fails rather than putting a
 * frame on the wire — see the note at the top of `mdioprobe_autocal.c` for
 * why that is a hard rule and not a missing feature.
 *
 * @param target_edges Edges to capture (4..128).
 * @param timeout_ms   How long the bus may stay silent before giving up.
 * @param out          Stats struct filled on success.
 * @return 0 on success, -EAGAIN if not enough edges captured, -EIO if
 *         outlier rejection accepted < 2 periods.
 */
int mdioprobe_measure_mdc_freq_passive(int target_edges, int timeout_ms,
				       struct mdioprobe_mdc_stats *out);
