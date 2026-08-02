/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Runtime muxing of the target connector's PB8/PB9 pair between MDIO,
 * USART3 and I2C1.
 *
 * PB8 and PB9 are wired straight to the connector — they do NOT pass
 * through the MDC output buffer. Consequences:
 *   - the serial personalities are fixed 3.3 V, with no level translation
 *     (the programmable supply only feeds the MDC buffer);
 *   - I2C1_SDA is bidirectional all the way to the connector, so nothing
 *     needs to be done about the buffer's direction;
 *   - the buffer must be OFF in serial modes, otherwise it and PB9 would
 *     both drive the connector's MDC/TX/SDA line.
 *
 * Mechanism: both USART3 and I2C1 are `status = "okay"` in DT so their
 * drivers are built and initialised, but each carries a "sleep" pinctrl
 * state that parks its pins as Analog. This module applies "sleep" to the
 * personalities that are off and "default" to the one that is on. No
 * reflash needed to change the connector's role.
 */

#include "mdioprobe_serial.h"
#include "mdioprobe_mdio_master.h"

#include <zephyr/kernel.h>
#include <zephyr/drivers/pinctrl.h>
#include <zephyr/logging/log.h>
#include <stm32g4xx_ll_bus.h>
#include <stm32g4xx_ll_gpio.h>

LOG_MODULE_REGISTER(mdioprobe_serial, CONFIG_LOG_DEFAULT_LEVEL);

#define UART_NODE DT_NODELABEL(usart3)
#define I2C_NODE  DT_NODELABEL(i2c1)

/*
 * The USART3/I2C1 pinctrl configs are owned by their drivers; we only
 * borrow them. That needs CONFIG_PINCTRL_DYNAMIC=y (makes the driver's
 * config non-static) and CONFIG_PINCTRL_KEEP_SLEEP_STATE=y (keeps the
 * "sleep" state from being compiled out) — both set in prj.conf.
 */
PINCTRL_DT_DEV_CONFIG_DECLARE(UART_NODE);
PINCTRL_DT_DEV_CONFIG_DECLARE(I2C_NODE);

static struct pinctrl_dev_config *const uart_pcfg = PINCTRL_DT_DEV_CONFIG_GET(UART_NODE);
static struct pinctrl_dev_config *const i2c_pcfg = PINCTRL_DT_DEV_CONFIG_GET(I2C_NODE);

static const struct device *const uart_dev = DEVICE_DT_GET(UART_NODE);
static const struct device *const i2c_dev = DEVICE_DT_GET(I2C_NODE);

static enum mdioprobe_serial_mode current_mode = MDIOPROBE_SERIAL_MODE_MDIO;

static int park(struct pinctrl_dev_config *pcfg, const char *what)
{
	int err = pinctrl_apply_state(pcfg, PINCTRL_STATE_SLEEP);

	if (err) {
		LOG_ERR("%s: cannot park pins (sleep state): %d", what, err);
	}
	return err;
}

const char *mdioprobe_serial_mode_name(enum mdioprobe_serial_mode mode)
{
	switch (mode) {
	case MDIOPROBE_SERIAL_MODE_MDIO:
		return "mdio";
	case MDIOPROBE_SERIAL_MODE_UART:
		return "uart";
	case MDIOPROBE_SERIAL_MODE_I2C:
		return "i2c";
	default:
		return "?";
	}
}

int mdioprobe_serial_set_mode(enum mdioprobe_serial_mode mode)
{
	int err;

	/*
	 * Always start from "nothing drives PB8/PB9, buffer off". The
	 * release also parks MDC_EN inactive, which is what the serial
	 * modes need — PB9 drives the connector line directly.
	 */
	mdioprobe_mdio_master_release();
	err = park(uart_pcfg, "usart3");
	if (err) {
		return err;
	}
	err = park(i2c_pcfg, "i2c1");
	if (err) {
		return err;
	}

	switch (mode) {
	case MDIOPROBE_SERIAL_MODE_MDIO:
		/* The master owns PB7/PB8 (PB9 stays parked) and keeps the
		 * buffer disabled until it actually clocks a frame out. */
		err = mdioprobe_mdio_master_init();
		break;

	case MDIOPROBE_SERIAL_MODE_UART:
		if (!device_is_ready(uart_dev)) {
			LOG_ERR("usart3 not ready");
			return -ENODEV;
		}
		err = pinctrl_apply_state(uart_pcfg, PINCTRL_STATE_DEFAULT);
		break;

	case MDIOPROBE_SERIAL_MODE_I2C:
		if (!device_is_ready(i2c_dev)) {
			LOG_ERR("i2c1 not ready");
			return -ENODEV;
		}
		err = pinctrl_apply_state(i2c_pcfg, PINCTRL_STATE_DEFAULT);
		break;

	default:
		return -EINVAL;
	}

	if (err) {
		return err;
	}

	current_mode = mode;
	LOG_INF("connector mode -> %s", mdioprobe_serial_mode_name(mode));
	return 0;
}

enum mdioprobe_serial_mode mdioprobe_serial_get_mode(void)
{
	return current_mode;
}

int mdioprobe_serial_init(void)
{
	return mdioprobe_serial_set_mode(MDIOPROBE_SERIAL_MODE_MDIO);
}

const struct device *mdioprobe_serial_uart_dev(void)
{
	return device_is_ready(uart_dev) ? uart_dev : NULL;
}

const struct device *mdioprobe_serial_i2c_dev(void)
{
	return device_is_ready(i2c_dev) ? i2c_dev : NULL;
}
