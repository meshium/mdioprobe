/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Passive MDC frequency measurement: TIM2 input-capture on COMP1_OUT
 * (internal mux via TIM2_TISEL.TI1SEL = 1) + DMA1_Channel3, then outlier
 * rejection around the median period.
 *
 * Listening only is a deliberate constraint, not a missing feature. An
 * earlier version injected one bitbang frame to guarantee edges, with a
 * comment claiming outlier rejection would sort out a mixed sample. It does
 * not, and the failure is the wrong way round: our bitbang MDC runs at
 * ~130 kHz against an external master's megahertz, so on a busy bus *ours*
 * is the majority of the sample, becomes the median, and the real clock is
 * discarded as outliers — measured, that reported 132 kHz for a 6.24 MHz
 * bus. Worse than a wrong number, it drove MDC into a wire that already had
 * a master. A probe must not become one by accident.
 */

#include "mdioprobe_autocal.h"

#include <errno.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/logging/log.h>

#include <stm32g4xx.h>
#include <stm32g4xx_ll_bus.h>
#include <stm32g4xx_ll_dma.h>
#include <stm32g4xx_ll_dmamux.h>
#include <stm32g4xx_ll_rcc.h>
#include <stm32g4xx_ll_tim.h>

LOG_MODULE_REGISTER(mdioprobe_autocal, CONFIG_LOG_DEFAULT_LEVEL);

#define MDC_FREQ_MAX_EDGES 128U

#define DMAMUX_REQ_TIM2_CH1 56U /* RM0440 Table 91 */

static uint32_t mdc_ts_buf[MDC_FREQ_MAX_EDGES] __aligned(4);

static int cmp_u32(const void *a, const void *b)
{
	uint32_t va = *(const uint32_t *)a;
	uint32_t vb = *(const uint32_t *)b;
	return (va > vb) - (va < vb);
}

static uint32_t tim2_clock_hz(void)
{
	/* TIM2 sits on APB1. With APB1 prescaler = 1 the timer clock equals
	 * HCLK; otherwise the TIM clock is doubled relative to PCLK1
	 * (== 2 × HCLK / apb1_div). */
	uint32_t apb1_div;
	switch (LL_RCC_GetAPB1Prescaler()) {
	case LL_RCC_APB1_DIV_2:  apb1_div = 2;  break;
	case LL_RCC_APB1_DIV_4:  apb1_div = 4;  break;
	case LL_RCC_APB1_DIV_8:  apb1_div = 8;  break;
	case LL_RCC_APB1_DIV_16: apb1_div = 16; break;
	default:                 apb1_div = 1;  break;
	}
	return (apb1_div == 1) ? SystemCoreClock
			       : (2U * SystemCoreClock) / apb1_div;
}

/*
 * Arm TIM2 input-capture + DMA for `target_edges` MDC rising edges and wait
 * for them. Returns the number of edges actually captured.
 */
static int mdc_capture(int target_edges, int timeout_ms)
{
	LL_APB1_GRP1_EnableClock(LL_APB1_GRP1_PERIPH_TIM2);
	LL_AHB1_GRP1_EnableClock(LL_AHB1_GRP1_PERIPH_DMA1);
	LL_AHB1_GRP1_EnableClock(LL_AHB1_GRP1_PERIPH_DMAMUX1);

	/* Wipe any stale state from a prior call. */
	LL_TIM_DisableCounter(TIM2);
	LL_TIM_DisableDMAReq_CC1(TIM2);
	LL_DMA_DisableChannel(DMA1, LL_DMA_CHANNEL_3);
	LL_DMA_ClearFlag_GI3(DMA1);

	LL_TIM_SetPrescaler(TIM2, 0);
	LL_TIM_SetAutoReload(TIM2, 0xFFFFFFFFU);
	LL_TIM_SetCounterMode(TIM2, LL_TIM_COUNTERMODE_UP);
	LL_TIM_SetClockDivision(TIM2, LL_TIM_CLOCKDIVISION_DIV1);
	LL_TIM_SetCounter(TIM2, 0);

	/* TI1 ← COMP1_OUT (internal mux, no pin remap). */
	TIM2->TISEL = (TIM2->TISEL & ~TIM_TISEL_TI1SEL_Msk) |
		      (1U << TIM_TISEL_TI1SEL_Pos);

	LL_TIM_IC_SetActiveInput(TIM2, LL_TIM_CHANNEL_CH1, LL_TIM_ACTIVEINPUT_DIRECTTI);
	LL_TIM_IC_SetPrescaler(TIM2, LL_TIM_CHANNEL_CH1, LL_TIM_ICPSC_DIV1);
	LL_TIM_IC_SetFilter(TIM2, LL_TIM_CHANNEL_CH1, LL_TIM_IC_FILTER_FDIV1);
	LL_TIM_IC_SetPolarity(TIM2, LL_TIM_CHANNEL_CH1, LL_TIM_IC_POLARITY_RISING);
	LL_TIM_CC_EnableChannel(TIM2, LL_TIM_CHANNEL_CH1);

	LL_DMAMUX_SetRequestID(DMAMUX1, LL_DMAMUX_CHANNEL_2, DMAMUX_REQ_TIM2_CH1);

	memset(mdc_ts_buf, 0, sizeof(uint32_t) * target_edges);
	LL_DMA_SetPeriphAddress(DMA1, LL_DMA_CHANNEL_3, (uint32_t)&TIM2->CCR1);
	LL_DMA_SetMemoryAddress(DMA1, LL_DMA_CHANNEL_3, (uint32_t)mdc_ts_buf);
	LL_DMA_SetDataLength(DMA1,    LL_DMA_CHANNEL_3, target_edges);
	LL_DMA_SetDataTransferDirection(DMA1, LL_DMA_CHANNEL_3,
					LL_DMA_DIRECTION_PERIPH_TO_MEMORY);
	LL_DMA_SetChannelPriorityLevel(DMA1, LL_DMA_CHANNEL_3, LL_DMA_PRIORITY_HIGH);
	LL_DMA_SetMode(DMA1,           LL_DMA_CHANNEL_3, LL_DMA_MODE_NORMAL);
	LL_DMA_SetPeriphIncMode(DMA1,  LL_DMA_CHANNEL_3, LL_DMA_PERIPH_NOINCREMENT);
	LL_DMA_SetMemoryIncMode(DMA1,  LL_DMA_CHANNEL_3, LL_DMA_MEMORY_INCREMENT);
	LL_DMA_SetPeriphSize(DMA1,     LL_DMA_CHANNEL_3, LL_DMA_PDATAALIGN_WORD);
	LL_DMA_SetMemorySize(DMA1,     LL_DMA_CHANNEL_3, LL_DMA_MDATAALIGN_WORD);

	LL_DMA_EnableChannel(DMA1, LL_DMA_CHANNEL_3);
	LL_TIM_EnableDMAReq_CC1(TIM2);
	LL_TIM_EnableCounter(TIM2);

	int waited_ms = 0;
	while (!LL_DMA_IsActiveFlag_TC3(DMA1) && waited_ms < timeout_ms) {
		k_sleep(K_MSEC(10));
		waited_ms += 10;
	}

	LL_TIM_DisableCounter(TIM2);
	LL_TIM_DisableDMAReq_CC1(TIM2);
	LL_DMA_DisableChannel(DMA1, LL_DMA_CHANNEL_3);

	uint32_t remaining = LL_DMA_GetDataLength(DMA1, LL_DMA_CHANNEL_3);

	return target_edges - (int)remaining;
}

int mdioprobe_measure_mdc_freq_passive(int target_edges, int timeout_ms,
				       struct mdioprobe_mdc_stats *out)
{
	if (target_edges < 4) {
		target_edges = 4;
	}
	if (target_edges > (int)MDC_FREQ_MAX_EDGES) {
		target_edges = (int)MDC_FREQ_MAX_EDGES;
	}
	if (timeout_ms <= 0) {
		timeout_ms = 2000;
	}

	/*
	 * The caller's timeout is the whole listen window: how long a bus may
	 * stay silent before we call it idle is the caller's judgement, not
	 * ours. On a bus that has a master at all it fills in well under a
	 * millisecond — one Clause-22 transaction carries 64 MDC cycles, which
	 * already satisfies the default request.
	 */
	int captured = mdc_capture(target_edges, timeout_ms);

	memset(out, 0, sizeof(*out));
	out->edges_captured = captured;

	if (captured < 4) {
		return -EAGAIN;
	}

	/* `periods` and `sorted` are static to keep them off the (1 KB by
	 * default) main-thread stack — boot-time autocal runs in main's
	 * context. The function is non-reentrant by
	 * design (TIM2 + DMA channel are shared resources), so static
	 * storage is safe. */
	static uint32_t periods[MDC_FREQ_MAX_EDGES];
	int n_periods = 0;
	for (int i = 1; i < captured; i++) {
		periods[n_periods++] = mdc_ts_buf[i] - mdc_ts_buf[i - 1];
	}
	out->periods_total = n_periods;

	static uint32_t sorted[MDC_FREQ_MAX_EDGES];
	memcpy(sorted, periods, n_periods * sizeof(uint32_t));
	qsort(sorted, n_periods, sizeof(uint32_t), cmp_u32);
	uint32_t median = sorted[n_periods / 2];
	uint32_t lo = median - (median / 4);
	uint32_t hi = median + (median / 4);

	uint64_t sum = 0;
	int n_accepted = 0;
	uint32_t pmin = UINT32_MAX, pmax = 0;
	for (int i = 0; i < n_periods; i++) {
		if (periods[i] >= lo && periods[i] <= hi) {
			sum += periods[i];
			n_accepted++;
			if (periods[i] < pmin) pmin = periods[i];
			if (periods[i] > pmax) pmax = periods[i];
		}
	}
	out->periods_accepted = n_accepted;

	if (n_accepted < 2) {
		return -EIO;
	}

	uint32_t tim_freq = tim2_clock_hz();
	out->avg_period_ticks = (uint32_t)(sum / n_accepted);
	out->jitter_ticks     = pmax - pmin;
	out->tim_freq_hz      = tim_freq;
	out->freq_milli_hz    = (uint64_t)n_accepted * 1000ULL *
				 (uint64_t)tim_freq / sum;
	return 0;
}

