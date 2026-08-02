/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "mdioprobe_bus.h"

#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include "mdioprobe_adc_dac.h"
#include "mdioprobe_led.h"

LOG_MODULE_REGISTER(mdioprobe_bus, CONFIG_LOG_DEFAULT_LEVEL);

#define BUS_POLL_MS 200

/* Below this the line is not being pulled up by anything we should treat as
 * a target — a floating input on this board reads a few hundred millivolts. */
#define BUS_MIN_PRESENT_MV 1500

/* Fraction of samples that must cluster at the rail for the reading to mean
 * anything. A genuinely idle-high line clears this easily even under the
 * PPU's ~4700 frames/s, where traffic occupies roughly 5% of the time. */
#define BUS_MIN_USED_DIV 4

/*
 * Standard rails, with non-overlapping acceptance windows: ±13.7% is the
 * widest tolerance that still keeps 1.8 / 2.5 / 3.3 V apart.
 */
#define BUS_VNOM_TOL_PERMIL 137

static const int32_t bus_rails[] = { 1800, 2500, 3300 };

/* Two consecutive agreeing samples to change state, so a single burst of
 * traffic or a contact bounce does not flap the indication. */
#define BUS_STREAK 2

static struct mdioprobe_bus_status status = {
	.nominal_mv = -1,
	.measured_mv = -1,
};
static struct k_mutex lock;
static struct k_thread thread;
static K_THREAD_STACK_DEFINE(thread_stack, 1024);

static int32_t forced_mv;          /* 0 = auto */
static atomic_t inhibit_depth;     /* master owns the wire while > 0 */
static uint8_t conn_streak;
static uint8_t disc_streak;

static bool rail_matches(int32_t mv, int32_t rail)
{
	const int32_t lo = (int32_t)(((int64_t)rail * (1000 - BUS_VNOM_TOL_PERMIL) + 999) / 1000);
	const int32_t hi = (int32_t)(((int64_t)rail * (1000 + BUS_VNOM_TOL_PERMIL)) / 1000);

	return mv >= lo && mv <= hi;
}

static bool classify(int32_t mv, int32_t *out_rail)
{
	for (size_t i = 0; i < ARRAY_SIZE(bus_rails); i++) {
		if (rail_matches(mv, bus_rails[i])) {
			*out_rail = bus_rails[i];
			return true;
		}
	}
	return false;
}

static void bus_update(void)
{
	/*
	 * Skip the window where our own master is driving PB8. Sampling then
	 * measures the probe, not the target, and would read as "the target
	 * went away" for the length of every transaction.
	 */
	if (atomic_get(&inhibit_depth) > 0) {
		return;
	}

	int32_t mv = -1;
	uint32_t used = 0, total = 0;
	int err = mdioprobe_adc_read_level(&mv, &used, &total);

	uint32_t min_used = total / BUS_MIN_USED_DIV;

	if (min_used < 2U) {
		min_used = 2U;
	}

	const bool sane = (err == 0) && (mv > 0) && (used >= min_used);
	int32_t rail = -1;
	const bool classified = sane && (mv >= BUS_MIN_PRESENT_MV) &&
				classify(mv, &rail);

	k_mutex_lock(&lock, K_FOREVER);

	const bool was_present = status.target_present;

	if (classified) {
		disc_streak = 0;
		if (conn_streak < BUS_STREAK) {
			conn_streak++;
		}
	} else {
		conn_streak = 0;
		if (disc_streak < BUS_STREAK) {
			disc_streak++;
		}
	}

	bool present = was_present;

	if (!was_present && conn_streak >= BUS_STREAK) {
		present = true;
	} else if (was_present && disc_streak >= BUS_STREAK) {
		present = false;
	}

	status.target_present = present;
	status.measured_mv    = sane ? mv : -1;
	status.samples_used   = used;
	status.samples_total  = total;
	status.level_forced   = (forced_mv != 0);
	status.nominal_mv     = (forced_mv != 0) ? forced_mv
			      : (present ? rail : -1);

	const int32_t report_mv = status.nominal_mv;

	k_mutex_unlock(&lock);

	mdioprobe_led_set_connected(present || forced_mv != 0);

	if (present != was_present) {
		if (present) {
			LOG_INF("target: connected (%d mV rail, measured %d mV, "
				"%u/%u samples at rail)",
				report_mv, mv, used, total);
		} else {
			LOG_INF("target: disconnected (measured %d mV, "
				"%u/%u samples at rail)",
				mv, used, total);
		}
	}
}

static void bus_thread(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	while (1) {
		bus_update();
		k_sleep(K_MSEC(BUS_POLL_MS));
	}
}

void mdioprobe_bus_start(void)
{
	static bool started;

	if (started) {
		return;
	}
	started = true;

	k_mutex_init(&lock);

	/* Settle the state before anyone asks: two passes satisfy the streak
	 * so a target that is already attached is known by the time boot
	 * finishes, rather than 400 ms later. */
	bus_update();
	bus_update();

	(void)k_thread_create(&thread, thread_stack,
			      K_THREAD_STACK_SIZEOF(thread_stack),
			      bus_thread, NULL, NULL, NULL,
			      K_PRIO_PREEMPT(5), 0, K_NO_WAIT);
	k_thread_name_set(&thread, "mdioprobe_bus");
}

void mdioprobe_bus_get_status(struct mdioprobe_bus_status *out)
{
	if (out == NULL) {
		return;
	}
	k_mutex_lock(&lock, K_FOREVER);
	*out = status;
	k_mutex_unlock(&lock);
}

bool mdioprobe_bus_ready(void)
{
	bool ready;

	k_mutex_lock(&lock, K_FOREVER);
	ready = status.target_present || (forced_mv != 0);
	k_mutex_unlock(&lock);
	return ready;
}

int32_t mdioprobe_bus_nominal_mv(void)
{
	int32_t mv;

	k_mutex_lock(&lock, K_FOREVER);
	mv = (forced_mv != 0) ? forced_mv
	   : (status.target_present ? status.nominal_mv : -1);
	k_mutex_unlock(&lock);
	return mv;
}

int mdioprobe_bus_force_level(int32_t nominal_mv)
{
	if (nominal_mv != 0) {
		bool known = false;

		for (size_t i = 0; i < ARRAY_SIZE(bus_rails); i++) {
			if (nominal_mv == bus_rails[i]) {
				known = true;
				break;
			}
		}
		if (!known) {
			return -EINVAL;
		}
	}

	k_mutex_lock(&lock, K_FOREVER);
	forced_mv = nominal_mv;
	status.level_forced = (nominal_mv != 0);
	if (nominal_mv != 0) {
		status.nominal_mv = nominal_mv;
	}
	k_mutex_unlock(&lock);

	if (nominal_mv != 0) {
		LOG_INF("target: rail forced to %d mV — capture and master unblocked",
			nominal_mv);
		mdioprobe_led_set_connected(true);
	} else {
		LOG_INF("target: rail back to auto-detection");
	}
	return 0;
}

void mdioprobe_bus_inhibit(bool on)
{
	if (on) {
		atomic_inc(&inhibit_depth);
	} else if (atomic_get(&inhibit_depth) > 0) {
		atomic_dec(&inhibit_depth);
	}
}
