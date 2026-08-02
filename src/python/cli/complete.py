"""Tab completion for the command line.

Called from C with the whole line and the cursor at the end of it; returns
(insert, listing). `insert` is what to add at the cursor — the longest common
prefix of the candidates, minus what has already been typed. `listing` is a
block of candidates to print when there is more than one, or None.

Three things get completed, in the order a line is read: the command name,
then whatever keywords that command takes in that position, then paths.
Paths are also completed after a redirection, which is where a long filename
is most annoying to retype.
"""

import os

from cli import parser
from cli.registry import COMMANDS

# Keywords each command takes, by argument position (0 = first argument).
# Only the ones from a fixed set are here; addresses and register numbers
# obviously cannot be completed.
_MODES = ("page", "mvpage", "extpage", "mmd", "rdb", "extreg")

_KEYWORDS = {
    "read":    {0: _MODES},
    "write":   {0: _MODES},
    "monitor": {0: _MODES},
    "modify":  {0: ("set", "clear"), 1: _MODES},
    "mv":      {0: ("reg", "phy", "scratch"),
                1: ("read", "write", "modify", "monitor")},
    "probe":   {0: ("mmd",)},
    "trace":   {0: ("on", "off")},
    "mode":    {0: ("mdio", "uart", "i2c")},
    "bus":     {0: ("force", "auto")},
    "vreg":    {0: ("off", "reset")},
    "cap":     {1: ("decode",)},
    "format":  {0: ("yes",)},
    "help":    {0: None},          # None means "the command names"
}

# Where `use` looks for command modules.
_LIB = "/flash/lib"

# Commands whose arguments are paths, and which of those arguments. `ls`
# lists two because of its `-a`, which pushes the path along one place.
_PATHS = {
    "ls": (0, 1), "cd": (0,), "cat": (0,), "rm": (0,), "mkdir": (0,),
    "exec": (0,), "script": (0,), "edit": (0,), "df": (0,),
    "copy": (0, 1), "move": (0, 1),
}


def _common_prefix(names):
    prefix = names[0]

    for name in names[1:]:
        while not name.startswith(prefix):
            prefix = prefix[:-1]
    return prefix


def _result(candidates, typed, suffix=" "):
    """Turn a candidate list into what C needs.

    A single match is completed and closed off with `suffix`; several share
    their common prefix and are listed. Nothing matching inserts nothing,
    which shows up as a Tab that does not move — the usual signal.
    """
    if not candidates:
        return "", None

    candidates = sorted(candidates)
    if len(candidates) == 1:
        return candidates[0][len(typed):] + suffix, None

    shared = _common_prefix(candidates)
    listing = "  ".join(candidates)

    return shared[len(typed):], listing


def _paths(typed):
    """Complete a filename. The directory part is taken as written."""
    cut = typed.rfind("/")
    directory = typed[:cut + 1] if cut >= 0 else ""
    stem = typed[cut + 1:] if cut >= 0 else typed

    try:
        entries = os.ilistdir(directory if directory else ".")
    except OSError:
        return "", None

    names = []
    for entry in entries:
        name = entry[0]
        if not name.startswith(stem):
            continue
        names.append(directory + name + ("/" if entry[1] & 0x4000 else ""))

    if not names:
        return "", None

    # A directory completes to its slash and stays open for the next segment;
    # a file gets the usual trailing space.
    if len(names) == 1 and names[0].endswith("/"):
        return names[0][len(typed):], None
    return _result(names, typed)


def _redirect_target(line):
    """If the cursor sits in a `> path`, return that partial path."""
    cut = line.rfind(">")

    if cut < 0:
        return None
    tail = line[cut + 1:]
    if tail.startswith(">"):
        tail = tail[1:]
    if " " in tail.strip():
        return None                # already a complete word plus something
    return tail.lstrip()


def complete(line):
    """The entry point C calls. Never raises — a completer that throws would
    take the command line with it."""
    try:
        return _complete(line)
    except Exception:
        return "", None


def _modules(typed):
    """Complete a module name for `use`, from the files in /flash/lib."""
    names = []

    try:
        entries = os.ilistdir(_LIB)
    except OSError:
        return "", None

    for entry in entries:
        name = entry[0]
        if name.endswith(".py") and name[:-3].startswith(typed):
            names.append(name[:-3])
    return _result(names, typed)


def _complete(line):
    target = _redirect_target(line)
    if target is not None:
        return _paths(target)

    # Splitting for completion is not the same as splitting to run: a
    # trailing space means "start of the next argument", which tokenize()
    # cannot express because it drops it.
    tokens = parser.tokenize(line)
    fresh = line.endswith(" ") or line == ""

    if fresh:
        tokens = tokens + [""]
    if not tokens:
        tokens = [""]

    if len(tokens) == 1:
        typed = tokens[0]
        return _result([n for n in COMMANDS if n.startswith(typed)], typed)

    name = tokens[0]
    typed = tokens[-1]
    position = len(tokens) - 2

    if name == "use" and position == 0:
        return _modules(typed)

    if name in _PATHS and position in _PATHS[name]:
        return _paths(typed)

    words = _KEYWORDS.get(name, {}).get(position)
    if words is None and name in _KEYWORDS and position in _KEYWORDS[name]:
        words = tuple(COMMANDS)
    if not words:
        return "", None

    return _result([w for w in words if w.startswith(typed)], typed)
