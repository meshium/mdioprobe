"""The one place the CLI talks to the MDIO bus.

Everything protocol-shaped — register reads and writes now, and the indirect
addressing modes and vendor helpers that come next — goes through here
rather than importing the builtin module directly. That is what makes the
next batch cheap: retarget this file and the layers above it keep working.
It also gives tracing somewhere to live without patching a builtin module,
which cannot be monkey-patched anyway.
"""

import mdioprobe

_trace = False


def set_trace(on):
    global _trace
    _trace = bool(on)


def tracing():
    return _trace


def read(phy, reg):
    # Trace the attempt before it can fail: a read that gets no answer is
    # exactly the case worth seeing traced, and reporting it only on success
    # would hide it.
    if _trace:
        print("  R phy={:02d} reg={:02d}".format(phy, reg), end="")
    try:
        value = mdioprobe.c22_read(phy, reg)
    except OSError:
        if _trace:
            print(" -> no answer")
        raise
    if _trace:
        print(" -> 0x{:04x}".format(value))
    return value


def write(phy, reg, value):
    if _trace:
        print("  W phy={:02d} reg={:02d} <- 0x{:04x}".format(phy, reg, value))
    mdioprobe.c22_write(phy, reg, value)
