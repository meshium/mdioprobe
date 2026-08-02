/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

/*
 * mdioprobe hardware API — the whole public surface of the instrument.
 *
 * This is the only header outside src/. Everything below it (comparators,
 * DAC thresholds, TIM3/HRTIM framing, the SPI2+DMA capture ring, the bitbang
 * master, the software LDO) is private, and deliberately so: the settings
 * those modules expose are the ones a caller should never have to reason
 * about. Thresholds, the NSS window width and the stream byte offset are all
 * configured under the hood when the function that needs them starts, and
 * their current values are readable through the diagnostics section for
 * debugging only.
 *
 * Resource conflicts are handled here rather than by the caller: the
 * connector can only have one personality at a time, the master needs the
 * translator supply at the bus rail before it can drive anything, and the
 * target detector must not sample the line while the master owns it.
 *
 * Blocking rule: capture and the master require a known bus rail. It is
 * either detected from the MDIO idle level or declared with
 * mdioprobe_bus_force_level(); until then they return -ENODEV. The rail is
 * one number for everything — it sets the comparator thresholds and the
 * translator supply, because physically it is one thing: the logic level of
 * the bus.
 */

#include <stdbool.h>
#include <stdint.h>

#include <zephyr/kernel.h>
#include <zephyr/device.h>

/* ------------------------------------------------------------------ */
/* Lifecycle                                                          */
/* ------------------------------------------------------------------ */

/**
 * Bring up the whole hardware layer in the correct order and start the
 * target detector.
 *
 * @return 0 on success, negative errno on failure (the fault indication is
 *         raised automatically).
 */
int mdioprobe_init(void);

/* ------------------------------------------------------------------ */
/* 1a. Bus and target                                                 */
/* ------------------------------------------------------------------ */

struct mdioprobe_bus_info {
	bool     target_present; /* rail classified from the measured level */
	bool     level_forced;   /* rail declared by the operator */
	int32_t  nominal_mv;     /* 1800 / 2500 / 3300, or -1 if unknown */
	int32_t  measured_mv;    /* last measured idle level, -1 if unusable */
	uint32_t samples_used;   /* samples that clustered at the rail */
	uint32_t samples_total;
};

void mdioprobe_bus_get(struct mdioprobe_bus_info *out);

/** True when the rail is known, whether detected or forced. */
bool mdioprobe_bus_ready(void);

/**
 * Declare the bus rail by hand, for a bench with no target attached.
 *
 * @param nominal_mv 1800, 2500 or 3300; 0 returns to auto-detection.
 * @return 0 on success, -EINVAL otherwise.
 */
int mdioprobe_bus_force_level(int32_t nominal_mv);

/* ------------------------------------------------------------------ */
/* 1b. Translator supply                                              */
/* ------------------------------------------------------------------ */

enum mdioprobe_vreg_state {
	MDIOPROBE_VREG_OFF = 0,
	MDIOPROBE_VREG_SOFTSTART,
	MDIOPROBE_VREG_ON,        /* regulating */
	MDIOPROBE_VREG_PASSTHRU,  /* gate full on, output = Vin */
	MDIOPROBE_VREG_FAULT,     /* short-circuit latched */
};

struct mdioprobe_vreg_info {
	enum mdioprobe_vreg_state state;
	uint32_t target_mv;
	uint32_t measured_mv;
	uint32_t min_mv;      /* excursion since the last target change */
	uint32_t max_mv;
};

/**
 * Set the translator supply.
 *
 * @param mv 1800..3000 regulated; above that the pass element is opened
 *           fully and the output follows Vin (~3.3 V).
 * @return 0 on success, -EINVAL if below the regulated minimum.
 */
int mdioprobe_vreg_set(uint32_t mv);

void mdioprobe_vreg_off(void);
void mdioprobe_vreg_get(struct mdioprobe_vreg_info *out);

/**
 * Re-arm the min/max window in struct mdioprobe_vreg_info.
 *
 * Otherwise it spans everything since the last target change, which always
 * includes the soft-start ramp — so the ramp masks whatever is being looked
 * for. Reset once the rail is settled, exercise the thing under test, read.
 */
void mdioprobe_vreg_reset_excursion(void);

/* ------------------------------------------------------------------ */
/* 2. MDC frequency, passive                                          */
/* ------------------------------------------------------------------ */

/**
 * Measure the MDC frequency by listening only — nothing is driven onto the
 * bus, so this is safe against a wire that already has a master.
 *
 * Accuracy is about three significant figures: this board has no crystal,
 * so the time base is HSI16, and at 170 MHz an MDC period at 6.25 MHz is
 * only 27 timer ticks. Measured against a scope-confirmed 6.250 MHz, the
 * systematic error is about -700 ppm and matches HSI16's own drift.
 *
 * @return 0 on success, -EAGAIN if the line stayed idle for the timeout.
 */
int mdioprobe_mdc_freq(uint32_t *out_hz, k_timeout_t timeout);

/* ------------------------------------------------------------------ */
/* 3. Passive capture                                                 */
/* ------------------------------------------------------------------ */

struct mdioprobe_frame {
	uint8_t  op;   /* 1 = write, 2 = read */
	uint8_t  phy;
	uint8_t  reg;
	uint16_t data;
	int      err;  /* 0 = clean, -1 = framing did not decode as Clause 22 */
};

/** Capture continuously until mdioprobe_capture_stop(). */
int mdioprobe_capture_start(void);

/** Capture exactly `frames` frames, then stop on its own. */
int mdioprobe_capture_start_n(uint32_t frames);

bool mdioprobe_capture_active(void);
void mdioprobe_capture_stop(void);

/**
 * Take the next captured frame.
 *
 * @return 0 when a frame is returned; -EAGAIN on timeout with the capture
 *         still running; -ENODATA when a counted capture has delivered all
 *         of its frames; -EPERM if capture is not running.
 */
int mdioprobe_capture_next(struct mdioprobe_frame *out, k_timeout_t timeout);

/* ------------------------------------------------------------------ */
/* 4. Active master, Clause 22                                        */
/* ------------------------------------------------------------------ */

int mdioprobe_c22_read(uint8_t phy, uint8_t reg, uint16_t *val);
int mdioprobe_c22_write(uint8_t phy, uint8_t reg, uint16_t val);

/**
 * MDC rate in kHz. Default 1500, range 10..2500.
 *
 * The one tuning knob this API deliberately does *not* hide, because unlike
 * the thresholds and the framing window it is not a property of this board.
 * MDIO is open-drain — a '1' is a pull-up charging the line — so the fastest
 * usable rate depends on that pull-up and on the wiring, both external. The
 * range stops at 2500 because that is the Clause-22 maximum (IEEE 802.3
 * §22.2.2.11); the default stops lower because an 88E6321 on the development
 * bench went quiet at about 1.8 MHz.
 *
 * The delivered rate is at or just below the request, never above: the spin
 * that times a half-period is quantised, and the correction lands on the next
 * step that is not too fast. Up to about 9% at the top of the range, nothing
 * at the bottom. mdioprobe_diag_get() reports both numbers.
 *
 * Before winding this down because a target answers nothing but 0xffff, check
 * that MDIO has a pull-up at all: that failure mode looks identical, and on
 * this board it is the more likely one.
 *
 * @param khz 10..2500. Returns -EINVAL outside that.
 */
int mdioprobe_c22_set_clock(uint32_t khz);
uint32_t mdioprobe_c22_get_clock(void);

/**
 * Whether a read must see the Clause-22 turnaround zero to be believed.
 *
 * On by default, and it earns its keep: with it off, a scan of a bus that
 * already had another master on it reported all 32 addresses as answering,
 * each with different garbage. The turnaround is what tells a collision from
 * an answer.
 *
 * Turn it off only for a target known to return data without driving the
 * acknowledgement. The 88E6390 Rev A0 does exactly that (release notes
 * MV-S302664 §3.13), and the note's own remedy is for the master to tolerate
 * it; with the check on, every read of such a part fails while carrying a
 * perfectly good value.
 */
void mdioprobe_c22_set_strict_ta(bool on);
bool mdioprobe_c22_get_strict_ta(void);

/* ------------------------------------------------------------------ */
/* 5. Target reset                                                    */
/* ------------------------------------------------------------------ */

/**
 * Pulse the target's reset line.
 *
 * @param assert_ms hold time; 0 selects the 10 ms default.
 */
int mdioprobe_target_reset(uint32_t assert_ms);

/**
 * Read the reset line back. It is open-drain with no pull on our side, so
 * this reports the wire, not what we drove.
 *
 * @return 0 released (the target's pull-up is present), 1 still asserted,
 *         negative errno on failure.
 */
int mdioprobe_target_reset_line(void);

/* ------------------------------------------------------------------ */
/* 6. Connector mode (experimental)                                   */
/* ------------------------------------------------------------------ */

/*
 * PB8/PB9 carry one personality at a time. UART and I2C reach the connector
 * directly at a fixed 3.3 V with no level translation, and the MDC buffer is
 * switched off in those modes so it cannot fight PB9.
 */
enum mdioprobe_connector_mode {
	MDIOPROBE_CONN_MDIO = 0,
	MDIOPROBE_CONN_UART,
	MDIOPROBE_CONN_I2C,
};

/** Switch the connector. Capture is stopped automatically when leaving MDIO. */
int mdioprobe_connector_set_mode(enum mdioprobe_connector_mode mode);

enum mdioprobe_connector_mode mdioprobe_connector_get_mode(void);
const char *mdioprobe_connector_mode_name(enum mdioprobe_connector_mode mode);

/** NULL unless the connector is in the matching mode. */
const struct device *mdioprobe_connector_uart(void);
const struct device *mdioprobe_connector_i2c(void);

/* ------------------------------------------------------------------ */
/* 10. Diagnostics — for debugging, not for normal operation          */
/* ------------------------------------------------------------------ */

struct mdioprobe_diag {
	/* Capture chain health. */
	uint32_t dma_errors;
	uint32_t overrun_count;
	uint32_t torn_count;
	int8_t   byte_offset;   /* locked stream rotation, -1 = auto-detect armed */
	uint16_t cur_cndtr;

	/* Front-end and framing configuration applied under the hood. */
	int32_t  threshold_mv;  /* comparator threshold in use */
	uint32_t nss_window;    /* NSS LOW width in MDC ticks (TIM3 ARR) */

	/* Comparator activity over a short sampling window. */
	uint32_t comp_mdc_high;
	uint32_t comp_mdc_total;
	uint32_t comp_mdio_high;
	uint32_t comp_mdio_total;

	/* Active master timing. `master_actual_khz` is timed on the spot, not
	 * computed: asking for a rate the bit loop cannot reach is allowed, and
	 * this is where the shortfall shows. */
	uint32_t master_khz;
	uint32_t master_actual_khz;

	/* USB reference trim (HSI48 has no crystal behind it). */
	bool     usb_crs_synced;
	uint32_t usb_crs_trim;
};

void mdioprobe_diag_get(struct mdioprobe_diag *out);

/**
 * Copy the raw capture ring, newest last, ignoring frame alignment and every
 * validity check — the escape hatch that tells a decode failure apart from a
 * capture failure.
 *
 * @return bytes copied, clamped to the ring size.
 */
uint32_t mdioprobe_diag_ring_dump(uint8_t *out, uint32_t n_bytes);
