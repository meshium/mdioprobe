/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Status LED. Blinking runs off the system work queue, so a state change is
 * cheap and safe to call from the capture loop, a detector thread or an API
 * function.
 */

#include "mdioprobe_led.h"

#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(mdioprobe_led, CONFIG_LOG_DEFAULT_LEVEL);

static const struct gpio_dt_spec led_g = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static const struct gpio_dt_spec led_r = GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios);

/*
 * How long an activity layer stays lit after its last holder lets go.
 *
 * Without this the master layer is invisible. A Clause-22 transaction at
 * 1500 kHz takes 84 us, and mdioprobe_c22_read() raises the layer and drops
 * it again inside that, so the red pattern was applied and replaced faster
 * than any eye or LED could follow. Capture never showed the problem because
 * there the layer is held across the whole session, seconds at a time.
 */
#define ACT_HOLD_MS 150

/* Layer inputs. Written from several contexts, resolved in one place. */
static atomic_t act_passive;  /* nesting depth of PASSIVE activity */
static atomic_t act_bus;      /* nesting depth of BUS activity */
static atomic_t act_passive_until;  /* uptime ms the hold expires at */
static atomic_t act_bus_until;
static bool connected;
static bool fault;
static bool ready;

static bool phase;

/* The pattern currently on the pins, so refresh() can tell a real change
 * from a repeat. Under a stream of transactions refresh() is called on every
 * one of them, and re-applying the same pattern would reset the phase and
 * the timer each time — the red blink would come out as a steady red, which
 * is the fault indication. */
static struct led_pattern current;
static bool have_current;

static void blink_work_handler(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(blink_work, blink_work_handler);

struct led_pattern {
	bool green_on;
	bool red_on;
	int32_t period_ms; /* 0 = static, no rescheduling */
};

/*
 * Resolved pattern, strongest layer first. Blink rates are chosen so the
 * three blinking cases are told apart at a glance: disconnected is the slow
 * one, capture is fast green, master is fast red.
 */
static bool layer_active(atomic_t *depth, atomic_t *until)
{
	if (atomic_get(depth) > 0) {
		return true;
	}

	/* Signed difference so the comparison survives the 32-bit ms wrap. */
	return (int32_t)((uint32_t)atomic_get(until) - k_uptime_get_32()) > 0;
}

static struct led_pattern resolve(void)
{
	if (fault) {
		return (struct led_pattern){ false, true, 0 };     /* red solid */
	}
	if (layer_active(&act_bus, &act_bus_until)) {
		return (struct led_pattern){ false, true, 100 };   /* red blink */
	}
	if (layer_active(&act_passive, &act_passive_until)) {
		return (struct led_pattern){ true, false, 100 };   /* green blink */
	}
	if (connected) {
		return (struct led_pattern){ true, false, 0 };     /* green solid */
	}
	return (struct led_pattern){ true, true, 900 };            /* both, slow */
}

static void apply(const struct led_pattern *p, bool lit)
{
	(void)gpio_pin_set_dt(&led_g, (lit && p->green_on) ? 1 : 0);
	(void)gpio_pin_set_dt(&led_r, (lit && p->red_on) ? 1 : 0);
}

static bool same(const struct led_pattern *a, const struct led_pattern *b)
{
	return a->green_on == b->green_on && a->red_on == b->red_on &&
	       a->period_ms == b->period_ms;
}

/* Start a pattern from the top: lit, and blinking again if it blinks. */
static void restart(const struct led_pattern *p)
{
	current = *p;
	have_current = true;
	phase = true;

	(void)k_work_cancel_delayable(&blink_work);
	apply(p, true);

	if (p->period_ms > 0) {
		k_work_reschedule(&blink_work, K_MSEC(p->period_ms / 2));
	}
}

static void refresh(void)
{
	if (!ready) {
		return;
	}

	struct led_pattern p = resolve();

	/* Nothing to do is the common case while traffic is running: the
	 * layer is already up and the timer is already going. */
	if (have_current && same(&p, &current)) {
		return;
	}
	restart(&p);
}

static void blink_work_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	struct led_pattern p = resolve();

	/* A hold can expire between ticks, and this handler is the only thing
	 * running then — nobody calls refresh() when a timer simply runs out. */
	if (!same(&p, &current)) {
		restart(&p);
		return;
	}

	phase = !phase;
	apply(&p, phase);

	if (p.period_ms > 0) {
		k_work_reschedule(&blink_work, K_MSEC(p.period_ms / 2));
	}
}

int mdioprobe_led_init(void)
{
	if (!gpio_is_ready_dt(&led_g) || !gpio_is_ready_dt(&led_r)) {
		LOG_ERR("LED GPIOs not ready");
		return -ENODEV;
	}

	int err = gpio_pin_configure_dt(&led_g, GPIO_OUTPUT_INACTIVE);

	if (err == 0) {
		err = gpio_pin_configure_dt(&led_r, GPIO_OUTPUT_INACTIVE);
	}
	if (err) {
		LOG_ERR("LED configure failed: %d", err);
		return err;
	}

	/* Park the holds in the present rather than at zero: the comparison in
	 * layer_active() is a signed difference, and a deadline of 0 read
	 * against an uptime past 2^31 ms would come out as "still holding". */
	atomic_set(&act_bus_until, (atomic_val_t)k_uptime_get_32());
	atomic_set(&act_passive_until, (atomic_val_t)k_uptime_get_32());

	ready = true;
	refresh();
	return 0;
}

void mdioprobe_led_set_connected(bool c)
{
	if (connected == c) {
		return;
	}
	connected = c;
	refresh();
}

void mdioprobe_led_set_fault(bool f)
{
	if (fault == f) {
		return;
	}
	fault = f;
	refresh();
}

void mdioprobe_led_activity_begin(enum mdioprobe_led_activity a)
{
	atomic_t *ctr = (a == MDIOPROBE_LED_BUS) ? &act_bus : &act_passive;

	if (atomic_inc(ctr) == 0) {
		refresh();  /* first holder — the resolved pattern changes */
	}
}

void mdioprobe_led_activity_end(enum mdioprobe_led_activity a)
{
	atomic_t *ctr = (a == MDIOPROBE_LED_BUS) ? &act_bus : &act_passive;
	atomic_t *until = (a == MDIOPROBE_LED_BUS) ? &act_bus_until
						   : &act_passive_until;

	/* Guard against an unmatched end(): a negative depth would latch the
	 * activity layer off for good. Checked before the hold is armed, so a
	 * stray end() cannot light the indicator either. */
	if (atomic_get(ctr) <= 0) {
		return;
	}

	/* Leave a visible tail behind, whatever the layer was raised for. */
	atomic_set(until, (atomic_val_t)(k_uptime_get_32() + ACT_HOLD_MS));

	if (atomic_dec(ctr) == 1) {
		refresh();  /* last holder released */
	}
}
