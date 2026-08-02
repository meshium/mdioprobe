"""`edit` — a line editor for the files on /flash.

Deliberately small. The previous project carried ~845 lines here, most of it
streaming every change through a temporary file to keep RAM down, because it
had no VFS and no way to know how big a file was going to be. This one loads
the file into a list and refuses anything large, which trades a capability
for about six hundred lines. The capability is not much of a loss: `msd`
hands the volume to the host, and a host editor is better than this one.

What is kept from the old design is the write: into a temporary file first,
then a rename dance, so a failure part-way through cannot leave the original
truncated.
"""

import os

from cli.registry import CommandError, command

# Anything larger goes to the host. Chosen against the GC heap (32 KB): the
# list of lines costs the file's size again plus per-object overhead, so a
# limit much above this starts failing in the middle of an edit instead of
# at the door.
MAX_BYTES = 8192

_PAGE = 50

_HELP = """  p [a [b]]   print lines a..b        n [a [b]]   same, numbered
  a [line]    append after line       i <line>    insert before line
  e <line>    replace one line        r <line>    replace with a block
  d <a> [b]   delete lines            g <text>    find lines containing text
  w           write                   q / q!      quit / quit discarding
A block of text ends with a line holding only `.`; to enter a literal `.`,
type `..`."""


def _load(path):
    try:
        size = os.stat(path)[6]
    except OSError:
        return []          # a new file

    if size > MAX_BYTES:
        raise CommandError(
            "{} is {} bytes; this editor stops at {}. Use `msd` and edit it "
            "from the host.".format(path, size, MAX_BYTES))

    with open(path) as f:
        return f.read().split("\n")


def _save(path, lines):
    """Keep the previous content aside, then write the new one.

    The obvious shape — write a temporary file and rename it over the target
    — is not available: os.rename is broken on this filesystem, because the
    Zephyr port's VFS shim builds both paths in one shared buffer and so
    renames every file to itself. So instead the old content is
    copied to `<name>.bak` first. That is weaker than an atomic replace — a
    failure part-way leaves the file half-written — but the previous version
    is still on the volume, which is the property that actually matters here.
    """
    from cli.cmd_files import copy_file

    try:
        os.stat(path)
    except OSError:
        pass                       # new file, nothing to preserve
    else:
        copy_file(path, path + ".bak")

    with open(path, "w") as f:
        f.write("\n".join(lines))


def _number(token, lines, what="line"):
    from cli.parser import integer

    n = integer(token, what)
    if n < 1 or n > len(lines):
        raise CommandError("{} out of range 1..{}: {}".format(what, len(lines), n))
    return n


def _read_block(ctx):
    """Collect lines until a lone `.`; `..` is an escaped literal dot."""
    block = []

    while True:
        try:
            line = input("| ")
        except (KeyboardInterrupt, EOFError):
            ctx.out.line("(cancelled)")
            return None
        if line == ".":
            return block
        block.append(line[1:] if line == ".." else line)


def _print(ctx, lines, args, numbered):
    first = _number(args[0], lines) if args else 1
    last = _number(args[1], lines) if len(args) > 1 else min(len(lines),
                                                             first + _PAGE - 1)
    if first > last:
        first, last = last, first

    for i in range(first, last + 1):
        if numbered:
            ctx.out.line("{:4d}  {}", i, lines[i - 1])
        else:
            ctx.out.line("{}", lines[i - 1])


@command("edit", category="files", syntax="edit <file>",
         summary="line editor for small text files",
         detail="""
Files larger than 8 KB are refused — hand the volume to the host with `msd`
and use a real editor.

  p [a [b]]   print lines a..b        n [a [b]]   same, numbered
  a [line]    append after line       i <line>    insert before line
  e <line>    replace one line        r <line>    replace with a block
  d <a> [b]   delete lines            g <text>    find lines containing text
  w           write                   q / q!      quit / quit discarding
A block of text ends with a line holding only `.`; to enter a literal `.`,
type `..`.

`a 0` inserts before the first line. Saving copies the previous content to
<name>.bak first, so a write that fails part-way leaves the old version on
the volume.
""")
def cmd_edit(ctx, args):
    if len(args) != 1:
        raise CommandError("usage: edit <file>")

    path = args[0]
    lines = _load(path)
    dirty = False

    ctx.out.line("{}: {} line(s). `h` for help, `q` to leave.", path, len(lines))

    while True:
        try:
            command_line = input("edit> ").strip()
        except KeyboardInterrupt:
            ctx.out.line("")
            continue
        except EOFError:
            ctx.out.line("")
            if dirty:
                ctx.out.line("unsaved changes — `w` to write, `q!` to discard")
                continue
            return

        if not command_line:
            continue

        # A mistyped editor command must not throw the operator out of the
        # editor along with their unsaved edits.
        try:
            dirty = _apply(ctx, path, lines, command_line, dirty)
        except _Done:
            return
        except CommandError as exc:
            ctx.out.line("error: {}", exc)
        except OSError as exc:
            ctx.out.line("error: {}", exc)


class _Done(Exception):
    """`q` or `q!`, raised so the dispatcher can return from one place."""


def _apply(ctx, path, lines, command_line, dirty):
    parts = command_line.split(None, 1)
    verb = parts[0]
    rest = parts[1].split() if len(parts) > 1 else []
    tail = parts[1] if len(parts) > 1 else ""

    if verb in ("h", "?", "help"):
        for line in _HELP.split("\n"):
            ctx.out.line("{}", line)

    elif verb in ("p", "n"):
        if not lines:
            ctx.out.line("(empty)")
        else:
            _print(ctx, lines, rest, verb == "n")

    elif verb == "g":
        if not tail:
            raise CommandError("g needs some text to look for")
        hits = 0
        for i, line in enumerate(lines, 1):
            if tail in line:
                ctx.out.line("{:4d}  {}", i, line)
                hits += 1
        ctx.out.line("{} line(s) match", hits)

    elif verb in ("a", "i"):
        if verb == "i":
            at = _number(rest[0], lines) - 1 if rest else 0
        elif rest:
            # `a 0` is the documented way to get in front of line 1.
            from cli.parser import integer
            at = integer(rest[0], "line")
            if at < 0 or at > len(lines):
                raise CommandError("line out of range 0..{}".format(len(lines)))
        else:
            at = len(lines)

        block = _read_block(ctx)
        if block:
            lines[at:at] = block
            dirty = True
            ctx.out.line("{} line(s) added", len(block))

    elif verb == "e":
        if not rest:
            raise CommandError("e needs a line number")
        n = _number(rest[0], lines)
        ctx.out.line("{:4d}  {}", n, lines[n - 1])
        try:
            replacement = input("new>  ")
        except (KeyboardInterrupt, EOFError):
            ctx.out.line("(unchanged)")
            return dirty
        if replacement:
            lines[n - 1] = replacement
            dirty = True

    elif verb == "r":
        if not rest:
            raise CommandError("r needs a line number")
        n = _number(rest[0], lines)
        block = _read_block(ctx)
        if block is not None:
            lines[n - 1:n] = block
            dirty = True
            ctx.out.line("line {} replaced with {} line(s)", n, len(block))

    elif verb == "d":
        if not rest:
            raise CommandError("d needs a line number")
        first = _number(rest[0], lines)
        last = _number(rest[1], lines) if len(rest) > 1 else first
        if first > last:
            first, last = last, first
        del lines[first - 1:last]
        dirty = True
        ctx.out.line("{} line(s) deleted", last - first + 1)

    elif verb == "w":
        _save(path, lines)
        dirty = False
        ctx.out.line("{}: {} line(s) written", path, len(lines))

    elif verb == "q":
        if dirty:
            ctx.out.line("unsaved changes — `w` to write, `q!` to discard")
        else:
            raise _Done

    elif verb == "q!":
        raise _Done

    else:
        raise CommandError("unknown editor command: {} (try `h`)".format(verb))

    return dirty
