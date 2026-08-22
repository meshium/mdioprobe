/*
 * Copyright (c) 2026 Meshium
 *               Aleksandr Senin <al@meshium.net>
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * The `mdioprobe` builtin module: a mirror of include/mdioprobe_api.h and
 * nothing more.
 *
 * No policy lives here. The API layer already owns the interlocks (a known
 * bus rail before capture or the master will run, the connector's single
 * personality, the translator supply, the detector standing off while the
 * master drives), the front-end tuning and the indication. This file only
 * converts between C and Python, and turns negative errnos into exceptions
 * so a caller gets a traceback instead of a magic number.
 *
 * Files are deliberately absent: they reach Python through the standard `os`
 * module over the VFS, not through bindings of our own.
 */

#include <errno.h>
#include <string.h>

#include "py/runtime.h"
#include "py/obj.h"
#include "py/objstr.h"
#include "shared/readline/readline.h"


#include "mdioprobe_api.h"
#include "mdioprobe_readline.h"
#include "mdioprobe_version.h"
#include "mdioprobe_msd.h"
#include "mdioprobe_usbd.h"

/* Raise OSError(errno) for a negative return, otherwise pass the value on. */
static int check(int rc)
{
	if (rc < 0) {
		mp_raise_OSError(-rc);
	}
	return rc;
}

static void put(mp_obj_t d, qstr key, mp_obj_t val)
{
	mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(key), val);
}

static void put_int(mp_obj_t d, qstr key, mp_int_t val)
{
	put(d, key, mp_obj_new_int(val));
}

/* ---------------------------------------------------------------- */
/* Bus and target                                                    */
/* ---------------------------------------------------------------- */

static mp_obj_t mod_bus_get(void)
{
	struct mdioprobe_bus_info info;

	mdioprobe_bus_get(&info);

	mp_obj_t d = mp_obj_new_dict(6);

	put(d, MP_QSTR_target_present, mp_obj_new_bool(info.target_present));
	put(d, MP_QSTR_level_forced, mp_obj_new_bool(info.level_forced));
	put_int(d, MP_QSTR_nominal_mv, info.nominal_mv);
	put_int(d, MP_QSTR_measured_mv, info.measured_mv);
	put_int(d, MP_QSTR_samples_used, info.samples_used);
	put_int(d, MP_QSTR_samples_total, info.samples_total);
	return d;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_bus_get_obj, mod_bus_get);

static mp_obj_t mod_bus_ready(void)
{
	return mp_obj_new_bool(mdioprobe_bus_ready());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_bus_ready_obj, mod_bus_ready);

static mp_obj_t mod_bus_force_level(mp_obj_t mv_in)
{
	check(mdioprobe_bus_force_level(mp_obj_get_int(mv_in)));
	return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_bus_force_level_obj, mod_bus_force_level);

/* ---------------------------------------------------------------- */
/* Translator supply                                                 */
/* ---------------------------------------------------------------- */

static const char *vreg_state_name(enum mdioprobe_vreg_state s)
{
	switch (s) {
	case MDIOPROBE_VREG_OFF:       return "off";
	case MDIOPROBE_VREG_SOFTSTART: return "softstart";
	case MDIOPROBE_VREG_ON:        return "on";
	case MDIOPROBE_VREG_PASSTHRU:  return "passthru";
	case MDIOPROBE_VREG_FAULT:     return "fault";
	}
	return "?";
}

static mp_obj_t mod_vreg_get(void)
{
	struct mdioprobe_vreg_info info;

	mdioprobe_vreg_get(&info);

	mp_obj_t d = mp_obj_new_dict(5);

	put(d, MP_QSTR_state,
	    mp_obj_new_str_from_cstr(vreg_state_name(info.state)));
	put_int(d, MP_QSTR_target_mv, info.target_mv);
	put_int(d, MP_QSTR_measured_mv, info.measured_mv);
	put_int(d, MP_QSTR_min_mv, info.min_mv);
	put_int(d, MP_QSTR_max_mv, info.max_mv);
	return d;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_vreg_get_obj, mod_vreg_get);

static mp_obj_t mod_vreg_set(mp_obj_t mv_in)
{
	check(mdioprobe_vreg_set(mp_obj_get_int(mv_in)));
	return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_vreg_set_obj, mod_vreg_set);

static mp_obj_t mod_vreg_reset_excursion(void)
{
	mdioprobe_vreg_reset_excursion();
	return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_vreg_reset_excursion_obj,
				 mod_vreg_reset_excursion);

static mp_obj_t mod_vreg_off(void)
{
	mdioprobe_vreg_off();
	return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_vreg_off_obj, mod_vreg_off);

/* ---------------------------------------------------------------- */
/* MDC frequency, passive                                            */
/* ---------------------------------------------------------------- */

static mp_obj_t mod_mdc_freq(size_t n_args, const mp_obj_t *args)
{
	mp_int_t timeout_ms = (n_args > 0) ? mp_obj_get_int(args[0]) : 2000;
	uint32_t hz = 0;

	check(mdioprobe_mdc_freq(&hz, K_MSEC(timeout_ms)));
	return mp_obj_new_int_from_uint(hz);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_mdc_freq_obj, 0, 1, mod_mdc_freq);

/* ---------------------------------------------------------------- */
/* Passive capture                                                   */
/* ---------------------------------------------------------------- */

static mp_obj_t mod_capture_start(size_t n_args, const mp_obj_t *args)
{
	if (n_args > 0) {
		check(mdioprobe_capture_start_n(mp_obj_get_int(args[0])));
	} else {
		check(mdioprobe_capture_start());
	}
	return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_capture_start_obj, 0, 1,
					   mod_capture_start);

static mp_obj_t mod_capture_stop(void)
{
	mdioprobe_capture_stop();
	return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_capture_stop_obj, mod_capture_stop);

static mp_obj_t mod_capture_active(void)
{
	return mp_obj_new_bool(mdioprobe_capture_active());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_capture_active_obj, mod_capture_active);

/*
 * One frame, or None.
 *
 * None covers both "nothing arrived in time" and "the counted capture has
 * delivered everything"; capture_active() tells those apart. Returning None
 * rather than raising keeps the ordinary polling loop free of exception
 * handling — a timeout on a quiet bus is normal, not an error.
 */
static mp_obj_t mod_capture_next(size_t n_args, const mp_obj_t *args)
{
	mp_int_t timeout_ms = (n_args > 0) ? mp_obj_get_int(args[0]) : 1000;
	struct mdioprobe_frame f;

	int rc = mdioprobe_capture_next(&f, K_MSEC(timeout_ms));

	if (rc == -EAGAIN || rc == -ENODATA) {
		return mp_const_none;
	}
	check(rc);

	mp_obj_t d = mp_obj_new_dict(5);

	put_int(d, MP_QSTR_op, f.op);
	put_int(d, MP_QSTR_phy, f.phy);
	put_int(d, MP_QSTR_reg, f.reg);
	put_int(d, MP_QSTR_data, f.data);
	put_int(d, MP_QSTR_err, f.err);
	return d;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_capture_next_obj, 0, 1,
					   mod_capture_next);

/* ---------------------------------------------------------------- */
/* Active master, Clause 22                                          */
/* ---------------------------------------------------------------- */

static mp_obj_t mod_c22_read(mp_obj_t phy_in, mp_obj_t reg_in)
{
	uint16_t val = 0;

	check(mdioprobe_c22_read(mp_obj_get_int(phy_in), mp_obj_get_int(reg_in),
				 &val));
	return mp_obj_new_int_from_uint(val);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_c22_read_obj, mod_c22_read);

static mp_obj_t mod_c22_write(mp_obj_t phy_in, mp_obj_t reg_in, mp_obj_t val_in)
{
	check(mdioprobe_c22_write(mp_obj_get_int(phy_in), mp_obj_get_int(reg_in),
				  mp_obj_get_int(val_in)));
	return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_3(mod_c22_write_obj, mod_c22_write);

static mp_obj_t mod_c22_clock(size_t n_args, const mp_obj_t *args)
{
	if (n_args > 0) {
		check(mdioprobe_c22_set_clock(mp_obj_get_int(args[0])));
	}
	return mp_obj_new_int_from_uint(mdioprobe_c22_get_clock());
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_c22_clock_obj, 0, 1,
					   mod_c22_clock);

/* DIAGNOSTIC: (early, late) bit strings, MSB = first clock after the address. */
static mp_obj_t mod_c22_probe(size_t n_args, const mp_obj_t *args)
{
	uint32_t early = 0, late = 0;
	mp_int_t nbits = (n_args > 2) ? mp_obj_get_int(args[2]) : 24;

	check(mdioprobe_c22_probe(mp_obj_get_int(args[0]), mp_obj_get_int(args[1]),
				  (uint32_t)nbits, &early, &late));

	mp_obj_t t[3] = {
		mp_obj_new_int_from_uint(early),
		mp_obj_new_int_from_uint(late),
		mp_obj_new_int(nbits),
	};
	return mp_obj_new_tuple(3, t);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_c22_probe_obj, 2, 3, mod_c22_probe);

static mp_obj_t mod_c22_strict_ta(size_t n_args, const mp_obj_t *args)
{
	if (n_args > 0) {
		mdioprobe_c22_set_strict_ta(mp_obj_is_true(args[0]));
	}
	return mp_obj_new_bool(mdioprobe_c22_get_strict_ta());
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_c22_strict_ta_obj, 0, 1,
					   mod_c22_strict_ta);

/* ---------------------------------------------------------------- */
/* Target reset                                                      */
/* ---------------------------------------------------------------- */

static mp_obj_t mod_target_reset(size_t n_args, const mp_obj_t *args)
{
	mp_int_t ms = (n_args > 0) ? mp_obj_get_int(args[0]) : 0;

	check(mdioprobe_target_reset(ms));
	return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_target_reset_obj, 0, 1,
					   mod_target_reset);

/* True when the line reads released, i.e. the target's pull-up is present. */
static mp_obj_t mod_target_reset_released(void)
{
	return mp_obj_new_bool(check(mdioprobe_target_reset_line()) == 0);
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_target_reset_released_obj,
				 mod_target_reset_released);

/* ---------------------------------------------------------------- */
/* Connector mode                                                    */
/* ---------------------------------------------------------------- */

static mp_obj_t mod_mode_get(void)
{
	enum mdioprobe_connector_mode m = mdioprobe_connector_get_mode();

	return mp_obj_new_str_from_cstr(mdioprobe_connector_mode_name(m));
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_mode_get_obj, mod_mode_get);

static mp_obj_t mod_mode_set(mp_obj_t name_in)
{
	const char *name = mp_obj_str_get_str(name_in);
	enum mdioprobe_connector_mode m;

	if (strcmp(name, "mdio") == 0) {
		m = MDIOPROBE_CONN_MDIO;
	} else if (strcmp(name, "uart") == 0) {
		m = MDIOPROBE_CONN_UART;
	} else if (strcmp(name, "i2c") == 0) {
		m = MDIOPROBE_CONN_I2C;
	} else {
		mp_raise_ValueError(MP_ERROR_TEXT("mode must be mdio, uart or i2c"));
	}
	check(mdioprobe_connector_set_mode(m));
	return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_mode_set_obj, mod_mode_set);

/* ---------------------------------------------------------------- */
/* Diagnostics                                                       */
/* ---------------------------------------------------------------- */

static mp_obj_t mod_diag_get(void)
{
	struct mdioprobe_diag d;

	mdioprobe_diag_get(&d);

	mp_obj_t o = mp_obj_new_dict(14);

	put_int(o, MP_QSTR_dma_errors, d.dma_errors);
	put_int(o, MP_QSTR_overrun_count, d.overrun_count);
	put_int(o, MP_QSTR_torn_count, d.torn_count);
	put_int(o, MP_QSTR_byte_offset, d.byte_offset);
	put_int(o, MP_QSTR_cur_cndtr, d.cur_cndtr);
	put_int(o, MP_QSTR_threshold_mv, d.threshold_mv);
	put_int(o, MP_QSTR_nss_window, d.nss_window);
	put_int(o, MP_QSTR_comp_mdc_high, d.comp_mdc_high);
	put_int(o, MP_QSTR_comp_mdc_total, d.comp_mdc_total);
	put_int(o, MP_QSTR_comp_mdio_high, d.comp_mdio_high);
	put_int(o, MP_QSTR_comp_mdio_total, d.comp_mdio_total);
	put_int(o, MP_QSTR_master_khz, d.master_khz);
	put_int(o, MP_QSTR_master_actual_khz, d.master_actual_khz);
	put(o, MP_QSTR_usb_crs_synced, mp_obj_new_bool(d.usb_crs_synced));
	return o;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_diag_get_obj, mod_diag_get);

static mp_obj_t mod_diag_ring_dump(size_t n_args, const mp_obj_t *args)
{
	mp_int_t want = (n_args > 0) ? mp_obj_get_int(args[0]) : 64;

	if (want <= 0) {
		mp_raise_ValueError(MP_ERROR_TEXT("byte count must be positive"));
	}

	vstr_t vstr;

	vstr_init_len(&vstr, want);
	uint32_t got = mdioprobe_diag_ring_dump((uint8_t *)vstr.buf, want);

	vstr.len = got;
	return mp_obj_new_bytes_from_vstr(&vstr);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_diag_ring_dump_obj, 0, 1,
					   mod_diag_ring_dump);

/* ---------------------------------------------------------------- */
/* Version                                                           */
/* ---------------------------------------------------------------- */

static mp_obj_t mod_version(void)
{
	mp_obj_t d = mp_obj_new_dict(4);

	put(d, MP_QSTR_version, mp_obj_new_str_from_cstr(MDIOPROBE_VERSION));
	put(d, MP_QSTR_full, mp_obj_new_str_from_cstr(MDIOPROBE_VERSION_FULL));
	put(d, MP_QSTR_git, mp_obj_new_str_from_cstr(MDIOPROBE_GIT_DESCRIBE));
	put(d, MP_QSTR_dirty, mp_obj_new_bool(MDIOPROBE_GIT_DIRTY));
	return d;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_version_obj, mod_version);

/* ---------------------------------------------------------------- */
/* Console                                                           */
/* ---------------------------------------------------------------- */

static mp_obj_t mod_console_ready(void)
{
	return mp_obj_new_bool(mdioprobe_console_ready());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_console_ready_obj, mod_console_ready);

/*
 * The CLI's line reader. Same contract as input(), including the exceptions,
 * so the shell loop reads the same either way — but Tab reaches the
 * completer registered below instead of MicroPython's own Python-name one.
 */
static mp_obj_t mod_readline(size_t n_args, const mp_obj_t *args)
{
	const char *prompt = (n_args > 0) ? mp_obj_str_get_str(args[0]) : "";
	vstr_t line;

	vstr_init(&line, 32);

	int rc = mdioprobe_readline(&line, prompt);

	if (rc == CHAR_CTRL_C) {
		vstr_clear(&line);
		mp_raise_type(&mp_type_KeyboardInterrupt);
	}
	if (rc == CHAR_CTRL_D && vstr_len(&line) == 0) {
		vstr_clear(&line);
		mp_raise_type(&mp_type_EOFError);
	}
	return mp_obj_new_str_from_vstr(&line);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_readline_obj, 0, 1, mod_readline);

static mp_obj_t mod_set_completer(mp_obj_t fn)
{
	mdioprobe_readline_set_completer(fn);
	return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_set_completer_obj, mod_set_completer);

/*
 * Clear the line history.
 *
 * Needed because the Zephyr port never calls readline_init0(), which every
 * other port calls right after gc_init() on each soft reset. The history is
 * an array of pointers into the GC heap; a soft reset re-initialises that
 * heap and leaves the pointers dangling. _boot.py calls this, and _boot.py
 * is what upstream runs after every soft reset.
 */
static mp_obj_t mod_history_reset(void)
{
	readline_init0();
	return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_history_reset_obj, mod_history_reset);

/* ---------------------------------------------------------------- */
/* Mass storage                                                      */
/* ---------------------------------------------------------------- */

static mp_obj_t mod_format_storage(void)
{
	check(mdioprobe_msd_format());
	return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_format_storage_obj, mod_format_storage);

static mp_obj_t mod_volume_label(void)
{
	char label[12];

	check(mdioprobe_msd_label(label, sizeof(label)));
	return mp_obj_new_str(label, strlen(label));
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_volume_label_obj, mod_volume_label);

static mp_obj_t mod_msd(void)
{
	check(mdioprobe_msd_enter());
	return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_msd_obj, mod_msd);

/* ---------------------------------------------------------------- */
/* Module table                                                      */
/* ---------------------------------------------------------------- */

static const mp_rom_map_elem_t mdioprobe_module_globals_table[] = {
	{ MP_ROM_QSTR(MP_QSTR___name__),        MP_ROM_QSTR(MP_QSTR_mdioprobe) },

	{ MP_ROM_QSTR(MP_QSTR_bus_get),         MP_ROM_PTR(&mod_bus_get_obj) },
	{ MP_ROM_QSTR(MP_QSTR_bus_ready),       MP_ROM_PTR(&mod_bus_ready_obj) },
	{ MP_ROM_QSTR(MP_QSTR_bus_force_level), MP_ROM_PTR(&mod_bus_force_level_obj) },

	{ MP_ROM_QSTR(MP_QSTR_vreg_get),        MP_ROM_PTR(&mod_vreg_get_obj) },
	{ MP_ROM_QSTR(MP_QSTR_vreg_set),        MP_ROM_PTR(&mod_vreg_set_obj) },
	{ MP_ROM_QSTR(MP_QSTR_vreg_off),        MP_ROM_PTR(&mod_vreg_off_obj) },
	{ MP_ROM_QSTR(MP_QSTR_vreg_reset_excursion),
	  MP_ROM_PTR(&mod_vreg_reset_excursion_obj) },

	{ MP_ROM_QSTR(MP_QSTR_mdc_freq),        MP_ROM_PTR(&mod_mdc_freq_obj) },

	{ MP_ROM_QSTR(MP_QSTR_capture_start),   MP_ROM_PTR(&mod_capture_start_obj) },
	{ MP_ROM_QSTR(MP_QSTR_capture_stop),    MP_ROM_PTR(&mod_capture_stop_obj) },
	{ MP_ROM_QSTR(MP_QSTR_capture_active),  MP_ROM_PTR(&mod_capture_active_obj) },
	{ MP_ROM_QSTR(MP_QSTR_capture_next),    MP_ROM_PTR(&mod_capture_next_obj) },

	{ MP_ROM_QSTR(MP_QSTR_c22_read),        MP_ROM_PTR(&mod_c22_read_obj) },
	{ MP_ROM_QSTR(MP_QSTR_c22_write),       MP_ROM_PTR(&mod_c22_write_obj) },
	{ MP_ROM_QSTR(MP_QSTR_c22_clock),       MP_ROM_PTR(&mod_c22_clock_obj) },
	{ MP_ROM_QSTR(MP_QSTR_c22_strict_ta),   MP_ROM_PTR(&mod_c22_strict_ta_obj) },
	{ MP_ROM_QSTR(MP_QSTR_c22_probe),       MP_ROM_PTR(&mod_c22_probe_obj) },

	{ MP_ROM_QSTR(MP_QSTR_target_reset),    MP_ROM_PTR(&mod_target_reset_obj) },
	{ MP_ROM_QSTR(MP_QSTR_target_reset_released),
	  MP_ROM_PTR(&mod_target_reset_released_obj) },

	{ MP_ROM_QSTR(MP_QSTR_mode_get),        MP_ROM_PTR(&mod_mode_get_obj) },
	{ MP_ROM_QSTR(MP_QSTR_mode_set),        MP_ROM_PTR(&mod_mode_set_obj) },

	{ MP_ROM_QSTR(MP_QSTR_diag_get),        MP_ROM_PTR(&mod_diag_get_obj) },
	{ MP_ROM_QSTR(MP_QSTR_diag_ring_dump),  MP_ROM_PTR(&mod_diag_ring_dump_obj) },

	{ MP_ROM_QSTR(MP_QSTR_version),         MP_ROM_PTR(&mod_version_obj) },
	{ MP_ROM_QSTR(MP_QSTR_msd),             MP_ROM_PTR(&mod_msd_obj) },
	{ MP_ROM_QSTR(MP_QSTR_volume_label),    MP_ROM_PTR(&mod_volume_label_obj) },
	{ MP_ROM_QSTR(MP_QSTR_format_storage), MP_ROM_PTR(&mod_format_storage_obj) },
	{ MP_ROM_QSTR(MP_QSTR_console_ready),   MP_ROM_PTR(&mod_console_ready_obj) },
	{ MP_ROM_QSTR(MP_QSTR_readline),        MP_ROM_PTR(&mod_readline_obj) },
	{ MP_ROM_QSTR(MP_QSTR_set_completer),   MP_ROM_PTR(&mod_set_completer_obj) },
	{ MP_ROM_QSTR(MP_QSTR_history_reset),   MP_ROM_PTR(&mod_history_reset_obj) },
};
static MP_DEFINE_CONST_DICT(mdioprobe_module_globals,
			    mdioprobe_module_globals_table);

const mp_obj_module_t mdioprobe_module = {
	.base = { &mp_type_module },
	.globals = (mp_obj_dict_t *)&mdioprobe_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_mdioprobe, mdioprobe_module);
