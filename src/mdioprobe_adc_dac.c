/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "mdioprobe_adc_dac.h"

#include <zephyr/drivers/adc.h>
#include <zephyr/device.h>
#include <zephyr/logging/log.h>
#include <stm32g4xx_ll_comp.h>
#include <stm32g4xx_ll_dac.h>
#include <stm32g4xx_ll_bus.h>
#include <stm32g4xx_ll_gpio.h>

LOG_MODULE_REGISTER(mdioprobe_adc_dac, CONFIG_LOG_DEFAULT_LEVEL);

static const struct adc_dt_spec adc_channel = ADC_DT_SPEC_GET_BY_NAME(DT_PATH(mdioprobe_config), mdio_adc);

void mdioprobe_comp_init(void)
{
	/*
	 * STM32G474CET6 pin map (post COMP7 / TIM3-OPM migration):
	 * - COMP1_INP: PA1  (MDC)           [INPSEL=0]
	 * - COMP7_INP: PB14 (MDIO)          [INPSEL=0]
	 * - COMP1_OUT: PA0  (AF8) → PCB trace to SPI2_SCK  (PB13)
	 * - COMP7_OUT: PA8  (AF8) → PCB trace to SPI2_MOSI (PB15)
	 *
	 * COMP outputs are also routed INTERNALLY to the frame-gating logic:
	 *   comp1_out → TIM3_ETR  (ETRSEL=0001)
	 *   comp7_out → TIM3_TI1  (TI1SEL=7) and HRTIM EEV5 (NSS set)
	 * No pin needed for that — handled in mdioprobe_tim3_count_init /
	 * mdioprobe_hrtim_nss_init.
	 *
	 * Inverting inputs go to internal-only DACs (no package pins,
	 * no external coupling — see mdioprobe_dac_thresholds_init):
	 * - COMP1_INM: DAC3_CH1 (INMSEL=100)
	 * - COMP7_INM: DAC4_CH1 (INMSEL=100)
	 *
	 * Reserved pins (USB/JTAG/QSPI) kept free: PA2 PA3 PA6 PA7 PA11
	 * PA12 PA13 PA14 PA15 PB0 PB1 PB3.
	 */

	LL_AHB2_GRP1_EnableClock(LL_AHB2_GRP1_PERIPH_GPIOA);
	LL_AHB2_GRP1_EnableClock(LL_AHB2_GRP1_PERIPH_GPIOB);
	LL_APB2_GRP1_EnableClock(LL_APB2_GRP1_PERIPH_SYSCFG);

	/* PA1 (COMP1_INP) → Analog */
	LL_GPIO_SetPinMode(GPIOA, LL_GPIO_PIN_1, LL_GPIO_MODE_ANALOG);
	LL_GPIO_SetPinPull(GPIOA, LL_GPIO_PIN_1, LL_GPIO_PULL_NO);

	/* PB14 (COMP7_INP) → Analog (also ADC1_IN5) */
	LL_GPIO_SetPinMode(GPIOB, LL_GPIO_PIN_14, LL_GPIO_MODE_ANALOG);
	LL_GPIO_SetPinPull(GPIOB, LL_GPIO_PIN_14, LL_GPIO_PULL_NO);

	/* PA0 (COMP1_OUT) → AF8 */
	LL_GPIO_SetPinMode(GPIOA, LL_GPIO_PIN_0, LL_GPIO_MODE_ALTERNATE);
	LL_GPIO_SetAFPin_0_7(GPIOA, LL_GPIO_PIN_0, LL_GPIO_AF_8);
	LL_GPIO_SetPinSpeed(GPIOA, LL_GPIO_PIN_0, LL_GPIO_SPEED_FREQ_VERY_HIGH);

	/* PA8 (COMP7_OUT) → AF8 */
	LL_GPIO_SetPinMode(GPIOA, LL_GPIO_PIN_8, LL_GPIO_MODE_ALTERNATE);
	LL_GPIO_SetAFPin_8_15(GPIOA, LL_GPIO_PIN_8, LL_GPIO_AF_8);
	LL_GPIO_SetPinSpeed(GPIOA, LL_GPIO_PIN_8, LL_GPIO_SPEED_FREQ_VERY_HIGH);

	/* COMP1 (MDC) */
	LL_COMP_SetInputPlus(COMP1, LL_COMP_INPUT_PLUS_IO1); /* PA1 */
	LL_COMP_SetInputMinus(COMP1, LL_COMP_INPUT_MINUS_DAC3_CH1);
	LL_COMP_SetInputHysteresis(COMP1, LL_COMP_HYSTERESIS_NONE);
	LL_COMP_SetOutputPolarity(COMP1, LL_COMP_OUTPUTPOL_NONINVERTED);
	LL_COMP_SetOutputBlankingSource(COMP1, LL_COMP_BLANKINGSRC_NONE);
	LL_COMP_Enable(COMP1);

	/* COMP7 (MDIO) */
	LL_COMP_SetInputPlus(COMP7, LL_COMP_INPUT_PLUS_IO1); /* PB14 */
	LL_COMP_SetInputMinus(COMP7, LL_COMP_INPUT_MINUS_DAC4_CH1);
	LL_COMP_SetInputHysteresis(COMP7, LL_COMP_HYSTERESIS_NONE);
	LL_COMP_SetOutputPolarity(COMP7, LL_COMP_OUTPUTPOL_NONINVERTED);
	LL_COMP_SetOutputBlankingSource(COMP7, LL_COMP_BLANKINGSRC_NONE);
	LL_COMP_Enable(COMP7);
}

static void dac_threshold_channel_init(DAC_TypeDef *dac, uint32_t ch)
{
	LL_DAC_SetMode(dac, ch, LL_DAC_MODE_NORMAL_OPERATION);
	LL_DAC_SetOutputBuffer(dac, ch, LL_DAC_OUTPUT_BUFFER_DISABLE);
	LL_DAC_SetOutputConnection(dac, ch, LL_DAC_OUTPUT_CONNECT_INTERNAL);
	LL_DAC_SetTriggerSource(dac, ch, LL_DAC_TRIG_SOFTWARE);
	LL_DAC_DisableTrigger(dac, ch);
	LL_DAC_Enable(dac, ch);

	/* Default mid-rail until threshold is computed from idle measurement. */
	LL_DAC_ConvertData12RightAligned(dac, ch, 2048);
}

static void mdioprobe_dac_thresholds_init(void)
{
	/*
	 * Threshold references:
	 *   DAC3_CH1 → COMP1_INM (MDC threshold,  INMSEL=100)
	 *   DAC4_CH1 → COMP7_INM (MDIO threshold, INMSEL=100)
	 *
	 * Both DAC3 and DAC4 are **internal-only** on G474 — no bond wires
	 * to package pins (RM0440 DAC implementation table). The
	 * MCR.MODE=011 "buffer disabled, internal only" mode keeps the
	 * unbuffered DAC node purely on-die, with no pad acting as an
	 * antenna or parasitic capacitance source. Compare DAC1/DAC2 which
	 * have external pins (PA4/PA5/...) and would capacitively couple
	 * switching noise from neighbouring AF outputs into the COMP_INM
	 * reference.
	 *
	 * The Zephyr stm32 DAC driver mandates pinctrl which DAC3/DAC4
	 * don't have (no pins), so configure directly via LL.
	 */
	LL_AHB2_GRP1_EnableClock(LL_AHB2_GRP1_PERIPH_DAC3);
	LL_AHB2_GRP1_EnableClock(LL_AHB2_GRP1_PERIPH_DAC4);

	dac_threshold_channel_init(DAC3, LL_DAC_CHANNEL_1); /* COMP1_INM (MDC)  */
	dac_threshold_channel_init(DAC4, LL_DAC_CHANNEL_1); /* COMP7_INM (MDIO) */
}

int mdioprobe_adc_dac_init(void)
{
	if (!device_is_ready(adc_channel.dev)) {
		LOG_ERR("ADC device not ready");
		return -ENODEV;
	}

	int err = adc_channel_setup_dt(&adc_channel);
	if (err) {
		LOG_ERR("ADC setup failed: %d", err);
		return err;
	}

	/*
	 * Thresholds: DAC3_CH1 → COMP1_INM (MDC), DAC4_CH1 → COMP7_INM
	 * (MDIO). Both internal-only DACs on G474 — no pin coupling.
	 */
	mdioprobe_dac_thresholds_init();
	return 0;
}

/*
 * Number of samples taken to find the idle level. MDIO is open-drain
 * pulled up to the bus rail, so the *peak* of a burst of samples is the
 * rail and everything below it is traffic. A single sample was what this
 * used to do, and on a live bus it simply reported wherever it happened
 * to land: measured against the 88E6321's PPU on 2026-07-31 the same
 * 3.3 V bus read 2549, 2856, 2923, 3167 and 3299 mV on successive boots.
 * Two things ride on this number — the comparator threshold
 * (idle × 0.30) and the boot-time `vreg` auto-start target — so a low
 * reading powers the level translator at the wrong voltage.
 *
 * At the PPU's ~4700 frames/s a frame occupies roughly 5% of the line's
 * time, so most samples land in idle regardless; 64 makes missing the
 * rail entirely a non-event.
 */
#define IDLE_SAMPLES 64U

/*
 * How far below the maximum a sample may sit and still count as "the rail".
 * 128 codes out of 4095 is ~100 mV at VREF 3.3 V — wide enough to absorb
 * ADC noise and ripple, narrow enough that a mid-transition sample or a
 * driven-low bit is excluded.
 */
#define LEVEL_TOL_CODE 128

int mdioprobe_adc_read_level(int32_t *out_mv, uint32_t *out_used,
			     uint32_t *out_total)
{
	int16_t buf;
	struct adc_sequence sequence = {
		.buffer = &buf,
		.buffer_size = sizeof(buf),
	};
	int32_t raw[IDLE_SAMPLES];
	int32_t peak_raw = INT32_MIN;

	adc_sequence_init_dt(&adc_channel, &sequence);

	for (unsigned int i = 0; i < IDLE_SAMPLES; i++) {
		int err = adc_read_dt(&adc_channel, &sequence);

		if (err) {
			LOG_ERR("ADC read failed: %d", err);
			return err;
		}
		raw[i] = (int32_t)buf;
		if (raw[i] > peak_raw) {
			peak_raw = raw[i];
		}
	}

	/*
	 * The reported level is the peak: on an open-drain line pulled up to
	 * the rail, that *is* the rail, and anything lower is either traffic
	 * or noise pulling the sample down.
	 *
	 * `used` counts how many samples sit within tolerance of that peak,
	 * and is a confidence figure rather than part of the estimate. A
	 * genuinely pulled-up line puts nearly all of them there even under
	 * the PPU's ~4700 frames/s; a floating pin does not.
	 *
	 * Averaging the cluster instead was tried and rejected: measured on
	 * the bench it read 3154 mV for a 3.3 V rail, because the ADC spread
	 * inside the tolerance band drags the mean ~150 mV below the rail.
	 * That is worse than the spike it was meant to guard against, and it
	 * matters — the translator supply is set from this number.
	 */
	uint32_t used = 0;

	for (unsigned int i = 0; i < IDLE_SAMPLES; i++) {
		if (raw[i] >= peak_raw - LEVEL_TOL_CODE) {
			used++;
		}
	}

	int32_t mv = peak_raw;
	int err = adc_raw_to_millivolts_dt(&adc_channel, &mv);

	if (err) {
		LOG_ERR("ADC conversion failed: %d", err);
		return err;
	}

	if (out_mv != NULL) {
		*out_mv = mv;
	}
	if (out_used != NULL) {
		*out_used = used;
	}
	if (out_total != NULL) {
		*out_total = IDLE_SAMPLES;
	}
	return 0;
}

int mdioprobe_adc_read_idle_voltage(int32_t *out_mv)
{
	return mdioprobe_adc_read_level(out_mv, NULL, NULL);
}

int mdioprobe_dac_set_threshold(int32_t mv)
{
	/* mV -> 12-bit raw. VDDA = VREF+ = 3.3 V on this board. */
	uint32_t val = (uint32_t)(mv * 4095) / 3300U;
	if (val > 4095U) {
		val = 4095U;
	}

	/* MDC threshold via DAC3_CH1 → COMP1_INM. */
	LL_DAC_ConvertData12RightAligned(DAC3, LL_DAC_CHANNEL_1, val);

	/* MDIO threshold via DAC4_CH1 → COMP7_INM. */
	LL_DAC_ConvertData12RightAligned(DAC4, LL_DAC_CHANNEL_1, val);
	return 0;
}

int mdioprobe_dac_set_mdio_threshold(int32_t mv)
{
	/*
	 * Override the MDIO comparator threshold (DAC4_CH1 → COMP7_INM) only.
	 * Useful for handling open-drain MDIO with slow RC-limited rising edge
	 * at high MDC frequencies: setting threshold low (~0.7–0.9V) lets the
	 * comparator detect the rise earlier than the symmetric idle/2 value.
	 * The MDC threshold (COMP1, DAC3_CH1) is unchanged.
	 */
	uint32_t val = (uint32_t)(mv * 4095) / 3300U;
	if (val > 4095U) {
		val = 4095U;
	}
	LL_DAC_ConvertData12RightAligned(DAC4, LL_DAC_CHANNEL_1, val);
	return 0;
}
