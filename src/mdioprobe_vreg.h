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
 * Programmable supply for the MDC level translator — a software-loop
 * LDO: an external P-MOSFET pass element driven by DAC1_CH1 (PA4),
 * with Vout sensed by ADC2_IN12 (PB2). A 20 kHz TIM6 ISR runs a PI
 * control loop and polls the ADC analog watchdog for short-circuit
 * cutoff. That ISR is zero-latency — the bitbang MDIO master locks
 * interrupts for a whole frame, and a starved loop loses the rail.
 * See `src/mdioprobe_vreg.c` for the design.
 */

/*
 * Output range.
 *
 * VREG_REG_MAX_MV is where *regulation* stops, and it is a property of the
 * topology, not a tuning limit: a series pass element needs volts across it
 * to have any control authority, and Vin is only 3.3 V. Measured on hardware
 * 2026-07-30 — the gate still moves the output 20 mV per DAC code at 3000 mV,
 * but at a 3300 mV target it pins at dac_code = 0 and regulation is gone.
 *
 * Above VREG_REG_MAX_MV there is nothing to regulate *to* except the input
 * rail itself, so the driver stops pretending: the gate is parked fully on
 * and the output becomes the board's own 3.3 V, minus a sub-millivolt drop
 * across Rds(on) at our few mA. That is more accurate than the loop could
 * ever be, and it is exactly what a 3.3 V target wants.
 *
 * Nothing in between is offered: asking for 3.1 V would get 3.29 V, so the
 * status reports the pass-through explicitly instead.
 */
#define VREG_MIN_MV      1800U
#define VREG_REG_MAX_MV  3000U   /* top of the regulated range */
#define VREG_MAX_MV      3300U   /* pass-through: gate full on, = Vin */

enum vreg_state {
	VREG_OFF = 0,      /* P-MOSFET held off */
	VREG_SOFTSTART,    /* setpoint ramping up, AWD not yet armed */
	VREG_ON,           /* in regulation, short-circuit watchdog armed */
	VREG_PASSTHRU,     /* gate full on, output = Vin; no regulation, SC armed */
	VREG_FAULT,        /* short-circuit latched — output off until re-armed */
};

struct mdioprobe_vreg_status {
	enum vreg_state state;
	uint32_t target_mv;    /* requested output voltage */
	uint32_t measured_mv;  /* live ADC reading of Vout */
	uint32_t sc_trip_mv;   /* absolute short-circuit trip threshold */
	uint16_t dac_code;     /* current gate DAC code (0..4095) */

	/* PI gains, see the control notes in the .c file. */
	uint32_t ki_q16;       /* integral gain × 65536 */
	uint32_t kp_q16;       /* proportional gain × 65536 */

	/* Vout excursion seen by the 20 kHz control tick since the last
	 * mdioprobe_vreg_track_reset() (or the last target change). */
	uint32_t min_mv;
	uint32_t max_mv;
};

/**
 * Initialize the programmable supply: DAC1_CH1 gate driver on PA4,
 * ADC2_IN12 sense on PB2, TIM6 10 kHz control tick. Starts in
 * VREG_OFF with the P-MOSFET held off.
 */
void mdioprobe_vreg_init(void);

/**
 * Set the target output voltage (mV, clamped to VREG_MIN_MV..MAX) and
 * begin a soft-started ramp. Also re-arms from a latched fault.
 */
void mdioprobe_vreg_set_target(uint32_t target_mv);

/** Turn the regulator off — P-MOSFET held off. */
void mdioprobe_vreg_shutdown(void);

/**
 * Set the short-circuit margin: the output is latched off if Vout
 * drops more than `margin_mv` below the target.
 */
void mdioprobe_vreg_set_sc_margin(uint32_t margin_mv);

/**
 * Set the integral and proportional gains as `num`/`den`. Both are bench
 * knobs: the plant is an integrator (current source into Cout), so the
 * loop needs the P term for phase margin, and the right values depend on
 * the board's Cout and the operating point. Defaults are KI = 1/5 (the
 * best value measured on hardware) and KP = 0 (the P path ships inert
 * until it has been tuned on a scope).
 */
void mdioprobe_vreg_set_ki(uint32_t num, uint32_t den);
void mdioprobe_vreg_set_kp(uint32_t num, uint32_t den);

/**
 * Restart the min/max excursion window at the current output level.
 *
 * The regulator samples Vout at 20 kHz; no caller gets close to that, so
 * this tracker is how a load transient gets a number attached to it without
 * a scope. Reset, apply the load, read the status.
 */
void mdioprobe_vreg_track_reset(void);

void mdioprobe_vreg_get_status(struct mdioprobe_vreg_status *out);

const char *mdioprobe_vreg_state_name(enum vreg_state s);
