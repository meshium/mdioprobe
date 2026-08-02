/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <stdbool.h>

/*
 * The USB device, built here rather than by Zephyr's cdc-acm-console
 * snippet.
 *
 * The probe presents one personality at a time: either a CDC ACM console or
 * a mass-storage drive, never both. That is not a limitation of the USB
 * stack — it is the only arrangement in which the FAT volume has exactly one
 * writer. Under MSC the host does raw block access, and there is no reliable
 * signal telling the firmware the host has mounted the volume, so the two
 * cannot be allowed to hold it at once.
 */

enum mdioprobe_usb_profile {
	MDIOPROBE_USB_CDC = 0,  /* console */
	MDIOPROBE_USB_MSC,      /* mass storage */
};

/** Build and enable the requested profile, replacing whatever is running. */
int mdioprobe_usbd_select(enum mdioprobe_usb_profile profile);

enum mdioprobe_usb_profile mdioprobe_usbd_profile(void);

/**
 * Has a host opened the console port (DTR asserted)?
 *
 * The console is CDC ACM, so anything written before the host opens it goes
 * nowhere. Whatever wants to greet the operator has to wait for this, or the
 * greeting is lost and the probe looks dead.
 */
bool mdioprobe_console_ready(void);
