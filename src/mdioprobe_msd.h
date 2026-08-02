/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <stddef.h>
#include <stdbool.h>

/**
 * Hand the storage volume to the host as a USB drive.
 *
 * This takes the console away: the probe cannot be a serial port and a mass
 * storage device at the same time, because the FAT volume must have exactly
 * one writer and under MSC that writer is the host. The firmware unmounts
 * the volume before the switch and gets it back when the host goes away.
 *
 * There is no command to switch back, and there cannot be one — the command
 * interface is the thing being removed. Unplug the probe.
 *
 * @return 0 on success, -EBUSY if already in mass-storage mode, or a
 *         negative errno from the filesystem or USB stack.
 */
int mdioprobe_msd_enter(void);

/** True while the host has the volume. */
bool mdioprobe_msd_active(void);

/**
 * Format the storage volume. Unmounts it first; the caller remounts.
 *
 * @return 0, or a negative errno. -EBUSY while the host owns the volume.
 */
int mdioprobe_msd_format(void);

/*
 * Make sure the volume is labelled MDIOPROBE, and report the label.
 *
 * Idempotent: it reads first and only writes when the label is wrong, so it
 * is safe to call on every boot. `out` may be NULL; when given it must have
 * room for 12 characters.
 */
int mdioprobe_msd_label(char *out, size_t len);
