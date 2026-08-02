"""Files on the QSPI volume.

Thin wrappers over `os` and `open()`. The volume reaches Python through
MicroPython's VFS, so there is nothing to reimplement here — which is the
payoff for mounting it that way instead of writing file bindings in C.
"""

import os

from cli.registry import CommandError, command

_CHUNK = 256


def _one(args, usage):
    if len(args) != 1:
        raise CommandError("usage: " + usage)
    return args[0]


def _two(args, usage):
    if len(args) != 2:
        raise CommandError("usage: " + usage)
    return args[0], args[1]


def _size(n):
    """Bytes for small files, one decimal above 10 KB.

    A device volume is 620 KB, so exact byte counts stay meaningful for
    almost everything on it; the abbreviation is only there to stop a large
    capture log from pushing the name column around.
    """
    if n < 10240:
        return str(n)
    if n < 1024 * 1024:
        return "{}.{}K".format(n // 1024, (n % 1024) * 10 // 1024)
    return "{}.{}M".format(n // 1048576, (n % 1048576) * 10 // 1048576)


@command("ls", category="files", syntax="ls [-a] [path]",
         summary="list a directory",
         detail="""
Directories first, then files, each sorted by name and case-insensitively —
so `Logs` and `logs` land next to each other rather than in separate halves
of the listing.

Names starting with a dot are counted but not shown; `-a` shows them. They
are almost always the resource forks and index directories a macOS host
leaves behind after `msd`, and on a volume this small they otherwise crowd
out the files that are actually yours.
""")
def cmd_ls(ctx, args):
    show_all = False

    if args and args[0] == "-a":
        show_all = True
        args = args[1:]
    if len(args) > 1:
        raise CommandError("usage: ls [-a] [path]")

    path = args[0] if args else os.getcwd()

    try:
        entries = list(os.ilistdir(path))
    except OSError:
        raise CommandError("cannot list {}".format(path))

    hidden = 0
    dirs = []
    files = []

    for entry in entries:
        # ilistdir yields three fields or four: the size is optional, and the
        # VFS root leaves it out because a mount point does not have one.
        # Unpacking four unconditionally is what made `ls /` a traceback.
        name, kind = entry[0], entry[1]
        size = entry[3] if len(entry) > 3 else 0

        if name.startswith(".") and not show_all:
            hidden += 1
            continue
        (dirs if kind & 0x4000 else files).append((name, size))

    dirs.sort(key=lambda e: e[0].lower())
    files.sort(key=lambda e: e[0].lower())

    for name, _size_ in dirs:
        ctx.out.line("{:>8}  {}/", "<DIR>", name)
    for name, size in files:
        ctx.out.line("{:>8}  {}", _size(size), name)

    total = 0
    for _name, size in files:
        total += size

    parts = []
    if dirs:
        parts.append("{} director{}".format(len(dirs),
                                            "y" if len(dirs) == 1 else "ies"))
    parts.append("{} file{}, {} bytes".format(len(files),
                                              "" if len(files) == 1 else "s",
                                              total))
    if hidden:
        parts.append("{} hidden (-a shows them)".format(hidden))
    ctx.out.line("{}", ", ".join(parts))


@command("cd", category="files", syntax="cd <path>", summary="change directory")
def cmd_cd(ctx, args):
    path = _one(args, "cd <path>")

    try:
        os.chdir(path)
    except OSError:
        raise CommandError("no such directory: {}".format(path))
    ctx.out.line("{}", os.getcwd())


@command("pwd", category="files", syntax="pwd", summary="print the current directory")
def cmd_pwd(ctx, args):
    ctx.out.line("{}", os.getcwd())


@command("cat", category="files", syntax="cat <file>", summary="print a file")
def cmd_cat(ctx, args):
    path = _one(args, "cat <file>")

    try:
        source = open(path)
    except OSError:
        raise CommandError("cannot open {}".format(path))

    with source:
        while True:
            chunk = source.read(_CHUNK)
            if not chunk:
                break
            ctx.out.line("{}", chunk.rstrip("\n"))


@command("mkdir", category="files", syntax="mkdir <path>", summary="create a directory")
def cmd_mkdir(ctx, args):
    path = _one(args, "mkdir <path>")

    try:
        os.mkdir(path)
    except OSError:
        raise CommandError("cannot create {}".format(path))


@command("rm", category="files", syntax="rm <path>",
         summary="remove a file, or an empty directory")
def cmd_rm(ctx, args):
    path = _one(args, "rm <path>")

    try:
        if os.stat(path)[0] & 0x4000:
            os.rmdir(path)
        else:
            os.remove(path)
    except OSError:
        raise CommandError("cannot remove {}".format(path))


def copy_file(src, dst):
    try:
        source = open(src, "rb")
    except OSError:
        raise CommandError("cannot open {}".format(src))

    with source:
        try:
            sink = open(dst, "wb")
        except OSError:
            raise CommandError("cannot create {}".format(dst))
        with sink:
            while True:
                chunk = source.read(_CHUNK)
                if not chunk:
                    break
                sink.write(chunk)


@command("move", category="files", syntax="move <src> <dst>",
         summary="move a file",
         detail="""
Copies and then deletes rather than renaming, because os.rename does not
work on this filesystem: the Zephyr port's VFS shim builds both paths in one
shared buffer, so every rename renames a file to itself. Two consequences
worth knowing: the move needs room for both copies for a moment, and a
directory cannot be moved at all.
""")
def cmd_move(ctx, args):
    src, dst = _two(args, "move <src> <dst>")

    try:
        is_dir = os.stat(src)[0] & 0x4000
    except OSError:
        raise CommandError("cannot stat {}".format(src))

    if is_dir:
        raise CommandError(
            "{} is a directory, and this filesystem cannot rename — copy the "
            "files across and remove the old directory".format(src))

    copy_file(src, dst)
    try:
        os.remove(src)
    except OSError:
        raise CommandError(
            "copied to {} but could not remove {} — both exist now".format(dst, src))


@command("copy", category="files", syntax="copy <src> <dst>", summary="copy a file")
def cmd_copy(ctx, args):
    src, dst = _two(args, "copy <src> <dst>")

    copy_file(src, dst)


@command("format", category="files", syntax="format [yes]",
         summary="erase the volume and lay down a fresh filesystem",
         detail="""
Destroys everything on /flash. Without `yes` it only says what it would
destroy.

The reason this exists rather than "reformat it from a host": space can be
lost on this volume in a way nothing else recovers. A command that writes
through `>` and does not finish cleanly leaves its clusters allocated in the
FAT while the directory entry — written when the file is closed — never gets
a size, so `df` reports the space gone and there is no file to delete. That
happened here after a capture was cut short by a reset.

If the format or the remount fails the volume is left unmounted, and the
file commands will say so. A reboot then mounts it, formatting first if it
has to, which is the same path a blank volume takes on a new board.
""")
def cmd_format(ctx, args):
    if args and args != ["yes"]:
        raise CommandError("usage: format [yes]")

    try:
        import vfs
        import zephyr
    except ImportError:
        raise CommandError("no VFS on this build")

    fs_type = getattr(zephyr, "FileSystem", None)
    names = fs_type.fstab() if fs_type else []

    if not names:
        raise CommandError("no volume declared in the devicetree")

    name = names[0]
    at = name.rstrip(":")

    if not args:
        ctx.out.line("{} would be erased:", at)
        try:
            stat = os.statvfs(at)
            ctx.out.line("  {} KB total, {} KB free",
                         stat[0] * stat[2] // 1024, stat[0] * stat[3] // 1024)
        except OSError:
            ctx.out.line("  (not mounted)")
        ctx.out.line("say `format yes` to go ahead")
        return

    # Step out of the volume first: the cwd cannot live on a filesystem
    # that is about to be unmounted.
    try:
        os.chdir("/")
    except OSError:
        pass

    fs = fs_type(name)

    try:
        vfs.umount(at)
    except OSError:
        pass                # already unmounted, which is fine

    # Not fs.mkfs(): the port's shim hands FatFs the mount's FATFS object
    # where the format parameters go, and the DT disk name where the volume
    # path goes, so it fails outright. mdioprobe.format_storage() does the
    # same job with the arguments Zephyr's own mount code uses.
    import mdioprobe
    mdioprobe.format_storage()

    vfs.mount(fs, at)
    os.chdir(at)

    # mkfs leaves the volume unnamed, so the label has to go back on. It is
    # set at boot too; doing it here as well means a format does not leave
    # the drive showing up on a host as NO NAME until the next power cycle.
    try:
        mdioprobe.volume_label()
    except OSError:
        pass

    ctx.out.line("{} formatted", at)
    ctx.out.line("note: /flash/lib went with it — chip helpers have to be "
                 "copied back before `use` can find them")
    _report_free(ctx, at)


def _report_free(ctx, path):
    try:
        stat = os.statvfs(path)
    except OSError:
        raise CommandError("cannot stat {}".format(path))

    block, total, free = stat[0], stat[2], stat[3]
    if total == 0:
        # The VFS root itself is not a filesystem — it only holds mounts.
        return False
    ctx.out.line("{:<10} {} KB free of {} KB",
                 path, block * free // 1024, block * total // 1024)
    return True


@command("df", category="files", syntax="df [path]",
         summary="free space, for every mounted volume if none is given")
def cmd_df(ctx, args):
    if not args:
        # Local, like the one in cmd_format: this module is otherwise pure
        # `os` and `open()`, and the hardware module has no business being
        # imported for the commands that do not need it.
        import mdioprobe
        try:
            ctx.out.line("volume     {}", mdioprobe.volume_label())
        except OSError:
            pass

    if args:
        if not _report_free(ctx, args[0]):
            raise CommandError("{} is not a filesystem".format(args[0]))
        return

    shown = 0
    for name in os.listdir("/"):
        if _report_free(ctx, "/" + name):
            shown += 1
    if not shown:
        ctx.out.line("nothing mounted")
