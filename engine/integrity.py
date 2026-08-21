import time
import logging
from typing import List, Dict, Any
from . import database
from .exchange_interface import normalize_symbol

logger = logging.getLogger("IntegrityEnforcer")

_flag_cycle_count = 0  # Throttle counter for flag_unmatched_positions
_dir_side_cycle_count = 0  # Throttle counter for the direction/position_side check


def check_direction_side_consistency(conn=None) -> List[Dict[str, Any]]:
    """
    READ-ONLY consistency check: bots.direction vs trades.position_side.

    WHY THIS EXISTS (task t_fc30b679):
    get_pair_virtual_net (engine/database.py) signs each trades row by
    trades.position_side, while get_bot_signed_contribution
    (engine/parity_gates.py) signs by bots.direction. On live rows the two
    agree, but nothing enforces that. If they ever diverge for a bot with
    open_qty != 0, pair-level net and bot-level contribution silently disagree
    and wipe-proof / parity-gate math gets subtly wrong — no error is raised
    anywhere else. This check NOTICEs the divergence instead of staying silent.

    RULE — mirrors the two sign conventions EXACTLY:
      effective_side(position_side) = 'SHORT' if upper == 'SHORT' else 'LONG'
          (get_pair_virtual_net treats any non-SHORT value, including legacy
           'BOTH'/NULL, as LONG: SHORT rows contribute -open_qty, all others +)
      effective_dir(direction)      = 'LONG' if upper == 'LONG' else 'SHORT'
          (get_bot_signed_contribution: +qty only when direction == 'LONG')
    A row diverges when effective_side != effective_dir. So a legacy
    position_side='BOTH' row fires only when direction='SHORT' (a real sign
    disagreement), not when direction='LONG'.

    Scope: every bot with a trades row where open_qty != 0 (active or not —
    is_active is included in the log line for triage). open_qty is UNSIGNED;
    the sign lives in position_side (see get_pair_virtual_net docstring and
    commit 205592d — do not re-litigate that convention here).

    READ-ONLY: never mutates DB or exchange state. On divergence, logs a loud
    ERROR with bot_id, pair, and both values, and returns the divergent rows
    as dicts so callers/tests can inspect them.

    SURFACING DECISION (documented per task):
    - Periodic integrity pass: YES — called from enforce_integrity() via
      _maybe_check_direction_side(), throttled to every 30 cycles (~2.5 min),
      same cadence as flag_unmatched_positions.
    - Startup barrier: NON-FATAL — called once in BotRunner init right after
      the early check_and_fix_integrity() (engine/runner/startup.py). Divergence
      logs ERROR but does NOT abort startup. Rationale: this check is new and
      unproven in production; divergence degrades parity-report accuracy but
      does not itself move money, so a hard block risks a false-positive
      outage. If divergence is ever confirmed live, escalate this to a
      blocking gate deliberately.
    """
    if conn is None:
        conn = database.get_connection()
    divergences: List[Dict[str, Any]] = []
    try:
        rows = conn.execute("""
            SELECT b.id, b.pair, b.direction, t.position_side, t.open_qty, b.is_active
            FROM bots b
            JOIN trades t ON t.bot_id = b.id
            WHERE COALESCE(t.open_qty, 0) != 0
        """).fetchall()
    except Exception as e:
        logger.error(f"[DIR-SIDE-CHECK] Query failed (skipping, read-only): {e}")
        return divergences

    for bot_id, pair, direction, position_side, open_qty, is_active in rows:
        dir_eff = 'LONG' if str(direction or '').upper() == 'LONG' else 'SHORT'
        side_eff = 'SHORT' if str(position_side or '').upper() == 'SHORT' else 'LONG'
        if dir_eff != side_eff:
            divergences.append({
                'bot_id': bot_id,
                'pair': pair,
                'direction': str(direction),
                'position_side': str(position_side),
                'open_qty': float(open_qty),
                'is_active': is_active,
            })
            logger.error(
                f"🚨 [DIR-SIDE-DIVERGENCE] Bot {bot_id} ({pair}): bots.direction={direction!r} "
                f"but trades.position_side={position_side!r} with open_qty={open_qty} "
                f"(is_active={is_active}). Pair-net signs by position_side, bot-contribution "
                f"signs by direction — they DISAGREE for this bot; parity/wipe-proof math is "
                f"unreliable until fixed. Manual inspection required (this check never mutates state)."
            )
    return divergences


def _maybe_check_direction_side():
    """Throttled wrapper: run the read-only direction/position_side check every 30 cycles."""
    global _dir_side_cycle_count
    _dir_side_cycle_count += 1
    if _dir_side_cycle_count % 30 != 0:
        return
    check_direction_side_consistency()

def enforce_integrity(runner_instance, exchange_snapshot: Dict[str, Any]):
    """
    Main entry point for state integrity checks.
    Called by BotRunner.run_cycle() periodically.

    1. Fixes internal DB inconsistencies (e.g. Scanning status with invested > 0).
    2. Flags unmatched physical positions (Zombies) — NEVER adopts them.
    3. Cleans up stuck/orphan orders.
    """
    try:
        # 1. Internal DB Fixes
        database.check_and_fix_integrity()

        # 1b. READ-ONLY: bots.direction vs trades.position_side divergence check
        # (throttled every 30 cycles; never mutates state — see
        # check_direction_side_consistency docstring for the surfacing decision)
        _maybe_check_direction_side()

        # 2. Flag unmatched positions (report only, never modify trade data)
        flag_unmatched_positions(runner_instance, exchange_snapshot)

        # 3. Cleanup Orphaned Orders
        fix_stuck_orders(runner_instance, exchange_snapshot)

    except Exception as e:
        logger.error(f"Integrity enforcement failed: {e}")


def flag_unmatched_positions(runner, snapshot: Dict[str, Any]):
    """
    Compares physical exchange positions against virtual bot positions.
    If a physical position has no corresponding bot trade record, it is
    flagged as 'UNMATCHED — possibly manual trade' and LEFT ALONE.

    Throttled to every 30 calls (~2.5 min) to avoid log spam.
    """
    global _flag_cycle_count
    _flag_cycle_count += 1
    if _flag_cycle_count % 30 != 0:
        return  # Skip — not this cycle

    # 1. Aggregate Physical Positions from exchange snapshot
    physical_map = {}  # {norm_pair: {'long': notional_usd, 'short': notional_usd}}

    for mt, snap in snapshot.items():
        if not snap or 'positions' not in snap:
            continue
        for p in snap['positions']:
            pair = normalize_symbol(p['symbol'])
            amt = float(p.get('contracts', 0) or p.get('size', 0))
            entry = float(p.get('entryPrice', 0))
            if amt == 0 or entry == 0:
                continue

            if pair not in physical_map:
                physical_map[pair] = {'long': 0.0, 'short': 0.0}

            side = 'long' if amt > 0 else 'short'
            physical_map[pair][side] += abs(amt)

    # 2. Aggregate Virtual Positions (Query directly from DB for accurate avg_entry_price)
    virtual_map = {}  # {norm_pair: {'long': qty, 'short': qty}}
    bot_by_pair_side = {}  # {(norm_pair, side): [bot_id, ...]} for self-heal lookups

    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT b.id, b.pair, t.position_side, t.total_invested, t.avg_entry_price
        FROM bots b
        JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active = 1 AND t.total_invested > 0
    """)
    active_bot_trades = c.fetchall()

    for row in active_bot_trades:
        b_id, b_pair, b_side, b_invested, b_avg_entry = row
        pair = normalize_symbol(b_pair)
        direction = str(b_side or 'LONG').lower()
        invested = float(b_invested or 0)
        avg_entry = float(b_avg_entry or 0)

        if pair not in virtual_map:
            virtual_map[pair] = {'long': 0.0, 'short': 0.0}

        if invested > 0 and avg_entry > 0:
            qty = invested / avg_entry
            virtual_map[pair][direction] += qty

        bot_by_pair_side.setdefault((pair, direction), []).append(b_id)

    # 3. Compare — ONE-WAY MODE Netting validation
    # Since Binance operates in One-Way mode for multi-bot hedging, we must net the virtual
    # positions before comparing them to the exchange's netted physical position.
    all_pairs = set(list(physical_map.keys()) + list(virtual_map.keys()))
    
    for pair in all_pairs:
        p_data = physical_map.get(pair, {'long': 0.0, 'short': 0.0})
        v_data = virtual_map.get(pair, {'long': 0.0, 'short': 0.0})

        # Calculate Net Positions (LONG is positive, SHORT is negative)
        p_net = p_data['long'] - p_data['short']
        
        # 🛡️ HEDGE-AWARE PARITY (v2.5.0):
        # We must include the net filled hedge position in the system's virtual footprint.
        # Otherwise, One-Way mode netting on the exchange will look like a "Ghost Position"
        # mismatch if a bot side is perfectly hedged.
        from engine.database import get_pair_virtual_net
        v_net = get_pair_virtual_net(pair)
        
        diff_qty = abs(p_net - v_net)
        
        if diff_qty > _MISMATCH_TOLERANCE_QTY:
            # Determine the dominant side for logging
            side_label = "LONG" if v_net >= 0 else "SHORT"
            
            if abs(v_net) < _MISMATCH_TOLERANCE_QTY and abs(p_net) > _MISMATCH_TOLERANCE_QTY:
                # Exchange has a position we don't know about — possibly manual trade
                logger.warning(
                    f"⚠️ UNMATCHED {side_label} POSITION: {pair} "
                    f"PhysNet={p_net:.4f} SystemNet={v_net:.4f} — "
                    f"Possibly manual trade or cross-bot leak. Reconciler will solve."
                )

            elif abs(p_net) < _MISMATCH_TOLERANCE_QTY and abs(v_net) > _MISMATCH_TOLERANCE_QTY:
                # ── GHOST VIRTUAL POSITION ───────────────────────────────────────
                logger.warning(
                    f"👻 GHOST {side_label} POSITION: {pair} "
                    f"PhysNet={p_net:.4f} SystemNet={v_net:.4f} — "
                    f"Exchange shows zero, system has phantom. Triggering seal heal."
                )
                # Heal ALL active bots for this pair since we don't know which is wrong
                affected_bots = bot_by_pair_side.get((pair, 'long'), []) + bot_by_pair_side.get((pair, 'short'), [])
                if affected_bots:
                    # try:
                    #     from engine.ledger import seal_trade_state
                    #     for bot_id in set(affected_bots): # Deduplicate
                    #         logger.info(f"🩺 [INTEGRITY-HEAL] Sealing bot {bot_id} ({pair} {side_label}) to resolve ghost position.")
                    #         seal_trade_state(bot_id)
                    # except Exception as heal_err:
                    #     logger.error(f"[INTEGRITY-HEAL] seal_trade_state failed for {pair} {side_label}: {heal_err}")
                    logger.info(f"🩺 [INTEGRITY-REPORT] Bots {set(affected_bots)} ({pair} {side_label}) flagged for ghost position. Reconciler will handle.")
                else:
                    logger.warning(f"[INTEGRITY-HEAL] No bots found for ({pair}) — orphaned DB row.")

            else:
                logger.warning(
                    f"⚠️ SIZE DISCREPANCY: {pair} "
                    f"PhysNet={p_net:.4f} SystemNet={v_net:.4f} (Diff: {diff_qty:.4f} qty)"
                )
                # Heal ALL active bots for this pair
                affected_bots = bot_by_pair_side.get((pair, 'long'), []) + bot_by_pair_side.get((pair, 'short'), [])
                if affected_bots:
                    # try:
                    #     from engine.ledger import seal_trade_state
                    #     for bot_id in set(affected_bots):
                    #         logger.info(f"🩺 [INTEGRITY-HEAL] Sealing bot {bot_id} ({pair}) to resolve size discrepancy.")
                    #         seal_trade_state(bot_id)
                    # except Exception as heal_err:
                    #     logger.error(f"[INTEGRITY-HEAL] seal_trade_state failed for {pair}: {heal_err}")
                    logger.info(f"🩺 [INTEGRITY-REPORT] Bots {set(affected_bots)} ({pair}) flagged for size discrepancy. Reconciler will handle.")
                else:
                    logger.warning(f"[INTEGRITY-HEAL] No bots found for ({pair}) — orphaned DB row.")


# Use strict quantity rounding tolerance to avoid floating point math errors
_MISMATCH_TOLERANCE_QTY = 0.0001


def fix_stuck_orders(runner, snapshot: Dict[str, Any]):
    """
    Cancels orders that are 'open' in DB but not linked to any active trade.
    These are ORPHAN orders — they have order IDs in our DB, so we CAN trace them.
    """
    # 1. Get all open orders from DB
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("SELECT id, bot_id, order_id, order_type, created_at FROM bot_orders WHERE status IN ('open', 'new', 'placing')")
    open_orders = c.fetchall()

    active_trade_orders = set()

    # 2. Protect orders tracked in trades table (entry + tp fast-lookup columns)
    c.execute("SELECT entry_order_id, tp_order_id FROM trades")
    for row in c.fetchall():
        if row[0]: active_trade_orders.add(str(row[0]))
        if row[1]: active_trade_orders.add(str(row[1]))

    # 🛡️ FIX: Also protect ALL open orders in bot_orders (grid, tp, entry).
    # Grid orders live ONLY in bot_orders — without this they were treated as
    # orphans after 60s, cancelled, then immediately re-placed, causing a
    # runaway accumulation loop where positions grew unboundedly.
    c.execute("SELECT order_id FROM bot_orders WHERE status IN ('open', 'new', 'placing', 'cancelling')")
    for row in c.fetchall():
        if row[0]: active_trade_orders.add(str(row[0]))

    # 3. Check and Cancel
    for row in open_orders:
        db_id, bot_id, ex_oid, otype, created_at = row
        ex_oid = str(ex_oid)

        # If order is active in trades table, skip cleanup
        if ex_oid in active_trade_orders:
            continue

        # Ignore recently created orders (give them 60s grace period)
        if (time.time() - created_at) < 60:
            continue

        # ORPHAN DETECTED — this order HAS an ID trail, so we can safely clean it
        logger.warning(f"🗑️ ORPHAN ORDER DETECTED: Bot {bot_id} Order {ex_oid} ({otype}). Cancelling.")

        # Fetch bot pair for exchange routing
        c.execute("SELECT pair FROM bots WHERE id=?", (bot_id,))
        b_res = c.fetchone()
        if not b_res:
            continue
        pair = b_res[0]

        # Try to cancel on exchange (best effort)
        try:
            c.execute("SELECT config FROM bots WHERE id=?", (bot_id,))
            cfg_json = c.fetchone()[0]
            import json
            cfg = json.loads(cfg_json) if cfg_json else {}

            from config.settings import config as app_config
            mt = cfg.get('market_type', app_config.MARKET_TYPE)

            ex = runner.exchanges.get(mt)
            if not ex and len(runner.exchanges) == 1:
                ex = list(runner.exchanges.values())[0]

            if ex:
                try:
                    logger.debug(f"Attempting to cancel orphan order {ex_oid} on exchange...")
                    ex.cancel_order(ex_oid, pair)
                except Exception as e:
                    # 400/Unknown order = already filled or cancelled on exchange, expected
                    logger.debug(f"Orphan cancel attempt failed (likely already closed): {e}")

        except Exception as e:
            logger.warning(f"Error resolving exchange for bot {bot_id}: {e}")

        # Mark 'failed' in DB — this order has a trail so we know it's ours
        database.update_order_status(ex_oid, 'failed', bot_id)
        logger.info(f"✅ Marked orphan order {ex_oid} as failed in DB.")
