"""The interactive loop.

Reads a line, dispatches it, formats whatever went wrong. Line editing is
still MicroPython's readline — arrows, Home/End and history come free, and
there is no hand-rolled ANSI editor here; the previous project carried ~650
lines of one. What is ours is Tab, which upstream wires to Python-name
completion: mdioprobe.readline() runs the same loop with that one branch
diverted to cli.complete. See src/mdioprobe_readline.c.
"""

import sys

from cli import parser
from cli.output import Output
from cli.registry import CommandError, lookup

_PROMPT = "mdio"


def prompt():
    """`mdio /flash/logs> ` — the cwd is worth the width once `cd` exists."""
    try:
        import os
        here = os.getcwd()
    except Exception:
        return _PROMPT + "> "

    # The VFS returns subdirectories with a trailing slash but the root
    # without one, which reads as an inconsistency rather than a detail.
    if len(here) > 1 and here.endswith("/"):
        here = here[:-1]
    return "{} {}> ".format(_PROMPT, here)


class Context:
    """What a handler is given: somewhere to write, and the shell itself."""

    def __init__(self, out, shell):
        self.out = out
        self.shell = shell


class Shell:
    def __init__(self):
        self.out = Output()
        self.ctx = Context(self.out, self)
        self.running = True

    def execute(self, line):
        """Run one line. Raises CommandError; everything else is a bug."""
        line = line.strip()
        if not line or line.startswith("#"):
            return

        line, path, append = parser.split_redirect(line)
        tokens = parser.tokenize(line)
        if not tokens:
            raise CommandError("nothing to run before the redirection")

        cmd = lookup(tokens[0])
        args = tokens[1:]

        if path is None:
            cmd.fn(self.ctx, args)
        else:
            with self.out.redirect(path, append):
                cmd.fn(self.ctx, args)

    def run_line(self, line):
        """Run one line and report failures. True if it worked."""
        try:
            self.execute(line)
            return True
        except CommandError as exc:
            self.out.line("error: {}", exc)
        except OSError as exc:
            self.out.line("error: {}", _explain_oserror(exc))
        except KeyboardInterrupt:
            self.out.line("interrupted")
        except Exception as exc:          # a bug in us, not in the input
            self.out.line("internal error:")
            sys.print_exception(exc)
        return False


# errno numbers the probe's API actually returns, in the situations it
# returns them. Anything else falls through as a bare number.
_ERRNO_HELP = {
    5:   "the target did not respond",
    11:  "the bus stayed idle",
    16:  "the connector is not in MDIO mode",
    19:  "no target detected — connect one, or declare the rail with `bus force <mv>`",
    22:  "value out of range",
    110: "the translator supply did not reach the bus rail",
}


def _explain_oserror(exc):
    code = exc.args[0] if exc.args else None
    text = _ERRNO_HELP.get(code)

    return text if text else "operation failed (errno {})".format(code)


def banner(shell):
    import mdioprobe

    try:
        shell.out.line("mdioprobe {}", mdioprobe.version()["full"])
    except Exception:
        shell.out.line("mdioprobe")
    try:
        bus = mdioprobe.bus_get()
        if bus["target_present"]:
            shell.out.line("target on a {} mV bus", bus["nominal_mv"])
        elif bus["level_forced"]:
            shell.out.line("no target; rail declared as {} mV", bus["nominal_mv"])
        else:
            shell.out.line("no target detected — capture and the master are "
                           "blocked until one is, or until `bus force <mv>`")
    except Exception:
        pass
    shell.out.line("`help` lists commands")


def wait_for_console():
    """Wait for a host to open the console before greeting it.

    The port is USB CDC ACM: everything written before the host opens it goes
    nowhere. Without this the banner is lost, and an operator connecting even
    a second after boot sees an empty screen — the prompt is there, but
    nothing says so until they press a key, which reads as a probe that did
    not start.

    The wait has no deadline, on purpose. A timeout would only mean going
    back to printing into the void once it expired, so it buys nothing; and
    a probe with nobody attached has no use for a command line anyway. The
    parts that must keep working without a console — the target detector and
    the indication — run in their own threads and are unaffected.

    If the port has no line control to ask, this returns immediately rather
    than waiting for a signal that cannot arrive.
    """
    import time

    import mdioprobe

    ready = getattr(mdioprobe, "console_ready", None)
    if ready is None:
        return

    while not ready():
        time.sleep_ms(50)

    # The host has the port; let its terminal settle so the first line
    # arrives whole.
    time.sleep_ms(150)


def run():
    # Importing a command module is what registers its commands.
    import cli.cmd_sys      # noqa: F401
    import cli.cmd_bus      # noqa: F401
    import cli.cmd_mdio     # noqa: F401
    import cli.cmd_probe    # noqa: F401
    import cli.cmd_monitor  # noqa: F401
    import cli.cmd_marvell  # noqa: F401
    import cli.cmd_capture  # noqa: F401
    import cli.cmd_diag     # noqa: F401
    import cli.cmd_files    # noqa: F401
    import cli.cmd_edit     # noqa: F401

    import mdioprobe

    from cli import complete

    shell = Shell()
    wait_for_console()
    banner(shell)

    # Reading through mdioprobe.readline() rather than input() is what puts
    # Tab under our control; everything else about the line — arrows, Home,
    # End, history — is still MicroPython's readline underneath.
    read = getattr(mdioprobe, "readline", None)
    if read is not None:
        mdioprobe.set_completer(complete.complete)
    else:
        read = input

    while shell.running:
        try:
            line = read(prompt())
        except KeyboardInterrupt:
            shell.out.line("")
            continue
        except EOFError:
            if _recover_from_eof(shell):
                banner(shell)
            continue
        shell.run_line(line)


def _recover_from_eof(shell):
    """Decide what an end-of-input means and carry on. True if reconnected.

    Two things arrive here as the same exception, and they want opposite
    treatment:

      - the host closed the port, or unplugged. Leaving the loop here is what
        the first version did, and it meant a probe that was fine until
        someone closed a terminal and then had no command line until a power
        cycle. Instead, go back to waiting for a console, exactly as at boot.

      - Ctrl-D on an open port. Ending the session on a stray keystroke is
        not what anyone means by it, and `exit` already exists for going to
        the REPL deliberately. Say so and keep the prompt.

    `console_ready()` is what tells them apart. Without it, treat EOF as a
    keystroke — the sleep keeps a port that reports EOF forever from spinning
    the CPU at the expense of the vreg loop.
    """
    import time

    import mdioprobe

    ready = getattr(mdioprobe, "console_ready", None)
    if ready is None or ready():
        shell.out.line("")
        shell.out.line("(end of input — `exit` leaves the shell for the REPL)")
        time.sleep_ms(100)
        return False

    wait_for_console()
    return True
