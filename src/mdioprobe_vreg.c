/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Programmable supply for the MDC level translator — software-loop LDO.
 *
 *        Vin 3.3V
 *          ├──[ R_pu 100k-1M ]──┐
 *      ┌───┴───┐ S              │
 *      │P-MOSFET│ G ◄───────────┴── PA4 (DAC1_OUT1, buffered)
 *      └───┬───┘
 *          │ D ──► Vout (1.8-3.3V) ──► level translator supply
 *        [ Cout ]   └───────────────── PB2 (ADC2_IN12)
 *          │
 *         GND
 *
 * The internal opamps cannot be used on this LQFP48 pinout (their VOUT
 * pins are all occupied), so the regulation loop runs in software:
 *   - DAC1_CH1 (PA4, buffered) drives the P-MOSFET gate.
 *   - ADC2_IN12 (PB2) senses Vout continuously.
 *   - TIM6 fires a 20 kHz ISR that runs an integral controller and
 *     polls the ADC analog watchdog. The tick rate is the floor for
 *     peak load-step sag: sag_floor ≈ ΔI × T_tick / Cout, so 50 µs
 *     between samples is what bounds how deep a sudden load doubles
 *     before the loop can react.
 *   - The R_pu gate pull-up makes the P-MOSFET fail-safe-off before
 *     firmware runs and whenever the DAC is parked high.
 *
 * Control polarity: a lower gate voltage turns the P-MOSFET on harder,
 * raising Vout. So Vout-too-low → decrease the DAC code.
 *
 * Short-circuit protection: the ADC analog watchdog (AWD1) flags any
 * Vout dip below `target - sc_margin`, but a single flag is not a
 * fault — a load step (e.g. parallel resistor added) sags Vout for a
 * few ms while the integrator winds the gate down to deliver more
 * current. FAULT latches only when the controller is *saturated* (gate
 * pinned at DAC_GATE_MIN, full effort applied) AND the AWD stays
 * active for VREG_SC_TICKS consecutive ticks. A transient sag with the
 * gate still moving just resets the counter. Recovery is by an
 * explicit `mdioprobe_vreg_set_target()`.
 */

#include "mdioprobe_vreg.h"

#include <zephyr/kernel.h>
#include <zephyr/irq.h>
#include <zephyr/logging/log.h>
#include <stm32g4xx_ll_bus.h>
#include <stm32g4xx_ll_gpio.h>
#include <stm32g4xx_ll_tim.h>
#include <stm32g4xx_ll_dac.h>
#include <stm32g4xx_ll_adc.h>
#include <stm32g4xx.h>

LOG_MODULE_REGISTER(mdioprobe_vreg, CONFIG_LOG_DEFAULT_LEVEL);

/* Reference for the mV <-> 12-bit-code conversion (nominal VDDA). */
#define VREG_VREF_MV          3300U

#define DAC_GATE_OFF          4095U   /* gate high → P-MOSFET off */
#define DAC_GATE_MIN          0U      /* gate fully low → P-MOSFET full on */

#define VREG_SC_MARGIN_DFL    200U    /* mV below target → short-circuit trip */

/* Soft-start: setpoint counts added per 50 µs tick. ~4095/8 = 512
 * ticks ≈ 25 ms full-scale ramp. */
#define VREG_SOFTSTART_STEP   8U

/*
 * PI gains, Q16 (x_q16 = gain * 65536). The gate correction each tick is
 *
 *     gate = acc - Kp*e,      acc -= Ki*e
 *
 * with e in ADC counts and the result in DAC codes. e > 0 means Vout is
 * low, and a lower gate turns the P-MOSFET on harder.
 *
 * Why a P term is needed at all — the plant is an integrator. The MOSFET
 * is a current source into Cout, so dVout/dt = I(Vgate)/C; it is not a
 * static gain with a pole, which is what an earlier revision of this file
 * assumed. An integral controller on an integrating plant is 1/s^2: 180
 * degrees of lag, oscillatory at *any* gain. That is exactly what the
 * bench showed on 2026-07-28 — lowering KI from 1/16 to 1/107 dropped the
 * oscillation from ~110 Hz to ~40 Hz and stretched the ringdown from
 * ~200 ms to ~800 ms while leaving the cycle count untouched, and 1/809
 * made it far worse (600-800 mV of sawtooth). Proportional action turns
 * the loop into a single integrator, which is stable with ~90 degrees of
 * margin.
 *
 * Defaults KI = 1/5, KP = 1/8 are both bench-tuned on hardware
 * 2026-07-28. KI alone: 1/100 gave 600 mV of overshoot settling in 800 ms,
 * 1/10 gave 100 mV in 100 ms. Adding KP shrinks the sawtooth further but
 * does *not* remove it, which is the expected result and worth stating
 * plainly:
 *
 * The pass element can only source current. Nothing in this circuit pulls
 * Vout down — an overshoot decays only as fast as ~40 uA of leakage
 * discharges Cout. So every cycle is "overshoot, then wait for leakage",
 * and that is what makes the oscillation a sawtooth. The PI loop can make
 * the overshoot that starts each cycle smaller; it cannot shorten the wait.
 * The residual sawtooth is the signature of the missing sink, not of a
 * mistuned controller, and tuning further is wasted effort.
 *
 * Confirmed on the bench: a bleeder resistor across VREG-GND removed it.
 * That is a board change and is NOT on the PCB yet — the numbers quoted
 * elsewhere in this file assume one is clipped on.
 */
#define VREG_KI_Q16_DFL       13107U  /* 1/5 */
#define VREG_KP_Q16_DFL       8192U   /* 1/8 */
#define VREG_GAIN_Q16_MAX     65536U  /* gain = 1 */

/* Fixed-point gate accumulator: the integrator needs sub-LSB resolution.
 * With the corrected gains a single tick's correction is a fraction of a
 * DAC code, and an integer accumulator would truncate every one of them
 * to zero — the loop would simply stop. */
#define VREG_ACC_FRAC_BITS    8

/* TIM6 @ 170 MHz / 170 / 50 = 20 kHz control tick. The 50 µs period
 * sets the peak-sag floor for a load step: sag ≈ ΔI × 50 µs / Cout. */
#define VREG_TIM6_PSC         169U
#define VREG_TIM6_ARR         49U

/* Short-circuit debounce: AWD must stay active with the gate saturated
 * at DAC_GATE_MIN for this many consecutive 50 µs ticks. 100 ticks = 5 ms,
 * comfortably longer than the integrator's load-step recovery time but
 * still fast enough to act on a real short before anything overheats. */
#define VREG_SC_TICKS         100U
#define VREG_GATE_SAT_MARGIN  32U   /* dac_code <= MIN+this → saturated */

/* ---- state (shared between the TIM6 ISR and the API) ---- */
static volatile enum vreg_state vreg_state = VREG_OFF;
static volatile uint16_t vreg_v_set;          /* live setpoint, ADC counts */
static volatile uint16_t vreg_v_set_target;   /* ADC counts */
static volatile uint16_t vreg_dac_code = DAC_GATE_OFF;
static volatile int32_t  vreg_dac_acc;        /* gate code in Q8 — the integrator */
static volatile uint16_t vreg_sc_code;        /* AWD low threshold, ADC counts */
static volatile uint32_t vreg_target_mv;
static uint32_t vreg_sc_margin_mv = VREG_SC_MARGIN_DFL;
static uint16_t vreg_sc_consec;               /* ISR-only: AWD-active tick counter */
static volatile bool vreg_passthru_req;       /* target above the regulated range */

/* Excursion tracker: min/max of Vout seen by the control tick since the last
 * reset. The ISR samples at 20 kHz, which no caller can approach, so
 * this is the only way to put numbers on a load transient without a scope.
 * Only accumulated in VREG_ON, so a soft-start ramp does not poison the
 * window. */
static volatile uint16_t vreg_v_min;
static volatile uint16_t vreg_v_max;

/* ---- PI gains, bench-tunable via `vreg ki` / `vreg kp` ---- */
static volatile uint32_t vreg_ki_q16 = VREG_KI_Q16_DFL;
static volatile uint32_t vreg_kp_q16 = VREG_KP_Q16_DFL;

static uint16_t mv_to_code(uint32_t mv)
{
	uint32_t c = (mv * 4095U) / VREG_VREF_MV;

	return (c > 4095U) ? 4095U : (uint16_t)c;
}

static uint32_t code_to_mv(uint16_t code)
{
	return ((uint32_t)code * VREG_VREF_MV) / 4095U;
}

/*
 * Critical section against the control tick.
 *
 * NOT irq_lock(): the tick is a zero-latency IRQ (see vreg_tim6_init()) and
 * runs straight through irq_lock(). Masking TIM6 at the NVIC is what actually
 * keeps it out. A tick that comes due while masked stays pending and fires
 * once on release — the loop just skips one 50 µs sample, which is harmless.
 * These sections do not nest.
 */
static inline void vreg_isr_lock(void)
{
	irq_disable(TIM6_DAC_IRQn);
}

static inline void vreg_isr_unlock(void)
{
	irq_enable(TIM6_DAC_IRQn);
}

static void vreg_dac_write(uint16_t code)
{
	LL_DAC_ConvertData12RightAligned(DAC1, LL_DAC_CHANNEL_1, code);
}

/* Force the gate to `code`, keeping the Q8 integrator in step with it.
 * Every path that moves the gate outside the control law goes through
 * here, or the accumulator and the DAC drift apart. */
static void vreg_gate_park(uint16_t code)
{
	vreg_dac_code = code;
	vreg_dac_acc = (int32_t)code << VREG_ACC_FRAC_BITS;
	vreg_dac_write(code);
}

/* Latch the output off — called from the ISR on a short-circuit. */
static void vreg_enter_fault(void)
{
	vreg_gate_park(DAC_GATE_OFF);
	vreg_state = VREG_FAULT;
}

/*
 * 20 kHz control tick — a ZERO-LATENCY interrupt, see vreg_tim6_init().
 * That means it must not touch any kernel API: only LL register accesses
 * below, no logging, no k_* calls, and it returns 0 (never reschedules).
 *
 * The short-circuit check runs first so a real fault never gets an extra
 * loop iteration to wind the gate further open — but we only count it as a
 * fault when the controller is saturated (gate at MIN). A load-step sag
 * with the gate still mid-range is a transient, not a short: reset the
 * counter and let the integrator catch up.
 */
ISR_DIRECT_DECLARE(vreg_tick_isr)
{
	LL_TIM_ClearFlag_UPDATE(TIM6);

	uint16_t v = (uint16_t)LL_ADC_REG_ReadConversionData12(ADC2);

	if (vreg_state == VREG_ON || vreg_state == VREG_PASSTHRU) {
		if (v < vreg_v_min) {
			vreg_v_min = v;
		}
		if (v > vreg_v_max) {
			vreg_v_max = v;
		}

		if (LL_ADC_IsActiveFlag_AWD1(ADC2)) {
			LL_ADC_ClearFlag_AWD1(ADC2);
			if (vreg_dac_code <= DAC_GATE_MIN + VREG_GATE_SAT_MARGIN) {
				if (++vreg_sc_consec >= VREG_SC_TICKS) {
					vreg_enter_fault();
					return 0;
				}
			} else {
				vreg_sc_consec = 0;
			}
		} else {
			vreg_sc_consec = 0;
		}
	}

	if (vreg_state != VREG_SOFTSTART && vreg_state != VREG_ON) {
		/* OFF / FAULT — gate parked high. PASSTHRU — parked low, and the
		 * only thing left to do for it (the SC check) already ran above. */
		return 0;
	}

	if (vreg_state == VREG_SOFTSTART) {
		uint32_t ns = (uint32_t)vreg_v_set + VREG_SOFTSTART_STEP;

		if (ns >= vreg_v_set_target) {
			vreg_v_set = vreg_v_set_target;
			LL_ADC_ClearFlag_AWD1(ADC2);   /* drop ramp-time trips */
			vreg_sc_consec = 0;            /* arm fresh */

			if (vreg_passthru_req) {
				/* Ramp is over and there is nothing to regulate: hand
				 * the rail straight through. The PI has already walked
				 * the gate most of the way down chasing an unreachable
				 * setpoint, so parking it at MIN is not a step. */
				vreg_gate_park(DAC_GATE_MIN);
				vreg_state = VREG_PASSTHRU;
				return 0;
			}
			vreg_state = VREG_ON;          /* AWD now armed */
		} else {
			vreg_v_set = (uint16_t)ns;
		}
	}

	/*
	 * PI step, everything in Q8 DAC codes. e > 0 (Vout low) → lower the
	 * gate → Vout rises, hence both terms subtract. e is at most ±4095 and
	 * either gain at most 65536, so the products stay inside int32.
	 *
	 * The integrator is clamped on its own (anti-windup) *and* the sum is
	 * clamped again: with the P term added the output can demand a gate
	 * beyond the rails while the accumulator itself is still in range, and
	 * letting that wrap would be a spectacular way to lose the output.
	 */
	int32_t e = (int32_t)vreg_v_set - (int32_t)v;
	int32_t acc = vreg_dac_acc - ((e * (int32_t)vreg_ki_q16) >> VREG_ACC_FRAC_BITS);

	if (acc < ((int32_t)DAC_GATE_MIN << VREG_ACC_FRAC_BITS)) {
		acc = (int32_t)DAC_GATE_MIN << VREG_ACC_FRAC_BITS;
	} else if (acc > ((int32_t)DAC_GATE_OFF << VREG_ACC_FRAC_BITS)) {
		acc = (int32_t)DAC_GATE_OFF << VREG_ACC_FRAC_BITS;
	}
	vreg_dac_acc = acc;

	int32_t out = acc - ((e * (int32_t)vreg_kp_q16) >> VREG_ACC_FRAC_BITS);

	if (out < ((int32_t)DAC_GATE_MIN << VREG_ACC_FRAC_BITS)) {
		out = (int32_t)DAC_GATE_MIN << VREG_ACC_FRAC_BITS;
	} else if (out > ((int32_t)DAC_GATE_OFF << VREG_ACC_FRAC_BITS)) {
		out = (int32_t)DAC_GATE_OFF << VREG_ACC_FRAC_BITS;
	}
	vreg_dac_code = (uint16_t)(out >> VREG_ACC_FRAC_BITS);
	vreg_dac_write(vreg_dac_code);

	return 0;   /* zero-latency ISR: never request a reschedule */
}

static void vreg_dac1_init(void)
{
	LL_AHB2_GRP1_EnableClock(LL_AHB2_GRP1_PERIPH_GPIOA);
	LL_AHB2_GRP1_EnableClock(LL_AHB2_GRP1_PERIPH_DAC1);

	/* PA4 = DAC1_OUT1, analog. */
	LL_GPIO_SetPinMode(GPIOA, LL_GPIO_PIN_4, LL_GPIO_MODE_ANALOG);

	/* Buffered output to the GPIO — must drive the MOSFET gate. */
	LL_DAC_SetOutputMode(DAC1, LL_DAC_CHANNEL_1, LL_DAC_OUTPUT_MODE_NORMAL);
	LL_DAC_SetOutputBuffer(DAC1, LL_DAC_CHANNEL_1, LL_DAC_OUTPUT_BUFFER_ENABLE);
	LL_DAC_SetOutputConnection(DAC1, LL_DAC_CHANNEL_1,
				   LL_DAC_OUTPUT_CONNECT_GPIO);

	/* Park the gate high (P-MOSFET off) before enabling. */
	vreg_dac_write(DAC_GATE_OFF);
	LL_DAC_Enable(DAC1, LL_DAC_CHANNEL_1);
	k_busy_wait(LL_DAC_DELAY_STARTUP_VOLTAGE_SETTLING_US + 5);
}

static void vreg_adc2_init(void)
{
	LL_AHB2_GRP1_EnableClock(LL_AHB2_GRP1_PERIPH_GPIOB);
	LL_AHB2_GRP1_EnableClock(LL_AHB2_GRP1_PERIPH_ADC12);

	/* PB2 = ADC2_IN12, analog. */
	LL_GPIO_SetPinMode(GPIOB, LL_GPIO_PIN_2, LL_GPIO_MODE_ANALOG);

	/* The ADC12 kernel clock / common config is already set up by the
	 * ADC1 path in mdioprobe_adc_dac_init(); ADC2 just needs its own
	 * power-up + calibration. Channel 12 is single-ended by default. */
	LL_ADC_DisableDeepPowerDown(ADC2);
	LL_ADC_EnableInternalRegulator(ADC2);
	k_busy_wait(LL_ADC_DELAY_INTERNAL_REGUL_STAB_US + 10);

	LL_ADC_StartCalibration(ADC2, LL_ADC_SINGLE_ENDED);
	while (LL_ADC_IsCalibrationOnGoing(ADC2)) {
	}
	k_busy_wait(2);

	LL_ADC_Enable(ADC2);
	while (!LL_ADC_IsActiveFlag_ADRDY(ADC2)) {
	}

	LL_ADC_REG_SetSequencerLength(ADC2, LL_ADC_REG_SEQ_SCAN_DISABLE);
	LL_ADC_REG_SetSequencerRanks(ADC2, LL_ADC_REG_RANK_1,
				     LL_ADC_CHANNEL_12);
	LL_ADC_SetChannelSamplingTime(ADC2, LL_ADC_CHANNEL_12,
				      LL_ADC_SAMPLINGTIME_47CYCLES_5);
	LL_ADC_REG_SetContinuousMode(ADC2, LL_ADC_REG_CONV_CONTINUOUS);
	LL_ADC_REG_SetOverrun(ADC2, LL_ADC_REG_OVR_DATA_OVERWRITTEN);
	LL_ADC_REG_SetTriggerSource(ADC2, LL_ADC_REG_TRIG_SOFTWARE);

	/* AWD1 watches channel 12; low threshold updated per target in
	 * mdioprobe_vreg_set_target(). Start with low=0 so it never trips until
	 * a target is set. */
	LL_ADC_SetAnalogWDMonitChannels(ADC2, LL_ADC_AWD1,
					LL_ADC_AWD_CHANNEL_12_REG);
	LL_ADC_ConfigAnalogWDThresholds(ADC2, LL_ADC_AWD1, 4095U, 0U);

	LL_ADC_REG_StartConversion(ADC2);
}

static void vreg_tim6_init(void)
{
	LL_APB1_GRP1_EnableClock(LL_APB1_GRP1_PERIPH_TIM6);

	LL_TIM_SetPrescaler(TIM6, VREG_TIM6_PSC);
	LL_TIM_SetAutoReload(TIM6, VREG_TIM6_ARR);
	LL_TIM_GenerateEvent_UPDATE(TIM6);
	LL_TIM_ClearFlag_UPDATE(TIM6);
	LL_TIM_EnableIT_UPDATE(TIM6);

	/*
	 * Zero-latency, and it has to be.
	 *
	 * The bitbang MDIO master holds irq_lock() for a whole Clause-22 frame
	 * (~430 µs) so the bit timing cannot be preempted. With an ordinary ISR
	 * that starves this loop for 8-9 ticks: the rail sags unregulated, then
	 * the integrator applies all of it at once — and in the subthreshold
	 * region one DAC code is ~30 mV of output, so the correction massively
	 * overshoots. Measured 2026-07-30 on a back-to-back burst: 571..3282 mV
	 * on a 1800 mV target. Inserting a 500 µs gap between frames (10 ticks
	 * of loop time) collapsed the same burst to 51 mV p-p, which is what
	 * pinned the cause on starvation rather than on load.
	 *
	 * A zero-latency IRQ is not masked by irq_lock(), so the loop keeps its
	 * 20 kHz cadence straight through a frame. The cost is that this ISR can
	 * preempt the bit loop and stretch an MDC half-period by its own runtime
	 * (a couple of µs, ~13 % of bits at 150 kHz). Clause 22 sets no minimum
	 * MDC rate, so a stretched level is legal and every target tolerates it.
	 *
	 * Consequence for the rest of this file: irq_lock() no longer keeps the
	 * ISR out, so every critical section here masks TIM6 at the NVIC instead
	 * — see vreg_isr_lock().
	 */
	IRQ_DIRECT_CONNECT(TIM6_DAC_IRQn, 0, vreg_tick_isr, IRQ_ZERO_LATENCY);
	irq_enable(TIM6_DAC_IRQn);

	LL_TIM_EnableCounter(TIM6);
}

void mdioprobe_vreg_init(void)
{
	vreg_state = VREG_OFF;
	vreg_dac_code = DAC_GATE_OFF;
	vreg_dac_acc = (int32_t)DAC_GATE_OFF << VREG_ACC_FRAC_BITS;
	vreg_target_mv = 0;
	vreg_sc_margin_mv = VREG_SC_MARGIN_DFL;
	vreg_ki_q16 = VREG_KI_Q16_DFL;
	vreg_kp_q16 = VREG_KP_Q16_DFL;

	vreg_dac1_init();
	vreg_adc2_init();
	vreg_tim6_init();

	LOG_INF("vreg: ready (DAC1/PA4 gate, ADC2/PB2 sense, TIM6 20kHz) — OFF");
}

void mdioprobe_vreg_set_target(uint32_t target_mv)
{
	if (target_mv > VREG_MAX_MV) {
		target_mv = VREG_MAX_MV;
	}
	if (target_mv < VREG_MIN_MV) {
		target_mv = VREG_MIN_MV;
	}

	/*
	 * Above the regulated range there is nothing to regulate to but Vin, so
	 * ask for the rail itself and hand it through once the ramp is done.
	 * Anything between VREG_REG_MAX_MV and VREG_MAX_MV would silently come
	 * out at ~3.29 V, so it is folded into the pass-through rather than
	 * pretended to be a setpoint — the status reports which one it did.
	 */
	bool passthru = (target_mv > VREG_REG_MAX_MV);

	if (passthru) {
		target_mv = VREG_MAX_MV;
	}

	uint32_t sc_mv = (target_mv > vreg_sc_margin_mv)
			 ? target_mv - vreg_sc_margin_mv : 0U;

	/* Block the control tick for the multi-field update. */
	vreg_isr_lock();

	vreg_passthru_req = passthru;
	vreg_target_mv = target_mv;
	vreg_v_set_target = mv_to_code(target_mv);
	vreg_sc_code = mv_to_code(sc_mv);
	LL_ADC_ConfigAnalogWDThresholds(ADC2, LL_ADC_AWD1, 4095U, vreg_sc_code);

	/* Ramp from the current output so re-targeting while ON is bumpless;
	 * a cold start (OFF/FAULT) parks the gate off and ramps from 0. */
	uint16_t v_now = (uint16_t)LL_ADC_REG_ReadConversionData12(ADC2);

	if (vreg_state == VREG_OFF || vreg_state == VREG_FAULT ||
	    vreg_state == VREG_PASSTHRU) {
		/* PASSTHRU is included on purpose: its gate sits at MIN, so
		 * re-targeting downward from it has to start from off, or the
		 * rail would simply stay at Vin while the setpoint ramps under
		 * it and the loop would have nothing to do. */
		vreg_gate_park(DAC_GATE_OFF);
	}
	vreg_v_set = (v_now < vreg_v_set_target) ? v_now : vreg_v_set_target;
	vreg_v_min = v_now;
	vreg_v_max = v_now;

	LL_ADC_ClearFlag_AWD1(ADC2);
	vreg_state = VREG_SOFTSTART;

	vreg_isr_unlock();
}

void mdioprobe_vreg_shutdown(void)
{
	vreg_isr_lock();

	vreg_state = VREG_OFF;
	vreg_gate_park(DAC_GATE_OFF);

	vreg_isr_unlock();
}

static uint32_t gain_to_q16(uint32_t num, uint32_t den)
{
	uint32_t g = (num * 65536U) / den;

	return (g > VREG_GAIN_Q16_MAX) ? VREG_GAIN_Q16_MAX : g;
}

void mdioprobe_vreg_set_ki(uint32_t num, uint32_t den)
{
	if (den == 0U) {
		return;
	}

	vreg_isr_lock();

	vreg_ki_q16 = gain_to_q16(num, den);

	vreg_isr_unlock();
}

void mdioprobe_vreg_set_kp(uint32_t num, uint32_t den)
{
	if (den == 0U) {
		return;
	}

	vreg_isr_lock();

	vreg_kp_q16 = gain_to_q16(num, den);

	vreg_isr_unlock();
}

void mdioprobe_vreg_track_reset(void)
{
	vreg_isr_lock();

	uint16_t v = (uint16_t)LL_ADC_REG_ReadConversionData12(ADC2);

	/* Seed from the live reading rather than from ±extremes, so the window
	 * is always valid to print even if no tick has run since. */
	vreg_v_min = v;
	vreg_v_max = v;

	vreg_isr_unlock();
}

void mdioprobe_vreg_set_sc_margin(uint32_t margin_mv)
{
	vreg_sc_margin_mv = margin_mv;
	/* Re-apply against the current target so the change takes effect. */
	if (vreg_target_mv != 0U) {
		mdioprobe_vreg_set_target(vreg_target_mv);
	}
}

void mdioprobe_vreg_get_status(struct mdioprobe_vreg_status *out)
{
	vreg_isr_lock();

	out->state     = vreg_state;
	out->target_mv = vreg_target_mv;
	out->sc_trip_mv = code_to_mv(vreg_sc_code);
	out->dac_code  = vreg_dac_code;
	out->ki_q16    = vreg_ki_q16;
	out->kp_q16    = vreg_kp_q16;
	uint16_t v     = (uint16_t)LL_ADC_REG_ReadConversionData12(ADC2);
	uint16_t vmin  = vreg_v_min;
	uint16_t vmax  = vreg_v_max;

	vreg_isr_unlock();

	out->measured_mv = code_to_mv(v);
	out->min_mv = code_to_mv(vmin);
	out->max_mv = code_to_mv(vmax);
}

const char *mdioprobe_vreg_state_name(enum vreg_state s)
{
	switch (s) {
	case VREG_OFF:       return "OFF";
	case VREG_SOFTSTART: return "SOFTSTART";
	case VREG_ON:        return "ON";
	case VREG_PASSTHRU:  return "PASSTHRU";
	case VREG_FAULT:     return "FAULT";
	default:             return "?";
	}
}
