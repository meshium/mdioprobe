/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * MDIO Clause-22 capture: custom LL-based SPI2 slave + DMA1_Channel1 in
 * CIRCULAR mode with a ring of frame slots.
 *
 * Design rationale (continuous-capture, post-race-condition fix)
 * ----------------------------------------------------------------
 * Earlier we ran DMA in NORMAL mode with a single 4-byte buffer, and
 * sniff_one disabled/re-enabled SPI between captures. That had a 2 µs
 * window per call where SPI was disabled — if the next PPU frame's EE2
 * happened to fire inside that window, SPI got re-enabled mid-frame and
 * captured "tail of frame N + head of frame N+1" (the `79 49 63 06`
 * pattern in the logs).
 *
 * The fix is to never disable SPI or DMA in steady state:
 *   - DMA1_Channel1 in CIRCULAR mode with a ring of MDIO_SLOTS × 4 bytes.
 *   - SPI2 slave armed once at boot and left running.
 *   - HRTIM CHA1 still gates NSS LOW for exactly 32 MDC pulses per frame.
 *   - Each NSS-LOW window produces one 4-byte slot; DMA auto-advances and
 *     wraps around the ring.
 *   - The consumer derives the DMA write position from CNDTR, byte by
 *     byte, and keeps its own read-slot index. (Originally HT/TC ISRs
 *     maintained a write cursor, but half-ring granularity meant a frame
 *     on a quiet bus waited for 31 more behind it — see mdio_head_bytes().)
 *
 * Buffer sizing: 64 slots × 4 bytes = 256 B. PPU at 6.27 MHz polls at
 * ~500 frames/sec → 64 slots = ~128 ms of buffering. Absorbs any
 * burstiness in the deferred Zephyr log thread.
 *
 * Reasons not to use the Zephyr SPI/DMA driver — unchanged from earlier
 * commit message: slave-mode TX-side races and RX FIFO desync on re-arm.
 */

#include "mdioprobe_mdio_api.h"
#include <zephyr/kernel.h>
#include <zephyr/irq.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/byteorder.h>
#include <string.h>

#include <stm32g4xx.h>
#include <stm32g4xx_ll_bus.h>
#include <stm32g4xx_ll_spi.h>
#include <stm32g4xx_ll_dma.h>
#include <stm32g4xx_ll_dmamux.h>

LOG_MODULE_REGISTER(mdioprobe_mdio, CONFIG_LOG_DEFAULT_LEVEL);

#define MDIO_FRAME_LEN  4U                              /* 32-bit Clause-22 frame */
#define MDIO_SLOTS      64U                             /* ring size in frames */
#define MDIO_BUF_BYTES  (MDIO_SLOTS * MDIO_FRAME_LEN)   /* 256 bytes total */

/* RM0440 Table 91: DMAMUX1 request line for SPI2_RX is 12. */
#define DMAMUX_REQ_SPI2_RX  12U

static uint8_t mdio_rx_buf[MDIO_BUF_BYTES] __aligned(4);

/* Read cursor, in slots (0..MDIO_SLOTS-1); advances in sniff_one. There is no
 * write cursor variable — see mdio_head_bytes(). */
static volatile uint16_t mdio_read_slot;
static volatile uint32_t mdio_dma_errors;
static volatile uint32_t mdio_overrun_count; /* slots overwritten before consumer caught up */
static volatile uint32_t mdio_torn_count;    /* torn-frame timeouts detected by HRTIM CMP2 IRQ */
static volatile uint32_t mdio_irq_count;     /* total CMP2 IRQs fired (sanity) */
static volatile uint16_t mdio_last_cndtr = MDIO_BUF_BYTES; /* CNDTR snapshot for diff in CMP2 IRQ */
static volatile bool mdio_raw_log = true;    /* per-frame `raw=` line; see set_raw_log() */

/*
 * Stream byte-alignment offset, detected at the first valid frame.
 *
 * DMA1_Channel1 writes a *continuous* byte stream into the circular
 * ring; it has no notion of MDIO frame boundaries. Whether ring byte 0
 * coincides with a frame's first byte depends on the SPI/FIFO state at
 * the instant SPI is enabled (stale RX-FIFO bytes, or NSS LOW catching
 * a partial frame). The result is a constant shift of 0..3 bytes for
 * the whole session: frame k occupies stream bytes
 * [4k + offset .. 4k + offset + 3], wrapping over the 256-byte ring.
 *
 * The shift must therefore be corrected by reading 4 *consecutive*
 * stream bytes — the 4th byte of an offset frame legitimately lives in
 * the next slot. A cyclic rotation *within* a fixed 4-byte slot cannot
 * do this: it would pull the previous frame's tail byte instead, which
 * yields head-correct / tail-stale frames that still pass an ST-only
 * check.
 *
 * mdio_byte_offset: 0 = aligned, 1..3 = stream shift; -1 = not yet
 * detected.
 */
static int8_t mdio_byte_offset = -1;

/*
 * Read one frame's 4 consecutive stream bytes from the circular ring,
 * starting at byte index `base + off`. MDIO_BUF_BYTES is a power of
 * two, so the wrap is a mask.
 */
static void mdio_read_frame_bytes(uint32_t base, int off,
                                  uint8_t rx[MDIO_FRAME_LEN])
{
    for (uint32_t i = 0; i < MDIO_FRAME_LEN; i++) {
        rx[i] = mdio_rx_buf[(base + (uint32_t)off + i) & (MDIO_BUF_BYTES - 1U)];
    }
}

/*
 * Where the DMA is writing right now, in bytes from the start of the ring.
 *
 * This replaces a write cursor maintained by the half-transfer/transfer-
 * complete ISRs. Those fire only every half ring — 32 frames — so a frame
 * on a quiet bus sat undelivered until 31 more arrived behind it. That was
 * invisible on the g474 bench, where the switch's PPU ran ~500 frames/s and
 * a half ring filled in ~64 ms, but it makes the probe useless for sniffing
 * a bus that is polled once a second. CNDTR gives the same information
 * byte-exactly at any instant and costs one register read.
 *
 * CNDTR counts *down* and reloads to MDIO_BUF_BYTES on wrap, so both the
 * reloaded value and a momentarily observed 0 must map to head = 0; the
 * mask does that, MDIO_BUF_BYTES being a power of two.
 */
static inline uint32_t mdio_head_bytes(void)
{
    uint32_t cndtr = (uint32_t)LL_DMA_GetDataLength(DMA1, LL_DMA_CHANNEL_1);

    return (MDIO_BUF_BYTES - cndtr) & (MDIO_BUF_BYTES - 1U);
}

/* Interpret 4 big-endian frame bytes as a Clause-22 frame. */
static void mdio_decode_frame(const uint8_t rx[MDIO_FRAME_LEN],
                              struct mdioprobe_mdio22_frame *out)
{
    uint32_t frame = sys_get_be32(rx);
    uint8_t  st    = (frame >> 30) & 0x03;

    out->op   = (frame >> 28) & 0x03;
    out->phy  = (frame >> 23) & 0x1F;
    out->reg  = (frame >> 18) & 0x1F;
    out->data = frame & 0xFFFF;
    out->err  = (st == 0x01) ? 0 : -1;
}

/*
 * Is this 4-byte window a plausible Clause-22 frame?
 *
 * Constraints applied, strongest first:
 *   ST == 01                — 2 bits
 *   OP ∈ {01, 10}           — rules out the two reserved codes
 *   TA == 10 when OP == 01  — a write's turnaround is driven by the
 *                             master and is mandatory, so it is a hard
 *                             check. For a read TA is Z followed by a
 *                             bit the *target* drives, and an unanswered
 *                             address leaves both bits high, so reads
 *                             give nothing to test.
 *
 * The TA rule is not cosmetic. Without it a window rotated across a
 * write frame's boundary passes: on the 88E6321's PPU stream
 * `5a 00 00 62` (tail of `52 5a 00 00` plus the head of the next
 * frame) reads as ST=01, OP=01 — and the detector locked offset 0 on
 * it in 5 runs out of 8, mis-decoding 62.5% of every frame that
 * followed. Its TA is 00, so the rule rejects it outright.
 *
 * Every candidate is read as 4 *consecutive* stream bytes, so a
 * passing candidate is a genuine frame — head and tail both correct.
 */
static bool mdio_window_is_c22(uint32_t base, int off)
{
    uint8_t rx[MDIO_FRAME_LEN];

    mdio_read_frame_bytes(base, off, rx);

    uint32_t frame = sys_get_be32(rx);
    uint8_t  st    = (frame >> 30) & 0x03;
    uint8_t  op    = (frame >> 28) & 0x03;
    uint8_t  ta    = (frame >> 16) & 0x03;

    if (st != 0x01) {
        return false;
    }
    if (op == 0x01) {
        return ta == 0x02;
    }
    return op == 0x02;
}

/*
 * Scan the four candidate offsets. Returns the single candidate that
 * passes, -1 if none does, or -EAGAIN-style ambiguity via `*ambiguous`
 * when more than one passes — the caller must not guess in that case,
 * it has to look at a different slot.
 */
static int mdio_detect_offset(uint32_t base, bool *ambiguous)
{
    int found = -1;

    *ambiguous = false;
    for (int cand = 0; cand < 4; cand++) {
        if (!mdio_window_is_c22(base, cand)) {
            continue;
        }
        if (found >= 0) {
            *ambiguous = true;
            return -1;
        }
        found = cand;
    }
    return found;
}

/*
 * HRTIM TIMA CMP2 ISR hook: called from `hrtim_tima_isr` in
 * `mdioprobe_timers.c` after the CMP2 interrupt flag is cleared. Runs
 * once per "EE2 cycle" — once per accepted frame start (normal or
 * torn) and additionally once per HRTIM PERxR rollover (~12 ms) when
 * idle.
 *
 * Job:
 *   1. Compute how many DMA bytes were transferred this cycle by
 *      diffing CNDTR against the last snapshot.
 *      - 0  → idle CMP2 wrap; no work.
 *      - 4  → normal 32-bit frame finished cleanly; no work.
 *      - 1..3 → torn frame; SPI dropped the partial trailing byte,
 *        DMA wrote only K bytes, so subsequent slot writes are
 *        shifted by K relative to the previously locked
 *        `mdio_byte_offset`. Bump the offset by K so future slots
 *        decode with the corrected rotation. The slot straddling the
 *        torn boundary itself is unrecoverable — its bytes mix old
 *        and new rotations and will surface to the consumer as
 *        `err = -1` (ST mismatch) which is the right signal.
 *
 * Must stay short (< IFG, ~25 µs at 6 MHz PPU) so the recovery is
 * complete before the next real frame's EE2 fires.
 */
void mdioprobe_mdio_check_frame_alignment(void)
{
    mdio_irq_count++;
    uint16_t cndtr_now = (uint16_t)LL_DMA_GetDataLength(DMA1, LL_DMA_CHANNEL_1);

    /* Wrap-safe diff: CNDTR decreases with each transfer and reloads
     * to MDIO_BUF_BYTES at end of each circular pass. */
    uint16_t diff = (mdio_last_cndtr + (uint16_t)MDIO_BUF_BYTES - cndtr_now) %
                    (uint16_t)MDIO_BUF_BYTES;
    mdio_last_cndtr = cndtr_now;

    if (diff == 0U || diff == 4U) {
        return;
    }
    if (diff >= 1U && diff <= 3U) {
        /* Apply offset shift only after the boot auto-detect has
         * locked an initial rotation; otherwise the boot path will
         * sort itself out on the first valid frame. */
        if (mdio_byte_offset >= 0) {
            mdio_byte_offset = (int8_t)((mdio_byte_offset + diff) & 0x3);
        }
        mdio_torn_count++;
    }
    /* diff > 4 should not happen at supported rates (< 256 bytes
     * between CMP2 IRQs even at the fastest PPU). If it does, leave
     * the offset alone — bumping by 5+ has no clean recovery. */
}

static void mdio_dma1_ch1_isr(const void *arg)
{
    ARG_UNUSED(arg);

    /*
     * Only transfer errors are of interest now. HT/TC used to maintain the
     * write cursor at half-ring granularity; the consumer reads CNDTR
     * instead (see mdio_head_bytes()), so their interrupts are disabled in
     * mdioprobe_mdio_init() and the flags are left alone.
     */
    if (LL_DMA_IsActiveFlag_TE1(DMA1)) {
        LL_DMA_ClearFlag_TE1(DMA1);
        mdio_dma_errors++;
    }
}

int mdioprobe_mdio_init(void)
{
    /*
     * SPI2 lives on APB1; DMA1 + DMAMUX1 on AHB1. The Zephyr SPI2
     * driver has already enabled SPI2 clock and applied pinctrl
     * (PB12/PB13/PB15 → AF5) — we just need DMA1/DMAMUX1 clocks here.
     */
    LL_AHB1_GRP1_EnableClock(LL_AHB1_GRP1_PERIPH_DMA1);
    LL_AHB1_GRP1_EnableClock(LL_AHB1_GRP1_PERIPH_DMAMUX1);
    LL_APB1_GRP1_EnableClock(LL_APB1_GRP1_PERIPH_SPI2);

    /* Override anything the Zephyr SPI driver may have set. */
    LL_SPI_Disable(SPI2);

    /*
     * Slave RX-only capture mode:
     *   - Mode 0 (CPOL=0, CPHA=0): MOSI sampled on rising SCK edge.
     *   - MSB first, 8-bit words → 4 bytes = 32 MDIO bits.
     *   - Hardware NSS input: SPI runs only while PB12 (= HRTIM CHA1
     *     via PCB trace) is LOW. HRTIM holds it LOW for exactly 32 MDC
     *     cycles per frame, so the DMA transfer length matches.
     *   - RX FIFO threshold = QUARTER (8-bit): RXNE fires on each byte.
     *   - DMA_RX enabled; TX side is left full-duplex but ignored
     *     (MISO PB14 is unwired).
     */
    LL_SPI_SetMode(SPI2,               LL_SPI_MODE_SLAVE);
    LL_SPI_SetClockPhase(SPI2,         LL_SPI_PHASE_1EDGE);
    LL_SPI_SetClockPolarity(SPI2,      LL_SPI_POLARITY_LOW);
    LL_SPI_SetTransferDirection(SPI2,  LL_SPI_FULL_DUPLEX);
    LL_SPI_SetTransferBitOrder(SPI2,   LL_SPI_MSB_FIRST);
    LL_SPI_SetDataWidth(SPI2,          LL_SPI_DATAWIDTH_8BIT);
    LL_SPI_SetNSSMode(SPI2,            LL_SPI_NSS_HARD_INPUT);
    LL_SPI_SetRxFIFOThreshold(SPI2,    LL_SPI_RX_FIFO_TH_QUARTER);
    LL_SPI_DisableCRC(SPI2);
    LL_SPI_EnableDMAReq_RX(SPI2);

    /* DMAMUX channel 0 feeds DMA1_Channel1. */
    LL_DMAMUX_SetRequestID(DMAMUX1, LL_DMAMUX_CHANNEL_0, DMAMUX_REQ_SPI2_RX);

    /*
     * DMA1_Channel1: peripheral→memory, byte, periph fixed, memory
     * increment, **CIRCULAR**, priority VERY_HIGH. After each 32-bit
     * frame the DMA advances 4 bytes; after 256 bytes (= 64 frames)
     * it wraps and overwrites the oldest slot. CNDTR resets each cycle.
     */
    memset(mdio_rx_buf, 0, MDIO_BUF_BYTES);
    LL_DMA_DisableChannel(DMA1, LL_DMA_CHANNEL_1);
    LL_DMA_ClearFlag_GI1(DMA1);

    LL_DMA_SetPeriphAddress(DMA1, LL_DMA_CHANNEL_1, (uint32_t)&SPI2->DR);
    LL_DMA_SetMemoryAddress(DMA1, LL_DMA_CHANNEL_1, (uint32_t)mdio_rx_buf);
    LL_DMA_SetDataLength(DMA1,    LL_DMA_CHANNEL_1, MDIO_BUF_BYTES);
    LL_DMA_SetDataTransferDirection(DMA1, LL_DMA_CHANNEL_1,
                                    LL_DMA_DIRECTION_PERIPH_TO_MEMORY);
    LL_DMA_SetChannelPriorityLevel(DMA1, LL_DMA_CHANNEL_1,
                                   LL_DMA_PRIORITY_VERYHIGH);
    LL_DMA_SetMode(DMA1,           LL_DMA_CHANNEL_1, LL_DMA_MODE_CIRCULAR);
    LL_DMA_SetPeriphIncMode(DMA1,  LL_DMA_CHANNEL_1, LL_DMA_PERIPH_NOINCREMENT);
    LL_DMA_SetMemoryIncMode(DMA1,  LL_DMA_CHANNEL_1, LL_DMA_MEMORY_INCREMENT);
    LL_DMA_SetPeriphSize(DMA1,     LL_DMA_CHANNEL_1, LL_DMA_PDATAALIGN_BYTE);
    LL_DMA_SetMemorySize(DMA1,     LL_DMA_CHANNEL_1, LL_DMA_MDATAALIGN_BYTE);

    /* HT/TC deliberately not enabled: the consumer derives the write
     * position from CNDTR, so these would only add an interrupt every half
     * ring for nothing. TE is the one that still matters. */
    LL_DMA_EnableIT_TE(DMA1, LL_DMA_CHANNEL_1);

    /*
     * IRQ_CONNECT is compile-time. CONFIG_DMA=n in this project, so
     * the Zephyr DMA driver is not pulled in and there is no conflict
     * for DMA1_Channel1_IRQn.
     */
    IRQ_CONNECT(DMA1_Channel1_IRQn, 1, mdio_dma1_ch1_isr, NULL, 0);
    irq_enable(DMA1_Channel1_IRQn);

    /* Start continuous capture. From here SPI and DMA run indefinitely. */
    mdio_read_slot = 0;
    mdio_dma_errors = 0;
    mdio_overrun_count = 0;
    mdio_torn_count = 0;
    mdio_last_cndtr = (uint16_t)MDIO_BUF_BYTES;

    LL_DMA_EnableChannel(DMA1, LL_DMA_CHANNEL_1);
    LL_SPI_Enable(SPI2);

    LOG_INF("MDIO continuous capture: SPI2 slave + DMA1_Ch1 CIRCULAR, %u-slot ring",
            MDIO_SLOTS);
    return 0;
}

/**
 * Skip pending unread slots — used by `cap` to start fresh without
 * draining historical frames captured while capture was paused.
 */
void mdioprobe_mdio_skip_to_latest(void)
{
    /* Set read = the slot the DMA is filling now, so the next sniff_one
     * returns only frames that arrive from here on. */
    mdio_read_slot = (uint16_t)((mdio_head_bytes() / MDIO_FRAME_LEN) %
                                MDIO_SLOTS);
}

void mdioprobe_mdio_set_raw_log(bool enable)
{
    mdio_raw_log = enable;
}

void mdioprobe_mdio_get_stats(struct mdioprobe_mdio_stats *out)
{
    out->torn_count    = mdio_torn_count;
    out->irq_count     = mdio_irq_count;
    out->dma_errors    = mdio_dma_errors;
    out->overrun_count = mdio_overrun_count;
    out->byte_offset   = mdio_byte_offset;
    out->last_cndtr    = mdio_last_cndtr;
    out->cur_cndtr     = (uint16_t)LL_DMA_GetDataLength(DMA1, LL_DMA_CHANNEL_1);
}

/*
 * Manually override the stream byte-alignment offset. Normally the
 * offset is auto-detected from the first valid frame and locked for
 * the session; this lets a caller force a specific offset, or re-arm
 * the auto-detect.
 *
 *   offset 0..3 → lock that stream offset
 *   offset < 0  → clear the lock, re-arm the first-frame auto-detect
 *
 * Returns the resulting offset (0..3), or -1 when auto-detect is
 * re-armed. mdio_byte_offset is a single byte — the write is atomic
 * against the consumer in sniff_one; worst case is one frame decoded
 * with the previous offset.
 */
int mdioprobe_mdio_set_byte_offset(int offset)
{
    if (offset < 0) {
        mdio_byte_offset = -1;
        return -1;
    }
    mdio_byte_offset = (int8_t)(offset & 0x3);
    return mdio_byte_offset;
}

uint32_t mdioprobe_mdio_dump_raw(uint8_t *out, uint32_t n_bytes)
{
    if (n_bytes > MDIO_BUF_BYTES) {
        n_bytes = MDIO_BUF_BYTES;
    }

    /*
     * CNDTR counts *down* from MDIO_BUF_BYTES, so the next byte DMA will
     * write is at MDIO_BUF_BYTES - CNDTR. Walk backwards from there so the
     * caller gets the newest bytes last, in wire order.
     *
     * Deliberately unsynchronised: this is a diagnostic, the ring is
     * circular and never stops, and taking a lock would only freeze a
     * consistent-looking snapshot of a stream that is inherently moving.
     * A byte torn by a concurrent DMA write is itself informative.
     */
    uint32_t head = MDIO_BUF_BYTES -
                    (uint32_t)LL_DMA_GetDataLength(DMA1, LL_DMA_CHANNEL_1);

    for (uint32_t i = 0; i < n_bytes; i++) {
        uint32_t idx = (head + MDIO_BUF_BYTES - n_bytes + i) &
                       (MDIO_BUF_BYTES - 1U);

        out[i] = mdio_rx_buf[idx];
    }
    return n_bytes;
}

int mdioprobe_mdio_sniff_one_to(struct mdioprobe_mdio22_frame *out,
                                int timeout_ms)
{
    /*
     * Wait until all 4 stream bytes of the frame at mdio_read_slot have
     * been written, counted in BYTES rather than slots.
     *
     * Frame k lives at stream bytes [4k + off .. 4k + off + 3], so it is
     * complete once the DMA head has passed 4k + off + 4. Expressed as a
     * distance from the start of our slot, that is `off + 4` bytes. With
     * off = 0 a single frame is enough and surfaces immediately; with
     * off = 1..3 the tail genuinely lives in the next slot, so a second
     * frame must land — the DMA writes 4 bytes at a time and there is no
     * finer granularity to be had.
     *
     * While the offset is still unknown we cannot simply assume the worst
     * case of 3: that would demand 7 bytes and so make the very first
     * frame of a session wait for a companion, which is exactly the
     * sparse-bus problem this loop exists to solve. Instead try offset 0
     * as soon as its own 4 bytes are in — that is the alignment this board
     * actually comes up with — and only fall back to scanning candidates
     * 1..3 (which do need 7 bytes) if that window is not a valid frame.
     *
     * The old test compared slot indices against a write cursor that only
     * moved every half ring, which is why isolated frames never surfaced.
     */
    int waited_ms = 0;
    /* Slots discarded because the offset scan could not resolve them.
     * Bounded by the ring so a pathological stream cannot pin this
     * thread; a full lap means nothing here is decodable. */
    unsigned int skipped = 0;

    for (;;) {
        uint32_t head = mdio_head_bytes();
        uint32_t slot_base = (uint32_t)mdio_read_slot * MDIO_FRAME_LEN;
        uint32_t written = (head + MDIO_BUF_BYTES - slot_base) &
                           (MDIO_BUF_BYTES - 1U);

        /*
         * Overrun: the writer has come almost all the way round and is
         * about to overwrite the slot we are still pointing at. Reading it
         * would hand back a mix of old and new frames, so count it and
         * resync to the live edge instead.
         *
         * `overrun_count` was declared from the start but never
         * incremented; this is the first code to touch it.
         *
         * Known limit: this catches the *approach* to a lap, not a
         * completed one. `written` is modulo the ring, so a consumer
         * blocked for a whole ring period (64 frames — 26 ms at the
         * bitbang master's rate, ~128 ms at PPU rates) comes back to a
         * small `written` and cannot tell it was lapped. Detecting that
         * needs a monotonic stream position, i.e. a wrap counter, and a
         * wrap counter has to handle the race between CNDTR reloading and
         * the TC interrupt that would increment it. Not worth it until
         * something actually laps: a 200-frame burst on 2026-07-30 never
         * tripped even this check.
         */
        if (written > MDIO_BUF_BYTES - 2U * MDIO_FRAME_LEN) {
            mdio_overrun_count++;
            mdio_read_slot = (uint16_t)((head / MDIO_FRAME_LEN) % MDIO_SLOTS);
            continue;
        }

        if (mdio_byte_offset >= 0) {
            if (written >= (uint32_t)mdio_byte_offset + MDIO_FRAME_LEN) {
                break;
            }
        } else if (written >= 3U + MDIO_FRAME_LEN) {
            /*
             * Seven bytes present: all four candidates are testable, so
             * do the full scan and demand a unique answer. If two
             * candidates pass, the window straddles a frame boundary in
             * a way this slot cannot resolve — drop the slot and look at
             * the next one rather than lock a coin flip for the whole
             * session. The `written` figure shrinks by a frame on each
             * skip, so this cannot spin.
             */
            bool ambiguous;
            int detected = mdio_detect_offset(slot_base, &ambiguous);

            if (detected >= 0) {
                mdio_byte_offset = (int8_t)detected;
                LOG_INF("DMA byte alignment auto-detected: offset=%d",
                        detected);
                break;
            }
            if ((ambiguous || written >= 2U * MDIO_FRAME_LEN) &&
                ++skipped < MDIO_SLOTS) {
                mdio_read_slot = (uint16_t)((mdio_read_slot + 1U) %
                                            MDIO_SLOTS);
                continue;
            }
            /* Nothing valid and no spare slot to move to: hand this one
             * up and let the decoder's ST check report it. */
            break;
        } else if (written >= MDIO_FRAME_LEN &&
                   mdio_window_is_c22(slot_base, 0)) {
            /*
             * Only the slot's own four bytes are in. Offset 0 is the
             * one candidate testable from them, and it is the alignment
             * the DMA comes up with — accepting it here is what lets a
             * single frame on a sparse bus surface without waiting for
             * a companion frame to arrive behind it.
             */
            mdio_byte_offset = 0;
            LOG_INF("DMA byte alignment auto-detected: offset=0");
            break;
        }
        if (waited_ms >= timeout_ms) {
            return -ETIMEDOUT;
        }
        k_sleep(K_MSEC(2));
        waited_ms += 2;
    }

    uint32_t base = (uint32_t)mdio_read_slot * MDIO_FRAME_LEN;
    mdio_read_slot = (uint16_t)((mdio_read_slot + 1U) % MDIO_SLOTS);

    /* The offset was locked by the wait loop above, which is the only
     * place that knows how many bytes are actually present. It holds for
     * the session — DMA alignment does not change once boot is over — and
     * mdioprobe_mdio_set_byte_offset() can re-arm or override it. */

    uint8_t rx[MDIO_FRAME_LEN];
    mdio_read_frame_bytes(base, (mdio_byte_offset >= 0) ? mdio_byte_offset : 0,
                          rx);
    mdio_decode_frame(rx, out);

    if (mdio_raw_log) {
        LOG_INF("raw=%02x %02x %02x %02x", rx[0], rx[1], rx[2], rx[3]);
    }
    return 0;
}
