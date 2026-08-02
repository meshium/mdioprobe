"""Frozen boot hook.

The Zephyr port calls this by name (`pyexec_frozen_module("_boot.py")`) once
the interpreter is up, before the REPL. Two jobs: mount the FAT volume into
the VFS, then hand over to the CLI.

Both are guarded. A probe with a broken filesystem, or a broken CLI, must
still drop into a usable REPL rather than become a brick.
"""

import sys


def mount_storage():
    """Mount the Zephyr-owned FAT volume into MicroPython's VFS.

    The volume is declared in the devicetree (`zephyr,fstab`) and its FatFs
    belongs to Zephyr; `zephyr.FileSystem` is the port's shim that exposes it
    as a VFS object, which is what makes `import` and `open()` work over it.

    Returns the mount point, or None if there is no usable volume.
    """
    try:
        import vfs
        import zephyr
    except ImportError:
        return None

    fs_type = getattr(zephyr, "FileSystem", None)
    if fs_type is None:
        return None

    for name in fs_type.fstab():
        fs = fs_type(name)

        # Zephyr's FatFs mount points carry a trailing colon ("/flash:"),
        # which is FatFs volume syntax rather than anything a user should
        # have to type. MicroPython translates paths through the mount
        # object, so the VFS side can drop it and be a plain "/flash".
        at = name.rstrip(":")
        try:
            vfs.mount(fs, at)
            return at
        except OSError:
            pass

        # Unformatted volume — first boot, or a volume the user wiped.
        if not hasattr(fs, "mkfs"):
            continue
        try:
            fs.mkfs()
            vfs.mount(fs, at)
            print("boot: formatted {}".format(at))
            return at
        except OSError as exc:
            print("boot: cannot mount {}: {}".format(name, exc))
    return None


_mount = mount_storage()

if _mount:
    # Make the volume importable, so a user module dropped on it works with
    # a plain `import` and no ceremony. This is the whole reason the volume
    # goes through MicroPython's VFS rather than bespoke file bindings.
    if _mount not in sys.path:
        sys.path.append(_mount)

    # And a place for command modules that is not the same place as logs and
    # captures. `use mv6321` finds /flash/lib/mv6321.py; nothing is imported
    # from here at boot, because a helper that fails should cost a command,
    # not the CLI.
    _lib = _mount + "/lib"
    try:
        import os
        os.stat(_lib)
    except OSError:
        pass
    else:
        if _lib not in sys.path:
            sys.path.append(_lib)

    # Start in the volume rather than the VFS root: the root holds only
    # mount points, so `ls` there says nothing useful and `df` has nothing
    # to measure.
    try:
        import os
        os.chdir(_mount)
    except OSError:
        pass

# Clear the line history before anything can read from it.
#
# The Zephyr port never calls readline_init0(), which every other port calls
# right after gc_init() on each soft reset. The history is an array of
# pointers into the GC heap, and a soft reset re-initialises that heap, so
# without this the slots point at freed memory the first time someone
# presses Up. _boot.py is the right place because upstream runs it after
# every soft reset.
try:
    import mdioprobe
    mdioprobe.history_reset()
except (ImportError, AttributeError):
    pass

try:
    import cli.shell
    cli.shell.run()
except Exception as exc:
    print("boot: CLI did not start, falling back to the REPL")
    sys.print_exception(exc)
