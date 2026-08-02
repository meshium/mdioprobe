/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <zephyr/device.h>

struct mdioprobe_mdio22_frame {
    uint8_t op;
    uint8_t phy;
    uint8_t reg;
    uint16_t data;
    int err;
};

/**
 * Initialize MDIO capture subsystem.
 */
int mdioprobe_mdio_init(void);

/**
 * Capture a single MDIO frame.
 *
 * @param out        Pointer to frame structure.
 * @param timeout_ms how long to wait for a frame; 0 polls once and returns.
 * @return 0 on success, -ETIMEDOUT if no frame arrived in time.
 */
int mdioprobe_mdio_sniff_one_to(struct mdioprobe_mdio22_frame *out,
                                int timeout_ms);

/**
 * Skip any frames captured while capture was paused — read pointer
 * advances to current write position so the next `sniff_one` returns
 * only frames captured after this call.
 */
void mdioprobe_mdio_skip_to_latest(void);

/**
 * Enable/disable the per-frame `raw=` log line (on by default).
 *
 * At PPU rates the deferred logger cannot drain over CDC ACM and the ring
 * overruns on the console, not on the capture path — turn this off when the
 * point of the run is the capture counters rather than the frames.
 */
void mdioprobe_mdio_set_raw_log(bool enable);

struct mdioprobe_mdio_stats {
    uint32_t torn_count;    /* HRTIM CMP2 IRQ detected DMA misalignment */
    uint32_t irq_count;     /* total CMP2 IRQs fired */
    uint32_t dma_errors;    /* DMA TE flag fires */
    uint32_t overrun_count; /* ring slot overwritten before consumer */
    int8_t   byte_offset;   /* current locked rotation, -1 if not yet detected */
    uint16_t last_cndtr;    /* CNDTR snapshot from last CMP2 IRQ */
    uint16_t cur_cndtr;     /* CNDTR right now */
};

void mdioprobe_mdio_get_stats(struct mdioprobe_mdio_stats *out);

/**
 * Manually set the circular-buffer stream byte-alignment offset,
 * overriding the first-frame auto-detect.
 * @param offset 0..3 to lock a stream offset; negative to re-arm
 *               the auto-detect.
 * @return resulting offset (0..3), or -1 when auto-detect is re-armed.
 */
int mdioprobe_mdio_set_byte_offset(int offset);

/**
 * Copy the raw capture ring, newest last, ignoring frame alignment and
 * every validity check.
 *
 * This is the escape hatch for when `sniff_one` returns nothing: it shows
 * what the DMA actually wrote, so a decode failure can be told apart from
 * a capture failure. Compare against the frame you know you sent — e.g.
 * `mdio w 6 0 0x9A03` puts ST=01 OP=01 PHY=00110 REG=00000 TA=10 on the
 * wire ahead of the data, i.e. the 32-bit word 0x53029A03.
 *
 * @param out    destination, at least `n_bytes` long
 * @param n_bytes how many of the most recently written bytes to copy
 * @return number of bytes copied (clamped to the ring size)
 */
uint32_t mdioprobe_mdio_dump_raw(uint8_t *out, uint32_t n_bytes);
