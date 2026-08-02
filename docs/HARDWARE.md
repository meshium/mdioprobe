# MDIO Probe v2 — hardware map

MCU: **STM32G474CET6**, LQFP48, 512 KB flash / 128 KB RAM, 170 MHz.
No crystal: HSI16 → PLL (M=4, N=85, R=2) = 170 MHz; USB FS on HSI48 + CRS.

**The missing crystal is not only a USB concern.** HSI16 is the only
reference behind anything that measures time. Measured on this unit:
`kernel uptime` against the host clock over 120 s runs **+660 ppm** fast,
and the MDC frequency measurement reads low by the same amount (−721 ppm
against a scope-confirmed 6.250 MHz). The datasheet allows ±1%. Framing
does not care — TIM3 counts real edges — but no absolute time figure from
this board is good beyond about three significant digits.

## 1. Full pin map

| Pin | Signal | Function in firmware |
| :-- | :-- | :-- |
| PA0 | SPI2_SCK source | COMP1_OUT (AF8), MDC digital → **PCB trace to PB13** |
| PA1 | MDC sense | COMP1_INP (Analog) — sniffed clock |
| PA2 | QSPI_NSS | QUADSPI1_BK1_NCS |
| PA3 | QSPI_SCK | QUADSPI1_CLK |
| PA4 | VREG_CTL | DAC1_OUT1 (buffered) → P-MOSFET gate |
| PA5 | — | unused |
| PA6 | QSPI_D3 | QUADSPI1_BK1_IO3 |
| PA7 | QSPI_D2 | QUADSPI1_BK1_IO2 |
| PA8 | SPI2_MOSI source | COMP7_OUT (AF8), MDIO digital → **PCB trace to PB15** |
| PA9 | LED_G | GPIO, active low |
| PA10 | SPI2_NSS source | HRTIM1_CHB1 (AF13), NSS latch → **PCB trace to PB12** |
| PA11 / PA12 | USB_N / USB_P | USB FS device (CDC ACM console) |
| PA13 / PA14 | JTMS / JTCK | SWD |
| PA15 | JTDI | reserved |
| PB0 | QSPI_D1 | QUADSPI1_BK1_IO1 |
| PB1 | QSPI_D0 | QUADSPI1_BK1_IO0 |
| PB2 | VREG | ADC2_IN12 — regulated translator supply sense |
| PB3 | JTDO | reserved |
| PB4 | — | unused |
| PB5 | PHY_RST | GPIO open-drain, no pull, active low |
| PB6 | MDC_EN | GPIO **open-drain**, active low — buffer enable; 4.7 kΩ pull-up to VREG |
| PB7 | MDC_OUT | GPIO push-pull — bitbang MDC into the buffer |
| PB8 | MDIO out / USART3_RX / I2C1_SCL | direct to connector; OD no pull (MDIO TX) / Analog (RX, or parked) |
| PB9 | USART3_TX / I2C1_SDA | direct to connector, 3.3 V; parked (Analog) in MDIO mode |
| PB10 | — | unused |
| PB11 | LED_R | GPIO, active low |
| PB12 | SPI2_NSS | AF5 — chip select from PA10 |
| PB13 | SPI2_SCK | AF5 — clock capture from PA0 |
| PB14 | MDIO sense | ADC1_IN5 + COMP7_INP (Analog) — sniffed bus **and** master read-back |
| PB15 | SPI2_MOSI | AF5 — data capture from PA8 |

Internal-only, no pins: **DAC3_CH1** → COMP1_INM (MDC threshold),
**DAC4_CH1** → COMP7_INM (MDIO threshold).

## 2. Target connector

| Line | Driven by | Notes |
| :-- | :-- | :-- |
| MDC (MDIO mode) | PB7 → SN74LV1T125DBV buffer | enabled by MDC_EN (PB6, active low, open-drain); buffer VCC = VREG, so MDC swings at the target's level — measured 1800 → 1816…1840 mV, 2500 → 2543…2566, 3300 → 3176…3200; output also loops back to PA1/COMP1 |
| USART3_TX / I2C1_SDA | PB9, **direct** | bypasses the buffer — fixed 3.3 V |
| MDIO | PB8 out + PB14 in | **two MCU pins, one connector line**: PB8 drives (OD), PB14 senses via COMP7 |
| USART3_RX / I2C1_SCL | PB8, direct | same pin as the MDIO driver, different personality |
| PHY_RST | PB5 | open-drain, no pull — pull-up must come from the target |
| GND | — | |

Exactly one personality is live at a time, and the two never overlap on the
shared connector line:

- **MDIO mode** — PB7 clocks the buffer, PB8 drives data, PB14 reads it back.
  PB9 is parked (Analog) and both serial peripherals sit in their "sleep"
  pinctrl state, so nothing loads the bus the analog front end measures. The
  MDIO drive pin is Analog while receiving, open-drain (no pull) while
  transmitting.
- **UART / I2C mode** — the buffer is **off** (otherwise it would fight PB9
  on the same connector line), PB8/PB9 drive the connector directly at a
  fixed 3.3 V. No level translation on this hardware revision: the
  programmable supply feeds only the MDC buffer. I2C is master-only, and SDA
  stays bidirectional end to end because it never crosses the buffer.

## 3. Programmable translator supply (vreg)

An external P-MOSFET pass element driven by DAC1_CH1 (PA4), Vout sensed by
ADC2_IN12 (PB2), a 20 kHz TIM6 ISR running the PI loop, and an ADC analog
watchdog for short-circuit cutoff.

The control tick is a **zero-latency IRQ**. It has to be: the bitbang
master holds `irq_lock()` for a whole frame, and an ordinary interrupt would
leave the loop blind for all of it. Measured on a back-to-back burst when a
frame was ~430 µs — 8-9 missed ticks — that cost 2711 mV of swing on an
1800 mV target, against 43 mV with the tick unmaskable.

Raising MDC to 1500 kHz shortened the frame to ~44 µs, so the exposure is now
under one tick and the burst test would no longer show the difference. The
zero-latency tick stays: `clock` can be wound back down to 10 kHz, where a
frame is 6.4 ms and the old numbers apply with interest.

**The board is missing a bleeder resistor from VREG to GND** (~1 kΩ). The
pass element can only source current, so without a discharge path an
overshoot decays through ~40 µA of leakage, which shows up as a sawtooth.
It is currently clipped on at the bench — put it on the next revision.

```
      Vin 3.3V
        ├───[ R_pu 100k-1M ]───┐
    ┌───┴────┐ S               │
    │P-MOSFET│ G ◄─────────────┴──── PA4  (DAC1_OUT1, buffered)
    └───┬────┘
        │ D ──► Vout (1.8-3.3 V) ──► MDC buffer / level translator supply
      [ Cout ]  └───────────────────  PB2  (ADC2_IN12)
        │
       GND
```

**Range: 1800–3000 mV regulated, above that pass-through.** This is a
property of the topology, not a setting: a series pass element needs some
voltage across it to be controllable, and Vin is only 3.3 V. At a 3000 mV
target the gate still moves the output by 20 mV per DAC code; at 3300 it
bottoms out at `dac_code = 0` and there is no regulation left.

So a request above 3000 mV puts the node into `PASSTHRU`: gate fully on,
output = the board's 3.3 V minus a fraction of a millivolt across Rds(on) at
our few milliamps. For a target with 3.3 V logic that is exactly right, and
tighter than the loop could manage anyway — 8 mV p-p under continuous
generation, against 63 mV at a regulated 3000.

Two things about `PASSTHRU` worth remembering:

- **Measuring the output stops meaning anything.** VDDA = VREF+ = the same
  3.3 V as Vin, so the ADC measures the rail against itself and always reads
  near full scale. Do not take an absolute value from it.
- **Short-circuit protection still works but loses discrimination.** The
  FAULT condition is "gate saturated AND the analog watchdog active for 100
  ticks". Gate saturation used to mean the loop had given everything it had;
  here it is true by construction, so only the 5 ms debounce remains. The
  threshold itself stays meaningful — a short pulls Vout down relative to
  VDDA.

There is nothing between 3000 and 3300: a request for 3100 would silently
deliver 3.29 V, so it collapses into pass-through and the driver says so.

Control interface: `mdioprobe_vreg_set()` / `_off()` / `_get()` in
`include/mdioprobe_api.h`; the PI gains and the short-circuit margin are
tunable through the private `src/mdioprobe_vreg.h`.

## 4. QSPI NOR

Winbond **W25Q80DVSNIG**, 8 Mbit, bank 1, memory-mapped window at 0x90000000.
Driven by Zephyr's `st,stm32-qspi-nor` driver; partitions `capture` (768 KB)
and `qspi-storage` (256 KB).

The `quadspi` DT node itself comes from Zephyr's G4 SoC dtsi — only pinctrl,
the flash child and partitions live in the board DTS.

**`writeoc = "PP_1_1_4"` is mandatory in the DTS.** The driver's default page
program opcode is 0x38 (PP_1_4_4), which this part does not implement — and
it does not fail: the write is accepted, nothing is programmed, and the
error only surfaces as a verification mismatch later. SFDP carries no
quad-program information, so nothing detects this automatically.

## 5. Hardware facts worth restating (confirmed against the schematic)

1. **Serial personalities are 3.3 V only.** PB8/PB9 reach the connector
   directly; the tri-state buffer and the programmable supply are on the MDC
   path alone. Only an I2C **master** and a UART are supported on this
   revision.
2. **The buffer is exclusive to MDC.** In UART/I2C mode `mdioprobe_serial.c`
   de-asserts MDC_EN; in MDIO mode PB9 is parked. The two sources never
   drive the connector line at once.
3. **Generated MDC loops back to PA1/COMP1** from the buffer output, so the
   frequency measurement and the threshold autocal have a clock source even
   with no external master. That self-trigger only fires when the line is
   genuinely idle — driving MDC into a bus that already has a master is a
   collision, and it also corrupts the measurement, because our ~130 kHz
   bitbang becomes the majority of the sample and the real megahertz clock
   is rejected as outliers.
4. **VDDA = VREF+ = 3.3 V**, which is what the DAC threshold maths in
   `mdioprobe_adc_dac.c` assumes.
5. **LED colours are PA9 = green, PB11 = red** — the reverse of the initial
   pinout notes, confirmed by watching an LED-only image on real hardware.
6. **MDC_EN must be driven open-drain.** The line has a 4.7 kΩ pull-up to
   VREG — the buffer's own supply. Push-pull HIGH feeds the 3.3 V domain
   into the VREG rail through that resistor; since the rail's only load is
   the buffer's quiescent current, it floats up to ~3.26 V regardless of
   the pass MOSFET, and the vreg loop saturates its gate fully shut trying
   to correct an output it does not actually control. Observed as
   `target=2989 measured=3257 dac_code=4095`.
7. **The MDC_EN release edge is measured and clean.** On a scope, OE rises
   **1…2.5 µs after the falling edge of the last MDC pulse**, with a steep
   edge — the 4.7 kΩ pull-up does not smear it, so the OE node capacitance
   is what the estimate assumed (τ ≈ 4.7 kΩ × 10-15 pF ≈ 50-70 ns). The
   1…2.5 µs is firmware, not RC: `irq_unlock()` sits between the last
   `mdc_set(0)` and the buffer being released, and the lock had been held
   for the whole frame, so every deferred interrupt flushes there. Harmless
   — MDC is parked low before the unlock and the bus rests low anyway.
   Releasing the buffer is deliberately kept **outside** the lock, so the
   load step does not land on a blinded vreg loop.
8. **The MDC buffer is a TI SN74LV1T125DBV** — a single-bit 3-state buffer
   and one-way level translator, VCC = VREG. Datasheet facts that matter:
   - **VCC range 1.8–5.5 V**, so VREG's whole 1.8–3.3 V span is in spec;
   - **inputs tolerate 5 V regardless of VCC** — driving PB7 push-pull at
     3.3 V into a buffer powered from 1.8 V is a designed-for case, not an
     overvoltage. No clamp-diode current, nothing to fix;
   - **OE is active low**, matching MDC_EN;
   - *reduced* input thresholds (at VCC 3.3 V it is specced for 1.8 V
     inputs, at 1.8 V for 1.2 V ones), so our 3.3 V swing is read reliably.

   Net effect: the input side lives in the MCU's 3.3 V domain, the output
   side swings at the target's level. Being a single buffer, it also
   confirms that only MDC crosses it — PB8/PB9 reach the connector direct.

9. **PB8 is also BOOT0 — every board needs `nSWBOOT0 = 0`.** RM0440 §2.6.1,
   Table 5: out of the factory `nSWBOOT0 = 1` (BOOT0 comes from the PB8 pin)
   and `nBOOT1 = 1`, so a high level on PB8 at reset release boots System
   memory — the ROM bootloader — instead of our firmware. PB8 carries MDIO
   straight to the connector, and any live target pulls that line up, so a
   factory-configured board **does not start at all once the target is
   connected**.

   One-time per board, over SWD:

   ```
   STM32_Programmer_CLI -c port=SWD mode=UR -ob nSWBOOT0=0
   ```

   BOOT0 then comes from the `nBOOT0` option bit, which is already 1 → main
   flash regardless of the pin. `west flash` does not set this — it is a
   per-device setting, not part of the image. A pull-down on PB8 is **not**
   an alternative: it would divide against the target's MDIO pull-up.

10. **MDIO needs a pull-up, and this board does not always have one.** The
    line is open-drain in both directions: PB8 only ever pulls low, so every
    '1' — including every bit the target sends back — is a resistor charging
    the line. Nothing on the MDC path helps here; the buffer is one-way and
    on the other wire entirely.

    Two v2 boards exist, one with an external pull-up fitted on MDIO and one
    without, and the difference is not cosmetic: on the board without it a
    Marvell 88E6321 returned nothing but `0xffff` to every read, while the
    same firmware on the pulled-up board read it correctly. The switch's own
    internal weak pull-ups are **not** sufficient. Note that this contradicts
    the assumption in item 9 that "any live target pulls that line up" — a
    target may well not, which is also why that board still booted.

    The failure looks exactly like clocking MDC too fast, and it is worth
    knowing that it is not: an all-`0xffff` read is a floating line far more
    often than it is a slow one, and slowing `clock` will not rescue it.
    Fit the pull-up. Every board should carry one — 1.5–4.7 kΩ to the
    target's rail (i.e. to VREG, not to 3.3 V) — and that belongs in the
    next revision alongside the vreg bleeder.

    Decided for that revision: **one resistor behind a switch**, not a
    permanent one. Permanent to VREG would break the rail measurement in
    both directions — with VREG off the resistor is a path to a dead node,
    with VREG on the probe would be measuring its own rail and could never
    see the target leave. The switch stays open until the rail is known,
    which keeps today's automatic detection exactly as it is and adds the
    case it cannot handle.

    What the firmware owes in return: while the switch is closed the probe
    can no longer tell the rail or whether a target is attached — no passive
    observer can distinguish 1.8 V from 3.3 V on a wire it is holding up
    itself — so that state has to be visible in `diag` and on the LED rather
    than discovered.

11. **How fast MDC can actually go, measured three ways.** These are three
    different limits and only the first belongs to the probe:

    | Limit | Rate | What sets it |
    | :-- | :-- | :-- |
    | Bit loop | ~4.0 MHz | GPIO writes, the shift-and-mask and the cycle-counter spin, at 170 MHz and `-Os` |
    | Clause 22 | 2.5 MHz | IEEE 802.3 §22.2.2.11, the specified MDC maximum |
    | This bench | ~1.8 MHz | an 88E6321 over a short lead stops answering above it |

    The bench figure is a sweep, not an estimate: 1205, 1317, 1556 and
    1762 kHz delivered gave 10/10 correct switch-ID reads each, while
    2096 kHz and above gave 0/10. Nothing in between was tried, so the edge
    is somewhere in that gap. The firmware default is 1500 kHz, which leaves
    margin under it; 300 indirect reads at 1451 kHz delivered ran with no
    mismatches and no errors.

    Since MDIO is open-drain, the third row is a property of the pull-up and
    the wiring rather than of either device, so expect it to move with the
    lead and the resistor. A stronger pull-up is the thing to try if a target
    needs to run faster.

12. **The MDC buffer stays enabled between frames, and MDC is driven while
    it does.** The firmware releases it 20 ms after the last frame rather
    than after each one, so a series of transactions enables it once. While
    it is up, PB7 drives MDC through the buffer and holds it low at idle —
    which means that for those 20 ms the probe owns the clock line and
    another master on the same wire cannot use it.

    Observable without a scope: with the buffer released, the target's
    pull-up wins and `diag` reports `comp MDC: 200/200 high`; immediately
    after a frame it reads lower, because part of the sampling window caught
    the line held low.

    What this buys is the settle. Enabling the buffer is a load step on the
    VREG rail, so it is followed by a 1 ms wait whenever the supply is
    regulating — paying that once per series instead of once per transaction
    is the whole point. In pass-through there is no loop to protect and the
    wait is skipped entirely (measured: a 200-transaction burst leaves the
    rail inside its idle window either way, and the transaction drops from
    1081 µs to 84).

13. **The bench target's "PPU traffic" is the switch polling two external
    PHYs, and one scratch bit decides whether that bus exists.** This was
    used blind through the whole bring-up — `mv scratch modify clear 6 0x63
    0x80` made frames appear and setting the bit again silenced them — so
    the mechanism is worth writing down.

    On the 88E6321, `scratch[0x63]` bit 7 is `NormalSMI` (Functional
    Specification Table 208, p. 334). With P5_MODE not 0x1 or 0x2, the
    P5_COL and P5_CRS pins become MDIO_PHY and MDC_PHY *if the NO_CPU strap
    was one at reset*; clearing the bit inverts the sense of that strap.
    Nothing else routes an external MDIO bus off this part.

    On this bench NO_CPU is 0 (`scratch[0x71] = 0x83`), so the pins carry
    the external bus only with NormalSMI **cleared**. Measured both ways:
    with it set, PHY addresses 5 and 6 read `0xffff`; with it cleared they
    answer (BMSR 0x7949, ID 0x4f51/0xe91a). External PHYs sit on ports 5
    and 6 of this board, so those are the two the PPU polls, and that is
    the traffic the capture chain was verified against.

    Address map, since the same window reaches all of them (spec §9.3):
    internal PHYs of ports 3 and 4 at SMI addresses 3 and 4, internal
    SERDES of ports 0 and 1 at 0x0C and 0x0D, external PHYs at the address
    matching their port number. `mv extbus` and `mv id` print all of this.

    **Two ways to an external PHY, and they are independent.** The SMI PHY
    window reads the PHY's registers directly. The PPU is the other one: it
    polls the PHY whose SMI address matches the port number and publishes
    link/speed/duplex into that port's status register for the MAC. Only
    the second needs the port's `PHYDetect` bit (Port offset 0x00 bit 12),
    and **the PPU's own detection does not always set it** — measured here,
    ports 5 and 6 stayed `no PHY detected` with the bus routed and both
    PHYs answering, through a soft reset as well.

    PHYDetect is the one writable bit in that register and the spec says
    the poll routine follows it, so setting it by hand is the fix
    (`mv port <sw> <ports> detect on`). Measured: port 5 went from
    `0x0e07` — link up, 1000 Mbps, no PHY detected — to `0x1007`, link
    down with the PHY detected, i.e. the PPU replaced a status that had
    not come from a PHY at all with the real one. A Global1 software reset
    re-runs PPU init and rewrites the register, so the setting does not
    survive one.

    One more thing that only the spec says: the switch's SMI PHY window
    (Global2 0x18/0x19) works **only while the PPU is enabled** — with it
    off, SMIBusy self-clears and every read returns `0xffff`. The enable is
    Global1 0x04 bit 14, which the register table calls "Reserved, must be
    set to 1" while the footnote under the SMI PHY register names it. Note
    it is *not* PPUState in Global1 0x00 bit 15: measured with the PPU
    disabled, status went 0xc800 → 0x8800, so bit 15 stayed at "polling".
