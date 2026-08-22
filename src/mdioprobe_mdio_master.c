/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Bitbang Clause-22 MDIO master.
 *
 * Why not `zephyr,mdio-gpio`: that driver owns a single bidirectional MDIO
 * pin which it flips between OUTPUT and INPUT. This board splits the path —
 * PB8 drives the line, PB14 senses it through COMP7 — and gates MDC through
 * an external tri-state buffer (PB6/PB7). Neither fits the driver's model,
 * so the protocol is open-coded here.
 *
 * The generated MDC comes back to PA1/COMP1 from the buffer output, so the
 * capture chain's clock front end sees the probe's own traffic. Capturing
 * self-generated frames is not a goal, but it does mean `mdc_freq` and the
 * autocal self-trigger work without an external master.
 *
 * Reading through COMP7 rather than a digital GPIO is deliberate: the
 * comparator threshold is autocalibrated to the target's idle level, so a
 * 1.8 V target reads correctly on a 3.3 V MCU without a receive-side
 * translator. It also means COMP7 must be up — call mdioprobe_comp_init()
 * before the first transaction.
 */

#include "mdioprobe_mdio_master.h"

#include "mdioprobe_vreg.h"

#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/devicetree.h>
#include <zephyr/logging/log.h>
#include <cmsis_core.h>
#include <stm32g4xx_ll_bus.h>
#include <stm32g4xx_ll_comp.h>
#include <stm32g4xx_ll_gpio.h>

LOG_MODULE_REGISTER(mdioprobe_mdio_master, CONFIG_LOG_DEFAULT_LEVEL);

#define MASTER_NODE DT_NODELABEL(mdio_master)

/*
 * The bit-level path is open-coded in LL for timing, but the DT node stays
 * the single source of truth for the pinout — these asserts fail the build
 * if the two ever drift apart.
 */
BUILD_ASSERT(DT_SAME_NODE(DT_GPIO_CTLR(MASTER_NODE, mdc_gpios), DT_NODELABEL(gpiob)) &&
	     DT_GPIO_PIN(MASTER_NODE, mdc_gpios) == 7,
	     "mdc-gpios must be PB7");
BUILD_ASSERT(DT_SAME_NODE(DT_GPIO_CTLR(MASTER_NODE, mdc_en_gpios), DT_NODELABEL(gpiob)) &&
	     DT_GPIO_PIN(MASTER_NODE, mdc_en_gpios) == 6,
	     "mdc-en-gpios must be PB6");
BUILD_ASSERT(DT_SAME_NODE(DT_GPIO_CTLR(MASTER_NODE, mdio_out_gpios), DT_NODELABEL(gpiob)) &&
	     DT_GPIO_PIN(MASTER_NODE, mdio_out_gpios) == 8,
	     "mdio-out-gpios must be PB8");

#define PIN_MDC     LL_GPIO_PIN_7
#define PIN_MDC_EN  LL_GPIO_PIN_6
#define PIN_MDIO    LL_GPIO_PIN_8

/*
 * MDC rate.
 *
 * Three separate ceilings, and it is worth keeping them apart because only
 * one of them is ours:
 *
 *   ~4 MHz   what this bit loop can clock at, measured (see calibrate).
 *   2.5 MHz  the Clause-22 maximum, IEEE 802.3 §22.2.2.11. The knob stops
 *            here — going faster is out of spec for any target.
 *   ~1.8 MHz where an 88E6321 on this bench stopped answering. That is the
 *            target and the wiring, not the probe, and it is why the default
 *            is 1500 rather than the spec ceiling: a default that does not
 *            work on the bench it was developed on is not a default.
 *
 * The old default of 3 µs per half-period (~167 kHz) was inherited from
 * `zephyr,mdio-gpio`, whose whole timing model is `k_busy_wait(1)` per edge,
 * and it was never chosen against a measurement. What made anything faster
 * unreachable was that primitive rather than the wire: `k_busy_wait()` takes
 * whole microseconds, so 500 kHz was the fastest expressible rate. The
 * half-period is now counted in core cycles off DWT's counter — 5.9 ns per
 * tick at 170 MHz — and the floor became the bit loop itself.
 *
 * Why the knob stays, given the default is no longer arbitrary: MDIO is
 * open-drain, so every '1' — including every bit a target sends back — is a
 * pull-up charging the line, and that rise time belongs to the pull-up and
 * the wiring. The 1.8 MHz above is one bench with one lead; another target
 * will sit somewhere else. At 167 kHz there was nothing a knob could usefully
 * do, and at these rates there is.
 *
 * It is still not a fix for a *missing* pull-up. History, because the comment
 * here used to claim the opposite: a session on 2026-07-31 read nothing but
 * 0xffff from that switch and blamed rise time, on the strength of reads
 * starting to work after the clock was slowed. That was a confounded
 * experiment — the wiring was changed in the same step. Sweeping back to the
 * fast setting kept working, which disproved it. The real cause was hardware:
 * of the two v2 boards, only the one without an external pull-up on MDIO
 * failed. Slowing the clock never fixed anything.
 *
 * Requesting a rate the bit loop cannot reach is not an error: the spin
 * simply exits immediately and the master clocks at its floor. `get_clock()`
 * therefore reports what was asked for, not what is delivered — for that,
 * `measure_clock()` times the loop on the spot, and `diag` shows both.
 */
#define MDC_CLOCK_MIN_KHZ  10U
#define MDC_CLOCK_MAX_KHZ  2500U
#define MDC_CLOCK_DFL_KHZ  1500U

/* Require the turnaround zero before believing a read. See transact_frame(). */
static bool strict_ta = true;

static uint32_t mdc_clock_khz = MDC_CLOCK_DFL_KHZ;
static uint32_t mdc_half_cycles;

/*
 * Measured half-period as a function of the spin, in core cycles:
 *
 *     cycles(spin) = cal_offset + spin * cal_slope_num / cal_slope_den
 *
 * `cal_slope_num <= 0` means it has not been measured yet.
 */
static int32_t cal_offset;
static int32_t cal_slope_num;
static int32_t cal_slope_den = 1;

/*
 * How long to hold off the frame after enabling the buffer, so the vreg loop
 * (20 kHz) can absorb the load step before interrupts go off. 1 ms = 20 ticks,
 * measured to be enough with the bleeder resistor fitted; trim on a scope.
 *
 * Only paid when the supply is actually regulating — see buffer_up().
 */
#define MDC_BUFFER_SETTLE_US  1000U

/* How long the buffer stays up after the last frame — see buffer_release(). */
#define MDC_BUFFER_LINGER_MS  20

static K_MUTEX_DEFINE(master_lock);
static bool initialised;

/* Buffer state, and when it was last needed. See buffer_up()/buffer_release(). */
static bool    buffer_on;
static int64_t last_use_ms;

static void buffer_release(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(buffer_release_work, buffer_release);

/*
 * Half-period spin on DWT's cycle counter.
 *
 * Sampling CYCCNT once and comparing an unsigned difference is wrap-safe: the
 * counter is free-running 32-bit, and `now - start` stays correct across the
 * rollover for any interval far shorter than 2^32 cycles (25 s here).
 *
 * The read-modify-compare costs a handful of cycles, so asking for fewer
 * cycles than that just makes the loop fall straight through — which is the
 * behaviour we want at the top of the range.
 */
static inline void wait_half_period(void)
{
	const uint32_t start = DWT->CYCCNT;
	const uint32_t n = mdc_half_cycles;

	while ((DWT->CYCCNT - start) < n) {
		/* spin — interrupts are already locked for the frame */
	}
}

/*
 * Turn the requested rate into a spin length.
 *
 * The correction is the point. A half-period is not just the spin: it is also
 * the GPIO write that opens it, the shift-and-mask that produced the bit, and
 * the loop carrying both — all fixed cost, while the spin shrinks. At 167 kHz
 * that was a rounding error; at 2.5 MHz a half-period is 34 cycles, so
 * ignoring it delivered well under what was asked for.
 *
 * Two measured points rather than one offset, because the relationship is not
 * quite `spin + constant`. A first version measured the loop once with the
 * spin forced to zero and subtracted that: the spin loop is entered on every
 * half-period but only *taken* when the spin is non-zero, so the zero case
 * misses a couple of cycles of branch that the real case pays. It came out
 * over-corrected — 2000 kHz asked for, 2125 kHz measured on the wire. Fitting
 * a line through two non-zero spins has no such blind spot.
 *
 * The line is measured at init rather than written down here because its
 * coefficients belong to whatever the compiler emitted, and would rot the
 * first time anything nearby changed.
 */
static void apply_clock(void)
{
	const int32_t want = (int32_t)(SystemCoreClock / (2U * 1000U * mdc_clock_khz));

	if (cal_slope_num <= 0) {
		/* Never calibrated — run uncorrected rather than not at all. */
		mdc_half_cycles = (uint32_t)want;
		return;
	}

	const int32_t spin = ((want - cal_offset) * cal_slope_den) / cal_slope_num;

	mdc_half_cycles = (spin > 0) ? (uint32_t)spin : 0U;
}

/* Defined below, next to the bit loop it times. */
static void trim_clock(void);

int mdioprobe_mdio_master_set_clock(uint32_t khz)
{
	if (khz < MDC_CLOCK_MIN_KHZ || khz > MDC_CLOCK_MAX_KHZ) {
		return -EINVAL;
	}
	mdc_clock_khz = khz;
	apply_clock();
	if (initialised) {
		trim_clock();
	}
	return 0;
}

uint32_t mdioprobe_mdio_master_get_clock(void)
{
	return mdc_clock_khz;
}

void mdioprobe_mdio_master_set_strict_ta(bool on)
{
	strict_ta = on;
}

bool mdioprobe_mdio_master_get_strict_ta(void)
{
	return strict_ta;
}


/*
 * DWT is a debug unit and comes out of reset with its clock gated: CYCCNT
 * reads a constant zero until TRCENA is set, which would turn every spin into
 * an infinite loop. Enabling it here rather than at boot keeps it with the
 * only code that uses it.
 *
 * If a debugger already enabled it, this is a no-op — and CYCCNT is left
 * running rather than zeroed, because the spin only ever looks at differences
 * and resetting it would step on whoever else is watching.
 */
static void cycle_counter_enable(void)
{
	CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
	DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
}

static inline void mdc_set(int level)
{
	if (level) {
		LL_GPIO_SetOutputPin(GPIOB, PIN_MDC);
	} else {
		LL_GPIO_ResetOutputPin(GPIOB, PIN_MDC);
	}
}

/* MDIO is open-drain: '1' releases the line to the target's pull-up. */
static inline void mdio_drive(int level)
{
	if (level) {
		LL_GPIO_SetOutputPin(GPIOB, PIN_MDIO);
	} else {
		LL_GPIO_ResetOutputPin(GPIOB, PIN_MDIO);
	}
}

static inline int mdio_sense(void)
{
	/* COMP7 = PB14 vs DAC4_CH1 threshold, non-inverted output. */
	return LL_COMP_ReadOutputLevel(COMP7) ? 1 : 0;
}

static void mdio_pin_transmit(void)
{
	LL_GPIO_SetOutputPin(GPIOB, PIN_MDIO); /* released before enabling drive */
	LL_GPIO_SetPinOutputType(GPIOB, PIN_MDIO, LL_GPIO_OUTPUT_OPENDRAIN);
	LL_GPIO_SetPinPull(GPIOB, PIN_MDIO, LL_GPIO_PULL_NO);
	LL_GPIO_SetPinSpeed(GPIOB, PIN_MDIO, LL_GPIO_SPEED_FREQ_HIGH);
	LL_GPIO_SetPinMode(GPIOB, PIN_MDIO, LL_GPIO_MODE_OUTPUT);
}

/*
 * Analog mode, not INPUT: an input buffer still presents a leakage path and
 * a clamp diode to VDD, which would load a 1.8 V target's MDIO line while
 * the probe is only listening. Analog fully disconnects the pad.
 */
static void mdio_pin_highz(void)
{
	LL_GPIO_SetPinMode(GPIOB, PIN_MDIO, LL_GPIO_MODE_ANALOG);
	LL_GPIO_SetPinPull(GPIOB, PIN_MDIO, LL_GPIO_PULL_NO);
}

void mdioprobe_mdc_buffer_enable(bool on)
{
	/* MDC_EN is active LOW. */
	if (on) {
		LL_GPIO_ResetOutputPin(GPIOB, PIN_MDC_EN);
	} else {
		LL_GPIO_SetOutputPin(GPIOB, PIN_MDC_EN);
	}
}

static void write_bit(int bit)
{
	mdio_drive(bit);
	wait_half_period();
	mdc_set(1);
	wait_half_period();
	mdc_set(0);
}

/* Data is driven by the PHY on the falling edge and sampled on the rising
 * edge — read right after MDC goes high. */
static int read_bit(void)
{
	wait_half_period();
	mdc_set(1);
	wait_half_period();
	int v = mdio_sense();

	mdc_set(0);
	return v;
}

static void write_bits(uint32_t value, int nbits)
{
	for (int i = nbits - 1; i >= 0; i--) {
		write_bit((value >> i) & 1U);
	}
}

/* write_bits() takes its pattern in a uint32_t, so this is the ceiling. */
#define CAL_BITS 32

/* Roughly how long a measurement may hold interrupts off. */
#define MEASURE_WINDOW_CYCLES 17000U   /* ~100 µs at 170 MHz */

/*
 * Time the bit loop, in 1/16ths of a cycle per half-period.
 *
 * It runs the real `write_bits()`, not a stand-in, and that is the whole
 * point. A first version timed a bare `mdc_set` + spin loop and was wrong by
 * enough to matter: the loop a frame actually runs also carries the data GPIO
 * write, the shift-and-mask, and the loop itself, so anything cheaper
 * under-measures — and under-measuring is invisible until the spin has shrunk
 * to nothing and the rate silently saturates.
 *
 * The fractional units are not decoration either: at the top of the range a
 * half-period is ~34 cycles, so rounding the average to a whole cycle is a 3%
 * error in the very thing being corrected.
 *
 * Safe against a live target, and not by luck:
 *   - MDC reaches the outside world only through the tri-state buffer, and
 *     MDC_EN is parked inactive whenever this runs. That also keeps it off
 *     PA1/COMP1, since the loopback comes off the buffer's output.
 *   - MDIO is forced to Analog, which disconnects the pad — the ODR writes
 *     inside write_bit() land in a register the pin is not listening to.
 * So the sequence is timed exactly as it will run, while nothing leaves the
 * board.
 *
 * The alternating pattern is deliberate: mdio_drive() branches on the bit, so
 * all-ones or all-zeros would measure one side of that branch, and a
 * perfectly predicted one at that.
 *
 * Interrupts are locked for the same reason they are locked around a frame: a
 * systick landing inside the window would be charged to the bit loop.
 */
static uint32_t time_bit_loop_q4(uint32_t spin, int bits)
{
	const uint32_t saved = mdc_half_cycles;

	mdio_pin_highz();   /* unconditional: the ODR writes must go nowhere */
	mdc_half_cycles = spin;

	const unsigned int key = irq_lock();
	const uint32_t t0 = DWT->CYCCNT;

	write_bits(0xAAAAAAAAU, bits);

	const uint32_t elapsed = DWT->CYCCNT - t0;

	irq_unlock(key);
	mdc_half_cycles = saved;

	return (elapsed * 16U) / (2U * (uint32_t)bits);
}

/*
 * How many bits to time, so the locked window stays bounded whatever the rate.
 *
 * Two errors pull in opposite directions and this is where they are balanced.
 * Too few bits and the one-off cost of entering write_bits() is divided by too
 * few half-periods, which inflates the figure — that is not hypothetical, an
 * 8-bit measurement inflated enough to make the trim below a silent no-op.
 * Too many bits at a slow rate and interrupts are off for milliseconds.
 *
 * Bounding by time rather than by count resolves it: at 2.5 MHz a half-period
 * is 34 cycles, so the window buys the full 32 bits and the entry cost is
 * spread over 64 of them; at 10 kHz it is 8500 cycles, one bit fills the
 * window on its own, and the entry cost is 0.1% of a half-period anyway.
 */
static int measure_bits(uint32_t half_cycles)
{
	if (half_cycles == 0U) {
		return CAL_BITS;
	}

	const uint32_t bits = MEASURE_WINDOW_CYCLES / (2U * half_cycles);

	if (bits < 1U) {
		return 1;
	}
	return (bits > CAL_BITS) ? CAL_BITS : (int)bits;
}

/*
 * Fit cycles(spin) through two points.
 *
 * Both spins are non-zero and both are small, which keeps the locked window
 * short — the wider point is ~128 cycles per half-period, so the whole
 * measurement is under 50 µs. A wider lever arm would fit better and blind
 * the vreg loop for longer; this is the trade, and 120 cycles of lever is
 * already an order of magnitude more than the residual being corrected.
 */
#define CAL_SPIN_LOW   8U
#define CAL_SPIN_HIGH  128U

static void calibrate_bit_loop(void)
{
	const int32_t lo = (int32_t)time_bit_loop_q4(CAL_SPIN_LOW, CAL_BITS);
	const int32_t hi = (int32_t)time_bit_loop_q4(CAL_SPIN_HIGH, CAL_BITS);

	if (lo <= 0 || hi <= lo) {
		/* Either CYCCNT never moved or the two points make no sense.
		 * Leave the fit unset; apply_clock() then runs uncorrected,
		 * which is slower than asked for but works. */
		LOG_ERR("MDC timing calibration failed (%d, %d q4-cycles)", lo, hi);
		cal_slope_num = 0;
		return;
	}

	cal_slope_num = hi - lo;
	cal_slope_den = (int32_t)(CAL_SPIN_HIGH - CAL_SPIN_LOW);

	/* offset = lo - CAL_SPIN_LOW * slope, all still in 1/16 cycles, then
	 * back to whole cycles for apply_clock() to work in. */
	const int32_t offset_q4 =
		lo - ((int32_t)CAL_SPIN_LOW * cal_slope_num) / cal_slope_den;

	cal_offset = offset_q4 / 16;

	/* apply_clock() works in whole cycles, so scale the slope to match. */
	cal_slope_den *= 16;
}

/*
 * Close the loop on the half-period: measure, and lengthen the spin until the
 * rate is at or below what was asked for.
 *
 * The fit alone leaves a few percent, and not as a smooth error — the spin
 * ends when a polled counter passes a threshold, so the half-periods actually
 * available come in steps of however long one turn of that poll takes. Near
 * the top of the range a step is a sizeable fraction of the whole
 * half-period, and the residual jumps around: measured +3.8% at 1000 kHz,
 * -2.1% at 1500, +6.6% at 2000. Percent-level accuracy does not matter for an
 * MDC clock, but the *sign* does — 2500 kHz is a specified ceiling, not a
 * target, and quietly clocking past it is the one error worth ruling out.
 *
 * So this only ever adds cycles, and stops as soon as the measurement is no
 * longer above the request.
 *
 * The round count is not generosity. Each round adds the shortfall in whole
 * cycles, which near the top of the range is one or two — and one or two
 * cycles is smaller than the step the spin can actually resolve, so several
 * rounds can pass with the measurement not moving at all before it jumps a
 * whole step. Four rounds left 2000 kHz still 5% fast; twelve converges.
 */
#define TRIM_ROUNDS 12

static void trim_clock(void)
{
	const int32_t want = (int32_t)(SystemCoreClock / (2U * 1000U * mdc_clock_khz));

	if (cal_slope_num <= 0) {
		return;
	}

	for (int i = 0; i < TRIM_ROUNDS; i++) {
		const int32_t got_q4 = (int32_t)time_bit_loop_q4(
			mdc_half_cycles, measure_bits(mdc_half_cycles));

		if (got_q4 >= want * 16) {
			return;
		}
		/* Short by this many whole cycles, at least one. */
		const int32_t short_by = (want * 16 - got_q4 + 15) / 16;

		mdc_half_cycles += (uint32_t)short_by;
	}
}

/*
 * What the master is really clocking at, timed rather than computed.
 *
 * Worth having as its own entry point because apply_clock()'s arithmetic is
 * exactly the kind of thing that can be quietly wrong — and it already was
 * once, in both directions. It is also not something host-side timing can
 * settle: above about 1.5 MHz the difference between one setting and the next
 * is smaller than the jitter on a USB round trip, which is how the first
 * version's saturation looked real when it was not.
 *
 * Runs the same loop the calibration does, so the same argument applies:
 * buffer disabled, MDIO in Analog, nothing leaves the board.
 */
uint32_t mdioprobe_mdio_master_measure_clock(void)
{
	if (!initialised) {
		return 0U;
	}

	k_mutex_lock(&master_lock, K_FOREVER);
	const uint32_t q4 = time_bit_loop_q4(mdc_half_cycles,
					     measure_bits(mdc_half_cycles));

	k_mutex_unlock(&master_lock);

	if (q4 == 0U) {
		return 0U;
	}
	/* Rounded, not truncated: at 10 kHz a whole kHz is 10% of the reading,
	 * and truncation there reports 9 for a clock that is 0.01% low. */
	return ((SystemCoreClock / 1000U) * 16U + q4) / (2U * q4);
}

static void send_preamble(void)
{
	mdio_pin_transmit();
	for (int i = 0; i < 32; i++) {
		write_bit(1);
	}
}

/*
 * One frame, buffer already enabled. Caller holds `master_lock`.
 *
 * The bit loop must not be preempted mid-frame: a scheduling gap stretches
 * one MDC half-period and the PHY sees a malformed frame. That is the whole
 * reason for the irq_lock(), and raising the clock made it cheap — a 64-bit
 * frame is ~26 µs at 2.5 MHz where it was ~430 µs at the old 167 kHz, so the
 * lock now costs about half a systick rather than eight of them.
 * The lock is taken per frame, not per burst, so the vreg control ISR gets a
 * window between frames.
 */
static int transact_frame(bool is_write, uint8_t prtad, uint8_t regad, uint16_t *data)
{
	int rc = 0;
	unsigned int key = irq_lock();

	send_preamble();

	write_bits(0x1, 2);                    /* ST = 01 */
	write_bits(is_write ? 0x1 : 0x2, 2);   /* OP = 01 write, 10 read */
	write_bits(prtad & 0x1F, 5);
	write_bits(regad & 0x1F, 5);

	if (is_write) {
		write_bits(0x2, 2);            /* TA = 10, driven by master */
		write_bits(*data, 16);
		mdio_drive(1);
	} else {
		/*
		 * Turnaround — one clock, then the data. Counted, not hunted.
		 *
		 * Clause 22 gives TA two bit times: the STA releases MDIO for
		 * the first, the PHY drives a zero during the second, and data
		 * starts after that. Every part this probe has met answers a
		 * bit earlier than that — the acknowledgement lands in the
		 * first TA slot and data bit 15 in the second — whatever the
		 * datasheets draw. The 88E6240 Functional Specification Table
		 * 48 spells the frame out as "z0", and the silicon does not do
		 * it: a fixed two-slot skip read 0x3102 back as 0x6205, exactly
		 * (value << 1) | 1, measured 2026-07-30.
		 *
		 * Measured directly on 2026-08-21 against an 88E6390 (rev 1,
		 * identifier 0x3901) with a diagnostic that samples the line
		 * twice per clock — see mdioprobe_mdio_master_probe(). Counting
		 * MDC pulses from the start of the frame, so that clock 46 is
		 * the last address bit:
		 *
		 *   clock 47   nothing driven, line high
		 *   clock 48   data bit 15
		 *   ...
		 *   clock 63   data bit 0
		 *   clock 64   released
		 *
		 * Identical strings at 100, 500 and 2000 kHz, which rules out
		 * every propagation-delay explanation: the offset is counted,
		 * not analogue. The bit at clock 48 tracks the register
		 * contents, so it is data and not a smeared acknowledgement,
		 * and the level shifter in the path cannot be the cause. Nor is
		 * it: mdioprobe_v1, whose stock Zephyr mdio_gpio driver skips
		 * exactly one turnaround clock and never looks at the
		 * acknowledgement, reads this part flawlessly over the same
		 * harness and the same TXS0102 — six patterns including 0xAAAA
		 * and 0x8001, all exact.
		 *
		 * So the data always starts at clock 48, and the one clock
		 * before it carries the acknowledgement or nothing. That is
		 * v1's alignment, which is the one with field mileage across
		 * Marvell switches and ordinary PHYs alike.
		 *
		 * The previous version of this code hunted for the zero instead
		 * and let the hunt decide where data began. That works while
		 * the acknowledgement is there, and it is what made the 88E6240
		 * read correctly. It breaks on a part that does not drive it —
		 * the 88E6390 Rev A0 defect of MV-S302664 §3.13 — because the
		 * hunt then swallows data bit 15 as a candidate and every value
		 * comes back ((v & 0x7fff) << 1) | 1. Worse, it broke silently:
		 * with bit 15 clear the missing acknowledgement is
		 * indistinguishable from a real one, so the read returned a
		 * shifted value and no error at all.
		 *
		 * What the acknowledgement is still for is telling a device
		 * from silence, and that job is kept whole here — it just no
		 * longer decides the alignment. The strictness is worth its
		 * keep: with the check relaxed, a scan of a bus that already
		 * had another master on it reported all 32 addresses as
		 * answering, with different garbage at each. So the strict rule
		 * stays the default and the tolerance is asked for explicitly,
		 * by whoever knows they are talking to an affected part — and
		 * now that tolerance yields the right value rather than a
		 * shifted one, which is what the release note's own remedy
		 * always claimed it would.
		 *
		 * The cost, stated plainly: a part that really does follow the
		 * datasheet, with the acknowledgement at clock 48 and data from
		 * 49, would now read one bit right. Neither probe has met one.
		 */
		mdio_pin_highz();

		/* Clock 47: the acknowledgement, or nothing. Diagnostic only. */
		bool claimed = (read_bit() == 0);

		uint16_t v = 0;

		/* Clocks 48..63. */
		for (int i = 0; i < 16; i++) {
			v = (v << 1) | (uint16_t)read_bit();
		}
		*data = v;

		/*
		 * Clock 64, discarded. The target holds data bit 0 until a
		 * rising edge tells it to let go — measured, it releases just
		 * after clock 64 — so stopping at 63 would leave it driving
		 * into an idle bus, and a zero there would look exactly like a
		 * line held low. It also keeps the frame 64 clocks long, which
		 * is what Clause 22 asks for and what v1 emits.
		 */
		(void)read_bit();

		if (!claimed && (strict_ta || v == 0xFFFFU)) {
			/* Nobody acknowledged. All ones means nothing drove the
			 * line either, which is the idle line and no device. */
			rc = -EIO;
		}
	}

	mdc_set(0);
	mdio_pin_highz();

	irq_unlock(key);

	return rc;
}

/*
 * Bring the buffer up, and wait for the rail only when there is a rail to
 * wait for.
 *
 * Enabling the buffer is a load step, and the vreg regulator is a 20 kHz TIM6
 * ISR — so doing it inside irq_lock() drops the step onto a blind controller,
 * leaves it uncorrected for the whole frame, then dumps the accumulated error
 * at irq_unlock(). That was the bipolar spike on PB2 measured 2026-07-30 (8-9
 * missed ticks, back when a frame took ~430 µs). Hence a settle outside the
 * lock, where the loop absorbs the same step in a few ticks.
 *
 * None of which applies in PASSTHRU. There the gate is full on and the output
 * is Vin through the pass MOSFET — there is no control loop to blind and no
 * setpoint to miss, so the wait protects nothing and only costs. Measured, on
 * a 3.3 V target with 200 transactions back to back:
 *
 *   settle 1 ms   rail 3293-3298 mV during the burst, 3295-3300 idle
 *   settle none   rail 3292-3300 mV during the burst, 3294-3300 idle
 *
 * The burst is indistinguishable from the idle window either way, and the
 * transaction went from 1081 µs to 81. Switch reads stayed 100/100.
 *
 * The regulated case keeps the settle and keeps it unmeasured: the master
 * requires the supply at the bus rail, and on a 3.3 V target that is always
 * PASSTHRU, so a regulated burst cannot be produced on this bench at all.
 * That is the reason for the branch rather than a deletion.
 */
static void buffer_up(void)
{
	if (buffer_on) {
		return;
	}

	mdioprobe_mdc_buffer_enable(true);
	buffer_on = true;
	last_use_ms = k_uptime_get();

	struct mdioprobe_vreg_status st;

	mdioprobe_vreg_get_status(&st);
	if (st.state != VREG_PASSTHRU) {
		k_busy_wait(MDC_BUFFER_SETTLE_US);
	}

	/* Armed once, here. A transaction only stamps `last_use_ms`; the
	 * handler is what extends the linger, by rescheduling itself when it
	 * finds the line was busy again. Rearming per transaction instead cost
	 * ~8 µs of the 89 a transaction now takes, for nothing. */
	k_work_reschedule(&buffer_release_work, K_MSEC(MDC_BUFFER_LINGER_MS));
}

/*
 * Let the buffer go after a spell of quiet, rather than after every frame.
 *
 * A series of transactions is the normal case — `scan`, a register dump, any
 * indirect access is several frames — and toggling the buffer between them
 * buys nothing while costing a settle each time. In PASSTHRU that settle is
 * now zero so this is only tidiness; on a regulated target it is the
 * difference between 1 ms per transaction and 1 ms per series.
 *
 * The cost is that MDC is driven, idle low, for as long as the buffer is up.
 * That blocks any other master on the wire, which is why this lingers rather
 * than latching on: the bus is held during activity and for a short while
 * after, not indefinitely.
 *
 * The handler takes the master lock, so it can never fire mid-frame. It can
 * still be waiting on that lock while a new transaction starts, which is why
 * it re-checks the timestamp instead of trusting that it was scheduled for a
 * good reason, and reschedules itself if the line went busy again.
 */
static void buffer_release(struct k_work *work)
{
	ARG_UNUSED(work);

	k_mutex_lock(&master_lock, K_FOREVER);

	if (buffer_on) {
		const int64_t idle_ms = k_uptime_get() - last_use_ms;

		if (idle_ms >= MDC_BUFFER_LINGER_MS) {
			mdioprobe_mdc_buffer_enable(false);
			buffer_on = false;
		} else {
			k_work_reschedule(&buffer_release_work,
					  K_MSEC(MDC_BUFFER_LINGER_MS - idle_ms));
		}
	}
	k_mutex_unlock(&master_lock);
}

static int transact(bool is_write, uint8_t prtad, uint8_t regad, uint16_t *data)
{
	if (!initialised) {
		int err = mdioprobe_mdio_master_init();

		if (err) {
			return err;
		}
	}

	k_mutex_lock(&master_lock, K_FOREVER);

	buffer_up();
	int rc = transact_frame(is_write, prtad, regad, data);

	last_use_ms = k_uptime_get();
	k_mutex_unlock(&master_lock);

	return rc;
}
int mdioprobe_mdio_master_init(void)
{
	LL_AHB2_GRP1_EnableClock(LL_AHB2_GRP1_PERIPH_GPIOB);

	cycle_counter_enable();

	/*
	 * MDC_EN first, and inactive — never glitch the target's clock.
	 *
	 * OPEN-DRAIN, and this matters: on the board MDC_EN carries a 4.7 kΩ
	 * pull-up to VREG, the buffer's own supply. Driving the pin push-pull
	 * HIGH pushes 3.3 V through that resistor into the VREG rail, which
	 * has almost no load (buffer quiescent current only) — it back-powers
	 * the regulated supply to ~3.26 V no matter what the pass MOSFET
	 * does, and the vreg control loop then winds its gate fully shut in a
	 * futile attempt to bring the output down.
	 *
	 * Open-drain is what the pull-up is there for: we only ever pull the
	 * line LOW to enable the buffer, and the resistor produces the HIGH
	 * level at the buffer's own supply rail. It also means the pin is
	 * harmless before this function runs — reset state is high-Z input,
	 * so the pull-up already holds the buffer disabled.
	 */
	LL_GPIO_SetOutputPin(GPIOB, PIN_MDC_EN);
	LL_GPIO_SetPinOutputType(GPIOB, PIN_MDC_EN, LL_GPIO_OUTPUT_OPENDRAIN);
	LL_GPIO_SetPinPull(GPIOB, PIN_MDC_EN, LL_GPIO_PULL_NO);
	LL_GPIO_SetPinMode(GPIOB, PIN_MDC_EN, LL_GPIO_MODE_OUTPUT);

	LL_GPIO_ResetOutputPin(GPIOB, PIN_MDC);
	LL_GPIO_SetPinOutputType(GPIOB, PIN_MDC, LL_GPIO_OUTPUT_PUSHPULL);
	LL_GPIO_SetPinPull(GPIOB, PIN_MDC, LL_GPIO_PULL_NO);
	LL_GPIO_SetPinSpeed(GPIOB, PIN_MDC, LL_GPIO_SPEED_FREQ_HIGH);
	LL_GPIO_SetPinMode(GPIOB, PIN_MDC, LL_GPIO_MODE_OUTPUT);

	mdio_pin_highz();

	/* Pins are up and the buffer is off — the one moment the bit loop can
	 * be run for measurement without anything reaching the target. */
	calibrate_bit_loop();
	apply_clock();
	trim_clock();

	initialised = true;

	LOG_INF("MDIO master ready at %u kHz (%u cycles/half-period): "
		"MDC=PB7 (buffer EN=PB6), MDIO out=PB8, in=COMP7/PB14",
		mdc_clock_khz, mdc_half_cycles);
	return 0;
}

/*
 * DIAGNOSTIC — not part of the master's normal path.
 *
 * Sends a read frame's header exactly as transact_frame() does, then instead
 * of decoding, samples the line twice in every clock and hands both bit
 * strings back raw:
 *
 *   early[i]  taken at the end of the HIGH half of clock 47+i
 *             (where read_bit() samples today)
 *   late[i]   taken at the end of the LOW half, just before the rising
 *             edge of clock 47+i
 *
 * so late[0] still belongs to clock 46 — the master's own last address bit —
 * and is a free sanity check that the numbering is what we think it is.
 *
 * The point is to settle, by measurement rather than by argument, where a
 * target puts its turnaround zero and its data bit 15 relative to the clocks
 * this master actually emits, and on which side of which edge it switches.
 * Two samples per clock is what separates "the device answers a bit early"
 * from "we sample a bit late" — one sample cannot tell those apart.
 *
 * The extra sense read lengthens the low half slightly, so run this well
 * below the top of the clock range where that is noise.
 */
int mdioprobe_mdio_master_probe(uint8_t prtad, uint8_t regad, uint32_t nbits,
				uint32_t *early, uint32_t *late)
{
	if (early == NULL || late == NULL || nbits == 0U || nbits > 32U) {
		return -EINVAL;
	}
	if (!initialised) {
		int err = mdioprobe_mdio_master_init();

		if (err) {
			return err;
		}
	}

	k_mutex_lock(&master_lock, K_FOREVER);
	buffer_up();

	uint32_t e = 0;
	uint32_t l = 0;
	unsigned int key = irq_lock();

	send_preamble();
	write_bits(0x1, 2);           /* ST = 01 */
	write_bits(0x2, 2);           /* OP = 10, read */
	write_bits(prtad & 0x1F, 5);
	write_bits(regad & 0x1F, 5);

	mdio_pin_highz();

	for (uint32_t i = 0; i < nbits; i++) {
		wait_half_period();
		l = (l << 1) | (uint32_t)mdio_sense();
		mdc_set(1);
		wait_half_period();
		e = (e << 1) | (uint32_t)mdio_sense();
		mdc_set(0);
	}

	irq_unlock(key);

	mdc_set(0);
	mdio_pin_highz();

	last_use_ms = k_uptime_get();
	k_mutex_unlock(&master_lock);

	*early = e;
	*late = l;
	return 0;
}

int mdioprobe_mdio_master_read(uint8_t prtad, uint8_t regad, uint16_t *data)
{
	if (data == NULL) {
		return -EINVAL;
	}
	return transact(false, prtad, regad, data);
}

int mdioprobe_mdio_master_write(uint8_t prtad, uint8_t regad, uint16_t data)
{
	return transact(true, prtad, regad, &data);
}

void mdioprobe_mdio_master_release(void)
{
	/* Explicit hand-back: drop the buffer now rather than at the end of
	 * the linger, and cancel the pending release so it cannot re-fire. */
	(void)k_work_cancel_delayable(&buffer_release_work);

	mdc_set(0);
	mdioprobe_mdc_buffer_enable(false);
	buffer_on = false;
	mdio_pin_highz();
}
