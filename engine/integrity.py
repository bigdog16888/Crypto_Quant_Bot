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
            notional = abs(amt) * entry
            physical_map[pair][side] += notional

    # 2. Aggregate Virtual Positions (only from bots with actual trade records)
    bots = runner.get_active_bots()
    virtual_map = {}  # {norm_pair: {'long': invested_usd, 'short': invested_usd}}

    for bot in bots:
        b_id, b_name, b_pair, b_dir, _, _, b_invested, _, _, _ = bot
        pair = normalize_symbol(b_pair)
        direction = b_dir.lower()
        invested = float(b_invested or 0)

        if pair not in virtual_map:
            virtual_map[pair] = {'long': 0.0, 'short': 0.0}

        if invested > 10:  # meaningful amount
            virtual_map[pair][direction] += invested

    # 3. Compare — flag only, never modify
    for pair, phys_data in physical_map.items():
        for side in ['long', 'short']:
            phys_val = phys_data[side]
            if phys_val < 10:
                continue  # dust

            virt_val = virtual_map.get(pair, {}).get(side, 0.0)
            diff_val = phys_val - virt_val

            if diff_val > _MISMATCH_TOLERANCE_USD:
                if virt_val < 10:
                    # Complete zombie — no bot claims this at all
                    logger.warning(
                        f"⚠️ UNMATCHED POSITION: {pair} {side.upper()} "
                        f"Phys=${phys_val:.2f} Virt=${virt_val:.2f} — "
                        f"Possibly manual trade. NO ACTION TAKEN."
                    )
                else:
                    # Partial mismatch — bot tracks some, but exchange has more
                    logger.warning(
                        f"⚠️ SIZE DISCREPANCY: {pair} {side.upper()} "
                        f"Phys=${phys_val:.2f} Virt=${virt_val:.2f} "
                        f"(+${diff_val:.2f} untracked). NO ACTION TAKEN."
                    )


_MISMATCH_TOLERANCE_USD = 25.0


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
