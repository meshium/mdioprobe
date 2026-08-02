/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Frame gating for SPI NSS: TIM3 edge-counter + HRTIM SR-latch.
 *
 *   COMP7_OUT (MDIO ↓ = ST) ─┬─► HRTIM EEV5 ─► SETx1R(B) → NSS LOW
 *                            └─► TIM3_TI1   (trigger)
 *   COMP1_OUT (MDC ↑) ─────────► TIM3_ETR   (external clock, ECE=1)
 *                                 TIM3 one-pulse, ARR = 32 MDC
 *                                 CEN set by trigger, cleared by OPM;
 *                                 MMS = Enable → TRGO carries CNT_EN
 *                            ───► HRTIM EEV3 (CNT_EN ↓) ─► RSTx1R(B) → NSS HIGH
 *                                          HRTIM1 Timer B output CHB1 = PA10
 *
 * Why the split
 * -------------
 * A plain GP-timer OC output cannot produce NSS. Its level is a compare
 * against CNT, and the OPM-parked counter value (0) is *also* the
 * pulse-start value — so "HIGH when idle + zero-delay LOW pulse" is
 * unreachable with one OC channel (idle level is forced equal to the
 * pulse-start level). The HRTIM output is a true SR latch with
 * independent set and reset event sources; it holds its level between
 * events, which is exactly the missing primitive.
 *
 * TIM3 still does what it does well: count exactly 32 real MDC edges,
 * single-shot per trigger (mid-frame MDIO 1→0 ignored by OPM), no fMDC
 * calibration, IFG irrelevant. HRTIM is reduced to one output's
 * set/reset crossbar — no EEVA counter, no blanking, no compare, no IRQ.
 *
 * Set/reset priority (RM0440 §28.3.7): when EEV5 (set) and EEV3 (reset)
 * fall within the same tHRTIM period, reset wins and set is ignored — so
 * the 32-MDC boundary always resolves to NSS HIGH.
 *
 * Internal routing references (RM0440):
 *   - tim_etr1    = comp1_out for TIM3 (Table p.1293) → ETRSEL=0001.
 *   - tim_ti1_in7 = comp7_out for TIM3 (Table p.1291) → TI1SEL=7.
 *   - hrtim_eev3 SRC=10 → tim3_trgo ; hrtim_eev5 SRC=10 → comp7_out
 *     (RM0440 Table 240, External events).
 */

#include "mdioprobe_timers.h"

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <stm32g4xx_ll_bus.h>
#include <stm32g4xx_ll_gpio.h>
#include <stm32g4xx_ll_tim.h>
#include <stm32g4xx.h>

LOG_MODULE_REGISTER(mdioprobe_timers, CONFIG_LOG_DEFAULT_LEVEL);

/*
 * Default counter range in MDC ticks. Clause-22 frame data = 32 MDC.
 * In one-pulse mode the counter starts at 0 on each trigger and the
 * update event (UEV) — where OPM clears CEN — lands ARR MDC edges later.
 * That UEV is the NSS-release moment.
 *
 * On-bench tuning may shift ARR by ±1 to align the release exactly with
 * the last data bit. The DMA-rotation auto-detect in mdio_api absorbs
 * residual byte-level misalignment.
 */
#define TIM3_COUNT_ARR_DEFAULT 32U

/*
 * HRTIM1 timing unit driving NSS. sTimerxRegs index: A=0, B=1 … F=5.
 * Timer B output 1 (CHB1) is pinned to PA10 (AF13) — PA10 exists on
 * both LQFP48 and LQFP64 G474 packages (CHE/CHF are LQFP64-only).
 */
#define HRTIM_NSS_TIMER 1U

void mdioprobe_tim3_count_init(void)
{
	LL_APB1_GRP1_EnableClock(LL_APB1_GRP1_PERIPH_TIM3);

	/* Disable counter while configuring slave-mode and OPM. */
	LL_TIM_DisableCounter(TIM3);

	/* ETR source = comp1_out (tim_etr1 → ETRSEL=0001). */
	LL_TIM_SetETRSource(TIM3, LL_TIM_TIM3_ETRSOURCE_COMP1);

	/* TI1 source = comp7_out (tim_ti1_in7 → TI1SEL=7). */
	LL_TIM_SetRemap(TIM3, LL_TIM_TIM3_TI1_RMP_COMP7);

	/*
	 * External clock mode 2: counter clocked by ETR (MDC) rising edges,
	 * independent of the slave-mode trigger.
	 */
	LL_TIM_SetClockSource(TIM3, LL_TIM_CLOCKSOURCE_EXT_MODE2);
	LL_TIM_ConfigETR(TIM3,
			 LL_TIM_ETR_POLARITY_NONINVERTED,
			 LL_TIM_ETR_PRESCALER_DIV1,
			 LL_TIM_ETR_FILTER_FDIV1);

	/*
	 * TI1 = input capture (DIRECTTI), falling edge (MDIO 1→0 at ST=0).
	 * Slave mode = trigger: each trigger starts the counter (the counter
	 * is NOT reset). Combined with OPM this yields one pulse per trigger;
	 * mid-frame data 1→0 transitions, arriving while the counter runs,
	 * are ignored.
	 */
	LL_TIM_IC_SetActiveInput(TIM3, LL_TIM_CHANNEL_CH1,
				 LL_TIM_ACTIVEINPUT_DIRECTTI);
	LL_TIM_IC_SetPrescaler(TIM3, LL_TIM_CHANNEL_CH1, LL_TIM_ICPSC_DIV1);
	LL_TIM_IC_SetFilter(TIM3, LL_TIM_CHANNEL_CH1, LL_TIM_IC_FILTER_FDIV1);
	LL_TIM_IC_SetPolarity(TIM3, LL_TIM_CHANNEL_CH1, LL_TIM_IC_POLARITY_FALLING);

	LL_TIM_SetTriggerInput(TIM3, LL_TIM_TS_TI1FP1);
	LL_TIM_SetSlaveMode(TIM3, LL_TIM_SLAVEMODE_TRIGGER);

	/*
	 * TRGO = Enable: TRGO carries the CNT_EN signal — HIGH while the
	 * counter runs, LOW when it is stopped. HRTIM EEV3 takes the
	 * *falling* edge of CNT_EN (= the OPM stop at the ARR-th MDC tick)
	 * as the NSS-release event. Routing a level transition (rather than
	 * a one-cycle UEV pulse) keeps the edge wide and unambiguous for the
	 * HRTIM external-event resampler.
	 */
	LL_TIM_SetTriggerOutput(TIM3, LL_TIM_TRGO_ENABLE);

	/* Counter range = ARR ticks of MDC; no prescaler. */
	LL_TIM_SetPrescaler(TIM3, 0);
	LL_TIM_SetAutoReload(TIM3, TIM3_COUNT_ARR_DEFAULT);

	/* One-pulse mode: counter auto-stops at the next UEV after a trigger. */
	LL_TIM_SetOnePulseMode(TIM3, LL_TIM_ONEPULSEMODE_SINGLE);

	/* Load preload registers (PSC/ARR) into shadow before run. */
	LL_TIM_GenerateEvent_UPDATE(TIM3);

	/*
	 * No CEN needed: in slave-trigger + OPM mode the slave controller
	 * sets CEN on each trigger and OPM clears it at UEV. TIM3 drives no
	 * pin — TI1/ETR are internal mux inputs and TRGO is an internal
	 * signal to HRTIM.
	 */
}

void mdioprobe_hrtim_nss_init(void)
{
	LL_APB2_GRP1_EnableClock(LL_APB2_GRP1_PERIPH_HRTIM1);

	/* PA10 = HRTIM1_CHB1 (AF13), high-speed push-pull — the NSS line. */
	LL_AHB2_GRP1_EnableClock(LL_AHB2_GRP1_PERIPH_GPIOA);
	LL_GPIO_SetPinMode(GPIOA, LL_GPIO_PIN_10, LL_GPIO_MODE_ALTERNATE);
	LL_GPIO_SetAFPin_8_15(GPIOA, LL_GPIO_PIN_10, LL_GPIO_AF_13);
	LL_GPIO_SetPinSpeed(GPIOA, LL_GPIO_PIN_10, LL_GPIO_SPEED_FREQ_VERY_HIGH);
	LL_GPIO_SetPinOutputType(GPIOA, LL_GPIO_PIN_10, LL_GPIO_OUTPUT_PUSHPULL);

	/*
	 * External-event source mux (HRTIM_EECR1, RM0440 Table 240).
	 * EECR1 packs 6 bits per event: EExSRC[1:0], EExPOL, EExSNS[1:0],
	 * EExFAST. EE3 occupies bits 17:12, EE5 occupies bits 29:24.
	 *   EE3 ← tim3_trgo  (SRC=10), SNS=10 falling  → CNT_EN ↓ = NSS release
	 *   EE5 ← comp7_out  (SRC=10), SNS=10 falling  → MDIO ↓  = NSS assert
	 * POL/FAST left 0: both edges are clean, and synchronous resampling
	 * (~6-12 ns at fHRTIM) is negligible against a 160 ns MDC period.
	 */
	HRTIM1->sCommonRegs.EECR1 &= ~((0x3FU << 12) | (0x3FU << 24));
	HRTIM1->sCommonRegs.EECR1 |= (2U << 12) | (2U << 15); /* EE3: tim3_trgo, falling */
	HRTIM1->sCommonRegs.EECR1 |= (2U << 24) | (2U << 27); /* EE5: comp7_out, falling */

	/*
	 * Timer B counter — must be written before MCR.TBCEN. The counter
	 * value itself is unused (no compare/period event is wired to the
	 * output); the unit only has to be in RUN so the set/reset crossbar
	 * is live. CKPSC=0, CONT=1 (free-running), PER at the documented max.
	 */
	HRTIM1->sTimerxRegs[HRTIM_NSS_TIMER].TIMxCR = (0U << 0) | (1U << 3);
	HRTIM1->sTimerxRegs[HRTIM_NSS_TIMER].PERxR  = 0xFFDFU;

	/*
	 * CHB1 (NSS) is ACTIVE-LOW via OUTxR.POL1 = 1: the "active" state on
	 * the pin is LOW, the "inactive" (and post-reset default) state is
	 * HIGH.
	 *   SET on EXTEVNT5 (comp7_out ↓, frame Start)  → active   → NSS LOW
	 *   RST on EXTEVNT3 (tim3_trgo, CNT_EN ↓ @ 32)  → inactive → NSS HIGH
	 */
	HRTIM1->sTimerxRegs[HRTIM_NSS_TIMER].OUTxR = (1U << 1); /* POL1 = 1 */

	/* SET source: EXTEVNT5 = bit 25 (EXTEVNTn at bit 20+n). */
	HRTIM1->sTimerxRegs[HRTIM_NSS_TIMER].SETx1R = (1U << 25);

	/*
	 * RST source: EXTEVNT3 = bit 23. Written together with SRT (bit 0,
	 * software reset trigger, write-1 self-clearing) so the output is
	 * prepositioned INACTIVE = HIGH before it is connected to the pin.
	 * SRT reads back 0, leaving RSTx1R = EXTEVNT3 for steady state.
	 */
	HRTIM1->sTimerxRegs[HRTIM_NSS_TIMER].RSTx1R = (1U << 23) | (1U << 0);

	/* Timer B into RUN (MCR.TBCEN = bit 18), then connect CHB1 to the
	 * pin (OENR.TB1OEN = bit 2). */
	HRTIM1->sMasterRegs.MCR  |= (1U << 18);
	HRTIM1->sCommonRegs.OENR |= (1U << 2);
}

void mdioprobe_tim3_set_pulse(uint32_t arr)
{
	if (arr == 0) {
		arr = 1;
	}
	LL_TIM_SetAutoReload(TIM3, arr);
	/* ARPE is off — ARR takes effect immediately; the UEV just reloads
	 * the counter so the new range applies from the next trigger. */
	LL_TIM_GenerateEvent_UPDATE(TIM3);
}

uint32_t mdioprobe_tim3_get_arr(void)
{
	return LL_TIM_GetAutoReload(TIM3);
}
