/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <stdbool.h>

/*
 * Bicolour status LED: LED_G = PA9, LED_R = PB11, both active low.
 *
 * Two layers plus a fault override, re-resolved whenever anything changes.
 * Nothing outside this module has to remember to set a state — which is the
 * whole point: there is no command layer to remember it for us.
 *
 *   base      driven by the target detector
 *               connected     -> steady green
 *               disconnected  -> both blink
 *   activity  driven by the API functions themselves, overlaid for the
 *             duration of an operation
 *               PASSIVE (capture) -> green blink
 *               BUS     (master)  -> red blink
 *   fault     latched on top of both: vreg short-circuit, init failure
 *
 * Model ported from the previous project
 * (~/src/mdioprobe/src/mdioprobe_leds.h). One deliberate difference:
 * activity is reference-counted rather than a single flag. On this board a
 * capture loop and a master transaction genuinely overlap, and a plain flag
 * would let the end of the transaction switch off the indication of a
 * capture that is still running.
 */

enum mdioprobe_led_activity {
	MDIOPROBE_LED_PASSIVE = 0, /* listening: capture */
	MDIOPROBE_LED_BUS,         /* driving the wire: master */
};

int mdioprobe_led_init(void);

/** Base layer: is a target connected. */
void mdioprobe_led_set_connected(bool connected);

/** Fault layer: overrides everything until cleared. */
void mdioprobe_led_set_fault(bool fault);

/**
 * Activity layer. Calls nest; BUS outranks PASSIVE while both are held.
 * Every begin() must be matched by an end() with the same argument.
 */
void mdioprobe_led_activity_begin(enum mdioprobe_led_activity a);
void mdioprobe_led_activity_end(enum mdioprobe_led_activity a);
