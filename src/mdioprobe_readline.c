/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * A line reader for the CLI, so Tab can complete commands.
 *
 * MicroPython's readline is good — arrow keys, Home/End, history — and none
 * of it is reimplemented here. The one thing it does not offer is a way in:
 * Tab is wired straight to `mp_repl_autocomplete()`, which completes Python
 * names out of the `__main__` globals and the qstr pool. At a `mdio>` prompt
 * that is useless, and there is no hook, callback or config option to point
 * it elsewhere — the call in readline.c is by name.
 *
 * So the loop that `readline()` runs (shared/readline/readline.c) is
 * repeated here with one branch added: Tab is taken out before
 * readline_process_char() can see it, handed to a Python completer, and the
 * text to insert is fed back in character by character. Feeding it back is
 * what keeps this small — the redraw, the cursor and the history stay
 * upstream's job, and this file never touches a terminal escape except to
 * move the cursor to end of line.
 *
 * Why not the `mp_hal_readline` macro, which would have made input() do all
 * this: mp_builtin_input() prints the prompt itself and passes readline an
 * empty one (py/modbuiltins.c:224-230), so a completer reached that way
 * could never redraw the prompt after listing candidates. Being its own
 * function also leaves input() alone for user scripts.
 */

#include <string.h>

#include <zephyr/kernel.h>

#include "py/mphal.h"
#include "py/obj.h"
#include "py/runtime.h"
#include "shared/readline/readline.h"

/* Set from Python: fn(line) -> (insert, listing_or_None). */
static mp_obj_t completer = MP_OBJ_NULL;

void mdioprobe_readline_set_completer(mp_obj_t fn)
{
	completer = (fn == mp_const_none) ? MP_OBJ_NULL : fn;
}

static void feed(const char *text, size_t len)
{
	for (size_t i = 0; i < len; i++) {
		readline_process_char((int)(unsigned char)text[i]);
	}
}

/*
 * Complete at the cursor.
 *
 * The cursor is forced to end of line first, with an End escape fed through
 * the state machine. Two reasons: the completer only ever sees the whole
 * line, so completing from the middle would insert in the wrong place; and
 * the line vstr belongs to us, which means its contents are readable here
 * while the cursor position is private to readline. Moving the cursor is the
 * cheaper of the two ways to make those agree. `ESC [ F` is handled
 * unconditionally by readline, unlike the Ctrl-E alias, which is behind
 * MICROPY_REPL_EMACS_KEYS and off in this build.
 */
static void complete(vstr_t *line, const char *prompt)
{
	if (completer == MP_OBJ_NULL) {
		return;
	}

	feed("\x1b[F", 3);

	mp_obj_t arg = mp_obj_new_str(vstr_str(line), vstr_len(line));
	mp_obj_t result;

	/* A broken completer must not take the command line down with it. */
	nlr_buf_t nlr;
	if (nlr_push(&nlr) != 0) {
		nlr_pop();
		return;
	}
	result = mp_call_function_1(completer, arg);
	nlr_pop();

	mp_obj_t *parts;
	size_t n_parts;

	mp_obj_get_array(result, &n_parts, &parts);
	if (n_parts != 2) {
		return;
	}

	/* Candidates, if the completer had more than one. Printed above a fresh
	 * prompt, which then has to be redrawn along with what was typed —
	 * readline only ever draws what changes. */
	if (parts[1] != mp_const_none) {
		size_t len;
		const char *text = mp_obj_str_get_data(parts[1], &len);

		mp_hal_stdout_tx_str("\r\n");
		mp_hal_stdout_tx_strn(text, len);
		mp_hal_stdout_tx_str("\r\n");
		mp_hal_stdout_tx_str(prompt);
		mp_hal_stdout_tx_strn(vstr_str(line), vstr_len(line));
	}

	size_t insert_len;
	const char *insert = mp_obj_str_get_data(parts[0], &insert_len);

	feed(insert, insert_len);
}

/*
 * Read one line. Returns 0, or the control character that ended it, exactly
 * like readline() — the caller turns those into the same exceptions input()
 * raises.
 */
int mdioprobe_readline(vstr_t *line, const char *prompt)
{
	/* readline_init() prints the prompt itself (readline.c:558) — printing
	 * it here as well is how this drew two of them. */
	readline_init(line, prompt);

	for (;;) {
		int c = mp_hal_stdin_rx_chr();

		if (c == '\t') {
			complete(line, prompt);
			continue;
		}

		int r = readline_process_char(c);

		if (r >= 0) {
			return r;
		}
	}
}
