/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * USB is already up by the time MicroPython starts.
 *
 * The `cdc-acm-console` snippet sets CONFIG_CDC_ACM_SERIAL_INITIALIZE_AT_BOOT,
 * so Zephyr builds the USB device and enables it during its own init, and the
 * CDC ACM port it creates is the console this firmware logs to. The port's
 * `mp_usbd_init()` (ports/zephyr/src/usbd.c) would build a *second*
 * usbd_context and enable that instead, which is why that file is left out of
 * the build and this stub takes its place.
 *
 * Declared `extern int mp_usbd_init(void)` in ports/zephyr/main.c and called
 * unconditionally under CONFIG_USB_DEVICE_STACK_NEXT, so it has to exist.
 */
int mp_usbd_init(void)
{
	return 0;
}
