"""INV-31 part 1: maintain_orders cluster -> WriteQueue + B1 fix.
Deterministic, anchor-verified transform of engine/bot_executor.py.
Aborts (no write) if any replacement count != expected.
"""
import sys

PATH = "engine/bot_executor.py"
src = open(PATH, encoding="utf-8").read()
orig = src

applied = []

def rep(old, new, count=1, label=""):
    global src
    n = src.count(old)
    if n != count:
        print(f"ABORT [{label}]: expected {count} occurrence(s), found {n}")
        print("--- old text sought ---")
        print(old[:400])
        sys.exit(1)
    src = src.replace(old, new)
    applied.append(label)

# ══════════════════════════════════════════════════════════════════════════
# A. maintain_orders WriteQueue import
# ══════════════════════════════════════════════════════════════════════════
rep(
    '        """\n        Ensures TP and Grid orders are placed active trades.\n        """\n        trade_update_data = {}\n',
    '        """\n        Ensures TP and Grid orders are placed active trades.\n\n        INV-31: all trades/bot_orders/bots writes route through the WriteQueue\n        singleton via module-level _maintain_*_internal functions.\n        """\n        from engine.write_queue import WriteQueue\n        trade_update_data = {}\n',
    1, "A0 maintain_orders WQ import")

# ══════════════════════════════════════════════════════════════════════════
# B. _hc_enforce_conn sites
# ══════════════════════════════════════════════════════════════════════════
rep(
    '                                _hc_enforce_conn.execute(\n'
    '                                    "UPDATE bots SET status = \'pending_flatten\', cascade_started_at = ? WHERE id = ?",\n'
    '                                    (int(time.time()), bot_id)\n'
    '                                )\n'
    '                                _hc_enforce_conn.commit()\n',
    '                                WriteQueue().put_and_wait(\n'
    '                                    _maintain_pending_flatten_status_internal,\n'
    '                                    bot_id, int(time.time())\n'
    '                                )\n',
    1, "B1 pending_flatten")

# Live-guard INV30 block (2810-2863): move the whole DB-correction transaction
# into one internal fn (own connection, single commit). Exchange reads
# (fetch_ticker) stay on the caller side via the price hint fallback inside.
rep(
    '                                                try:\n'
    '                                                    _corrected_qty = round(live_hedge_qty, 8)\n'
    '                                                    _hc_enforce_conn.execute(\n'
    '                                                        "UPDATE trades SET open_qty = ? WHERE bot_id = ?",\n'
    '                                                        (_corrected_qty, bot_id)\n'
    '                                                    )\n'
    '                                                    # Insert reconciliation marker\n'
    '                                                    import time as _time_mod\n'
    "                                                    # Delete any prior LIVE_GUARD_INV30 rows for that exact (bot_id, step, cycle_id) to prevent stacking\n"
    '                                                    _hc_enforce_conn.execute(\n'
    '                                                        "DELETE FROM bot_orders "\n'
    "                                                        \"WHERE bot_id = ? AND step = ? AND cycle_id = ? AND client_order_id LIKE '%LIVE_GUARD_INV30%'\",\n"
    '                                                        (bot_id, child_step, child_cycle_id)\n'
    '                                                    )\n'
    '                                                    _recon_cid = f"CQB_{bot_id}_LIVE_GUARD_INV30_{child_cycle_id}_{child_step}"\n'
    '                                                    _recon_price = 0.0\n'
    '                                                    try:\n'
    '                                                        _ticker = exchange.fetch_ticker(pair)\n'
    "                                                        _recon_price = float(_ticker.get('last') or 0.0)\n"
    '                                                    except Exception:\n'
    '                                                        pass\n'
    '                                                    if _recon_price <= 0.0:\n'
    '                                                        try:\n'
    '                                                            _parent_row = _hc_enforce_conn.execute(\n'
    '                                                                "SELECT avg_entry_price FROM trades WHERE bot_id = ?",\n'
    '                                                                (bot_id,)\n'
    '                                                            ).fetchone()\n'
    '                                                            if _parent_row:\n'
    '                                                                _recon_price = float(_parent_row[0] or 0.0)\n'
    '                                                        except Exception:\n'
    '                                                            pass\n'
    '                                                    _hc_enforce_conn.execute(\n'
    '                                                        "INSERT OR IGNORE INTO bot_orders "\n'
    '                                                        "(bot_id, step, order_type, order_id, price, amount, status, "\n'
    '                                                        " created_at, client_order_id, updated_at, notes, cycle_id, "\n'
    '                                                        " filled_amount, position_side) "\n'
    "                                                        \"VALUES (?, ?, 'entry', ?, ?, ?, 'filled', ?, ?, ?, \"\n"
    "                                                        \" 'Live-guard INV30 DB sync: hedge already present on exchange.', \"\n"
    '                                                        " ?, ?, ?)",\n'
    '                                                        (\n'
    '                                                            bot_id, child_step,\n'
    '                                                            _recon_cid,\n'
    '                                                            _recon_price,\n'
    '                                                            _corrected_qty,\n'
    '                                                            int(_time_mod.time()),\n'
    '                                                            _recon_cid,\n'
    '                                                            int(_time_mod.time()),\n'
    '                                                            child_cycle_id,\n'
    '                                                            _corrected_qty,\n'
    '                                                            child_direction_early\n'
    '                                                        )\n'
    '                                                    )\n'
    '                                                    _hc_enforce_conn.commit()\n',
    '                                                try:\n'
    '                                                    _corrected_qty = round(live_hedge_qty, 8)\n'
    '                                                    _recon_price = 0.0\n'
    '                                                    try:\n'
    '                                                        _ticker = exchange.fetch_ticker(pair)\n'
    "                                                        _recon_price = float(_ticker.get('last') or 0.0)\n"
    '                                                    except Exception:\n'
    '                                                        pass\n'
    '                                                    _recon_cid = f"CQB_{bot_id}_LIVE_GUARD_INV30_{child_cycle_id}_{child_step}"\n'
    '                                                    WriteQueue().put_and_wait(\n'
    '                                                        _maintain_live_guard_recon_internal,\n'
    '                                                        bot_id, child_step, child_cycle_id,\n'
    '                                                        _corrected_qty, _recon_price,\n'
    '                                                        _recon_cid, child_direction_early\n'
    '                                                    )\n',
    1, "B2 live-guard INV30 block")

# ══════════════════════════════════════════════════════════════════════════
# C. _c_cancelling DELETE sites (x2 identical)
# ══════════════════════════════════════════════════════════════════════════
rep(
    '                            _c_cancelling.execute("DELETE FROM bot_orders WHERE id = ?", (db_id,))\n'
    '                            _c_cancelling.commit()\n',
    '                            WriteQueue().put_and_wait(_maintain_cancel_purge_order_internal, db_id)\n',
    2, "C1 cancel-purge delete x2")

# ══════════════════════════════════════════════════════════════════════════
# D. _hc_conn sites
# ══════════════════════════════════════════════════════════════════════════
rep(
    '                _hc_conn.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))\n'
    '                _hc_conn.commit()\n',
    '                WriteQueue().put_and_wait(_maintain_tp_clear_internal, bot_id)\n',
    1, "D1 placeholder tp clear (16sp)")

rep(
    '                            _hc_conn.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))\n'
    '                            _hc_conn.commit()\n',
    '                            WriteQueue().put_and_wait(_maintain_tp_clear_internal, bot_id)\n',
    4, "D2 hedge-maintain tp clear x4 (28sp)")

rep(
    '                            _hc_conn.execute(\n'
    '                                "UPDATE trades SET tp_order_id = NULL WHERE bot_id = ? "\n'
    "                                \"AND (tp_order_id IS NULL OR tp_order_id LIKE 'PENDING_BE_%')\",\n"
    '                                (bot_id,)\n'
    '                            )\n'
    '                            _hc_conn.commit()\n',
    '                            WriteQueue().put_and_wait(_maintain_tp_clear_placeholder_be_internal, bot_id)\n',
    1, "D3a PENDING_BE placeholder clear (28sp)")

rep(
    '                                    _hc_conn.execute(\n'
    '                                        "UPDATE trades SET tp_order_id = NULL WHERE bot_id = ? "\n'
    "                                        \"AND (tp_order_id IS NULL OR tp_order_id LIKE 'PENDING_BE_%')\",\n"
    '                                        (bot_id,)\n'
    '                                    )\n'
    '                                    _hc_conn.commit()\n',
    '                                    WriteQueue().put_and_wait(_maintain_tp_clear_placeholder_be_internal, bot_id)\n',
    1, "D3b PENDING_BE placeholder clear (36sp)")

rep(
    '                    _hc_conn.execute(\n'
    '                        "UPDATE bot_orders SET amount = ? WHERE bot_id = ? AND client_order_id = ? AND status = \'pending_placement\'",\n'
    '                        (tp_amount, bot_id, be_cid)\n'
    '                    )\n'
    '                    _hc_conn.commit()\n',
    '                    WriteQueue().put_and_wait(\n'
    '                        _maintain_be_tp_amount_sync_internal,\n'
    '                        bot_id, be_cid, tp_amount\n'
    '                    )\n',
    1, "D4 BE-TP amount sync")

rep(
    '                        _hc_conn.execute(\n'
    '                            "UPDATE bot_orders SET order_id = ?, status = ?, updated_at = ? "\n'
    '                            "WHERE bot_id = ? AND client_order_id = ? AND status = \'pending_placement\'",\n'
    "                            (resting['id'], resting.get('status', 'open'), int(time.time()), bot_id, be_cid)\n"
    '                        )\n'
    '                        _hc_conn.execute("UPDATE trades SET tp_order_id = ? WHERE bot_id = ?", (resting[\'id\'], bot_id))\n'
    '                        _hc_conn.commit()\n',
    '                        WriteQueue().put_and_wait(\n'
    '                            _maintain_be_tp_resting_heal_internal,\n'
    "                            bot_id, be_cid, resting['id'], resting.get('status', 'open'), int(time.time())\n"
    '                        )\n',
    1, "D5 BE-TP resting heal")

rep(
    '                                    _hc_conn.execute(\n'
    '                                        "UPDATE bot_orders SET order_id = ?, status = ?, client_order_id = ?, updated_at = ? "\n'
    '                                        "WHERE bot_id = ? AND client_order_id = ? AND status = \'pending_placement\'",\n'
    "                                        (order['id'], order.get('status', 'open'), actual_cid, int(time.time()), bot_id, be_cid)\n"
    '                                    )\n'
    '                                    _hc_conn.execute("UPDATE trades SET tp_order_id = ? WHERE bot_id = ?", (order[\'id\'], bot_id))\n'
    '                                    _hc_conn.commit()\n',
    '                                    WriteQueue().put_and_wait(\n'
    '                                        _maintain_be_tp_placed_internal,\n'
    "                                        bot_id, be_cid, actual_cid, order['id'], order.get('status', 'open'), int(time.time())\n"
    '                                    )\n',
    1, "D6 BE-TP placed")

# ══════════════════════════════════════════════════════════════════════════
# E. _conn_partial sites
# ══════════════════════════════════════════════════════════════════════════
rep(
    '                                wipe_bot_ghost(exchange, bot_id, _conn_partial)\n'
    '                                _conn_partial.commit()\n'
    '                                return None\n',
    '                                wipe_bot_ghost(exchange, bot_id, _conn_partial)\n'
    '                                return None\n',
    1, "E1 drop redundant commit after wipe_bot_ghost")

rep(
    '                        try:\n'
    '                            _conn_partial.execute(\n'
    "                                \"UPDATE trades SET cycle_phase='STUCK_DUST_NO_EXIT' WHERE bot_id=?\",\n"
    '                                (bot_id,)\n'
    '                            )\n'
    '                            _conn_partial.commit()\n',
    '                        try:\n'
    '                            WriteQueue().put_and_wait(_maintain_stuck_dust_phase_internal, bot_id)\n',
    1, "E2 partial-close stuck dust")

# ══════════════════════════════════════════════════════════════════════════
# F. _c sites (tp evictor) x6 — indentation varies (24/28/32/36sp), use regex
# ══════════════════════════════════════════════════════════════════════════
import re
_f_pat = re.compile(
    r'\n( +)_c = _gc\(\)\n'
    r'\1_c\.execute\("UPDATE trades SET tp_order_id = NULL WHERE bot_id = \?", \(bot_id,\)\)\n'
    r'\1_c\.commit\(\); _c\.close\(\)\n'
)
_f_matches = list(_f_pat.finditer(src))
if len(_f_matches) != 6:
    print(f"ABORT [F1 _c tp clear]: expected 6 regex matches, found {len(_f_matches)}")
    sys.exit(1)
# Verify all matches are inside maintain_orders (lines 2620-4941 of current src)
_mo_start_line = src[:src.index("def maintain_orders(")].count("\n") + 1
_mo_end_line = src[:src.index("def _signal_hedge_child_entry(")].count("\n") + 1
for m in _f_matches:
    ln = src[:m.start()].count("\n") + 1
    if not (_mo_start_line <= ln <= _mo_end_line):
        print(f"ABORT [F1]: match at line {ln} outside maintain_orders ({_mo_start_line}-{_mo_end_line})")
        sys.exit(1)
src = _f_pat.sub(
    lambda m: "\n" + m.group(1) + "WriteQueue().put_and_wait(_maintain_tp_clear_internal, bot_id)\n",
    src
)
applied.append("F1 _c tp clear x6 (regex)")

# ══════════════════════════════════════════════════════════════════════════
# G. with-block sites (stuck dust / margin held clear / margin held set)
# ══════════════════════════════════════════════════════════════════════════
rep(
    '                                                from engine.database import get_connection as _gc_stuck\n'
    '                                                with _gc_stuck() as _conn_stuck:\n'
    '                                                    _conn_stuck.execute(\n'
    "                                                        \"UPDATE trades SET cycle_phase='STUCK_DUST_NO_EXIT' WHERE bot_id=?\",\n"
    '                                                        (bot_id,)\n'
    '                                                    )\n',
    '                                                WriteQueue().put_and_wait(_maintain_stuck_dust_phase_internal, bot_id)\n',
    1, "G1 stuck dust with-block")

rep(
    '                                                from engine.database import get_connection as _gc_mhc\n'
    '                                                with _gc_mhc() as _conn_mhc:\n'
    '                                                    _conn_mhc.execute(\n'
    "                                                        \"UPDATE trades SET cycle_phase='ACTIVE' WHERE bot_id=? AND cycle_phase IN ('MARGIN_HELD', 'STUCK_DUST_NO_EXIT')\",\n"
    '                                                        (bot_id,)\n'
    '                                                    )\n',
    '                                                WriteQueue().put_and_wait(_maintain_margin_held_clear_internal, bot_id)\n',
    1, "G2 margin held clear with-block")

rep(
    '                                        from engine.database import get_connection as _gc_mh\n'
    '                                        with _gc_mh() as _conn_mh:\n'
    '                                            _conn_mh.execute(\n'
    "                                                \"UPDATE trades SET cycle_phase='MARGIN_HELD' WHERE bot_id=? AND cycle_phase NOT IN ('CARRY_PENDING')\",\n"
    '                                                (bot_id,)\n'
    '                                            )\n',
    '                                        WriteQueue().put_and_wait(_maintain_margin_held_set_internal, bot_id)\n',
    1, "G3 margin held set with-block")

# ══════════════════════════════════════════════════════════════════════════
# H. _csp entry_confirmed x2 identical
# ══════════════════════════════════════════════════════════════════════════
rep(
    '                                 _csp.execute(\n'
    '                                     "UPDATE trades SET entry_confirmed=1 WHERE bot_id=?", (bot_id,)\n'
    '                                 )\n'
    '                                 _csp.commit()\n',
    '                                 WriteQueue().put_and_wait(_maintain_entry_confirmed_internal, bot_id)\n',
    2, "H1 entry_confirmed x2")

# ══════════════════════════════════════════════════════════════════════════
# I. grid order clear
# ══════════════════════════════════════════════════════════════════════════
rep(
    '                                   _c = _gc()\n'
    '                                   _c.execute("UPDATE trades SET grid_order_id = NULL WHERE bot_id = ?", (bot_id,))\n'
    '                                   _c.commit(); _c.close()\n',
    '                                   WriteQueue().put_and_wait(_maintain_grid_order_clear_internal, bot_id)\n',
    1, "I1 grid order clear")

# ══════════════════════════════════════════════════════════════════════════
# J. O-10 watchdog freeze (bots table, in maintain_orders)
# ══════════════════════════════════════════════════════════════════════════
rep(
    '                                import time as _wd_time\n'
    '                                _conn_hcs.execute(\n'
    "                                    \"UPDATE bots SET status='REQUIRE_MANUAL_PROOF', last_error=?, last_error_time=? WHERE id=?\",\n"
    "                                    (\"HEDGE_ENGAGE_FAILURE:\" + _wd.get('reason', ''), int(_wd_time.time()), bot_id)\n"
    '                                )\n'
    '                                _conn_hcs.commit()\n',
    '                                import time as _wd_time\n'
    '                                WriteQueue().put_and_wait(\n'
    '                                    _maintain_hedge_freeze_internal,\n'
    "                                    bot_id, \"HEDGE_ENGAGE_FAILURE:\" + _wd.get('reason', ''), int(_wd_time.time())\n"
    '                                )\n',
    1, "J1 O-10 hedge freeze")

# ══════════════════════════════════════════════════════════════════════════
# K. Insert internal functions block before `class BotExecutor:`
# ══════════════════════════════════════════════════════════════════════════
INTERNAL_FNS = '''

# ═════════════════════════════════════════════════════════════════════════
# INV-31 WriteQueue internals — maintain_orders cluster (+ B1 fix)
# Each runs on the WriteQueue worker thread with its own connection.
# Must NOT be called directly — always via WriteQueue().put_and_wait().
# ═════════════════════════════════════════════════════════════════════════

def _maintain_pending_flatten_status_internal(bot_id: int, cascade_started_at: int):
    """INV-31 WriteQueue internal — over-hedged child -> pending_flatten."""
    from engine.database import get_connection
    conn = get_connection()
    conn.execute(
        "UPDATE bots SET status = 'pending_flatten', cascade_started_at = ? WHERE id = ?",
        (cascade_started_at, bot_id)
    )
    conn.commit()


def _maintain_live_guard_recon_internal(
    bot_id: int,
    child_step: int,
    child_cycle_id: int,
    corrected_qty: float,
    recon_price: float,
    recon_cid: str,
    position_side: str,
):
    """
    INV-31 WriteQueue internal — HEDGE-LIVE-GUARD-INV30 DB correction.
    Corrects trades.open_qty, deletes prior LIVE_GUARD_INV30 markers and
    inserts a fresh reconciliation row, in ONE transaction.
    If recon_price <= 0 the caller's ticker read failed; fall back to the
    parent's avg_entry_price (read on this connection).
    """
    import time as _time_mod
    from engine.database import get_connection
    conn = get_connection()
    if recon_price <= 0.0:
        try:
            _parent_row = conn.execute(
                "SELECT avg_entry_price FROM trades WHERE bot_id = ?",
                (bot_id,)
            ).fetchone()
            if _parent_row:
                recon_price = float(_parent_row[0] or 0.0)
        except Exception:
            pass
    conn.execute(
        "UPDATE trades SET open_qty = ? WHERE bot_id = ?",
        (corrected_qty, bot_id)
    )
    conn.execute(
        "DELETE FROM bot_orders "
        "WHERE bot_id = ? AND step = ? AND cycle_id = ? AND client_order_id LIKE '%LIVE_GUARD_INV30%'",
        (bot_id, child_step, child_cycle_id)
    )
    conn.execute(
        "INSERT OR IGNORE INTO bot_orders "
        "(bot_id, step, order_type, order_id, price, amount, status, "
        " created_at, client_order_id, updated_at, notes, cycle_id, "
        " filled_amount, position_side) "
        "VALUES (?, ?, 'entry', ?, ?, ?, 'filled', ?, ?, ?, "
        " 'Live-guard INV30 DB sync: hedge already present on exchange.', "
        " ?, ?, ?)",
        (
            bot_id, child_step,
            recon_cid,
            recon_price,
            corrected_qty,
            int(_time_mod.time()),
            recon_cid,
            int(_time_mod.time()),
            child_cycle_id,
            corrected_qty,
            position_side
        )
    )
    conn.commit()


def _maintain_cancel_purge_order_internal(order_row_id: int):
    """INV-31 WriteQueue internal — delete a verified-gone cancelling bot_orders row."""
    from engine.database import get_connection
    conn = get_connection()
    conn.execute("DELETE FROM bot_orders WHERE id = ?", (order_row_id,))
    conn.commit()


def _maintain_tp_clear_internal(bot_id: int):
    """INV-31 WriteQueue internal — clear trades.tp_order_id."""
    from engine.database import get_connection
    conn = get_connection()
    conn.execute(
        "UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?",
        (bot_id,)
    )
    conn.commit()


def _maintain_tp_clear_placeholder_be_internal(bot_id: int):
    """INV-31 WriteQueue internal — clear tp_order_id only if NULL or a PENDING_BE_* placeholder."""
    from engine.database import get_connection
    conn = get_connection()
    conn.execute(
        "UPDATE trades SET tp_order_id = NULL WHERE bot_id = ? "
        "AND (tp_order_id IS NULL OR tp_order_id LIKE 'PENDING_BE_%')",
        (bot_id,)
    )
    conn.commit()


def _maintain_be_tp_amount_sync_internal(bot_id: int, client_order_id: str, amount: float):
    """INV-31 WriteQueue internal — sync pending_placement BE-TP row amount."""
    from engine.database import get_connection
    conn = get_connection()
    conn.execute(
        "UPDATE bot_orders SET amount = ? WHERE bot_id = ? AND client_order_id = ? AND status = 'pending_placement'",
        (amount, bot_id, client_order_id)
    )
    conn.commit()


def _maintain_be_tp_resting_heal_internal(
    bot_id: int,
    client_order_id: str,
    order_id: str,
    status: str,
    updated_at: int,
):
    """INV-31 WriteQueue internal — heal DB when BE-TP already resting on exchange."""
    from engine.database import get_connection
    conn = get_connection()
    conn.execute(
        "UPDATE bot_orders SET order_id = ?, status = ?, updated_at = ? "
        "WHERE bot_id = ? AND client_order_id = ? AND status = 'pending_placement'",
        (order_id, status, updated_at, bot_id, client_order_id)
    )
    conn.execute(
        "UPDATE trades SET tp_order_id = ? WHERE bot_id = ?",
        (order_id, bot_id)
    )
    conn.commit()


def _maintain_be_tp_placed_internal(
    bot_id: int,
    client_order_id: str,
    actual_cid: str,
    order_id: str,
    status: str,
    updated_at: int,
):
    """INV-31 WriteQueue internal — record freshly placed BE-TP order."""
    from engine.database import get_connection
    conn = get_connection()
    conn.execute(
        "UPDATE bot_orders SET order_id = ?, status = ?, client_order_id = ?, updated_at = ? "
        "WHERE bot_id = ? AND client_order_id = ? AND status = 'pending_placement'",
        (order_id, status, actual_cid, updated_at, bot_id, client_order_id)
    )
    conn.execute(
        "UPDATE trades SET tp_order_id = ? WHERE bot_id = ?",
        (order_id, bot_id)
    )
    conn.commit()


def _maintain_stuck_dust_phase_internal(bot_id: int):
    """INV-31 WriteQueue internal — mark cycle_phase STUCK_DUST_NO_EXIT."""
    from engine.database import get_connection
    conn = get_connection()
    conn.execute(
        "UPDATE trades SET cycle_phase='STUCK_DUST_NO_EXIT' WHERE bot_id=?",
        (bot_id,)
    )
    conn.commit()


def _maintain_margin_held_clear_internal(bot_id: int):
    """INV-31 WriteQueue internal — clear MARGIN_HELD/STUCK_DUST_NO_EXIT back to ACTIVE after TP placement."""
    from engine.database import get_connection
    conn = get_connection()
    conn.execute(
        "UPDATE trades SET cycle_phase='ACTIVE' WHERE bot_id=? AND cycle_phase IN ('MARGIN_HELD', 'STUCK_DUST_NO_EXIT')",
        (bot_id,)
    )
    conn.commit()


def _maintain_margin_held_set_internal(bot_id: int):
    """INV-31 WriteQueue internal — stamp cycle_phase MARGIN_HELD on margin-cap rejection."""
    from engine.database import get_connection
    conn = get_connection()
    conn.execute(
        "UPDATE trades SET cycle_phase='MARGIN_HELD' WHERE bot_id=? AND cycle_phase NOT IN ('CARRY_PENDING')",
        (bot_id,)
    )
    conn.commit()


def _maintain_entry_confirmed_internal(bot_id: int):
    """INV-31 WriteQueue internal — promote trades.entry_confirmed=1 (step proof T2/T3)."""
    from engine.database import get_connection
    conn = get_connection()
    conn.execute(
        "UPDATE trades SET entry_confirmed=1 WHERE bot_id=?",
        (bot_id,)
    )
    conn.commit()


def _maintain_grid_order_clear_internal(bot_id: int):
    """INV-31 WriteQueue internal — clear trades.grid_order_id."""
    from engine.database import get_connection
    conn = get_connection()
    conn.execute(
        "UPDATE trades SET grid_order_id = NULL WHERE bot_id = ?",
        (bot_id,)
    )
    conn.commit()


def _maintain_hedge_freeze_internal(bot_id: int, last_error: str, last_error_time: int):
    """INV-31 WriteQueue internal — O-10 freeze parent to REQUIRE_MANUAL_PROOF."""
    from engine.database import get_connection
    conn = get_connection()
    conn.execute(
        "UPDATE bots SET status='REQUIRE_MANUAL_PROOF', last_error=?, last_error_time=? WHERE id=?",
        (last_error, last_error_time, bot_id)
    )
    conn.commit()


def _reset_to_hedge_standby_internal(child_bot_id: int, parent_cycle_id: int):
    """
    INV-31 WriteQueue internal (B1 fix, t_58d817a3) — Phase 2 DB writes of
    _reset_to_hedge_standby: point the child's trades row at the parent cycle
    and force status hedge_standby (before and after seal_trade_state, which
    may overwrite status). Runs inside WriteQueue serialization — must NOT be
    called directly.
    """
    from engine.database import get_connection
    conn = get_connection()
    conn.execute(
        "UPDATE trades SET cycle_id = ? WHERE bot_id = ?",
        (parent_cycle_id, child_bot_id)
    )
    conn.execute(
        "UPDATE bots SET status = 'hedge_standby', cascade_started_at = 0 WHERE id = ?",
        (child_bot_id,)
    )
    conn.commit()


'''

rep(
    "\n\nclass BotExecutor:\n",
    INTERNAL_FNS + "class BotExecutor:\n",
    1, "K1 internal fns block")

# ══════════════════════════════════════════════════════════════════════════
# Verify + write
# ══════════════════════════════════════════════════════════════════════════
import ast
try:
    ast.parse(src)
except SyntaxError as e:
    print(f"ABORT: post-transform syntax error: {e}")
    sys.exit(1)

open(PATH, "w", encoding="utf-8", newline="").write(src)
print(f"OK — {len(applied)} replacement groups applied, file written.")
for a in applied:
    print(f"  ✓ {a}")
print(f"lines: {len(orig.splitlines())} -> {len(src.splitlines())}")
