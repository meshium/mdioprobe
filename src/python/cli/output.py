"""Where command output goes.

Handlers write through this object rather than calling print(), which is
what makes `> file` redirection a matter of swapping a sink. The previous
project instead monkey-patched `print` into the globals of two modules and
wrapped sys.print_exception in a proxy, with a comment admitting it was
working around MicroPython's read-only builtins. Passing the sink avoids the
whole problem.
"""


# Stop a redirected command this far from filling the volume. Enough room
# left to delete things and to write the file's directory entry.
_SPACE_FLOOR = 32 * 1024

# statvfs is not free, so a long loop asks only every so often.
_SPACE_EVERY = 128


class Output:
    def __init__(self):
        self._file = None
        self._checks = 0

    def out_of_room(self):
        """True when redirected output is about to fill the volume.

        Long loops call this and stop. Without it a command that writes
        faster than the operator can react fills all 620 KB — and because
        the file is only closed when the command returns, an end that is
        not clean leaves the clusters allocated with no directory entry
        pointing at them. They are then unreachable short of a reformat,
        which is not a theoretical risk: it happened on the bench.
        """
        if self._file is None:
            return False

        self._checks += 1
        if self._checks % _SPACE_EVERY:
            return False

        try:
            import os
            stat = os.statvfs(".")
        except OSError:
            return False
        return stat[0] * stat[3] < _SPACE_FLOOR

    def line(self, text="", *args):
        if args:
            text = text.format(*args)
        if self._file is None:
            print(text)
        else:
            self._file.write(text)
            self._file.write("\n")

    def redirect(self, path, append=False):
        """Context manager: send output to a file for the duration."""
        return _Redirect(self, path, append)


class _Redirect:
    def __init__(self, out, path, append):
        self._out = out
        self._path = path
        self._append = append
        self._prev = None

    def __enter__(self):
        # Refuse up front rather than write into a volume with no room. FatFs
        # does not fail loudly here: the writes simply do not land, and the
        # command looks like it produced nothing at all, which is what
        # happened on the bench once the volume had been filled.
        try:
            import os
            stat = os.statvfs(".")
            free = stat[0] * stat[3]
        except OSError:
            free = None

        if free is not None and free < _SPACE_FLOOR:
            from cli.registry import CommandError
            raise CommandError(
                "only {} bytes free — redirecting there would write into a "
                "full volume, and FatFs does that silently".format(free))

        self._prev = self._out._file
        self._out._file = open(self._path, "a" if self._append else "w")
        return self._out

    def __exit__(self, *exc):
        try:
            self._out._file.close()
        finally:
            self._out._file = self._prev
        return False
