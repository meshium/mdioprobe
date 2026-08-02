cmake_minimum_required(VERSION 3.20.0)

# The board lives inside the app: boards/meshium/mdioprobe_v2/.
list(APPEND BOARD_ROOT ${CMAKE_CURRENT_SOURCE_DIR})

# No `cdc-acm-console` snippet: it builds a USB device with CDC ACM and
# nothing else, and this firmware has to be able to tear that down and come
# back as mass storage. The device is ours, in src/mdioprobe_usbd.c.

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(mdioprobe_v2)

# ---------------------------------------------------------------------------
# Version header
# ---------------------------------------------------------------------------
#
# Release version from the tracked version.txt, build identity from git. The
# script rewrites the header only when its contents change, so a rebuild
# after an unrelated edit does not cascade.

set(MDIOPROBE_VERSION_HEADER ${CMAKE_BINARY_DIR}/generated/mdioprobe_version.h)

add_custom_target(mdioprobe_version_header ALL
  COMMAND ${PYTHON_EXECUTABLE}
          ${CMAKE_CURRENT_LIST_DIR}/scripts/gen_version_header.py
          --version-file ${CMAKE_CURRENT_LIST_DIR}/version.txt
          --out-header ${MDIOPROBE_VERSION_HEADER}
  BYPRODUCTS ${MDIOPROBE_VERSION_HEADER}
  DEPENDS
    ${CMAKE_CURRENT_LIST_DIR}/version.txt
    ${CMAKE_CURRENT_LIST_DIR}/scripts/gen_version_header.py
  VERBATIM
)
add_dependencies(app mdioprobe_version_header)

# ---------------------------------------------------------------------------
# MicroPython
# ---------------------------------------------------------------------------
#
# Built as a plain CMake static library and linked into the app, rather than
# with zephyr_library(): the latter is for Zephyr modules and warns when used
# from an application. The consequence is that this target does NOT inherit
# Zephyr's compile settings automatically — they have to be pulled in by hand
# below, both for the target itself and for the separate QSTR preprocessing
# pass.

set(MICROPY_DIR "${CMAKE_CURRENT_LIST_DIR}/third_party/micropython"
    CACHE PATH "Path to the MicroPython source tree")
set(MICROPY_PORT_DIR "${MICROPY_DIR}/ports/zephyr")

if(NOT EXISTS "${MICROPY_PORT_DIR}/CMakeLists.txt")
  message(FATAL_ERROR
    "MicroPython not found at ${MICROPY_DIR}.\n"
    "It is a git submodule: git submodule update --init --recursive")
endif()
if(NOT EXISTS "${MICROPY_DIR}/lib/micropython-lib/README.md")
  message(FATAL_ERROR
    "micropython-lib is missing — the frozen manifest needs it.\n"
    "Run: git submodule update --init --recursive")
endif()

set(MICROPY_TARGET micropython)

include(${MICROPY_DIR}/py/py.cmake)
include(${MICROPY_DIR}/extmod/extmod.cmake)

# The port ships a devicetree binding (micropython,heap) for a split GC heap.
list(APPEND DTS_ROOT ${MICROPY_PORT_DIR}/dts)

if(CONFIG_MICROPY_FROZEN_MODULES)
  # Manifest paths in this project are relative to the app root, not to the
  # port directory the way upstream's own board manifests are.
  cmake_path(ABSOLUTE_PATH CONFIG_MICROPY_FROZEN_MANIFEST
             BASE_DIRECTORY ${CMAKE_CURRENT_LIST_DIR})
  set(MICROPY_FROZEN_MANIFEST ${CONFIG_MICROPY_FROZEN_MANIFEST})
endif()

# Must come after MICROPY_FROZEN_MANIFEST is resolved: it pulls in
# py/manifest.cmake, which turns the MICROPY_MANIFEST_* variables into the
# -v arguments makemanifest.py needs. Skip it and the frozen-content step
# dies with "TypeError: 'NoneType' object is not iterable".
include(${MICROPY_DIR}/py/usermod.cmake)

# Mirrors ports/zephyr/CMakeLists.txt, with two deliberate omissions:
#   src/zephyr_start.c — supplies main(); ours is in src/main.c so the
#                        hardware is up before the interpreter starts.
#   src/usbd.c         — would build a second USB device; Zephyr already
#                        owns ours (see src/mdioprobe_mp_usbd.c).
set(MICROPY_SOURCE_PORT
    main.c
    help.c
    modzephyr.c
    mphalport.c
    uart_core.c
    zephyr_device.c
    zephyr_storage.c
    zephyr_filesystem.c
    mpthreadport.c
)
list(TRANSFORM MICROPY_SOURCE_PORT PREPEND ${MICROPY_PORT_DIR}/)

# ports/zephyr/src/zephyr_getchar.c is the console input path for builds
# *without* CONFIG_CONSOLE_SUBSYS: it hooks the UART console debug server
# instead. We use the console subsystem (uart_core.c's tty layer), so that
# file is dead code here — and it does not even compile without
# CONFIG_UART_CONSOLE_DEBUG_SERVER_HOOKS, which we have no other reason to
# turn on.
set(MICROPY_SOURCE_PORT_SRC)

set(MICROPY_SOURCE_SHARED
    libc/printf.c
    readline/readline.c
    runtime/gchelper_generic.c
    runtime/interrupt_char.c
    runtime/mpirq.c
    runtime/pyexec.c
    runtime/stdout_helpers.c
    runtime/sys_stdio_mphal.c
    timeutils/timeutils.c
)
list(TRANSFORM MICROPY_SOURCE_SHARED PREPEND ${MICROPY_DIR}/shared/)

set(MICROPY_QSTRDEFS_PORT ${MICROPY_PORT_DIR}/qstrdefsport.h)

# Our own builtin module. It has to be listed here — and compiled as part of
# the micropython target below, not the app — because MP_REGISTER_MODULE() is
# discovered by scanning MICROPY_SOURCE_QSTR. Compiled into `app` instead it
# would build cleanly and simply not exist in Python.
set(MDIOPROBE_MICROPY_SOURCES
    ${CMAKE_CURRENT_LIST_DIR}/src/modmdioprobe.c
    ${CMAKE_CURRENT_LIST_DIR}/src/mdioprobe_readline.c
)

set(MICROPY_SOURCE_QSTR
    ${MICROPY_SOURCE_PY}
    ${MICROPY_SOURCE_EXTMOD}
    ${MICROPY_SOURCE_SHARED}
    ${MICROPY_SOURCE_PORT}
    ${MICROPY_SOURCE_PORT_SRC}
    ${MDIOPROBE_MICROPY_SOURCES}
)

# The QSTR extraction step runs the compiler as a bare preprocessor, outside
# any CMake target, so it sees none of Zephyr's include paths or -D flags
# unless they are handed over explicitly. Without this it silently
# mis-evaluates everything guarded by CONFIG_*.
zephyr_get_include_directories_for_lang(C includes)
zephyr_get_system_include_directories_for_lang(C system_includes)
zephyr_get_compile_definitions_for_lang(C definitions)
zephyr_get_compile_options_for_lang(C options)
set(MICROPY_CPP_FLAGS_EXTRA ${includes} ${system_includes} ${definitions} ${options})

# Zephyr injects -std=c17 through zephyr_interface, but MicroPython needs GNU
# C: shared/runtime/gchelper_generic.c uses `register ... asm("r4")`. Carried
# on a separate INTERFACE target linked *after* zephyr_interface so its option
# lands later on the command line and wins.
add_library(mdioprobe_micropython_cflags INTERFACE)
target_compile_options(mdioprobe_micropython_cflags INTERFACE -std=gnu99)

add_library(${MICROPY_TARGET} STATIC)
target_link_libraries(${MICROPY_TARGET} PRIVATE
    zephyr_interface
    mdioprobe_micropython_cflags
)
target_include_directories(${MICROPY_TARGET} PRIVATE
    ${MICROPY_INC_CORE}
    ${MICROPY_PORT_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/src
    ${CMAKE_CURRENT_LIST_DIR}/include
    ${CMAKE_CURRENT_BINARY_DIR}
    ${CMAKE_BINARY_DIR}/generated
)
# gchelper_generic.c scans the stack conservatively from captured registers,
# which assumes no frame pointer is in the way.
target_compile_options(${MICROPY_TARGET} PRIVATE -fomit-frame-pointer)
target_compile_definitions(${MICROPY_TARGET} PRIVATE
    NDEBUG
    MP_CONFIGFILE=<${CONFIG_MICROPY_CONFIGFILE}>
    MICROPY_HEAP_SIZE=${CONFIG_MICROPY_HEAP_SIZE}
    # Needed even with MICROPY_VFS_FAT off: the QSTR pass preprocesses every
    # extmod source, and modvfs.c pulls in vfs_fat.h -> oofatfs/ff.h, which
    # refuses to be preprocessed without it. The FatFs code itself is still
    # compiled out.
    FFCONF_H=\"${MICROPY_OOFATFS_DIR}/ffconf.h\"
    MICROPY_VFS_FAT=$<BOOL:${CONFIG_MICROPY_VFS_FAT}>
    MICROPY_VFS_LFS1=$<BOOL:${CONFIG_MICROPY_VFS_LFS1}>
    MICROPY_VFS_LFS2=$<BOOL:${CONFIG_MICROPY_VFS_LFS2}>
)
target_sources(${MICROPY_TARGET} PRIVATE ${MICROPY_SOURCE_QSTR})

include(${MICROPY_DIR}/py/mkrules.cmake)

# modmdioprobe.c includes the generated version header.
add_dependencies(${MICROPY_TARGET} mdioprobe_version_header)

# mpconfigport.h includes <zephyr/autoconf.h>, so the generated headers have
# to exist before anything in this target compiles.
add_dependencies(${MICROPY_TARGET} zephyr_generated_headers)
add_dependencies(BUILD_VERSION_HEADER zephyr_generated_headers)

target_link_libraries(app PRIVATE ${MICROPY_TARGET})

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

target_sources(app PRIVATE
    src/main.c
    src/mdioprobe_adc_dac.c
    src/mdioprobe_timers.c
    src/mdioprobe_mdio_api.c
    src/mdioprobe_mdio_master.c
    src/mdioprobe_autocal.c
    src/mdioprobe_vreg.c
    src/mdioprobe_serial.c
    src/mdioprobe_led.c
    src/mdioprobe_usb.c
    src/mdioprobe_api.c
    src/mdioprobe_bus.c
    src/mdioprobe_reset.c
    src/mdioprobe_mp_usbd.c
    src/mdioprobe_usbd.c
    src/mdioprobe_msd.c
)

# include/ is the public API surface; src/ holds the private module
# headers that mdioprobe_api.h deliberately hides.
target_include_directories(app PRIVATE include src ${CMAKE_BINARY_DIR}/generated)
