# SPDX-License-Identifier: Apache-2.0
#
# The board has no on-board debugger — flashing goes through an external
# SWD probe on the JTAG header (PA13/PA14/PB3).

board_runner_args(stm32cubeprogrammer "--port=swd" "--reset-mode=hw")
board_runner_args(pyocd "--target=stm32g474cetx")
board_runner_args(jlink "--device=STM32G474CE" "--speed=4000")

include(${ZEPHYR_BASE}/boards/common/stm32cubeprogrammer.board.cmake)
include(${ZEPHYR_BASE}/boards/common/openocd-stm32.board.cmake)
include(${ZEPHYR_BASE}/boards/common/pyocd.board.cmake)
include(${ZEPHYR_BASE}/boards/common/jlink.board.cmake)
