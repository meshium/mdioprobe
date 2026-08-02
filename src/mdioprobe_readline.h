/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * The CLI's line reader: MicroPython's readline with a completer we control.
 */

#pragma once

#include "py/obj.h"
#include "py/misc.h"

/**
 * Read one line, showing `prompt`.
 *
 * Returns 0 for a completed line, or the control character that ended it
 * (CHAR_CTRL_C, CHAR_CTRL_D) — the same contract as MicroPython's readline().
 */
int mdioprobe_readline(vstr_t *line, const char *prompt);

/**
 * Install the Tab completer: fn(line) -> (insert, listing_or_None).
 *
 * `insert` is the text to add at the cursor, `listing` a block of candidates
 * to print when there is more than one. mp_const_none removes the completer,
 * which makes Tab do nothing.
 */
void mdioprobe_readline_set_completer(mp_obj_t fn);
