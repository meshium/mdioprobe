/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "mdioprobe_reset.h"

#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(mdioprobe_reset, CONFIG_LOG_DEFAULT_LEVEL);

#define MDIOPROBE_NODE DT_PATH(mdioprobe_config)

/* Settling time after release before the line is worth reading back or the
 * target is worth talking to. */
#define RESET_BOOT_WAIT_MS  50U
#define RESET_DEFAULT_MS    10U

/*
 * PHY_RST is open-drain with no pull on our side: the pull-up comes from
 * the target's supply. That way an unpowered target is never back-powered
 * through the reset pin, and a target without a reset input can simply
 * leave the line unconnected.
 */
static const struct gpio_dt_spec target_reset =
	GPIO_DT_SPEC_GET(MDIOPROBE_NODE, target_reset_gpios);

int mdioprobe_reset_line_state(void)
{
	if (!gpio_is_ready_dt(&target_reset)) {
		return -ENODEV;
	}
	return gpio_pin_get_dt(&target_reset);
}

int mdioprobe_reset_pulse(uint32_t assert_ms)
{
	if (!gpio_is_ready_dt(&target_reset)) {
		LOG_WRN("target-reset-gpios not ready, skipping pulse");
		return -ENODEV;
	}

	if (assert_ms == 0U) {
		assert_ms = RESET_DEFAULT_MS;
	}

	int err = gpio_pin_configure_dt(&target_reset, GPIO_OUTPUT_ACTIVE);

	if (err) {
		LOG_ERR("target-reset configure failed: %d", err);
		return err;
	}

	LOG_INF("Asserting PHY_RST (%u ms)", assert_ms);
	k_sleep(K_MSEC(assert_ms));

	(void)gpio_pin_set_dt(&target_reset, 0);
	LOG_INF("Releasing PHY_RST, waiting %u ms for target boot",
		RESET_BOOT_WAIT_MS);
	k_sleep(K_MSEC(RESET_BOOT_WAIT_MS));

	/*
	 * Read the line back. The pin is open-drain, so reading IDR after
	 * releasing it reports the actual level on the wire, not what we
	 * drove — and there is no pull-up on our side by design (it must come
	 * from the target, see the comment above). If the target does not
	 * provide one, the line stays low and we hold it in reset forever,
	 * which looks exactly like "the target ignores MDIO".
	 */
	int lvl = gpio_pin_get_dt(&target_reset);

	if (lvl == 1) {
		LOG_WRN("PHY_RST reads STILL ASSERTED after release — no pull-up "
			"on the target side, or the target is holding it. The "
			"target will not respond on MDIO in this state.");
	} else if (lvl < 0) {
		LOG_WRN("PHY_RST read-back failed: %d", lvl);
	} else {
		LOG_INF("PHY_RST released, line reads high (target pull-up present)");
	}

	return 0;
}
