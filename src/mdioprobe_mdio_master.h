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
 * Bitbang Clause-22 MDIO master — the "generate" half of the probe.
 *
 * Hardware (see the `mdio_master` DT node):
 *   MDC_OUT  PB7  → input of an external tri-state buffer
 *   MDC_EN   PB6  → buffer output enable, active LOW
 *   MDIO out PB8  → open-drain, no pull; parked Analog while receiving
 *   MDIO in  PB14 → COMP7 (the sniffer front end) — read back through the
 *                   comparator, so the master reads correctly at whatever
 *                   logic level the target uses. PB8 and PB14 are the two
 *                   MCU pins of the one MDIO line on the connector.
 *
 * PB9 stays parked while the master is in charge: MDC is generated on PB7
 * and only ever reaches the connector through the buffer.
 *
 * The buffer is powered from the programmable translator supply, so bring
 * `vreg` up (mdioprobe_vreg_set()) before expecting MDC to reach the
 * target. Both signals are released (buffer disabled, MDIO high-Z) after
 * every transaction so the probe defaults to passive sniffing.
 *
 * MDC runs at a fixed ~150 kHz: GPIO bitbang cannot go meaningfully faster
 * on this part, and every Clause-22 target works at that rate, so there is
 * nothing worth configuring.
 */

/** Configure the pins and park the bus. Safe to call more than once. */
int mdioprobe_mdio_master_init(void);

/**
 * MDC rate in kHz. Range 10..2500; the top is the Clause-22 ceiling from
 * IEEE 802.3 §22.2.2.11 and is also the default.
 *
 * The half-period is timed off DWT's cycle counter, so the setting is honest
 * to ~6 ns rather than to the whole microsecond `k_busy_wait()` deals in.
 * Rates the bit loop cannot reach are not rejected — the spin falls through
 * and the master clocks at its floor — so the getter reports the request, not
 * the wire.
 */
int mdioprobe_mdio_master_set_clock(uint32_t khz);
uint32_t mdioprobe_mdio_master_get_clock(void);

/**
 * The rate the bit loop is really clocking at, timed on the spot against the
 * cycle counter. 0 before the master has been initialised.
 *
 * Below the request when the bit loop cannot go that fast — asking for more
 * than it can deliver is not an error, so this is how the difference shows.
 * Does not touch the bus: the MDC buffer stays disabled for the measurement.
 */
uint32_t mdioprobe_mdio_master_measure_clock(void);

/**
 * Whether a read must see the Clause-22 turnaround zero to be believed.
 *
 * On by default. Turning it off tolerates a target that returns data without
 * driving the acknowledgement — an 88E6390 Rev A0 defect (MV-S302664 §3.13)
 * whose documented remedy is exactly that. The cost is that collisions on a
 * bus with another master start looking like data. `ta` in the CLI.
 *
 * This governs belief only, never alignment: the data is read from the same
 * clocks either way, so with the check off an affected part yields the right
 * value rather than a shifted one. That was not true before 2026-08-21, when
 * the turnaround zero also decided where data began.
 */
void mdioprobe_mdio_master_set_strict_ta(bool on);
bool mdioprobe_mdio_master_get_strict_ta(void);

/**
 * DIAGNOSTIC. Send a read frame's header, then sample the line twice per
 * clock for `nbits` clocks and return both bit strings, MSB = first clock.
 *
 * `early` is sampled where read_bit() samples (end of the high half of clock
 * 47+i); `late` at the end of the low half, so late's first bit still belongs
 * to clock 46. Two samples per clock is what tells "the target answers a bit
 * early" apart from "we sample a bit late".
 */
int mdioprobe_mdio_master_probe(uint8_t prtad, uint8_t regad, uint32_t nbits,
				uint32_t *early, uint32_t *late);

/** Clause-22 read. Returns 0, -EIO if the PHY never drove the TA bit. */
int mdioprobe_mdio_master_read(uint8_t prtad, uint8_t regad, uint16_t *data);

/** Clause-22 write. Returns 0. */
int mdioprobe_mdio_master_write(uint8_t prtad, uint8_t regad, uint16_t data);

/**
 * Force the bus back to passive: MDC buffer disabled, MDIO pin Analog.
 * Done automatically at the end of each transaction — exposed for the
 * error paths.
 */
void mdioprobe_mdio_master_release(void);

/**
 * Enable/disable the external tri-state output buffer (MDC_EN, PB6).
 *
 * The buffer is shared: in MDIO mode it carries the bitbang MDC, in the
 * alternate personalities it carries USART3_TX / I2C1_SDA from PB9. That
 * makes it mdioprobe_serial.c's business too, hence the public setter.
 */
void mdioprobe_mdc_buffer_enable(bool on);
