/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "mdioprobe_usbd.h"

#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/logging/log.h>
#include <zephyr/usb/usbd.h>
#include <zephyr/usb/class/usbd_msc.h>

#include "mdioprobe_version.h"

LOG_MODULE_REGISTER(mdioprobe_usbd, CONFIG_LOG_DEFAULT_LEVEL);

/* TODO: Zephyr's test IDs — replace before shipping hardware. */
#define PROBE_USB_VID 0x2fe3
#define PROBE_USB_PID 0x0004

USBD_DEVICE_DEFINE(probe_usbd, DEVICE_DT_GET(DT_NODELABEL(zephyr_udc0)),
		   PROBE_USB_VID, PROBE_USB_PID);

USBD_DESC_LANG_DEFINE(probe_lang);
USBD_DESC_MANUFACTURER_DEFINE(probe_mfr, "Meshium");
USBD_DESC_PRODUCT_DEFINE(probe_product, "MDIO Probe v2");
IF_ENABLED(CONFIG_HWINFO, (USBD_DESC_SERIAL_NUMBER_DEFINE(probe_sn)));

USBD_DESC_CONFIG_DEFINE(probe_cfg_desc, "MDIO Probe");

/*
 * Bus-powered, 250 mA (the field is in 2 mA units). The translator supply
 * and whatever the target's pull-ups draw both come off this rail.
 *
 * The attribute argument has to be a constant expression: the macro folds it
 * into a static initialiser, and a `const` variable does not qualify in C.
 */
#define PROBE_CFG_ATTRIB 0
USBD_CONFIGURATION_DEFINE(probe_config, PROBE_CFG_ATTRIB, 125, &probe_cfg_desc);

/* The mass-storage LUN is the FAT volume's block device. Registering the MSC
 * class is what exposes it; the LUN definition itself is inert until then. */
USBD_DEFINE_MSC_LUN(probe_lun, "flash", "Meshium", "MDIO Probe", "1.00");

static enum mdioprobe_usb_profile current = MDIOPROBE_USB_CDC;
static bool built;

static int build(enum mdioprobe_usb_profile profile)
{
	int err;

	err = usbd_add_descriptor(&probe_usbd, &probe_lang);
	if (err) {
		return err;
	}
	err = usbd_add_descriptor(&probe_usbd, &probe_mfr);
	if (err) {
		return err;
	}
	err = usbd_add_descriptor(&probe_usbd, &probe_product);
	if (err) {
		return err;
	}
#if defined(CONFIG_HWINFO)
	(void)usbd_add_descriptor(&probe_usbd, &probe_sn);
#endif

	err = usbd_add_configuration(&probe_usbd, USBD_SPEED_FS, &probe_config);
	if (err) {
		return err;
	}

	const char *class_name = (profile == MDIOPROBE_USB_MSC) ? "msc_0"
								: "cdc_acm_0";

	err = usbd_register_class(&probe_usbd, class_name, USBD_SPEED_FS, 1);
	if (err) {
		LOG_ERR("cannot register %s: %d", class_name, err);
		return err;
	}

	/* A CDC ACM device has to advertise the IAD code triple, or a host
	 * will not bind the communication and data interfaces together. */
	if (profile == MDIOPROBE_USB_CDC) {
		err = usbd_device_set_code_triple(&probe_usbd, USBD_SPEED_FS,
						  USB_BCC_MISCELLANEOUS, 0x02, 0x01);
	} else {
		err = usbd_device_set_code_triple(&probe_usbd, USBD_SPEED_FS,
						  0, 0, 0);
	}
	if (err) {
		return err;
	}

	err = usbd_init(&probe_usbd);
	if (err) {
		return err;
	}
	return usbd_enable(&probe_usbd);
}

static void teardown(void)
{
	(void)usbd_disable(&probe_usbd);
	(void)usbd_shutdown(&probe_usbd);
}

int mdioprobe_usbd_select(enum mdioprobe_usb_profile profile)
{
	if (built && profile == current) {
		return 0;
	}
	if (built) {
		teardown();
		built = false;
		/* Let the host notice the disconnect before the new device
		 * appears, otherwise some hosts keep the old descriptors. */
		k_sleep(K_MSEC(300));
	}

	int err = build(profile);

	if (err) {
		LOG_ERR("USB profile %d failed: %d", (int)profile, err);
		return err;
	}
	current = profile;
	built = true;
	return 0;
}

enum mdioprobe_usb_profile mdioprobe_usbd_profile(void)
{
	return current;
}

bool mdioprobe_console_ready(void)
{
	const struct device *console = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
	uint32_t dtr = 0;

	if (!device_is_ready(console)) {
		return false;
	}
	if (uart_line_ctrl_get(console, UART_LINE_CTRL_DTR, &dtr) != 0) {
		/* No line control on this port — assume somebody is there
		 * rather than waiting forever for a signal that cannot come. */
		return true;
	}
	return dtr != 0;
}

static int probe_usbd_init(void)
{
	return mdioprobe_usbd_select(MDIOPROBE_USB_CDC);
}

/* After the CDC ACM UART device, before the console driver binds to it. */
SYS_INIT(probe_usbd_init, POST_KERNEL, CONFIG_APPLICATION_INIT_PRIORITY);
