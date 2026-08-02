/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <stdint.h>

/**
 * Pulse the target's reset line (PHY_RST).
 *
 * @param assert_ms how long to hold the line asserted; 0 selects the
 *                  default of 10 ms.
 * @return 0 on success, -ENODEV if the DT node has no usable GPIO,
 *         or a negative errno from the GPIO driver.
 */
int mdioprobe_reset_pulse(uint32_t assert_ms);

/**
 * Read the reset line back.
 *
 * The pin is open-drain with no pull on our side, so this reports the
 * actual level on the wire rather than what we last drove.
 *
 * @return 0 when the line is released (pull-up present on the target),
 *         1 when it still reads asserted, negative errno on failure.
 */
int mdioprobe_reset_line_state(void);
