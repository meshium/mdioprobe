"""Loading command modules from the volume.

The registry is a plain dict and `/flash/lib` is on `sys.path`, so a module
there can register commands with the same `@command` decorator the built-in
ones use — nothing here makes that possible, it already was. What this adds
is the bookkeeping: which module contributed which commands, so a reload can
take the old ones away first.

Without that, editing a helper and loading it again leaves the previous
definitions in place. A renamed command keeps answering under both names,
and a deleted one never goes away — the sort of thing that wastes an hour
before anyone suspects the loader rather than the code.
"""

import gc
import sys

from cli.registry import COMMANDS

# {module name: [command names it registered]}
LOADED = {}


def unload(name):
    """Take back whatever `name` registered. Returns the names removed."""
    gone = LOADED.pop(name, [])

    for command_name in gone:
        COMMANDS.pop(command_name, None)
    sys.modules.pop(name, None)
    return gone


def load(name):
    """Import (or re-import) `name` and return the commands it added.

    Raises whatever the module raises. That is deliberate: a helper is the
    operator's own code, and for their own code the traceback is the useful
    answer, not a tidied-up message.
    """
    unload(name)

    # Importing is the largest allocation the CLI ever makes — compiling a
    # source helper needs several kilobytes contiguous. Collecting first is
    # what decides whether a second helper fits at all.
    gc.collect()

    before = set(COMMANDS)
    try:
        __import__(name)
    except Exception:
        # A module that threw half way through may still have registered
        # some commands. Leave them out of LOADED and take them back, so a
        # failed load does not leave a partial command set behind.
        for command_name in set(COMMANDS) - before:
            COMMANDS.pop(command_name, None)
        sys.modules.pop(name, None)
        raise

    added = sorted(set(COMMANDS) - before)
    LOADED[name] = added
    return added
