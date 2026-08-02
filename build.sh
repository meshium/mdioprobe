#!/usr/bin/env bash
# Build mdioprobe_v2 for the custom Meshium MDIO Probe v2 board
# (STM32G474CET6). The board definition lives in-tree under
# boards/meshium/mdioprobe_v2 and is picked up via BOARD_ROOT, set in
# CMakeLists.txt.
#
# Usage:
#   ./build.sh                # pristine build
#   ./build.sh flash          # build + flash over SWD
#   ./build.sh -- -DEXTRA=... # extra cmake args (after `--`)
#
# Nothing below is specific to one machine. Every location is either taken
# from the environment you are already in, discovered, or overridable:
#
#   ZEPHYR_BASE               the Zephyr tree. Defaults to the stock
#                             ~/zephyrproject/zephyr.
#   MDIOPROBE_SDK             which SDK to use. Defaults to the newest one
#                             installed under $HOME.
#   MDIOPROBE_VENV            Python environment to activate, and only if
#                             `west` is not already on PATH.
#
# So: if your Zephyr environment is already active, this script uses it and
# changes nothing. If it is not, it looks in the places a stock `west init`
# workspace puts things, and says what is missing rather than failing deep
# inside CMake.
set -euo pipefail

die() {
	echo "build.sh: $*" >&2
	exit 1
}

cd "$(dirname "$0")"

# --- Python environment ----------------------------------------------------
#
# Only touched when `west` is missing. Somebody who has already activated
# their own environment should not have it swapped out from under them.
if ! command -v west >/dev/null 2>&1; then
	venv=${MDIOPROBE_VENV:-$HOME/zephyrproject/.venv}

	if [ -f "$venv/bin/activate" ]; then
		# shellcheck disable=SC1091
		source "$venv/bin/activate"
	fi
fi

command -v west >/dev/null 2>&1 || die \
	"west not found. Activate your Zephyr environment, or point
       MDIOPROBE_VENV at the virtualenv that has it."

# --- Zephyr ----------------------------------------------------------------
#
# Deliberately *not* `west topdir`. That answers "which workspace am I
# standing in", and this repository is intentionally not inside one — so it
# reports whatever workspace happens to enclose the checkout. Measured here:
# with the tree at ~/src/mdioprobe_v2 and an unrelated ~/src/.west, it
# returned ~/src and the build silently used ~/src/zephyr (4.3.99) instead of
# ~/zephyrproject/zephyr (4.4.99) — same board, same SDK, an image 7 KB
# different, and nothing on screen to say why.
#
# An explicit ZEPHYR_BASE wins; otherwise the layout `west init` produces.
if [ -z "${ZEPHYR_BASE:-}" ]; then
	if [ -d "$HOME/zephyrproject/zephyr" ]; then
		ZEPHYR_BASE="$HOME/zephyrproject/zephyr"
	else
		die "cannot find the Zephyr tree. Set ZEPHYR_BASE to it —
       guessing from the working directory is how you end up building
       against the wrong one."
	fi
fi

[ -f "$ZEPHYR_BASE/zephyr-env.sh" ] || die \
	"ZEPHYR_BASE=$ZEPHYR_BASE does not look like a Zephyr tree."

echo "== Zephyr: $ZEPHYR_BASE"

# shellcheck disable=SC1091
source "$ZEPHYR_BASE/zephyr-env.sh"

# --- Toolchain -------------------------------------------------------------
#
# Zephyr finds the SDK through a CMake package registry, and with several
# registered it takes whichever the registry lists first — not necessarily
# the one you meant. So this pins one deliberately.
#
# It pins over ZEPHYR_SDK_INSTALL_DIR even when that is already set, and the
# override is a variable of our own. That looks rude and is deliberate: a
# value inherited from a login profile is not a decision anyone made about
# *this* build, and letting it through means the same tree produces different
# images depending on whose shell ran it. Measured here — a profile pointing
# at 0.17.0 while 1.0.0 was installed moved the image by 7 KB.
#
# Hardcoding a version number would be the other failure: it works on one
# machine and nowhere else. Newest installed, overridable, announced.
sdk=${MDIOPROBE_SDK:-}

if [ -z "$sdk" ]; then
	sdk=$(ls -d "$HOME"/zephyr-sdk-* 2>/dev/null | sort -V | tail -1 || true)
fi

if [ -n "$sdk" ] && [ -d "$sdk" ]; then
	export ZEPHYR_SDK_INSTALL_DIR="$sdk"
	echo "== SDK: $ZEPHYR_SDK_INSTALL_DIR"
else
	echo "== SDK: leaving the choice to Zephyr — none found under \$HOME"
fi

# Host CPPFLAGS/LDFLAGS leak into the cross-compile and break things.
unset CPPFLAGS LDFLAGS

# --- Submodule -------------------------------------------------------------
#
# Checked here rather than left to a CMake error, because the message it
# produces otherwise does not say what to do about it.
[ -f third_party/micropython/py/mpconfig.h ] || die \
	"the MicroPython submodule is missing. Run:
       git submodule update --init --recursive"

# `flash` is ours, not west's — passing it through would make `west build`
# read it as the source directory and bail out.
do_flash=0
if [ "${1:-}" = "flash" ]; then
	do_flash=1
	shift
fi

west build -p always -b mdioprobe_v2 "$@"

# Chip helpers are not part of the image — they live on the probe's volume.
# But they no longer fit the on-device compiler: MicroPython compiles a
# helper when it is imported, and that wants a contiguous block out of a heap
# that has about 35 KB free. Cross-compiling here removes the compile step
# and with it the limit, so all three load together instead of two.
#
# Only the .mpy goes on the volume. Import tries <name>.py first and reaches
# for <name>.mpy only when there is no source beside it, so a stale .py left
# on /flash/lib silently cancels the whole thing.
MPY_CROSS="third_party/micropython/mpy-cross/build/mpy-cross"
if [ ! -x "$MPY_CROSS" ]; then
	echo "== building mpy-cross (once)"
	make -s -C third_party/micropython/mpy-cross
fi

mkdir -p build/lib
for src in scripts/lib/*.py; do
	"$MPY_CROSS" -o "build/lib/$(basename "${src%.py}").mpy" "$src"
done
echo "== helpers cross-compiled into build/lib:"
ls -l build/lib/*.mpy | awk '{printf "   %-28s %6d\n", $9, $5}'

if [ "$do_flash" = 1 ]; then
	west flash
fi
