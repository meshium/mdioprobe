/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "mdioprobe_msd.h"
#include "mdioprobe_usbd.h"

#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/fs/fs.h>
#include <ff.h>
#include <string.h>
#include <stdio.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(mdioprobe_msd, CONFIG_LOG_DEFAULT_LEVEL);

#define STORAGE_NODE DT_NODELABEL(qspi_fs)

FS_FSTAB_DECLARE_ENTRY(STORAGE_NODE);

static struct fs_mount_t *const storage = &FS_FSTAB_ENTRY(STORAGE_NODE);

static bool active;

int mdioprobe_msd_enter(void)
{
	if (active) {
		return -EBUSY;
	}

	/*
	 * Unmount first, and only switch the USB profile if that worked.
	 *
	 * The order matters: handing the block device to the host while our
	 * own FatFs still has it mounted means two writers on one volume,
	 * with cached metadata on our side that the host knows nothing
	 * about. Corruption there is silent and shows up much later.
	 */
	int err = fs_unmount(storage);

	if (err && err != -EINVAL) {   /* -EINVAL: was not mounted */
		LOG_ERR("cannot release %s: %d", storage->mnt_point, err);
		return err;
	}

	err = mdioprobe_usbd_select(MDIOPROBE_USB_MSC);
	if (err) {
		/* Put the volume back rather than leaving it in limbo. */
		(void)fs_mount(storage);
		return err;
	}

	active = true;
	LOG_WRN("mass storage mode — console gone until the probe is unplugged");
	return 0;
}

bool mdioprobe_msd_active(void)
{
	return active;
}

/*
 * Lay down a fresh filesystem on the storage volume.
 *
 * Lives here rather than being left to MicroPython's `zephyr.FileSystem`
 * because that shim's mkfs() is wrong for this configuration on two counts:
 * it passes `mount->fs_data` as the format parameters, which for FatFs is
 * the FATFS object rather than a MKFS_PARM, and it passes `storage_dev`,
 * the DT disk name, where f_mkfs() wants the FatFs volume path. Called from
 * Python it fails with "error formatting Zephyr File System", which is how
 * this was found.
 *
 * The volume must already be unmounted — the caller owns that, because on
 * the MicroPython side the VFS registration has to come down first and go
 * back up afterwards. `fs_mkfs` takes the mount point past its leading
 * slash, which is exactly what Zephyr's own fatfs_mount() hands to FatFs.
 */
int mdioprobe_msd_format(void)
{
	if (active) {
		return -EBUSY;
	}

	/* Idempotent: -EINVAL simply means it was not mounted. */
	int err = fs_unmount(storage);

	if (err && err != -EINVAL) {
		LOG_ERR("cannot release %s before formatting: %d",
			storage->mnt_point, err);
		return err;
	}

	err = fs_mkfs(FS_FATFS, (uintptr_t)&storage->mnt_point[1], NULL, 0);
	if (err) {
		LOG_ERR("mkfs on %s failed: %d", storage->mnt_point, err);
	}
	return err;
}


/*
 * The label a host sees when `msd` hands the volume over. FAT keeps it in
 * 8.3 form: eleven characters, upper case, and FatFs rejects anything else.
 */
#define VOLUME_LABEL "MDIOPROBE"

int mdioprobe_msd_label(char *out, size_t len)
{
	/* FatFs wants the volume in the string, and Zephyr's mount point is
	 * "/flash:" — the same name past its leading slash. */
	const char *volume = &storage->mnt_point[1];
	char label[12] = { 0 };
	char path[16];

	if (active) {
		return -EBUSY;
	}

	FRESULT fr = f_getlabel(volume, label, NULL);

	if (fr != FR_OK) {
		LOG_WRN("cannot read the volume label: %d", fr);
		return -EIO;
	}

	if (strcmp(label, VOLUME_LABEL) != 0) {
		(void)snprintf(path, sizeof(path), "%s%s", volume, VOLUME_LABEL);
		fr = f_setlabel(path);
		if (fr != FR_OK) {
			LOG_WRN("cannot set the volume label: %d", fr);
			return -EIO;
		}
		LOG_INF("volume labelled %s", VOLUME_LABEL);
		(void)strcpy(label, VOLUME_LABEL);
	}

	if (out != NULL && len > 0) {
		(void)strncpy(out, label, len - 1);
		out[len - 1] = '\0';
	}
	return 0;
}
