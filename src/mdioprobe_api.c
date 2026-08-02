/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * The façade. Everything the instrument can do, with the resource conflicts
 * and the front-end tuning kept on this side of the wall.
 */

#include "mdioprobe_api.h"

#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include <stm32g4xx_ll_comp.h>

#include "mdioprobe_adc_dac.h"
#include "mdioprobe_autocal.h"
#include "mdioprobe_bus.h"
#include "mdioprobe_led.h"
#include "mdioprobe_mdio_api.h"
#include "mdioprobe_mdio_master.h"
#include "mdioprobe_reset.h"
#include "mdioprobe_serial.h"
#include "mdioprobe_timers.h"
#include "mdioprobe_usb.h"
#include "mdioprobe_vreg.h"

LOG_MODULE_REGISTER(mdioprobe_api, CONFIG_LOG_DEFAULT_LEVEL);

/*
 * Comparator threshold as a fraction of the bus rail. 30% sits comfortably
 * between a driven low and an idle high on every rail we support, and it is
 * the ratio the capture chain was characterised with.
 */
#define THRESHOLD_PERMIL 300

/* Capture state. `remaining` counts down only for a counted capture. */
static bool     capture_on;
static bool     capture_counted;
static uint32_t capture_remaining;
static struct k_mutex capture_lock;

static bool initialised;

/* ------------------------------------------------------------------ */
/* Front-end tuning — under the hood, per the API contract            */
/* ------------------------------------------------------------------ */

/*
 * Apply everything that depends on the bus rail. Called when a function that
 * touches the wire starts, so the caller never has to think about
 * thresholds; the rail is the single number all of it derives from.
 */
static int apply_bus_settings(void)
{
	int32_t rail = mdioprobe_bus_nominal_mv();

	if (rail <= 0) {
		return -ENODEV;
	}

	int32_t thr = (rail * THRESHOLD_PERMIL) / 1000;

	(void)mdioprobe_dac_set_threshold(thr);
	return 0;
}

/* ------------------------------------------------------------------ */
/* Lifecycle                                                          */
/* ------------------------------------------------------------------ */

int mdioprobe_init(void)
{
	if (initialised) {
		return 0;
	}

	k_mutex_init(&capture_lock);

	int err = mdioprobe_led_init();

	if (err) {
		return err;
	}

	/*
	 * Order matters and is the reason this lives here rather than in a
	 * caller: the ADC/DAC block backs both the threshold and the detector,
	 * the framing timers must be armed before the comparators are allowed
	 * to drive them, and the capture engine wants its DMA ring standing
	 * before any of that produces an edge.
	 */
	err = mdioprobe_adc_dac_init();
	if (err) {
		LOG_ERR("ADC/DAC init failed: %d", err);
		mdioprobe_led_set_fault(true);
		return err;
	}

	mdioprobe_tim3_count_init();
	mdioprobe_hrtim_nss_init();
	mdioprobe_comp_init();

	err = mdioprobe_mdio_init();
	if (err) {
		LOG_ERR("capture init failed: %d", err);
		mdioprobe_led_set_fault(true);
		return err;
	}

	(void)mdioprobe_serial_init();
	mdioprobe_vreg_init();

	/* The detector needs the ADC, and the indication needs the detector. */
	mdioprobe_bus_start();

	initialised = true;
	return 0;
}

/* ------------------------------------------------------------------ */
/* 1a. Bus and target                                                 */
/* ------------------------------------------------------------------ */

void mdioprobe_bus_get(struct mdioprobe_bus_info *out)
{
	struct mdioprobe_bus_status st;

	if (out == NULL) {
		return;
	}
	mdioprobe_bus_get_status(&st);

	out->target_present = st.target_present;
	out->level_forced   = st.level_forced;
	out->nominal_mv     = st.nominal_mv;
	out->measured_mv    = st.measured_mv;
	out->samples_used   = st.samples_used;
	out->samples_total  = st.samples_total;
}

/* mdioprobe_bus_ready() and mdioprobe_bus_force_level() are the detector's
 * own symbols — the names already match the public contract. */

/* ------------------------------------------------------------------ */
/* 1b. Translator supply                                              */
/* ------------------------------------------------------------------ */

int mdioprobe_vreg_set(uint32_t mv)
{
	if (mv < VREG_MIN_MV) {
		return -EINVAL;
	}
	mdioprobe_vreg_set_target(mv);
	return 0;
}

void mdioprobe_vreg_off(void)
{
	mdioprobe_vreg_shutdown();
}

void mdioprobe_vreg_reset_excursion(void)
{
	mdioprobe_vreg_track_reset();
}

void mdioprobe_vreg_get(struct mdioprobe_vreg_info *out)
{
	struct mdioprobe_vreg_status st;

	if (out == NULL) {
		return;
	}
	mdioprobe_vreg_get_status(&st);

	out->state       = (enum mdioprobe_vreg_state)st.state;
	out->target_mv   = st.target_mv;
	out->measured_mv = st.measured_mv;
	out->min_mv      = st.min_mv;
	out->max_mv      = st.max_mv;

	mdioprobe_led_set_fault(st.state == VREG_FAULT);
}

/* ------------------------------------------------------------------ */
/* 2. MDC frequency, passive                                          */
/* ------------------------------------------------------------------ */

int mdioprobe_mdc_freq(uint32_t *out_hz, k_timeout_t timeout)
{
	struct mdioprobe_mdc_stats st;

	if (out_hz == NULL) {
		return -EINVAL;
	}

	int32_t ms = (int32_t)k_ticks_to_ms_floor32((uint64_t)timeout.ticks);

	if (ms <= 0) {
		ms = 200;
	}

	/*
	 * Passive only. The measurement routine can self-trigger a frame to
	 * guarantee edges, and that is exactly what must not happen through
	 * the public API: driving MDC into a bus that already has a master is
	 * a collision, and it also poisons the reading (our ~130 kHz bitbang
	 * becomes the median and the real clock is rejected as outliers).
	 */
	int rc = mdioprobe_measure_mdc_freq_passive(64, ms, &st);

	if (rc != 0) {
		return -EAGAIN;
	}

	*out_hz = (uint32_t)(st.freq_milli_hz / 1000ULL);
	return 0;
}

/* ------------------------------------------------------------------ */
/* 3. Passive capture                                                 */
/* ------------------------------------------------------------------ */

static int capture_begin(bool counted, uint32_t frames)
{
	if (!mdioprobe_bus_ready()) {
		return -ENODEV;
	}
	if (mdioprobe_serial_get_mode() != MDIOPROBE_SERIAL_MODE_MDIO) {
		return -EBUSY;
	}
	if (counted && frames == 0U) {
		return -EINVAL;
	}

	int err = apply_bus_settings();

	if (err) {
		return err;
	}

	k_mutex_lock(&capture_lock, K_FOREVER);
	if (capture_on) {
		k_mutex_unlock(&capture_lock);
		return -EALREADY;
	}

	/*
	 * Start from the live edge, and re-arm the stream alignment with it.
	 * The byte offset is a property of what is on the wire, not of the
	 * session: it shifts whenever the traffic changes, and a stale lock
	 * decodes every frame into plausible garbage rather than failing.
	 */
	mdioprobe_mdio_skip_to_latest();
	(void)mdioprobe_mdio_set_byte_offset(-1);

	capture_counted   = counted;
	capture_remaining = counted ? frames : 0U;
	capture_on        = true;
	k_mutex_unlock(&capture_lock);

	mdioprobe_led_activity_begin(MDIOPROBE_LED_PASSIVE);
	return 0;
}

int mdioprobe_capture_start(void)
{
	return capture_begin(false, 0);
}

int mdioprobe_capture_start_n(uint32_t frames)
{
	return capture_begin(true, frames);
}

bool mdioprobe_capture_active(void)
{
	return capture_on;
}

void mdioprobe_capture_stop(void)
{
	k_mutex_lock(&capture_lock, K_FOREVER);
	bool was_on = capture_on;

	capture_on = false;
	capture_counted = false;
	capture_remaining = 0;
	k_mutex_unlock(&capture_lock);

	if (was_on) {
		mdioprobe_led_activity_end(MDIOPROBE_LED_PASSIVE);
	}
}

int mdioprobe_capture_next(struct mdioprobe_frame *out, k_timeout_t timeout)
{
	if (out == NULL) {
		return -EINVAL;
	}
	if (!capture_on) {
		return -EPERM;
	}

	int32_t ms = (int32_t)k_ticks_to_ms_floor32((uint64_t)timeout.ticks);

	if (ms < 0) {
		ms = 0;
	}

	struct mdioprobe_mdio22_frame f;
	int rc = mdioprobe_mdio_sniff_one_to(&f, ms);

	if (rc == -ETIMEDOUT) {
		return -EAGAIN;
	}
	if (rc != 0) {
		return rc;
	}

	out->op   = f.op;
	out->phy  = f.phy;
	out->reg  = f.reg;
	out->data = f.data;
	out->err  = f.err;

	bool finished = false;

	k_mutex_lock(&capture_lock, K_FOREVER);
	if (capture_counted && capture_remaining > 0U) {
		if (--capture_remaining == 0U) {
			finished = true;
		}
	}
	k_mutex_unlock(&capture_lock);

	if (finished) {
		mdioprobe_capture_stop();
	}
	return 0;
}

/* ------------------------------------------------------------------ */
/* 4. Active master                                                   */
/* ------------------------------------------------------------------ */

/*
 * Bring the translator supply to the bus rail and *wait for it to get there*.
 *
 * The waiting is the point. Requesting a voltage only starts a soft-start
 * ramp — roughly 25 ms full-scale at 8 setpoint counts per 50 µs tick — and
 * the MDC buffer is powered from that rail. Driving a frame while the ramp is
 * still climbing puts MDC out at whatever the rail has reached so far, the
 * target does not recognise it, and the read comes back -EIO. This was
 * measured, not imagined: the first c22_read() after boot failed exactly this
 * way once the old shell's boot-time vreg auto-start went away and the API
 * became the only thing that raises the rail.
 */
#define SUPPLY_SETTLE_MS 200

static int supply_at_rail(void)
{
	int32_t rail = mdioprobe_bus_nominal_mv();
	struct mdioprobe_vreg_status vs;

	if (rail <= 0) {
		return -ENODEV;
	}

	mdioprobe_vreg_get_status(&vs);
	if (vs.state == VREG_FAULT) {
		return -EIO;
	}
	if (vs.state == VREG_OFF || vs.target_mv != (uint32_t)rail) {
		mdioprobe_vreg_set_target((uint32_t)rail);
	}

	for (int waited = 0; waited < SUPPLY_SETTLE_MS; waited += 5) {
		mdioprobe_vreg_get_status(&vs);
		if (vs.state == VREG_ON || vs.state == VREG_PASSTHRU) {
			return 0;
		}
		if (vs.state == VREG_FAULT) {
			return -EIO;
		}
		k_sleep(K_MSEC(5));
	}
	LOG_WRN("translator supply did not reach %d mV in %d ms (state %s)",
		rail, SUPPLY_SETTLE_MS, mdioprobe_vreg_state_name(vs.state));
	return -ETIMEDOUT;
}

/*
 * One entry point for both directions, because the setup is identical and
 * easy to get subtly different otherwise: the rail has to be known, the
 * connector has to be ours, the translator supply has to be at the rail
 * before the buffer drives anything, and the detector has to be told to keep
 * its hands off the line for the duration.
 */
static int master_xfer(bool is_write, uint8_t phy, uint8_t reg, uint16_t *val)
{
	if (!mdioprobe_bus_ready()) {
		return -ENODEV;
	}
	if (mdioprobe_serial_get_mode() != MDIOPROBE_SERIAL_MODE_MDIO) {
		return -EBUSY;
	}

	int err = apply_bus_settings();

	if (err) {
		return err;
	}

	err = supply_at_rail();
	if (err) {
		return err;
	}

	mdioprobe_bus_inhibit(true);
	mdioprobe_led_activity_begin(MDIOPROBE_LED_BUS);

	int rc = is_write ? mdioprobe_mdio_master_write(phy, reg, *val)
			  : mdioprobe_mdio_master_read(phy, reg, val);

	mdioprobe_led_activity_end(MDIOPROBE_LED_BUS);
	mdioprobe_bus_inhibit(false);

	return rc;
}

int mdioprobe_c22_read(uint8_t phy, uint8_t reg, uint16_t *val)
{
	if (val == NULL) {
		return -EINVAL;
	}
	return master_xfer(false, phy, reg, val);
}

int mdioprobe_c22_write(uint8_t phy, uint8_t reg, uint16_t val)
{
	return master_xfer(true, phy, reg, &val);
}

int mdioprobe_c22_set_clock(uint32_t khz)
{
	return mdioprobe_mdio_master_set_clock(khz);
}

uint32_t mdioprobe_c22_get_clock(void)
{
	return mdioprobe_mdio_master_get_clock();
}

void mdioprobe_c22_set_strict_ta(bool on)
{
	mdioprobe_mdio_master_set_strict_ta(on);
}

bool mdioprobe_c22_get_strict_ta(void)
{
	return mdioprobe_mdio_master_get_strict_ta();
}

/* ------------------------------------------------------------------ */
/* 5. Target reset                                                    */
/* ------------------------------------------------------------------ */

int mdioprobe_target_reset(uint32_t assert_ms)
{
	return mdioprobe_reset_pulse(assert_ms);
}

int mdioprobe_target_reset_line(void)
{
	return mdioprobe_reset_line_state();
}

/* ------------------------------------------------------------------ */
/* 6. Connector mode                                                  */
/* ------------------------------------------------------------------ */

int mdioprobe_connector_set_mode(enum mdioprobe_connector_mode mode)
{
	/* Leaving MDIO takes PB8/PB9 away from the capture path, so stop it
	 * here rather than leaving a loop reading a wire it no longer owns. */
	if (mode != MDIOPROBE_CONN_MDIO && capture_on) {
		mdioprobe_capture_stop();
	}
	return mdioprobe_serial_set_mode((enum mdioprobe_serial_mode)mode);
}

enum mdioprobe_connector_mode mdioprobe_connector_get_mode(void)
{
	return (enum mdioprobe_connector_mode)mdioprobe_serial_get_mode();
}

const char *mdioprobe_connector_mode_name(enum mdioprobe_connector_mode mode)
{
	return mdioprobe_serial_mode_name((enum mdioprobe_serial_mode)mode);
}

const struct device *mdioprobe_connector_uart(void)
{
	return mdioprobe_serial_uart_dev();
}

const struct device *mdioprobe_connector_i2c(void)
{
	return mdioprobe_serial_i2c_dev();
}

/* ------------------------------------------------------------------ */
/* 10. Diagnostics                                                    */
/* ------------------------------------------------------------------ */

/* Comparator sampling window. Bit 30 of COMP_CxCSR is the live output, so
 * this is a duty-cycle estimate: it tells idle apart from traffic, and both
 * apart from a floating pin. */
#define COMP_SAMPLES 200
#define COMP_STEP_US 1000

void mdioprobe_diag_get(struct mdioprobe_diag *out)
{
	struct mdioprobe_mdio_stats ms;

	if (out == NULL) {
		return;
	}

	mdioprobe_mdio_get_stats(&ms);
	out->dma_errors    = ms.dma_errors;
	out->overrun_count = ms.overrun_count;
	out->torn_count    = ms.torn_count;
	out->byte_offset   = ms.byte_offset;
	out->cur_cndtr     = ms.cur_cndtr;

	int32_t rail = mdioprobe_bus_nominal_mv();

	out->threshold_mv = (rail > 0) ? (rail * THRESHOLD_PERMIL) / 1000 : -1;
	out->nss_window   = mdioprobe_tim3_get_arr();

	uint32_t c1 = 0, c7 = 0;

	for (int i = 0; i < COMP_SAMPLES; i++) {
		if (LL_COMP_ReadOutputLevel(COMP1)) {
			c1++;
		}
		if (LL_COMP_ReadOutputLevel(COMP7)) {
			c7++;
		}
		k_busy_wait(COMP_STEP_US);
	}
	out->comp_mdc_high   = c1;
	out->comp_mdc_total  = COMP_SAMPLES;
	out->comp_mdio_high  = c7;
	out->comp_mdio_total = COMP_SAMPLES;

	out->master_khz        = mdioprobe_mdio_master_get_clock();
	out->master_actual_khz = mdioprobe_mdio_master_measure_clock();

	out->usb_crs_synced = mdioprobe_usb_crs_synced();
	out->usb_crs_trim   = mdioprobe_usb_crs_trim();
}

uint32_t mdioprobe_diag_ring_dump(uint8_t *out, uint32_t n_bytes)
{
	return mdioprobe_mdio_dump_raw(out, n_bytes);
}
