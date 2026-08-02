"""Turning a typed line into arguments.

Pure string handling — nothing here touches the probe, which is why it is
the one part of the previous project's CLI worth keeping almost as it was.
The change is that failures raise CommandError instead of printing a message
and returning, so the dispatcher formats every error the same way.
"""

from cli.registry import CommandError

_QUOTES = "\"'"


def tokenize(line):
    """Split on whitespace, honouring quotes so paths may contain spaces."""
    tokens = []
    cur = ""
    quote = None
    have = False

    for ch in line:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in _QUOTES:
            quote = ch
            have = True
        elif ch in " \t":
            if have or cur:
                tokens.append(cur)
                cur = ""
                have = False
        else:
            cur += ch
    if quote:
        raise CommandError("unbalanced {} quote".format(quote))
    if have or cur:
        tokens.append(cur)
    return tokens


def split_redirect(line):
    """Peel a trailing `> path` or `>> path` off the line.

    Returns (line, path, append). Only looks outside quotes, so a path or a
    value containing '>' survives.
    """
    quote = None

    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in _QUOTES:
            quote = ch
        elif ch == ">":
            append = line[i + 1:i + 2] == ">"
            path = line[i + (2 if append else 1):].strip()
            if not path:
                raise CommandError("redirection needs a file name")
            return line[:i].strip(), path, append
    return line, None, False


def integer(token, what="value"):
    """Decimal, or hex with an 0x prefix."""
    text = token.strip()

    try:
        if text[:2].lower() == "0x":
            return int(text[2:], 16)
        if text[:3].lower() == "-0x":
            return -int(text[3:], 16)
        return int(text, 10)
    except (ValueError, IndexError):
        raise CommandError("{} is not a number: {}".format(what, token))


def ranged(token, low, high, what="value"):
    value = integer(token, what)

    if value < low or value > high:
        raise CommandError("{} out of range {}..{}: {}".format(what, low, high, value))
    return value


def phy(token):
    return ranged(token, 0, 31, "PHY address")


def reg(token):
    return ranged(token, 0, 31, "register")


def u16(token):
    return ranged(token, 0, 0xFFFF, "16-bit value")


def spec(token, low=0, high=31, what="register"):
    """Expand "0", "0-7", "0,2-4,7" into a list, in the order written."""
    out = []

    for part in token.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part[1:]:
            cut = part.index("-", 1)
            first = ranged(part[:cut], low, high, what)
            last = ranged(part[cut + 1:], low, high, what)
            if first > last:
                raise CommandError("{} range runs backwards: {}".format(what, part))
            out.extend(range(first, last + 1))
        else:
            out.append(ranged(part, low, high, what))
    if not out:
        raise CommandError("empty {} specification".format(what))
    return out
