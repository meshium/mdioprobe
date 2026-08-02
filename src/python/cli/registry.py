"""Command registry.

One decorator puts a command in the table, and that is the only place a
command has to be mentioned. The previous project's shell kept a handler in
one file, an export list in a second and a COMMANDS dict in a third, so
adding a command meant editing three places and forgetting one of them was
silent.

Handlers take (ctx, args) and either return or raise CommandError. They do
not print errors and return None: a raised error gets uniform formatting in
the dispatcher, and `exec` stopping on the first failure falls out of it for
free instead of needing its own convention.
"""


class CommandError(Exception):
    """Anything the operator did wrong, or the probe refused to do."""


class Command:
    __slots__ = ("name", "fn", "category", "syntax", "summary", "detail")

    def __init__(self, name, fn, category, syntax, summary, detail):
        self.name = name
        self.fn = fn
        self.category = category
        self.syntax = syntax
        self.summary = summary
        self.detail = detail


COMMANDS = {}

# Order the help output follows; anything else is appended after these.
CATEGORY_ORDER = ("bus", "mdio", "capture", "files", "system")


def command(name, category="mdio", syntax="", summary="", detail=""):
    def register(fn):
        COMMANDS[name] = Command(name, fn, category, syntax or name,
                                 summary, detail)
        return fn
    return register


def lookup(name):
    cmd = COMMANDS.get(name)

    if cmd is not None:
        return cmd

    near = [n for n in COMMANDS if n.startswith(name)]
    if len(near) == 1:
        return COMMANDS[near[0]]
    if near:
        raise CommandError("{} is ambiguous: {}".format(name, ", ".join(sorted(near))))
    raise CommandError("unknown command: {} (try `help`)".format(name))


def by_category():
    """{category: [Command, ...]}, each list sorted by name."""
    groups = {}

    for cmd in COMMANDS.values():
        groups.setdefault(cmd.category, []).append(cmd)
    for cmds in groups.values():
        cmds.sort(key=lambda c: c.name)
    return groups


def ordered_categories(groups):
    known = [c for c in CATEGORY_ORDER if c in groups]
    rest = sorted(c for c in groups if c not in CATEGORY_ORDER)
    return known + rest
