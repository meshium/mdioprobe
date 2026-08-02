"""help, exit, version, and running commands from a file."""

import mdioprobe

from cli.registry import CommandError, by_category, command, lookup, ordered_categories


def _print_detail(ctx, text):
    """Print a help text one line at a time.

    Deliberately not `text.strip().split("\\n")`. Detail strings live in
    flash as part of the frozen module; strip() copies the whole thing onto
    the heap and split() then builds a list of every line on top of that. On
    a 32 KB GC heap the longest of them (`mv`, ~2 KB) failed outright with
    MemoryError. Walking the string and slicing one line at a time keeps the
    largest allocation down to a single line.
    """
    start = 0
    end = len(text)

    while start < end and text[start] == "\n":
        start += 1
    while end > start and text[end - 1] == "\n":
        end -= 1

    while start < end:
        cut = text.find("\n", start, end)
        if cut < 0:
            cut = end
        ctx.out.line("{}", text[start:cut])
        start = cut + 1


@command("help", category="system", syntax="help [command]",
         summary="list commands, or explain one")
def cmd_help(ctx, args):
    if args:
        cmd = lookup(args[0])
        ctx.out.line("{}", cmd.syntax)
        if cmd.summary:
            ctx.out.line("  {}", cmd.summary)
        if cmd.detail:
            ctx.out.line("")
            _print_detail(ctx, cmd.detail)
        return

    groups = by_category()
    for category in ordered_categories(groups):
        ctx.out.line("")
        ctx.out.line("{}:", category)
        for cmd in groups[category]:
            ctx.out.line("  {:<10} {}", cmd.name, cmd.summary)


@command("exit", category="system", syntax="exit",
         summary="leave the shell for the Python REPL")
def cmd_exit(ctx, args):
    ctx.shell.running = False
    ctx.out.line("dropping to the REPL; `import cli.shell; cli.shell.run()` returns")


@command("exec", category="system", syntax="exec <file>",
         summary="run commands from a file, stopping at the first failure",
         detail="Blank lines and lines starting with # are skipped.")
def cmd_exec(ctx, args):
    if len(args) != 1:
        raise CommandError("usage: exec <file>")

    try:
        source = open(args[0])
    except OSError:
        raise CommandError("cannot open {}".format(args[0]))

    with source:
        for number, line in enumerate(source, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ctx.out.line("+ {}", line)
            if not ctx.shell.run_line(line):
                raise CommandError("stopped at line {} of {}".format(number, args[0]))


@command("use", category="system", syntax="use [module | drop [module]]",
         summary="load a command module from the volume",
         detail="""
Chip-specific commands are not built into the firmware. They live as Python
files on the volume — `/flash/lib/mv6321.py` and the like — and `use` brings
one in:

    use mv6321
    mv6321 id 6

Without an argument it lists what is loaded and what each module gave.
Running it again on the same name reloads the file, taking that module's
previous commands away first, so editing a helper and re-loading it does
what you expect.

    use drop <module>   take one back
    use drop            take them all back

There is a limit on how many can be loaded at once: MicroPython compiles
each one on the device, and that needs a large contiguous block of a heap
only 48 KB in the first place. Two of the Marvell helpers fit together and a
third fails; `use drop` is the answer, and you are normally in front of one
chip anyway — though dropping does not always give the memory back in one
piece, and then a soft reset does. Cross-compiled `.mpy` helpers skip the
compile, so all three fit and each loads in a fifth of the time; what they do
not do is take less room once loaded.

An error while loading prints the traceback rather than a tidied message:
this is your own code, and the traceback is the useful answer. Commands the
module managed to register before it failed are taken back.

Loading does not survive a soft reset — modules are imported again from
scratch. Keep the `use` lines in a file and `exec` it if you want them back
in one step.
""")
def cmd_use(ctx, args):
    from cli import extend

    if not args:
        if not extend.LOADED:
            ctx.out.line("nothing loaded — `use <module>` loads one from "
                         "/flash/lib")
            return
        for name in sorted(extend.LOADED):
            ctx.out.line("{:<16} {}", name, " ".join(extend.LOADED[name]))
        return

    if args[0] == "drop":
        names = args[1:] if len(args) > 1 else sorted(extend.LOADED)
        if not names:
            ctx.out.line("nothing loaded")
        for name in names:
            gone = extend.unload(name)
            ctx.out.line("{}: {}", name,
                         " ".join(gone) if gone else "was not loaded")
        return

    if len(args) != 1:
        raise CommandError("usage: use [module | drop [module]]")

    try:
        added = extend.load(args[0])
    except ImportError as exc:
        # Only the module itself being absent is the operator's mistake and
        # gets a tidy message. An ImportError raised from inside the helper —
        # it imports something that is not there — keeps its traceback, which
        # is the line number they need.
        if "'{}'".format(args[0]) not in str(exc):
            raise
        raise CommandError(
            "no module named {} — helpers live in /flash/lib; `ls /flash/lib` "
            "shows what is there".format(args[0]))
    except MemoryError:
        loaded = " ".join(sorted(extend.LOADED))
        raise CommandError(
            "out of memory loading {}. About two helpers fit at once{}".format(
                args[0],
                "; try `use drop {}`, and if that is still not enough, a soft "
                "reset clears the heap properly".format(loaded) if loaded else
                " — this one may simply be too big"))

    if added:
        ctx.out.line("{}: {}", args[0], " ".join(added))
    else:
        ctx.out.line("{} loaded, but it registered no commands", args[0])


@command("script", category="system", syntax="script <file>",
         summary="run a Python file from the volume",
         detail="""
The file runs with a fresh, empty namespace, so it has to import what it
wants — `import mdioprobe` for the hardware, `from cli import transport` to
go through the same seam the commands use.

That is the difference from `exec`, which runs CLI commands. It is also the
difference from the previous project's `script`, which handed the C module
straight to the file and could not import anything else, because that
firmware had no filesystem behind `import`. Here a script can keep its own
library on /flash and import from it.
""")
def cmd_script(ctx, args):
    if len(args) != 1:
        raise CommandError("usage: script <file>")

    try:
        source = open(args[0])
    except OSError:
        raise CommandError("cannot open {}".format(args[0]))

    with source:
        code = source.read()

    exec(code, {"__name__": "__main__"})


@command("version", category="system", syntax="version",
         summary="firmware version and build identity")
def cmd_version(ctx, args):
    info = mdioprobe.version()

    ctx.out.line("mdioprobe {}", info["version"])
    ctx.out.line("build     {}{}", info["git"],
                 " (uncommitted changes)" if info["dirty"] else "")


@command("msd", category="system", syntax="msd",
         summary="hand the storage volume to the host as a USB drive",
         detail="""
The probe cannot be a serial port and a drive at once: under mass storage the
host owns the volume at block level, and two writers on one FAT corrupts it
silently. So this takes the console away.

There is no command to come back — the command interface is what is being
removed. Unplug the probe.
""")
def cmd_msd(ctx, args):
    ctx.out.line("switching to mass storage; this console will close")
    ctx.out.line("unplug the probe to get it back")
    try:
        mdioprobe.msd()
    except OSError as exc:
        raise CommandError("could not switch: errno {}".format(
            exc.args[0] if exc.args else "?"))
