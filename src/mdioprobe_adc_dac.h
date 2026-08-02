/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef MDIOPROBE_ADC_DAC_H
#define MDIOPROBE_ADC_DAC_H

#include <zephyr/kernel.h>

int mdioprobe_adc_dac_init(void);
int mdioprobe_adc_read_idle_voltage(int32_t *out_mv);

/**
 * Measure the bus idle level and report how well-defined it was.
 *
 * MDIO is open-drain pulled up to the bus rail, so the level that means
 * something is the *high* one; everything below it is traffic. This takes a
 * burst of samples, then averages only those within a tolerance of the
 * maximum — a plain peak would let one noise spike set the answer, and a
 * plain mean would be dragged down by however busy the bus happens to be.
 *
 * `out_used` is the validity signal: on a real pulled-up line most samples
 * land near the rail, so a low count means the line is floating or the
 * measurement is not to be trusted.
 *
 * @param out_mv     averaged near-max level in millivolts
 * @param out_used   how many samples were within tolerance of the maximum
 * @param out_total  how many samples were taken
 * @return 0 on success, negative errno from the ADC otherwise.
 */
int mdioprobe_adc_read_level(int32_t *out_mv, uint32_t *out_used,
			     uint32_t *out_total);
int mdioprobe_dac_set_threshold(int32_t mv);
int mdioprobe_dac_set_mdio_threshold(int32_t mv);
void mdioprobe_comp_init(void);

#endif /* MDIOPROBE_ADC_DAC_H */
