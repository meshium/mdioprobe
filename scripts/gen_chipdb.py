#!/usr/bin/env python3
"""Build the chip identifier database from Linux kernel sources.

The probe reads two registers off a PHY and gets a 32-bit number. Turning
that number into a part name needs a table, and the kernel already maintains
one — spread across `drivers/net/phy/` as `struct phy_driver` initialisers,
and across `drivers/net/dsa/` in a different shape for every vendor.

This script reads those sources and writes `data/chipdb/`. It never needs the
C preprocessor: every identifier in the PHY drivers is either a hex literal or
a plain object-like `#define` of one, with no arithmetic anywhere. What it
does need is brace matching (a `.name` can sit thirty lines from its id) and
expansion of a handful of driver-local macros that generate whole table
entries — without that last step every Broadcom set-top PHY and most of TI
silently vanish, because their entries contain neither `.name` nor `.phy_id`
as text.

Usage:
    scripts/gen_chipdb.py                 # regenerate data/chipdb/
    scripts/gen_chipdb.py --report        # and say what was found and lost
    scripts/gen_chipdb.py --linux ~/src/linux --out data/chipdb
"""

import argparse
import os
import re
import subprocess
import sys

# Headers outside drivers/net/phy/ that PHY ids are defined in. Without these
# the Marvell, Broadcom, Micrel, SMSC, Microchip and Realtek constants do not
# resolve and roughly a third of the table is lost.
EXTRA_HEADERS = (
    "include/linux/marvell_phy.h",
    "include/linux/brcmphy.h",
    "include/linux/micrel_phy.h",
    "include/linux/smscphy.h",
    "include/linux/microchipphy.h",
    "include/net/phy/realtek_phy.h",
)

PHY_DIRS = ("drivers/net/phy",)

# `#define NAME value` where value is a number or another name. Anchored at
# the line start with an explicit space/tab class: `\s+` would let the match
# run across a newline and swallow the next line's token as the value of a
# valueless include guard.
DEFINE_RE = re.compile(
    r"^#define[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]+([^\n\\]+)$", re.M)

FUNC_DEFINE_RE = re.compile(
    r"^#define[ \t]+([A-Za-z_][A-Za-z0-9_]*)\(([^)]*)\)[ \t]*(.*?)(?<!\\)\n",
    re.M | re.S)

GENMASK_RE = re.compile(r"^GENMASK\(\s*(\d+)\s*,\s*(\d+)\s*\)$")
NUMBER_RE = re.compile(r"^(0[xX][0-9a-fA-F]+|\d+)[uUlL]*$")


def read(path):
    with open(path, "r", errors="replace") as handle:
        return handle.read()


def walk_sources(root, dirs, suffixes=(".c", ".h")):
    for base in dirs:
        for here, _subdirs, files in os.walk(os.path.join(root, base)):
            for name in sorted(files):
                if name.endswith(suffixes):
                    yield os.path.join(here, name)


def strip_comments(text):
    """Remove /* */ and // comments, keeping the line count intact.

    Line count matters because the switch extractors report file:line so a
    reader can check a claim against the kernel.
    """
    out = []
    i = 0
    end = len(text)

    while i < end:
        if text.startswith("/*", i):
            close = text.find("*/", i + 2)
            close = end if close < 0 else close + 2
            out.append("\n" * text.count("\n", i, close))
            i = close
        elif text.startswith("//", i):
            close = text.find("\n", i)
            close = end if close < 0 else close
            i = close
        elif text[i] in "\"'":
            quote = text[i]
            j = i + 1
            while j < end and text[j] != quote:
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        else:
            out.append(text[i])
            i += 1

    return "".join(out)


def collect_defines(texts):
    """Object-like `#define`s, resolved to integers where possible."""
    raw = {}

    for text in texts:
        for name, value in DEFINE_RE.findall(text):
            raw.setdefault(name, value.strip())

    resolved = {}

    def resolve(name, seen):
        if name in resolved:
            return resolved[name]
        if name in seen or name not in raw:
            return None

        value = raw[name].strip()
        while value.startswith("(") and value.endswith(")"):
            value = value[1:-1].strip()

        number = NUMBER_RE.match(value)
        if number:
            got = int(number.group(1), 0)
        else:
            mask = GENMASK_RE.match(value)
            if mask:
                high, low = int(mask.group(1)), int(mask.group(2))
                got = ((1 << (high - low + 1)) - 1) << low
            elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value):
                got = resolve(value, seen | {name})
            else:
                got = None

        if got is not None:
            resolved[name] = got
        return got

    for name in raw:
        resolve(name, frozenset())

    return resolved


def value_of(token, defines):
    """An id or mask operand: a literal, a parenthesised one, or a name."""
    token = token.strip()
    while token.startswith("(") and token.endswith(")"):
        token = token[1:-1].strip()

    number = NUMBER_RE.match(token)
    if number:
        return int(number.group(1), 0)
    return defines.get(token)


def match_brace(text, start):
    """Index just past the `}` matching the `{` at `start`."""
    depth = 0

    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def split_elements(body):
    """Top-level elements of an array initialiser body."""
    elements = []
    depth = 0
    start = 0

    for i, char in enumerate(body):
        if char in "{(":
            depth += 1
        elif char in "})":
            depth -= 1
        elif char == "," and depth == 0:
            piece = body[start:i].strip()
            if piece:
                elements.append(piece)
            start = i + 1

    tail = body[start:].strip()
    if tail:
        elements.append(tail)
    return elements


def collect_entry_macros(text):
    """Driver-local macros that expand to a whole table entry.

    Recognised by having both a `.name` and an id field in the body. Four
    files use them — bcm7xxx, dp83848, dp83822, dp83869 — and between them
    they account for 35 entries that carry no `.name` and no `.phy_id` as
    text, so nothing anchored on either would ever see them.
    """
    macros = {}

    for name, params, body in FUNC_DEFINE_RE.findall(text):
        body = body.replace("\\\n", "\n")
        if ".name" not in body:
            continue
        if ".phy_id" not in body and "PHY_ID_MATCH" not in body:
            continue
        macros[name] = ([p.strip() for p in params.split(",") if p.strip()],
                        body)

    return macros


def split_args(text):
    args = []
    depth = 0
    start = 0

    for i, char in enumerate(text):
        if char in "{(":
            depth += 1
        elif char in "})":
            depth -= 1
        elif char == "," and depth == 0:
            args.append(text[start:i].strip())
            start = i + 1

    args.append(text[start:].strip())
    return args


def expand(element, macros):
    """`MACRO(a, b)` -> the macro body with parameters substituted."""
    call = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$", element, re.S)
    if not call:
        return element

    macro = macros.get(call.group(1))
    if macro is None:
        return element

    params, body = macro
    args = split_args(call.group(2))
    if len(args) != len(params):
        return element

    for param, arg in zip(params, args):
        body = re.sub(r"\b{}\b".format(re.escape(param)), arg, body)
    return body


# ---------------------------------------------------------------- switches
#
# There is no DSA equivalent of PHY_ID_MATCH_*. Every vendor family keeps its
# identifiers in a different shape, so each gets its own small extractor, and
# the ones that keep nothing machine-readable are listed by hand below.
#
# A switch record carries how to identify the part, not only its number:
#
#   family  id  prod_mask  rev_mask  addr  reg  access  name
#
# `access` is the cheapest way the identifier can be reached:
#
#   c22    a plain Clause-22 read at `addr`, register `reg` -- nothing written
#   c22w   Clause 22, but the addressing needs writes first
#   spi / i2c / mmio / pci   not on an MDIO bus at all
#
# `addr` and `reg` are `-` where the family is not reachable over MDIO.


class SwitchEntry:
    __slots__ = ("family", "chip_id", "prod_mask", "rev_mask", "addr", "reg",
                 "access", "name")

    def __init__(self, family, chip_id, prod_mask, rev_mask, addr, reg,
                 access, name):
        self.family = family
        self.chip_id = chip_id
        self.prod_mask = prod_mask
        self.rev_mask = rev_mask
        self.addr = addr
        self.reg = reg
        self.access = access
        self.name = name

    def line(self):
        def hexish(value, width):
            return "-" if value is None else "{:0{}x}".format(value, width)

        return "{:<10} {} {} {} {:>4} {:>3} {:<5} {}".format(
            self.family, hexish(self.chip_id, 8), hexish(self.prod_mask, 8),
            hexish(self.rev_mask, 8), hexish(self.addr, 2),
            hexish(self.reg, 2), self.access, self.name)


def table_entries(text, marker):
    """Elements of an initialiser whose declaration contains `marker`."""
    at = text.find(marker)
    if at < 0:
        return []

    open_brace = text.index("{", at)
    close = match_brace(text, open_brace)
    if close < 0:
        return []
    return split_elements(text[open_brace + 1:close - 1])


def field(element, name):
    """`.name = value` out of a designated initialiser element."""
    found = re.search(r"\.{}\s*=\s*([^,\n}}]+)".format(re.escape(name)),
                      element)
    return found.group(1).strip() if found else None


def string_field(element, name):
    found = re.search(r"\.{}\s*=\s*\(?\s*\"([^\"]*)\"".format(
        re.escape(name)), element)
    return found.group(1) if found else None


class PhyEntry:
    __slots__ = ("phy_id", "mask", "name", "source")

    def __init__(self, phy_id, mask, name, source):
        self.phy_id = phy_id
        self.mask = mask
        self.name = name
        self.source = source


def phy_id_masks(root):
    """The three PHY_ID_MATCH_* masks, read rather than assumed."""
    text = read(os.path.join(root, "include/linux/phy.h"))
    defines = collect_defines([text])

    masks = {
        # The kernel's own spelling of EXACT has a typo. Accept both so this
        # keeps working whichever way it is eventually fixed.
        "EXACT": defines.get("PHY_ID_MATCH_EXTACT_MASK",
                             defines.get("PHY_ID_MATCH_EXACT_MASK")),
        "MODEL": defines.get("PHY_ID_MATCH_MODEL_MASK"),
        "VENDOR": defines.get("PHY_ID_MATCH_VENDOR_MASK"),
    }

    missing = [key for key, value in masks.items() if value is None]
    if missing:
        raise SystemExit(
            "cannot read PHY_ID_MATCH masks from include/linux/phy.h: "
            "{}".format(", ".join(missing)))
    return masks


def extract_phy(root):
    """Every `struct phy_driver` entry that carries a static identifier."""
    texts = []
    sources = []

    for path in walk_sources(root, PHY_DIRS):
        texts.append(strip_comments(read(path)))
        sources.append((path, texts[-1]))

    for header in EXTRA_HEADERS:
        full = os.path.join(root, header)
        if os.path.exists(full):
            texts.append(strip_comments(read(full)))

    texts.append(strip_comments(read(os.path.join(root, "include/linux/phy.h"))))

    defines = collect_defines(texts)
    masks = phy_id_masks(root)

    entries = []
    skipped = []
    total = 0

    # Arrays, and the two generic drivers in phy_device.c that are declared
    # as single structs. Neither of those has an identifier -- they are the
    # wildcards phylib falls back on -- but the report should say so rather
    # than not know they exist.
    array_re = re.compile(r"struct\s+phy_driver\s+\w+\s*\[\s*\]\s*=\s*\{")
    single_re = re.compile(r"struct\s+phy_driver\s+\w+\s*=\s*\{")

    for path, text in sources:
        macros = collect_entry_macros(text)
        rel = os.path.relpath(path, root)

        for array in array_re.finditer(text):
            open_brace = text.index("{", array.end() - 1)
            close = match_brace(text, open_brace)
            if close < 0:
                continue

            for element in split_elements(text[open_brace + 1:close - 1]):
                total += 1
                entry, why = parse_phy_element(element, macros, defines,
                                               masks, rel)
                if entry is None:
                    skipped.append((rel, why))
                else:
                    entries.append(entry)

        for single in single_re.finditer(text):
            open_brace = text.index("{", single.end() - 1)
            close = match_brace(text, open_brace)
            if close < 0:
                continue

            total += 1
            entry, why = parse_phy_element(text[open_brace:close], macros,
                                           defines, masks, rel)
            if entry is None:
                skipped.append((rel, why))
            else:
                entries.append(entry)

    return entries, skipped, total


def parse_phy_element(element, macros, defines, masks, source):
    body = expand(element, macros)

    # The parentheses are not decoration: dp83822 and dp83869 write
    # `.name = (_name)`, and nine entries go missing without them.
    name = re.search(r"\.name\s*=\s*\(?\s*\"([^\"]*)\"", body)
    name = name.group(1) if name else None

    phy_id = mask = None

    match = re.search(r"PHY_ID_MATCH_(EXACT|MODEL|VENDOR)\s*\(([^)]*)\)", body)
    if match:
        phy_id = value_of(match.group(2), defines)
        mask = masks[match.group(1)]
    else:
        got_id = re.search(r"\.phy_id\s*=\s*([^,\n]+)", body)
        got_mask = re.search(r"\.phy_id_mask\s*=\s*([^,\n]+)", body)
        if got_id:
            phy_id = value_of(got_id.group(1), defines)
        if got_mask:
            mask = value_of(got_mask.group(1), defines)

    if phy_id is None or mask is None:
        if name is None:
            return None, "no name and no identifier"
        if re.search(r"\.match_phy_device", body):
            return None, "{}: identified by match_phy_device()".format(name)
        return None, "{}: identifier did not resolve".format(name)

    if name is None:
        return None, "identifier 0x{:08x} has no name".format(phy_id)

    # The two generic drivers carry 0xffffffff/0xffffffff. That is a
    # sentinel, not an identifier -- phylib picks them by falling back, never
    # by matching. It also happens to be exactly what an idle bus reads back,
    # so letting it into the table would name every empty address
    # "Generic PHY", which is the one answer worse than saying nothing.
    if phy_id == 0xFFFFFFFF and mask == 0xFFFFFFFF:
        return None, "{}: 0xffffffff is the fallback sentinel".format(name)

    return PhyEntry(phy_id, mask, name, source), None


ENUM_VALUE_RE = re.compile(
    r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(0[xX][0-9a-fA-F]+|\d+)\s*,?\s*$", re.M)


def collect_enum_values(text):
    """`NAME = 0x1234,` inside an enum.

    b53 and ksz keep their chip numbers in enums rather than #defines, so
    the define collector alone sees nothing at all for either family.
    """
    return {name: int(value, 0)
            for name, value in ENUM_VALUE_RE.findall(text)}


def source(root, *parts):
    path = os.path.join(root, *parts)
    return strip_comments(read(path)) if os.path.exists(path) else ""


def sw_mv88e6xxx(root):
    port_h = source(root, "drivers/net/dsa/mv88e6xxx/port.h")
    chip_c = source(root, "drivers/net/dsa/mv88e6xxx/chip.c")
    names = collect_defines([port_h])

    reg = names.get("MV88E6XXX_PORT_SWITCH_ID")
    prod_mask = names.get("MV88E6XXX_PORT_SWITCH_ID_PROD_MASK")
    rev_mask = names.get("MV88E6XXX_PORT_SWITCH_ID_REV_MASK")

    out = []
    for element in table_entries(chip_c, "mv88e6xxx_info mv88e6xxx_table[]"):
        prod = field(element, "prod_num")
        name = string_field(element, "name")
        base = field(element, "port_base_addr")
        if prod is None or name is None:
            continue

        chip_id = value_of(prod, names)
        if chip_id is None:
            continue

        # The address to poll depends on which chip it is, which is the
        # chicken-and-egg the kernel resolves by trying 0x10 first. Carrying
        # port_base_addr per entry is what lets a reader see that the 6250
        # family sits at 0x08 instead.
        out.append(SwitchEntry("mv88e6xxx", chip_id, prod_mask, rev_mask,
                               value_of(base, names) if base else None,
                               reg, "c22", name))
    return out


def sw_b53(root):
    priv_h = source(root, "drivers/net/dsa/b53/b53_priv.h")
    common = source(root, "drivers/net/dsa/b53/b53_common.c")
    names = collect_enum_values(priv_h)

    out = []
    for element in table_entries(common, "b53_chip_data b53_switch_chips[]"):
        chip = field(element, "chip_id")
        name = string_field(element, "dev_name")
        if chip is None or name is None:
            continue
        chip_id = value_of(chip, names)
        if chip_id is None:
            continue
        # Page 0x02 register 0x30, reached through the pseudo-PHY at address
        # 30 -- which needs the page written first, hence c22w.
        out.append(SwitchEntry("b53", chip_id, 0xFFFFFFFF, 0, 30, 0x30,
                               "c22w", name))
    return out


def sw_ksz(root):
    header = source(root, "include/linux/platform_data/microchip-ksz.h")
    common = source(root, "drivers/net/dsa/microchip/ksz_common.c")
    names = collect_enum_values(header)

    out = []
    for element in table_entries(common, "ksz_chip_data ksz_switch_chips[]"):
        chip = field(element, "chip_id")
        name = string_field(element, "dev_name")
        if chip is None or name is None:
            continue
        chip_id = value_of(chip, names)
        if chip_id is None:
            continue

        # Only the KSZ8863/8873 pair speaks anything MDIO-shaped, and there
        # the register number picks the PHY address, so a read needs no
        # writes. Everything else in the family is SPI or I2C.
        if names.get("KSZ88X3_CHIP_ID") == chip_id:
            # The KSZ8863/8873 SMI is byte-wide (ksz8863_smi.c:25): the
            # register number picks the MDIO address, and one read returns
            # one byte. So a single Clause-22 read at 0x10/0x00 yields the
            # family byte 0x88 and nothing more -- which is also all the
            # kernel switches on (ksz_common.c, case KSZ88_FAMILY_ID).
            out.append(SwitchEntry("ksz", 0x88, 0xFF, 0, 0x10, 0x00,
                                   "c22", name + " (family byte)"))
        else:
            out.append(SwitchEntry("ksz", chip_id, 0xFFFFFF00, 0xFF, None,
                                   0x00, "spi", name))
    return out


def sw_rtl8365mb(root):
    text = source(root, "drivers/net/dsa/realtek/rtl8365mb.c")

    out = []
    for element in table_entries(text, "rtl8365mb_chip_info rtl8365mb_chip_infos[]"):
        name = string_field(element, "name")
        chip = field(element, "chip_id")
        ver = field(element, "chip_ver")
        if name is None or chip is None:
            continue
        chip_id = value_of(chip, {})
        if chip_id is None:
            continue
        # All three parts answer 0x6367; only chip_ver at 0x1301 separates
        # them, so the version goes in the name rather than being dropped.
        if ver is not None:
            name = "{} (ver {})".format(name, ver)
        out.append(SwitchEntry("rtl8365mb", chip_id, 0xFFFFFFFF, 0, None,
                               0x00, "c22w", name))
    return out


def sw_sja1105(root):
    text = source(root, "drivers/net/dsa/sja1105/sja1105_spi.c")
    header = source(root, "drivers/net/dsa/sja1105/sja1105_static_config.h")
    names = collect_defines([header])

    out = []
    for block in re.finditer(r"struct\s+sja1105_info\s+\w+\s*=\s*\{", text):
        open_brace = text.index("{", block.end() - 1)
        close = match_brace(text, open_brace)
        if close < 0:
            continue
        element = text[open_brace:close]

        name = string_field(element, "name")
        device = field(element, "device_id")
        part = field(element, "part_no")
        if name is None or device is None:
            continue
        chip_id = value_of(device, names)
        if chip_id is None:
            continue
        # device_id alone is not unique: P and R share one, Q and S another,
        # and all four SJA1110 variants a third. part_no is the other half.
        if part is not None:
            part_value = value_of(part, names)
            if part_value is not None:
                name = "{} (part 0x{:04x})".format(name, part_value)
        out.append(SwitchEntry("sja1105", chip_id, 0xFFFFFFFF, 0, None, None,
                               "spi", name))
    return out


def sw_yt921x(root):
    text = source(root, "drivers/net/dsa/yt921x.c")
    header = source(root, "drivers/net/dsa/yt921x.h")
    names = collect_defines([text, header])

    out = []
    for element in table_entries(text, "yt921x_info yt921x_infos[]"):
        # Positional, not designated: { "YT9215SC", YT9215_MAJOR, 1, 0, ... }
        parts = split_args(element.strip().lstrip("{").rstrip("}"))
        if len(parts) < 2 or not parts[0].startswith("\""):
            continue
        name = parts[0].strip().strip("\"")
        chip_id = value_of(parts[1], names)
        if chip_id is None:
            continue
        # major alone covers five parts; chip_mode and an EEPROM word pick
        # the exact one, which a read-only probe cannot do.
        out.append(SwitchEntry("yt921x", chip_id, 0xFFFF0000, 0, None, None,
                               "c22w", name))
    return out


def sw_xrs700x(root):
    text = source(root, "drivers/net/dsa/xrs700x/xrs700x.c")
    names = collect_defines([text])

    out = []
    for block in re.finditer(
            r"struct\s+xrs700x_info\s+\w+\s*=\s*\{([^}]*)\}", text):
        parts = split_args(block.group(1))
        if len(parts) < 2:
            continue
        chip_id = value_of(parts[0], names)
        name = parts[1].strip().strip("\"")
        if chip_id is None or not name:
            continue
        out.append(SwitchEntry("xrs700x", chip_id, 0xFFFFFFFF, 0, None, 0x00,
                               "c22w", name))
    return out


def sw_ks8995(root):
    text = source(root, "drivers/net/dsa/ks8995.c")
    # Deliberately file-local: KSZ8795_CHIP_ID is 0x09 here and 0x8795 in
    # microchip-ksz.h. A tree-wide map by macro name produces nonsense.
    names = collect_defines([text])

    out = []
    for element in table_entries(text, "ks8995_chip_params ks8995_chip[]"):
        name = string_field(element, "name")
        chip = field(element, "chip_id")
        if name is None or chip is None:
            continue
        chip_id = value_of(chip, names)
        if chip_id is None:
            continue
        out.append(SwitchEntry("ks8995", chip_id, 0xFF, 0, None, 0x00,
                               "spi", name))
    return out


# Families that keep nothing a script can read: the number is a #define whose
# only human-readable name is the macro identifier, or a printf format. Each
# line names where it came from so it can be checked.
CURATED = (
    # drivers/net/dsa/mv88e6060.h:26, mv88e6060.c:27 -- addr 0x08, reg 0x03
    ("mv88e6060", 0x0600, 0xFFF0, 0x000F, 0x08, 0x03, "c22", "Marvell 88E6060"),
    # drivers/net/dsa/vitesse-vsc73xx-core.c:333, id = (val >> 12) & 0xffff
    ("vsc73xx", 0x7385, 0xFFFF, 0xF, None, None, "spi", "VSC7385"),
    ("vsc73xx", 0x7388, 0xFFFF, 0xF, None, None, "spi", "VSC7388"),
    ("vsc73xx", 0x7395, 0xFFFF, 0xF, None, None, "spi", "VSC7395"),
    ("vsc73xx", 0x7398, 0xFFFF, 0xF, None, None, "spi", "VSC7398"),
    # drivers/net/dsa/lan9303-core.c:28 -- LAN9303_CHIP_REV is register 0x14
    # of the chip, not of the bus. lan9303_mdio.c:17 turns that into a byte
    # offset (reg << 2 = 0x50) and then into an address/register pair,
    # PHY_ADDR(x) = ((x >> 6) + 0x10) & 0x1f and PHY_REG(x) = (x >> 1) & 0x1f.
    # The identifier is the high half (id = val >> 16), which is offset 0x52
    # -- MDIO address 0x11, register 0x09. Reading it takes no writes.
    # Five parts are defined; the probe accepts only the 9303 and the 9354.
    ("lan9303", 0x9303, 0xFFFF, 0, 0x11, 0x09, "c22", "LAN9303"),
    ("lan9303", 0x9352, 0xFFFF, 0, 0x11, 0x09, "c22", "LAN9352 (not probed)"),
    ("lan9303", 0x9353, 0xFFFF, 0, 0x11, 0x09, "c22", "LAN9353 (not probed)"),
    ("lan9303", 0x9354, 0xFFFF, 0, 0x11, 0x09, "c22", "LAN9354"),
    ("lan9303", 0x9355, 0xFFFF, 0, 0x11, 0x09, "c22", "LAN9355 (not probed)"),
    # drivers/net/dsa/mt7530.h:706 -- CREV 0x7ffc / 0x781c, id = val >> 16
    ("mt7530", 0x7530, 0xFFFF, 0x0F, None, 0x7F, "c22w", "MT7530"),
    ("mt7530", 0x7531, 0xFFFF, 0x0F, None, 0x78, "c22w", "MT7531"),
    # drivers/net/dsa/qca/qca8k.h:27 -- MASK_CTRL 0x000, id bits 15:8
    ("qca8k", 0x12, 0xFF00, 0xFF, 0x18, 0x00, "c22w", "QCA8327/QCA8328"),
    ("qca8k", 0x13, 0xFF00, 0xFF, 0x18, 0x00, "c22w", "QCA8334/QCA8337"),
    # drivers/net/dsa/realtek/rtl8366rb.c:136 -- reg 0x0509. The driver
    # prints "RTL%04x" of 0x5937, which names a part that does not exist.
    ("rtl8366rb", 0x5937, 0xFFFF, 0x0F, None, None, "c22w", "RTL8366RB"),
    # drivers/net/dsa/hirschmann/hellcreek.c:2085 -- an FPGA module id
    ("hellcreek", 0x4C30, 0xFFFF, 0, None, None, "mmio", "Hirschmann Hellcreek"),
)


def extract_switch(root):
    entries = []

    for extractor in (sw_mv88e6xxx, sw_b53, sw_ksz, sw_rtl8365mb, sw_sja1105,
                      sw_yt921x, sw_xrs700x, sw_ks8995):
        entries.extend(extractor(root))

    for row in CURATED:
        entries.append(SwitchEntry(*row))

    return entries


def write_switch(path, entries, version, commit):
    entries = sorted(entries, key=lambda e: (e.family, e.chip_id, e.name))
    lines = [
        "# DSA switch identifiers, extracted from Linux {} ({})".format(
            version, commit),
        "# by scripts/gen_chipdb.py -- do not edit by hand.",
        "#",
        "# family     id       prod     rev      addr reg access name",
        "#",
        "# Unlike PHYs there is no common scheme, so every record carries how",
        "# to identify the part: the MDIO address and register to read, the",
        "# mask that isolates the product number, the mask that isolates the",
        "# revision, and what access it takes:",
        "#",
        "#   c22   a plain Clause-22 read -- nothing is written to the bus",
        "#   c22w  Clause 22, but the addressing needs writes first",
        "#   spi / i2c / mmio / pci   not reachable over MDIO at all",
        "#",
        "# Where one number covers several parts, the discriminator the",
        "# kernel uses is spelled out in the name in parentheses.",
    ]

    for entry in entries:
        lines.append(entry.line())

    body = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(body)
    return len(body)


# ---------------------------------------------------- the frozen module
#
# The database goes into the firmware as `src/python/cli/chipdb.py`, which
# `package("cli", ...)` in micropython_manifest.py picks up without an edit.
#
# It holds two string constants and nothing else. Frozen, a string literal
# lives in flash and is used in place, so importing costs about the module's
# own dictionary — measured at 1696 bytes for a comparable module against
# 8560 for the same thing loaded off the volume. A dict
# or a list of tuples would have to be built on the heap at import instead,
# which throws the whole saving away.
#
# Records are fixed-width so a lookup can slice the two hex fields out
# without splitting the line, and the line out without splitting the table.

PHY_RECORD = "{:08x} {:08x} {}"
SWITCH_RECORD = "{:08x} {:08x} {:08x} {:>2} {:>2} {:<4} {:<10} {}"

MODULE_HEADER = '''"""Chip identifiers, generated by scripts/gen_chipdb.py — do not edit.

Extracted from Linux {version} ({commit}).

Two fixed-width tables, one record per line. Frozen into the firmware, both
strings live in flash and are read in place.

PHY, {phy_count} records:

    0        9        18
    |        |        |
    iiiiiiii mmmmmmmm name
    id       mask

A part matches when (read_id ^ id) & mask == 0, which is what
phy_id_compare() does in include/linux/phy.h.

SWITCH, {switch_count} records:

    0        9        18       27 30 33   38         49
    |        |        |        |  |  |    |          |
    iiiiiiii pppppppp rrrrrrrr aa gg cccc family     name
    id       prod     rev      addr  access

`aa` and `gg` are the MDIO address and register to read, `--` where the part
is not on an MDIO bus at all. `cccc` is c22 for a plain read, c22w when the
addressing needs writes first, otherwise the bus it does live on.
"""

KERNEL = "{version} ({commit})"

PHY_STRIDE = 18
SWITCH_NAME_AT = 49
'''


def write_module(path, phy, switches, version, commit):
    phy = sorted(phy, key=lambda e: (e.phy_id, e.mask, e.name))
    switches = sorted(switches, key=lambda e: (e.family, e.chip_id, e.name))

    out = [MODULE_HEADER.format(version=version, commit=commit,
                                phy_count=len(phy),
                                switch_count=len(switches))]

    out.append("\nPHY = (")
    for entry in phy:
        out.append('    "{}\\n"'.format(
            PHY_RECORD.format(entry.phy_id, entry.mask, entry.name)))
    out.append(")\n")

    out.append("SWITCH = (")
    for entry in switches:
        out.append('    "{}\\n"'.format(SWITCH_RECORD.format(
            entry.chip_id, entry.prod_mask, entry.rev_mask,
            _hexish(entry.addr, 2), _hexish(entry.reg, 2),
            entry.access, entry.family, entry.name)))
    out.append(")")

    body = "\n".join(out) + "\n"
    with open(path, "w") as handle:
        handle.write(body)
    return len(body)


def _hexish(value, width):
    return "-" * width if value is None else "{:0{}x}".format(value, width)


def kernel_version(root):
    makefile = read(os.path.join(root, "Makefile"))
    fields = {}

    for key in ("VERSION", "PATCHLEVEL", "SUBLEVEL", "EXTRAVERSION"):
        # `[ \t]*` and not `\s*`: EXTRAVERSION is empty in a release
        # tree, and `\s*` would step over the newline and take the next
        # line's value -- which is how the version came out as
        # "7.1.0NAME = Baby Opossum Posse".
        found = re.search(r"^{}[ \t]*=[ \t]*(.*)$".format(key), makefile, re.M)
        fields[key] = found.group(1).strip() if found else ""

    version = "{VERSION}.{PATCHLEVEL}.{SUBLEVEL}{EXTRAVERSION}".format(**fields)

    try:
        commit = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        commit = "unknown"

    return version, commit


def write_phy(path, entries, version, commit):
    entries = sorted(entries, key=lambda e: (e.phy_id, e.mask, e.name))
    lines = [
        "# PHY identifiers, extracted from Linux {} ({})".format(version, commit),
        "# by scripts/gen_chipdb.py -- do not edit by hand.",
        "#",
        "# id       mask     name",
        "# A part matches when (read_id ^ id) & mask == 0, which is what",
        "# phy_id_compare() does in include/linux/phy.h.",
    ]

    for entry in entries:
        lines.append("{:08x} {:08x} {}".format(entry.phy_id, entry.mask,
                                               entry.name))

    body = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(body)
    return len(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--linux", default=os.path.expanduser("~/src/linux"))
    ap.add_argument("--out", default="data/chipdb")
    ap.add_argument("--module", default="src/python/cli/chipdb.py",
                    help="the frozen module to write; - to skip")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(os.path.join(args.linux, "drivers/net/phy")):
        raise SystemExit("no kernel tree at {}".format(args.linux))

    version, commit = kernel_version(args.linux)
    entries, skipped, total = extract_phy(args.linux)

    size = write_phy(os.path.join(args.out, "phy.txt"), entries, version, commit)

    switches = extract_switch(args.linux)
    sw_size = write_switch(os.path.join(args.out, "switch.txt"), switches,
                           version, commit)

    print("linux {} ({})".format(version, commit))
    print("phy:    {} of {} entries, {} bytes".format(len(entries), total, size))
    print("switch: {} entries, {} bytes".format(len(switches), sw_size))
    print("total:  {} bytes".format(size + sw_size))

    if args.module != "-":
        module = write_module(args.module, entries, switches,
                              version, commit)
        print("frozen: {} ({} bytes of source)".format(
            args.module, module))

    if args.report:
        distinct = len({(e.phy_id & e.mask, e.mask) for e in entries})
        print()
        print("phy: {} distinct (id & mask, mask) pairs".format(distinct))
        print("phy: {} entries carry no static identifier:".format(len(skipped)))
        for where, why in sorted(skipped):
            print("       {:<44} {}".format(where, why))

        families = {}
        readable = 0
        for entry in switches:
            families[entry.family] = families.get(entry.family, 0) + 1
            if entry.access == "c22":
                readable += 1
        print()
        print("switch: by family")
        for family in sorted(families):
            print("       {:<12} {}".format(family, families[family]))
        print("switch: {} of {} identifiable by a plain read".format(
            readable, len(switches)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
