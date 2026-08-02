# Frozen modules for the MDIO probe CLI.
#
# Everything the CLI is made of lives in the firmware image, so the device is
# usable with an empty (or absent) filesystem. The FAT volume is for the
# user's own scripts and logs, not for the application.
#
# Deliberately no `require()` of micropython-lib packages yet: each one costs
# flash, and nothing here needs one.

# `_boot.py` is the name the Zephyr port looks for by hardcoded string, so it
# has to be frozen at the top level rather than inside the package.
module("_boot.py", base_path="./src/python")

# The CLI itself.
package("cli", base_path="./src/python")
