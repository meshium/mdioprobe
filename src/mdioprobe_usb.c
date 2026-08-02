/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * CRS (Clock Recovery System) setup for USB FS on HSI48.
 *
 * Without a crystal, HSI48 is factory-trimmed to ~±1 % at 25 °C and drifts
 * to roughly ±3 % over the full range — USB full-speed needs ±0.25 %. The
 * CRS peripheral compares HSI48 against the 1 kHz USB SOF packets from the
 * host and continuously adjusts the oscillator's trim, which brings it
 * inside spec as soon as the device is enumerated.
 *
 * Zephyr's STM32G4 clock driver does not touch CRS (only the C0/G0/U0
 * drivers do), so this runs as a SYS_INIT right after the clock control
 * device is up.
 */

#include "mdioprobe_usb.h"

#include <zephyr/init.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <stm32g4xx_ll_bus.h>
#include <stm32g4xx_ll_crs.h>
#include <stm32g4xx_ll_rcc.h>

LOG_MODULE_REGISTER(mdioprobe_usb, CONFIG_LOG_DEFAULT_LEVEL);

static int mdioprobe_usb_crs_init(void)
{
	if (!LL_RCC_HSI48_IsReady()) {
		LOG_WRN("HSI48 not running — USB will be unreliable");
		return 0;
	}

	LL_APB1_GRP1_EnableClock(LL_APB1_GRP1_PERIPH_CRS);

	/* SOF arrives every 1 ms → 48000 HSI48 cycles per sync window. */
	LL_CRS_SetSyncSignalSource(LL_CRS_SYNC_SOURCE_USB);
	LL_CRS_SetSyncPolarity(LL_CRS_SYNC_POLARITY_RISING);
	LL_CRS_SetSyncDivider(LL_CRS_SYNC_DIV_1);
	LL_CRS_SetReloadCounter(__LL_CRS_CALC_CALCULATE_RELOADVALUE(48000000U, 1000U));
	LL_CRS_SetFreqErrorLimit(LL_CRS_ERRORLIMIT_DEFAULT);
	LL_CRS_SetHSI48SmoothTrimming(LL_CRS_HSI48CALIBRATION_DEFAULT);

	LL_CRS_EnableAutoTrimming();
	LL_CRS_EnableFreqErrorCounter();

	LOG_INF("CRS armed: HSI48 auto-trim from USB SOF");
	return 0;
}

/*
 * After the clock control driver (which starts HSI48 per the &clk_hsi48 DT
 * node) and before the USB device stack enumerates.
 */
SYS_INIT(mdioprobe_usb_crs_init, PRE_KERNEL_1, CONFIG_KERNEL_INIT_PRIORITY_DEVICE);

bool mdioprobe_usb_crs_synced(void)
{
	return LL_CRS_IsActiveFlag_SYNCOK() && !LL_CRS_IsActiveFlag_SYNCMISS();
}

uint32_t mdioprobe_usb_crs_flags(void)
{
	return CRS->ISR;
}

uint32_t mdioprobe_usb_crs_trim(void)
{
	return LL_CRS_GetHSI48SmoothTrimming();
}
