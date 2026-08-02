# MDIO Capture — End-to-End Flow

This document describes the complete data path for capturing a single
Clause-22 MDIO frame: from the analog wire on the target board, through
the on-chip analog/timer/SPI front end, to a 32-bit decoded frame in
RAM. It is the reference for understanding *why* every peripheral is
configured the way it is in `src/mdioprobe_*.c`.

---

## 1. Why hardware-only capture

Clause-22 MDIO at 2.5 MHz means an MDC edge every 200 ns; live PPU
traffic from the MV88E6321 runs faster still (~6.27 MHz). A
software-ISR-per-bit approach is out of the question on a 170 MHz
Cortex-M4. Even DMA-from-GPIO is awkward because the start of a frame
is not self-clocked — we have to detect the preamble→ST=0 transition to
lock onto the 32-bit data field.

We solve this by chaining on-chip peripherals so the CPU is involved
only in **decoding the captured 32-bit word**, never in clocking,
edge-detection, or framing:

```
analog → comparators → TIM3 counter + HRTIM latch (NSS) → SPI2 slave + DMA → RAM
```

The CPU sleeps in `k_sleep(2 ms)` polling a ring-buffer index while the
hardware does the rest.

---

## 2. Structural block diagram

```
   ┌─────────────────────────── mdioprobe_v2 board ────────────────────────────┐
   │                                                                           │
   │  Target ── MDC ──► PA1 ──►┌ COMP1 ┐                                       │
   │                           │ + -   │ comp1_out ─► PA0 (AF8) ─[trace]► PB13 │
   │            DAC3_CH1 ─────►┘       │            (digital MDC)    SPI2 SCK  │
   │            (MDC threshold, internal-only)                                 │
   │                                                                           │
   │  Target ── MDIO ─► PB14 ──►┌ COMP7 ┐                                      │
   │            (ADC1_IN5)      │ + -   │ comp7_out ─► PA8 (AF8) ─[trace]► PB15│
   │            DAC4_CH1 ──────►┘       │            (digital MDIO)  SPI2 MOSI │
   │            (MDIO threshold, internal-only)                                │
   │                                                                           │
   │  comp1_out ─internal─► TIM3_ETR  (external clock, MDC edges)              │
   │  comp7_out ─internal─► TIM3_TI1  (trigger)  and  HRTIM EEV5               │
   │                                                                           │
   │  ┌──────────── TIM3 (one-pulse, ARR=32) ─────────────┐                    │
   │  │  trigger (comp7_out ↓ = ST) starts the counter    │                    │
   │  │  ETR (comp1_out ↑ = MDC) clocks it 0→32           │                    │
   │  │  OPM stops at 32; MMS=Enable → TRGO = CNT_EN      │                    │
   │  └──────────────────────┬────────────────────────────┘                    │
   │                         │ tim3_trgo (CNT_EN) ─internal─► HRTIM EEV3       │
   │  ┌──────────── HRTIM1 Timer B — NSS SR latch ────────┐                    │
   │  │  SETx1R = EXTEVNT5 (comp7_out ↓)  → NSS LOW       │                    │
   │  │  RSTx1R = EXTEVNT3 (CNT_EN ↓ @32) → NSS HIGH      │                    │
   │  │  OUTxR.POL1 = 1  → output active-low              │                    │
   │  └──────────────────────┬────────────────────────────┘                    │
   │                         ▼                                                 │
   │              CHB1 = PA10 (AF13) ─[trace]──► PB12 (SPI2 NSS)               │
   │                                                                           │
   │  ┌──────── SPI2 slave (custom LL driver) ────────┐                        │
   │  │  CPHA=0 CPOL=0, MSB-first, 8-bit, NSS hard in │                        │
   │  │  RXDMAEN=1, FRXTH=QUARTER (RXNE per byte)     │                        │
   │  └────────────┬──────────────────────────────────┘                        │
   │               │ RXNE → DMA request 12 (SPI2_RX)                           │
   │  ┌──── DMAMUX1 ch0 → DMA1 Channel 1 ────┐                                 │
   │  │  P→M, byte, MINC, CIRCULAR, 256 B    │                                 │
   │  │  TE IRQ only; position = CNDTR       │                                 │
   │  └────────────┬─────────────────────────┘                                 │
   │               ▼                                                           │
   │   uint8_t mdio_rx_buf[64 × 4]  ← 64-slot ring of 32-bit frames            │
   └───────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Signal chain, step by step

### 3.1 Analog input
- **MDC** on PA1 (COMP1 positive input).
- **MDIO** on PB14 (COMP7 positive input — also ADC1_IN5 for idle
  voltage sense; analog mode lets both peripherals read the pin
  without a pinmux conflict).
- Both pins set to `LL_GPIO_MODE_ANALOG`, no pull (target board
  provides the MDIO pull-up).

### 3.2 Threshold generation
The comparator negative inputs are driven by internal DACs:
- **DAC3_CH1 → COMP1_INM** (MDC threshold, INMSEL=100).
- **DAC4_CH1 → COMP7_INM** (MDIO threshold, INMSEL=100).

**Why DAC3 / DAC4?** Both are silicon **internal-only** on STM32G4 —
no bond wire to a package pin. The high-impedance reference node is
purely on-die, so there is nothing for neighbouring AF switching to
capacitively couple into. DAC1/DAC2 have external pins that would act
as antennas and corrupt captures with 0xff/0x00 garbage bytes
(confirmed empirically on this hardware). The Zephyr DAC driver
mandates pinctrl, which DAC3/DAC4 cannot provide, so they are
configured directly via LL (`CONFIG_DAC` is off).

Thresholds are derived from the **bus rail** at 30%. The rail comes
from the target detector (`mdioprobe_bus.c`), which measures the MDIO
idle level on PB14 with ADC1 and classifies it into 1.8 / 2.5 / 3.3 V;
the API applies the threshold when a function that needs the
comparators starts, so a caller never sets it by hand.

The measurement is the **peak of a 64-sample burst**, not a single
reading: MDIO is open-drain pulled up to the rail, so the high level is
the one that means something, and one sample lands wherever traffic
happens to be. Measured against live PPU traffic, the single-sample
version reported 2549, 2856, 2923, 3167 and 3299 mV on successive boots
for the same 3.3 V rail — and both the threshold and the translator
supply were set from it.

`mdioprobe_dac_set_threshold()` and `mdioprobe_dac_set_mdio_threshold()`
still override manually for bench work — useful for dropping the MDIO
threshold alone so the open-drain rising edge is caught early at high
MDC, while MDC (push-pull) keeps the symmetric threshold.

### 3.3 Digital conversion (comparators)
- **COMP1** (MDC): `+`=PA1, `-`=DAC3_CH1, non-inverted, no hysteresis,
  no output blanking. Output appears on **PA0** (AF8).
- **COMP7** (MDIO): `+`=PB14, `-`=DAC4_CH1, same options. Output
  appears on **PA8** (AF8).
- Each comparator output is also routed *internally* (no pin needed)
  to the timer fabric as `comp1_out` / `comp7_out`.

PCB traces carry the digital outputs into SPI2:
- **PA0 → PB13** (SPI2 SCK).
- **PA8 → PB15** (SPI2 MOSI).

### 3.4 Frame delimiting (TIM3 counter + HRTIM latch)

NSS — the SPI2 chip-select that gates exactly one 32-bit frame — must
be **HIGH when idle** and pulse **LOW for exactly the 32 data bits**,
starting at the frame's ST=0 edge with zero delay.

A plain general-purpose timer cannot produce this. A GP-timer output
compare is a level comparison against the counter value, and an
OPM-parked counter sits at the same value (0) that the pulse starts
from — so "HIGH idle + zero-delay LOW pulse" is unreachable with one
OC channel. The HRTIM output, by contrast, is a true **SR latch**:
independent set and reset event sources, and it *holds* its level
between events. The design therefore splits the job:

**TIM3 — the MDC edge counter** (`mdioprobe_tim3_count_init`):
- `TI1` ← `comp7_out`, falling edge — the preamble→ST=0 transition is
  the trigger.
- `ETR` ← `comp1_out`, rising edge, external clock mode 2 — the
  counter is clocked by real MDC edges.
- Slave mode = **trigger**: each trigger starts the counter (it is not
  reset). Combined with **one-pulse mode**, mid-frame MDIO 1→0
  transitions, which arrive while the counter runs, are ignored.
- `ARR = 32`: the update event (UEV), where OPM clears the counter
  enable, lands exactly 32 MDC edges after the trigger.
- `MMS = Enable`: TRGO carries the **CNT_EN** signal — HIGH while the
  counter runs, LOW when it stops. Its *falling* edge is the OPM stop
  at the 32nd MDC tick.

Because the counter counts real edges, there is no fMDC calibration,
no blanking window, and the inter-frame gap is irrelevant — the
counter is simply stopped between frames.

**HRTIM1 Timer B — the NSS SR latch** (`mdioprobe_hrtim_nss_init`):
- `EEV5` ← `comp7_out`, falling edge (RM0440 Table 240, EE5SRC=10).
- `EEV3` ← `tim3_trgo`, falling edge (EE3SRC=10) — catches CNT_EN ↓.
- `OUTxR.POL1 = 1` → the CHB1 output is **active-low**: inactive
  (idle / post-reset) level is HIGH, active level is LOW.
- `SETx1R = EXTEVNT5`: MDIO ↓ at ST sets the latch → **NSS LOW**.
- `RSTx1R = EXTEVNT3`: CNT_EN ↓ at the 32nd MDC tick resets it →
  **NSS HIGH**.
- At init, an `SRT` (software reset) write prepositions the latch
  inactive = HIGH before the output is connected to the pin.

Mid-frame MDIO ↓ edges re-fire `EXTEVNT5` while NSS is already LOW —
a harmless no-op on a set latch. During the preamble there are no MDIO
falling edges, so NSS stays HIGH until the next ST.

**Set/reset priority** (RM0440 §28.3.7): if `EXTEVNT5` (set) and
`EXTEVNT3` (reset) fall within the same tHRTIM period — possible right
at the 32-MDC boundary — **reset wins and set is ignored**, so the
boundary always resolves cleanly to NSS HIGH.

Result: CHB1 (PA10) stays HIGH at rest and pulses LOW for exactly the
32 MDC of one data field. PA10 is traced to **PB12**, SPI2's hardware
NSS input.

### 3.5 SPI slave capture
SPI2 is configured by the **custom LL driver** in
`src/mdioprobe_mdio_api.c`. Key settings:
- `LL_SPI_MODE_SLAVE`.
- `LL_SPI_PHASE_1EDGE` (CPHA=0): MOSI sampled on the **rising** SCK
  edge.
- `LL_SPI_POLARITY_LOW` (CPOL=0): SCK idle is LOW.
- `LL_SPI_DATAWIDTH_8BIT`, MSB first — four 8-bit transfers = 32-bit
  frame.
- `LL_SPI_NSS_HARD_INPUT` — SPI shifts only while NSS (PB12) is LOW.
- `LL_SPI_RX_FIFO_TH_QUARTER` — RXNE fires per byte.
- `EnableDMAReq_RX(SPI2)` — each RXNE raises a DMA request.
- Full-duplex direction (default); the TX/MISO side is unused.

CPOL=0 / CPHA=0 matches what the comparators produce: MDIO is valid
on the rising edge of MDC (Clause 22), so SPI samples MOSI = MDIO on
every MDC rising edge — exactly the intent.

### 3.6 DMA transfer
- **DMA1 Channel 1** (DMAMUX channel 0 → DMA1_Ch1 is the simplest
  fixed mapping).
- **DMAMUX1 channel 0** routed to request **12** (`SPI2_RX`).
- Direction peripheral→memory, byte size, peripheral fixed, memory
  increment, **CIRCULAR** mode, priority VERYHIGH.
- Source `&SPI2->DR`; destination `mdio_rx_buf` (64 slots × 4 bytes =
  256 B). Each NSS-LOW window deposits one 4-byte frame; after 64
  slots the DMA wraps and overwrites the oldest.
- Only the `TE` (transfer-error) interrupt is enabled on
  `DMA1_Channel1_IRQn`, and it only counts errors. There is no write
  cursor to advance: the consumer derives the write position from CNDTR
  byte-exactly, which is what lets a lone frame surface instead of
  waiting for a half ring to fill.

### 3.7 Decode
`mdio_decode_frame()` interprets the 32-bit big-endian word
according to the Clause-22 format:

| bits  | field | meaning                                       |
| ----- | ----- | --------------------------------------------- |
| 31:30 | ST    | Start of Frame (always `01`)                  |
| 29:28 | OP    | Opcode: `01` write, `10` read                 |
| 27:23 | PHY   | PHY address (5 bits)                          |
| 22:18 | REG   | Register address (5 bits)                     |
| 17:16 | TA    | Turn-around (driver-dependent, ignored)       |
| 15:0  | DATA  | 16-bit register data                          |

The DMA write pointer's byte offset (0..3) at boot is stochastic, so
the first `sniff_one` scans all four cyclic rotations for the one that
yields `ST=01` AND `OP ∈ {01,10}`, locks it, and applies it to every
later decode. `ST != 01` after lock flags `out->err = -1` (torn
frame).

---

## 4. Software flow

### 4.1 Boot-time init

Owned by `mdioprobe_init()` in `src/mdioprobe_api.c`, which is where the
ordering constraints below are enforced rather than left to a caller.

1. `mdioprobe_adc_dac_init()` — ADC1 ready, DAC3_CH1 / DAC4_CH1
   configured via LL for internal-only output.
2. Thresholds come from the **bus rail**, not from a raw reading: the
   detector (`mdioprobe_bus.c`) classifies the MDIO idle level into
   1.8 / 2.5 / 3.3 V and the API applies `rail × 0.30` when a function
   that needs the comparators starts. The level itself is the peak of a
   64-sample burst — a single sample lands wherever traffic happens to
   be, and on a live bus that read anywhere from 2549 to 3299 mV for the
   same 3.3 V rail.
3. `mdioprobe_tim3_count_init()` — TIM3 as the MDC edge counter
   (TI1←comp7_out, ETR←comp1_out, slave-trigger, OPM, ARR=32,
   MMS=Enable). Runs before COMP so the TI1/ETR muxes are locked
   before the comparators toggle.
4. `mdioprobe_hrtim_nss_init()` — HRTIM1 Timer B NSS latch
   (EEV5←comp7_out, EEV3←tim3_trgo, SET/RST crossbar, CHB1→PA10/AF13).
   Runs after TIM3 — it consumes TIM3's TRGO.
5. `mdioprobe_comp_init()` — PA1/PB14 → analog; PA0/PA8 → AF8;
   COMP1 / COMP7 configured and enabled.
6. `mdioprobe_mdio_init()` — DMA1/DMAMUX1/SPI2 clocks; SPI2 (slave,
   mode-0, 8-bit MSB, hardware NSS, RX DMA); DMAMUX ch0 → request 12;
   **DMA1_Channel1 in CIRCULAR mode** over the 64-slot ring; only the
   **TE** (transfer-error) IRQ is connected. SPI and DMA are enabled and
   **left running indefinitely** — steady-state capture is touch-free.
   HT/TC used to be enabled to advance a write cursor; that cursor is
   gone (see 4.2) and they served nothing else.
7. `mdioprobe_serial_init()` and `mdioprobe_vreg_init()` — connector
   personality and the translator supply.
8. `mdioprobe_bus_start()` — the target detector thread. It drives the
   base layer of the indication and gates capture and the master: until
   the rail is known, both refuse to run.

### 4.2 Steady-state continuous capture

After boot there is **no per-frame software reconfiguration**. SPI
and DMA run continuously; frames land in the ring as they arrive.

```
┌─ Frame gating hardware (per frame, no CPU) ──────────────────────┐
│                                                                  │
│ MDIO falling edge (preamble→ST=0)                                │
│   ├─► HRTIM EEV5 ─► SETx1R ─► CHB1 LOW  (= NSS asserted)         │
│   └─► TIM3 TI1 trigger ─► one-pulse counter starts               │
│                                                                  │
│ 32 MDC rising edges clock TIM3 (ETR)                             │
│   └─► UEV: OPM stops the counter, CNT_EN falls                   │
│        └─► HRTIM EEV3 ─► RSTx1R ─► CHB1 HIGH (= NSS deasserted)  │
│                                                                  │
│ Preamble of next frame: no MDIO ↓ → NSS stays HIGH               │
└──────────────────────────────────────────────────────────────────┘

┌─ SPI2 + DMA1 (per frame, no CPU) ────────────────────────────────┐
│ NSS LOW (from CHB1 trace) enables the SPI slave                  │
│ 32 SCK rising edges (from comp1_out trace):                      │
│   each samples MOSI=MDIO → 8-bit shift; every 8 bits RXNE→DMA    │
│ DMA copies 4 bytes SPI->DR → mdio_rx_buf[next slot]              │
│ No interrupt. The write position is CNDTR, read on demand.       │
└──────────────────────────────────────────────────────────────────┘

┌─ mdioprobe_mdio_sniff_one_to (consumer) ─────────────────────────┐
│ head = (BUF - CNDTR) & (BUF-1)          ← byte-exact, no cursor  │
│ written = (head - slot_base) mod BUF                             │
│ frame k is ready once written >= off + 4  (BYTES, not slots)     │
│ if written > BUF - 8: overrun++, resync to the live edge         │
│ snapshot mdio_rx_buf[read_slot], read_slot = (read_slot+1) % 64  │
│ decode → return frame                                            │
└──────────────────────────────────────────────────────────────────┘
```

The write cursor used to be a variable advanced by the DMA half/full
transfer interrupts — **once per half ring, i.e. every 32 frames**. A
frame therefore sat undelivered until 31 more arrived behind it, which
was invisible at PPU rates and fatal on a bus polled once a second.
Readiness is now derived from CNDTR in bytes, so a single frame surfaces
on its own.

### 4.3 Why the continuous-capture design

- **No per-frame SPI disable/re-enable.** Earlier designs
  reconfigured SPI between captures; the ~2-3 µs disabled window
  could swallow a frame's NSS edge and capture "tail of N + head of
  N+1". Continuous mode removes the window.
- **CIRCULAR DMA + ring buffer absorbs consumer jitter.** 64 slots ×
  4 bytes at PPU rates holds tens of ms of history, so the deferred
  Zephyr log thread can burst without dropping frames.
- **NSS HARD INPUT auto-aligns SPI between frames.** STM32 SPI slave
  resets its shift counter on every NSS-HIGH transition, so each
  NSS-LOW window starts clean — exactly 32 SCK → 4 bytes → one slot.
- **DMA byte alignment is auto-detected** — CIRCULAR DMA never resyncs
  to frame boundaries, so a constant 0..3-byte rotation is locked in
  software (`mdio_detect_offset` / `mdio_window_is_c22`). Two rules earn
  their keep. A candidate window must have ST=01, OP ∈ {01,10} **and**,
  for a write, TA=10 — the master drives that turnaround, so it is a
  hard check; without it a window straddling a frame boundary passes and
  the detector locked the wrong offset in 5 runs out of 8 on real PPU
  traffic. And when more than one candidate passes, the slot is dropped
  rather than guessed. The offset is a property of the **stream**, not
  the session: it shifts when the traffic changes, so starting a capture
  re-arms the detection.

---

## 5. Critical timing notes

| Concern | Mitigation |
| --- | --- |
| Comparator output glitches before threshold settles | Threshold written before COMP enable. The idle level behind it is the peak of a 64-sample burst, not one reading — one sample lands wherever traffic is and mis-sets both the threshold and the translator supply. |
| Comparator reference noise | Thresholds use DAC3/DAC4 — internal-only on G4, no package pins, so no antenna for switching noise. DAC1/DAC2 (bonded pins) corrupted captures and were abandoned. |
| Open-drain MDIO slow rising edge at high MDC | The MDIO threshold is settable independently of MDC's, so the rise can be caught early while MDC (push-pull) keeps the symmetric threshold. |
| Mid-frame MDIO 1→0 retriggering the frame | TIM3 one-pulse mode is single-shot per trigger; mid-frame edges arriving while the counter runs are ignored. No HRTIM blanking window needed. |
| NSS idle level | The HRTIM output is an SR latch (POL1=1) — it holds HIGH between frames. A GP-timer OC cannot do this (idle level is tied to the pulse-start level); hence the TIM3+HRTIM split. |
| set/reset coincidence at the 32-MDC boundary | RM0440 §28.3.7: reset has priority over set — the boundary resolves to NSS HIGH. |
| HRTIM output stuck after init | `SRT` preposition write forces NSS inactive HIGH before `TBCEN` → `OENR`. |
| SPI bit-offset drift between captures | CIRCULAR DMA + always-on SPI — no per-frame reconfig; NSS HARD INPUT realigns the SPI shift counter every NSS-HIGH. |
| Lost frame between sniffer iterations | 64-slot ring absorbs consumer (log thread) burstiness. |

---

## 6. Pin map summary

Capture-path pins only.

| Function | Pin | Mode | Notes |
| --- | --- | --- | --- |
| MDC sense | PA1 | Analog (COMP1+) | target MDC wire |
| MDIO sense | PB14 | Analog (COMP7+, ADC1_IN5) | target MDIO wire; also the master's read-back |
| MDC threshold | (DAC3_CH1) | internal | no GPIO (internal-only DAC) |
| MDIO threshold | (DAC4_CH1) | internal | no GPIO (internal-only DAC) |
| MDC digital out | PA0 | AF8 (COMP1_OUT) | **PCB trace** → PB13 |
| MDIO digital out | PA8 | AF8 (COMP7_OUT) | **PCB trace** → PB15 |
| NSS gen | PA10 | AF13 (HRTIM1_CHB1) | **PCB trace** → PB12 |
| SPI2 SCK | PB13 | AF5 | receives PA0 |
| SPI2 MOSI | PB15 | AF5 | receives PA8 |
| SPI2 NSS | PB12 | AF5 | receives PA10 |
| Target reset | PB5 | GPIO open-drain, active LOW | 10 ms pulse at boot; pull-up must come from the target |

TIM3 drives no GPIO — its TI1/ETR inputs and TRGO output are all
internal signals.

There is no CPU-side bitbang MDIO bus on this board: the probe's own
Clause-22 master uses PB6/PB7/PB8 through the level-translating buffer,
and the switch that generates test traffic is managed from a separate
probe.

---

## 7. Software / config dependencies

`prj.conf` deliberately keeps things minimal:
- `CONFIG_SPI=y` + `CONFIG_SPI_SLAVE=y` — only so the stm32 SPI
  driver applies pinctrl on the `&spi2` node at init. Its API is
  never called.
- No `CONFIG_DMA` — the custom LL driver owns DMA1_Channel1.
- `CONFIG_ADC=y` — Zephyr API for the ADC1 idle-voltage measurement.
- `CONFIG_DAC` is **off** — the threshold DACs are DAC3/DAC4
  channels, internal-only on G4 (no pins), so the pinctrl-mandating
  Zephyr DAC driver cannot manage them. They are configured via LL.
- **No `CONFIG_MDIO`.** There is no Zephyr MDIO bus on this board; the
  Clause-22 master is bitbanged directly in `mdioprobe_mdio_master.c`
  because `zephyr,mdio-gpio` assumes one bidirectional pin and this
  hardware drives and senses on two (PB8 out, PB14 in through COMP7).
- `CONFIG_ZERO_LATENCY_IRQS=y` — the vreg control tick must run through
  the master's `irq_lock()`, or the rail collapses during a frame.
- `CONFIG_SHELL=y` — interactive shell over USB CDC ACM, with enlarged
  RX ring and command buffers (long `flash` commands overran the 64-byte
  defaults and the shell silently executed the remainder).

HRTIM and TIM3 have no Kconfig — both are driven entirely via LL and
direct register writes.

Board definition lives in-tree at `boards/meshium/mdioprobe_v2/`, picked
up via `BOARD_ROOT` from `CMakeLists.txt`; there is no Nucleo overlay.
Relevant nodes:
- `mdioprobe_config` with `io-channels = <&adc1 5>` and
  `target-reset-gpios` on PB5.
- `mdio_master` — describes PB6/PB7/PB8 even though the code muxes them
  itself; `BUILD_ASSERT`s in `mdioprobe_mdio_master.c` break the build if
  DT and code disagree.
- `spi2` enabled but **without** `dmas` — the node exists only so its
  pinctrl (PB12/PB13/PB15 → AF5) is applied. SCK/MOSI drop their default
  pull-downs, which would skew edges at multi-MHz MDC.
- `adc1` with `channel@5` (ADC1_IN5 = PB14).
- No `&dac3` / `&dac4` node — configured purely via LL.

---

## 8. Debugging recipe

If a capture comes back wrong, the raw bytes in `mdio_rx_buf` tell you
*where* in the chain things broke:

| Pattern | Diagnosis |
| --- | --- |
| `00 00 00 00` | DMA never advanced — SCK isn't reaching SPI2. Check the PA0 → PB13 trace, the COMP1 threshold, COMP1 enable, and that PA0 toggles on a scope. The comparator output duty is in `mdioprobe_diag_get()`. |
| `FF FF FF FF` | DMA advanced but MOSI stuck high — MDIO idle the whole NSS window. Check the PA8 → PB15 trace, the MDIO threshold, and that COMP7 isn't oscillating. |
| NSS never goes LOW (scope PA10 stuck HIGH) | No SET event. Check `comp7_out` reaches HRTIM EEV5 (EECR1: EE5 SRC=2, SNS=2) and COMP7 toggles. |
| NSS stuck LOW | No RESET event. Check TIM3 is counting (SMCR configured, CNT moving) and HRTIM EE3 SRC=2 / SNS=2, RSTF1R bit23 set. |
| NSS LOW far too wide / too narrow | Wrong `ARR`. TIM3 ARR should be 32. Measured working range is **31…38** (clocks = ARR+1): 30 and below truncates the frame, 39 and above lets a 40th clock in and a fifth byte enters the stream, collapsing the 4-byte slotting. |
| Frame shifted by N bytes | DMA byte-alignment lottery — auto-detected, logged as `DMA byte alignment auto-detected: offset=N`. If every frame decodes as plausible garbage, suspect a **stale** lock from earlier traffic and re-arm the detection with `mdioprobe_mdio_set_byte_offset(-1)`; starting a capture does this anyway. |

What you need to be able to reach while triaging, and where it lives in
`mdioprobe_api.h`:

| Capability | API |
| --- | --- |
| Run the capture loop, continuously or for N frames | `mdioprobe_capture_start()` / `_start_n()` / `_next()` / `_stop()` |
| Count frames instead of printing them — at PPU rates a per-frame log makes the console, not the chain, the bottleneck | any consumer that does not print; `mdioprobe_capture_next()` returns the frame, printing is the caller's choice |
| Raw ring, no alignment or validity checks — tells a decode failure apart from a capture failure | `mdioprobe_diag_ring_dump()` |
| Counters, locked byte offset, NSS window width, comparator duty, live threshold | `mdioprobe_diag_get()` |
| Measure MDC frequency without driving the bus | `mdioprobe_mdc_freq()` |
| Drive one Clause-22 frame to exercise the chain | `mdioprobe_c22_read()` / `mdioprobe_c22_write()` |

The comparator thresholds, the NSS window width and the stream byte
offset are applied under the hood and are intentionally not settable
through the API — they are visible in `mdioprobe_diag_get()` when a
measurement needs them.

---

## 9. Out of scope here

- The active Clause-22 master — `src/mdioprobe_mdio_master.c`.
- The target detector that supplies the bus rail — `src/mdioprobe_bus.c`.
- The public API that wraps all of this — `include/mdioprobe_api.h`.
