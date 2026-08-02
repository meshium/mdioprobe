"""Diagnostics — for working out why something is wrong, not for normal use."""

import mdioprobe

from cli.parser import integer
from cli.registry import command


@command("diag", category="capture", syntax="diag",
         summary="capture-chain counters and front-end settings",
         detail="""
byte_offset is the stream rotation the decoder locked on to; -1 means it is
still looking. It is a property of what is on the wire, not of the session,
so starting a capture re-arms the detection.

The comparator figures are duty over a short sampling window: they tell an
idle line from a busy one, and both from a floating pin.
""")
def cmd_diag(ctx, args):
    d = mdioprobe.diag_get()

    ctx.out.line("capture:   dma_errors {}  overruns {}  torn {}",
                 d["dma_errors"], d["overrun_count"], d["torn_count"])
    ctx.out.line("stream:    byte_offset {}  cndtr {}",
                 d["byte_offset"], d["cur_cndtr"])
    ctx.out.line("front end: threshold {} mV  NSS window {} MDC ticks",
                 d["threshold_mv"], d["nss_window"])
    ctx.out.line("comp MDC:  {}/{} high", d["comp_mdc_high"], d["comp_mdc_total"])
    ctx.out.line("comp MDIO: {}/{} high", d["comp_mdio_high"], d["comp_mdio_total"])

    want, got = d["master_khz"], d["master_actual_khz"]
    ctx.out.line("master:    MDC {} kHz requested, {} delivered",
                 want, "{} kHz".format(got) if got else "not measured yet")

    ctx.out.line("USB CRS:   {}", "synced" if d["usb_crs_synced"] else "not synced")


@command("ring", category="capture", syntax="ring [bytes]",
         summary="raw capture ring, no alignment or validity checks",
         detail="""
The escape hatch for when `cap` returns nothing: this shows what the DMA
actually wrote, so a decoder failure can be told from a capture failure.
Newest bytes last.
""")
def cmd_ring(ctx, args):
    count = integer(args[0], "byte count") if args else 64
    data = mdioprobe.diag_ring_dump(count)

    for start in range(0, len(data), 16):
        chunk = data[start:start + 16]
        ctx.out.line("{:04x}  {}", start,
                     " ".join("{:02x}".format(b) for b in chunk))
