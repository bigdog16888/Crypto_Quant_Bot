import time
import logging
from typing import List, Dict, Any
from . import database
from .exchange_interface import normalize_symbol

logger = logging.getLogger("IntegrityEnforcer")

_flag_cycle_count = 0  # Throttle counter for flag_unmatched_positions

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
        v_net = v_data['long'] - v_data['short']
        
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
                    try:
                        from engine.ledger import seal_trade_state
                        for bot_id in set(affected_bots): # Deduplicate
                            logger.info(f"🩺 [INTEGRITY-HEAL] Sealing bot {bot_id} ({pair} {side_label}) to resolve ghost position.")
                            seal_trade_state(bot_id)
                    except Exception as heal_err:
                        logger.error(f"[INTEGRITY-HEAL] seal_trade_state failed for {pair} {side_label}: {heal_err}")
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
                    try:
                        from engine.ledger import seal_trade_state
                        for bot_id in set(affected_bots):
                            logger.info(f"🩺 [INTEGRITY-HEAL] Sealing bot {bot_id} ({pair}) to resolve size discrepancy.")
                            seal_trade_state(bot_id)
                    except Exception as heal_err:
                        logger.error(f"[INTEGRITY-HEAL] seal_trade_state failed for {pair}: {heal_err}")
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
    c.execute("SELECT id, bot_id, order_id, order_type, created_at FROM bot_orders WHERE status='open'")
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
    c.execute("SELECT order_id FROM bot_orders WHERE status='open'")
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
