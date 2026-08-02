/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <zephyr/device.h>

/*
 * Personalities of the target connector's two shared pins:
 *
 *   PB9 = MDC (through the tri-state buffer) / USART3_TX / I2C1_SDA
 *   PB8 = MDIO out                           / USART3_RX / I2C1_SCL
 *
 * Only one personality is live at a time. In MDIO mode the USART/I2C
 * peripherals are parked in their "sleep" pinctrl state (Analog), so they
 * cannot load the bus the sniffer is measuring — the whole point of the
 * analog front end is that nothing else touches those pins.
 */
enum mdioprobe_serial_mode {
	MDIOPROBE_SERIAL_MODE_MDIO = 0,
	MDIOPROBE_SERIAL_MODE_UART,
	MDIOPROBE_SERIAL_MODE_I2C,
};

/** Park both alternate peripherals and enter MDIO mode. */
int mdioprobe_serial_init(void);

/**
 * Switch the connector personality. Switching away from MDIO releases the
 * bitbang master first; switching back re-arms it.
 *
 * @return 0 on success, negative errno if the target peripheral is not
 *         ready or its pinctrl state could not be applied.
 */
int mdioprobe_serial_set_mode(enum mdioprobe_serial_mode mode);

enum mdioprobe_serial_mode mdioprobe_serial_get_mode(void);

const char *mdioprobe_serial_mode_name(enum mdioprobe_serial_mode mode);

/** UART device for MDIOPROBE_SERIAL_MODE_UART, NULL if unavailable. */
const struct device *mdioprobe_serial_uart_dev(void);

/** I2C device for MDIOPROBE_SERIAL_MODE_I2C, NULL if unavailable. */
const struct device *mdioprobe_serial_i2c_dev(void);
