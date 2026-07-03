"""
One-way (single net position) accounting across multiple bots on the same pair.

Root cause addressed: a SHORT bot's sell entry reduces the shared exchange position
but only credited that bot's ledger — LONG siblings kept full open_qty, so
get_pair_virtual_net disagreed with fetch_positions.

Rules:
  - Entry/grid on bot A that nets against the exchange book must reduce open_qty
    on opposite-direction bots on the same pair (virtual_netting audit rows).
  - New opposite-direction entries are blocked while siblings hold open_qty.
  - Pair virtual net = sum of direction-signed trades.open_qty (not raw order sums).
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple

from config.settings import config
from engine.exchange_interface import normalize_symbol

logger = logging.getLogger(__name__)

# ADR-005 Phase 3 — Proportional Allocation stale-cycle tracker.
# Counts consecutive reconciler cycles per pair where get_exchange_signed_net
# returned None (API down).  When count > PA_SYNC_MAX_STALE_CYCLES, bots on
# that pair are set to REQUIRE_MANUAL_PROOF.
_pa_stale_cycles: dict = {}



def _qty_tol() -> float:
    return float(getattr(config, 'PAIR_PARITY_QTY_TOLERANCE', 0.002))


def _pair_norm(pair: str) -> str:
    return normalize_symbol(pair).upper()


def get_pair_open_qty_net(pair: str) -> float:
    """Signed net from trades.open_qty — matches one-way exchange position when accurate."""
    from engine.database import get_connection

    norm = _pair_norm(pair)
    conn = get_connection()
    total = 0.0
    for _bid, direction, raw_pair, bot_norm, open_qty in conn.execute(
        """
        SELECT b.id, b.direction, b.pair, b.normalized_pair, COALESCE(t.open_qty, 0)
        FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1
        """
    ).fetchall():
        current = (bot_norm or normalize_symbol(raw_pair)).upper()
        if current != norm:
            continue
        oq = float(open_qty or 0)
        if str(direction).upper() == 'LONG':
            total = round(total + oq, 8)
        else:
            total = round(total - oq, 8)
    return total


def gate_oneway_opposite_entry(
    bot_id: int,
    pair: str,
    direction: str,
) -> Tuple[bool, str]:
    """
    Block new entry/grid when opposite-direction bots still hold open_qty on this
    one-way pair (prevents uncredited cross-bot exchange netting).
    """
    if not getattr(config, 'ONE_WAY_BLOCK_OPPOSITE_ENTRY', True):
        return True, ''

    from engine.database import get_connection

    conn = get_connection()
    try:
        b_row = conn.execute("SELECT bot_type FROM bots WHERE id = ?", (bot_id,)).fetchone()
        if b_row and b_row[0] == 'hedge_child':
            return True, ''
    except Exception as e:
        logger.warning(f"Failed to check bot_type for gate bypass on bot {bot_id}: {e}")

    norm = _pair_norm(pair)
    my_dir = str(direction).upper()
    opp_dir = 'SHORT' if my_dir == 'LONG' else 'LONG'
    tol = _qty_tol()

    # Bypass condition: if this is a fresh entry (total_invested == 0 and current_step == 0),
    # allow the entry regardless of sibling bot positions.
    try:
        t_row = conn.execute(
            "SELECT total_invested, current_step FROM trades WHERE bot_id = ?",
            (bot_id,)
        ).fetchone()
        if t_row:
            total_invested, current_step = t_row
            if float(total_invested or 0) <= 0.01 and int(current_step or 0) == 0:
                return True, ''
    except Exception as e:
        logger.warning(f"Failed to fetch trade status for bypass check on bot {bot_id}: {e}")

    opp_open = 0.0
    for _bid, bdir, raw_pair, bot_norm, oq in conn.execute(
        """
        SELECT b.id, b.direction, b.pair, b.normalized_pair, COALESCE(t.open_qty, 0)
        FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1 AND b.bot_type != 'hedge_child'
          AND b.id != ?
          AND b.id != COALESCE((SELECT hedge_child_bot_id FROM bots WHERE id = ?), -1)
          AND b.id != COALESCE((SELECT parent_bot_id FROM bots WHERE id = ?), -1)
        """,
        (bot_id, bot_id, bot_id),
    ).fetchall():
        if (bot_norm or normalize_symbol(raw_pair)).upper() != norm:
            continue
        if str(bdir).upper() == opp_dir:
            opp_open += float(oq or 0)

    if opp_open > tol:
        return False, (
            f"one-way pair {norm}: {opp_dir} bots hold {opp_open:.6f} open — "
            f"cannot place {my_dir} entry until opposite side is flat"
        )
    return True, ''


def reconcile_oneway_pair_open_qty(
    exchange,
    pair: str,
) -> Optional[str]:
    """
    Align sum(direction-signed open_qty) to live exchange net for one pair.
    Used at startup when historical cross-bot fills left stale open_qty.
    """
    from engine.database import get_connection, save_bot_order
    from engine.ledger import seal_trade_state
    from engine.parity_gates import get_exchange_signed_net

    if not exchange:
        return None

    physical = get_exchange_signed_net(exchange, pair)
    if physical is None:
        return None

    virtual = get_pair_open_qty_net(pair)
    diff = round(virtual - physical, 8)
    if abs(diff) < 1e-8:
        return None

    norm = _pair_norm(pair)
    conn = get_connection()

    # Virtual too high → trim LONG open_qty first, then SHORT magnitude
    if diff > 1e-8:
        remaining = diff
        reduced_bots = []
        for target_dir in ('LONG', 'SHORT'):
            bots: List[Tuple[int, float]] = []
            for bid, bdir, raw_pair, bot_norm, oq in conn.execute(
                """
                SELECT b.id, b.direction, b.pair, b.normalized_pair, COALESCE(t.open_qty, 0)
                FROM bots b JOIN trades t ON t.bot_id = b.id WHERE b.is_active = 1 AND b.bot_type != 'hedge_child'
                """
            ).fetchall():
                if (bot_norm or normalize_symbol(raw_pair)).upper() != norm:
                    continue
                if str(bdir).upper() != target_dir:
                    continue
                oqf = float(oq or 0)
                if oqf > 0:
                    bots.append((bid, oqf))
            for bid, oq in sorted(bots, key=lambda x: -x[1]):
                if remaining <= 1e-8:
                    break
                cut = round(min(oq, remaining), 8)
                if cut <= 0:
                    continue
                
                # Fix C: Add a MAX_OWAY_REPAIR_QTY guard
                MAX_REPAIR_QTY = float(getattr(config, 'MAX_OWAY_REPAIR_QTY', 50.0))
                if cut > MAX_REPAIR_QTY:
                    logger.error(f"[ONEWAY-REPAIR] Refusing to trim {cut:.4f} > MAX_OWAY_REPAIR_QTY ({MAX_REPAIR_QTY}). Set manually.")
                    continue

                cycle_row = conn.execute(
                    "SELECT cycle_id FROM trades WHERE bot_id = ?", (bid,)
                ).fetchone()
                cycle_id = int(cycle_row[0] or 1) if cycle_row else 1
                audit_cid = f"CQB_{bid}_OWAY_REPAIR_{int(time.time())}"

                current_price = 0.0
                try:
                    ticker = exchange.fetch_ticker(pair)
                    if ticker and 'last' in ticker:
                        current_price = float(ticker['last'] or 0.0)
                except Exception as e_tick:
                    logger.warning(f"Failed to fetch ticker price for {pair} during oway repair: {e_tick}")

                # Only write adoption_reduce if we have confirmed exchange data
                # and the cut is above minimum meaningful size
                if cut > 1e-6 and current_price > 0:
                    save_bot_order(
                        bid,
                        'adoption_reduce',
                        audit_cid,
                        current_price,
                        cut,
                        0,
                        status='filled',
                        client_order_id=audit_cid,
                        notes=f"ONEWAY_REPAIR: align open_qty to exchange (cut {cut:.6f})",
                        cycle_id=cycle_id,
                    )
                    conn.execute(
                        "UPDATE bot_orders SET filled_amount = ? WHERE client_order_id = ? AND bot_id = ?",
                        (cut, audit_cid, bid),
                    )
                    remaining -= cut
                    reduced_bots.append(bid)
                    logger.warning(
                        f"🔧 [ONEWAY-REPAIR] {norm}: trimmed bot {bid} open_qty −{cut:.6f} "
                        f"(virtual {virtual:.6f} → exchange {physical:.6f})"
                    )
                else:
                    logger.warning(
                        f"⚠️ [ONEWAY-REPAIR] Skip repair for bot {bid}: cut={cut:.6f}, price={current_price:.4f}"
                    )
        conn.commit()
        for bid in reduced_bots:
            try:
                seal_trade_state(bid, force_recompute=True)
            except Exception as e_seal:
                logger.error(f"Failed to seal repaired bot {bid}: {e_seal}")
        return f"trimmed virtual excess {diff:.6f} on {norm}"

    elif diff < -1e-8:
        # Fix B: Virtual too LOW — system under-reports vs exchange
        # Write a diagnostic drift_note; do NOT auto-inflate open_qty
        # (that would require knowing which bot owns the missing qty)
        norm = _pair_norm(pair)
        logger.warning(
            f"⚠️ [ONEWAY-REPAIR-LOW] {norm}: virtual {virtual:.6f} < physical {physical:.6f} "
            f"(gap {abs(diff):.6f}). System under-reports. Manual review required."
        )
        # Optionally write a drift_note for audit trail here
        return f"under-report gap {abs(diff):.6f} on {norm} — manual review"

    return None


def get_authoritative_close_qty(exchange, pair: str, direction: str, db_qty: float) -> float:
    """
    Get the authoritative close quantity on the exchange.
    Exchange is always authoritative for close qty — DB is a hint only (INV-15).
    """
    from engine.parity_gates import get_exchange_signed_net

    physical_net = get_exchange_signed_net(exchange, pair)
    if physical_net is None:
        # Fallback to db_qty if exchange call failed, to be safe.
        logger.warning(
            f"[ONEWAY-NETTING] get_exchange_signed_net failed for {pair}. "
            f"Fallback to db_qty={db_qty:.6f} as hint."
        )
        return db_qty

    d = str(direction).upper()
    if d == 'LONG':
        exchange_qty = max(0.0, physical_net)
    elif d == 'SHORT':
        exchange_qty = max(0.0, -physical_net)
    else:
        exchange_qty = 0.0

    return round(min(db_qty, exchange_qty), 8)


def detect_bot_ghost(exchange, bot_id, conn) -> bool:
    """
    Returns True if the trades table claims open_qty > 0 but the authoritative
    recompute_invested_from_orders shows that the bot's actual filled orders
    sum to 0 (i.e. the cached open_qty is a phantom/ghost not backed by fills).
    """
    bot_row = conn.execute(
        "SELECT b.pair, b.direction, t.open_qty, t.cycle_id "
        "FROM bots b JOIN trades t ON t.bot_id = b.id "
        "WHERE b.id = ?", (bot_id,)
    ).fetchone()
    if not bot_row or float(bot_row[2] or 0) <= 0.0001:
        return False

    current_cycle = bot_row[3]

    # Sanity check: does any filled entry exist for current cycle?
    filled_entry_count = conn.execute(
        "SELECT COUNT(*) FROM bot_orders "
        "WHERE bot_id=? AND cycle_id=? "
        "AND order_type IN ('entry','grid','adoption','carry') "
        "AND status IN ('filled','partially_filled') "
        "AND filled_amount > 0.0001",
        (bot_id, current_cycle)
    ).fetchone()[0]

    if filled_entry_count == 0:
        # No fills in current cycle — could be cycle_id mismatch,
        # not a ghost. Log warning but do NOT declare ghost.
        logger.warning(
            f"[GHOST-CHECK] Bot {bot_id}: open_qty={bot_row[2]:.6f} "
            f"but zero filled entries in cycle {current_cycle}. "
            f"Possible cycle_id mismatch — NOT declaring ghost. "
            f"Run operator_repair.py diagnose to investigate."
        )
        return False

    # Only now run recompute — cycle has real fills so result is trustworthy
    from engine.database import recompute_invested_from_orders
    try:
        _, _, recomputed_qty, _ = recompute_invested_from_orders(bot_id)
    except Exception as e:
        logger.error(f"[GHOST-CHECK] recompute failed for bot {bot_id}: {e}")
        return False

    if recomputed_qty <= 0.0001:
        logger.warning(
            f"[GHOST-CHECK] Bot {bot_id} GHOST CONFIRMED: "
            f"trades.open_qty={bot_row[2]:.6f} but recomputed=0 "
            f"from {filled_entry_count} filled entries in cycle {current_cycle}."
        )
        return True

    return False



def wipe_bot_ghost(exchange, bot_id, conn):
    # 1. Fetch details
    row = conn.execute(
        "SELECT b.name, b.pair, b.direction, b.bot_type, t.open_qty, t.cycle_id "
        "FROM bots b JOIN trades t ON t.bot_id = b.id "
        "WHERE b.id = ?", (bot_id,)
    ).fetchone()
    if not row:
        return
    name, pair, direction, bot_type, open_qty, cycle_id = row

    target_status = 'hedge_standby' if bot_type == 'hedge_child' else 'Scanning'

    # 1. Cancel any open orders for this bot on exchange
    if exchange:
        try:
            exchange.cancel_orders_by_bot_id(bot_id, pair)
            logger.info(f"🧹 [GHOST-WIPE] Cancelled open orders for bot {bot_id} on {pair}.")
        except Exception as e:
            logger.error(f"Failed to cancel open orders for bot {bot_id} on {pair}: {e}")

    # 2. Set status to target status
    conn.execute(
        "UPDATE bots SET status = ? WHERE id = ?", (target_status, bot_id)
    )

    # 3. Cancel open internal orders and archive filled orders to prevent zombie revival
    conn.execute(
        "UPDATE bot_orders SET status='cancelled' "
        "WHERE bot_id=? AND status IN ('open', 'new', 'placing', 'cancelling')",
        (bot_id,)
    )
    conn.execute(
        "UPDATE bot_orders SET status='reset_cleared' "
        "WHERE bot_id=? AND (status NOT IN ('open', 'new', 'placing', 'cancelling', 'auto_closed', 'reset_cleared', 'cancelled') OR (status IN ('cancelled', 'canceled') AND filled_amount > 0))",
        (bot_id,)
    )
    conn.commit()

    # 4. Seal the bot using its new 'reset_cleared' state (reads 0.0 open_qty)
    from engine.ledger import seal_trade_state
    seal_trade_state(bot_id, force_recompute=True)

    # Force status to target_status if seal overwrote it to Scanning
    conn.execute(
        "UPDATE bots SET status = ? WHERE id = ?",
        (target_status, bot_id)
    )
    # Reset cycle_phase to IDLE — seal_trade_state zeros open_qty but does not
    # touch cycle_phase, leaving it ACTIVE. An ACTIVE cycle with open_qty=0 and
    # no orders creates the GHOST_STEP illegal state that GTR is designed to catch.
    conn.execute(
        "UPDATE trades SET cycle_phase = 'IDLE' WHERE bot_id = ?",
        (bot_id,)
    )
    conn.commit()

    # 5. Write a drift_note audit row
    from engine.database import save_bot_order
    ts_now = int(time.time())
    drift_cid = f"CQB_{bot_id}_GH_WP_{ts_now}"
    try:
        save_bot_order(
            bot_id, 'drift_note', f'GHOST_WIPE_{bot_id}_{ts_now}',
            price=0.0, amount=0.0, step=0, status='audit',
            client_order_id=drift_cid,
            notes=f"[GHOST-WIPE] DB claims {open_qty} but exchange is flat on this side. Auto-wiped to {target_status}.",
            cycle_id=cycle_id
        )
    except Exception as e:
        logger.error(f"Failed to save drift_note audit row for ghost bot {bot_id}: {e}")

    # 6. Log CRITICAL
    logger.critical(
        f"[GHOST-WIPE] Bot {bot_id} ({name}): DB claims "
        f"{open_qty} but exchange is flat. Auto-wiped to {target_status}."
    )


# Backward compatibility aliases
def detect_hedge_child_ghost(exchange, child_bot_id, conn) -> bool:
    return detect_bot_ghost(exchange, child_bot_id, conn)


def wipe_hedge_child_ghost(exchange, child_bot_id, conn):
    return wipe_bot_ghost(exchange, child_bot_id, conn)


def sync_pair_to_exchange(pair, exchange, conn):
    """
    Fetches real exchange net position for the pair via get_exchange_signed_net.
    Fetches current sum of trades.open_qty (signed by direction) for all active bots on that pair.
    If diff > qty_tolerance(): log WARNING with full breakdown.
    This phase is OBSERVATION ONLY — it detects drift and logs it with full diagnostic detail but does not change any bot's open_qty.
    Saves the last check result to a local JSON cache so that the UI can render it.
    """
    from engine.parity_gates import get_exchange_signed_net, qty_tolerance
    from engine.exchange_interface import normalize_symbol
    import json
    import os
    import time
    
    exchange_net = get_exchange_signed_net(exchange, pair)
    if exchange_net is None:
        logger.warning(f"[EXCHANGE-SYNC] Failed to fetch real exchange net position for pair {pair}.")
        # ADR-005 stale-cycle tracking: increment counter and apply circuit breaker.
        try:
            from config.settings import config as _cfg
            _pa_stale_cycles[pair] = _pa_stale_cycles.get(pair, 0) + 1
            _stale = _pa_stale_cycles[pair]
            if _cfg.PROPORTIONAL_ALLOCATION and _stale >= _cfg.PA_SYNC_MAX_STALE_CYCLES:
                logger.critical(
                    f"[PA-SYNC] Exchange API unavailable for {pair} for {_stale} consecutive "
                    f"reconciler cycles (threshold={_cfg.PA_SYNC_MAX_STALE_CYCLES}). "
                    f"Setting bots to REQUIRE_MANUAL_PROOF."
                )
                from engine.exchange_interface import normalize_symbol as _ns
                _norm = _ns(pair).upper()
                conn.execute(
                    "UPDATE bots SET status='REQUIRE_MANUAL_PROOF' "
                    "WHERE is_active=1 AND normalized_pair=? "
                    "AND status NOT IN ('STOPPED','REQUIRE_MANUAL_PROOF')",
                    (_norm,)
                )
                conn.commit()
        except Exception as _stale_err:
            logger.error(f"[PA-SYNC] Stale-cycle handler failed for {pair}: {_stale_err}")
        return None

    norm_pair = normalize_symbol(pair).upper()
    
    # Fetch active bots on this pair
    rows = conn.execute("""
        SELECT b.id, b.name, b.direction, COALESCE(t.open_qty, 0.0) as open_qty
        FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1 AND b.normalized_pair = ?
    """, (norm_pair,)).fetchall()
    
    db_sum_qty = 0.0
    bot_details = []
    for row in rows:
        bot_id = row[0]
        bot_name = row[1]
        direction = row[2].upper()
        oq = float(row[3])
        signed_oq = oq if direction == 'LONG' else -oq
        db_sum_qty += signed_oq
        bot_details.append({
            'bot_id': bot_id,
            'name': bot_name,
            'direction': direction,
            'open_qty': oq,
            'signed_qty': signed_oq
        })
        
    db_sum_qty = round(db_sum_qty, 8)
    diff = round(exchange_net - db_sum_qty, 8)
    tolerance = qty_tolerance()
    drift_detected = abs(diff) > tolerance
    
    sync_data = {
        'timestamp': int(time.time()),
        'pair': pair,
        'exchange_net': exchange_net,
        'db_sum_qty': db_sum_qty,
        'diff': diff,
        'tolerance': tolerance,
        'drift_detected': drift_detected,
        'bots': bot_details
    }
    
    # Save the result to JSON cache for UI process-safe communication
    from config.settings import config
    os.makedirs(os.path.join(config.ROOT_DIR, 'data'), exist_ok=True)
    cache_file = os.path.join(config.ROOT_DIR, 'data', 'exchange_sync_diagnostics.json')
    try:
        data = {}
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r') as f:
                    data = json.load(f)
            except Exception:
                pass
        data[pair] = sync_data
        with open(cache_file, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to write exchange sync diagnostics: {e}")
        
    if drift_detected:
        bot_breakdown = "\n".join([
            f"    - Bot ID {b['bot_id']} ({b['name']}, {b['direction']}): open_qty={b['open_qty']:.8f} (signed={b['signed_qty']:.8f})"
            for b in bot_details
        ])
        logger.warning(
            f"[EXCHANGE-SYNC-DRIFT] Position drift detected for pair {pair}!\n"
            f"  Exchange net: {exchange_net:.8f}\n"
            f"  DB sum(open_qty): {db_sum_qty:.8f}\n"
            f"  Diff (Exchange - DB): {diff:.8f}\n"
            f"  Breakdown of contributing active bots:\n"
            f"{bot_breakdown}"
        )
        # Phase 2: attempt controlled FIFO reseal to correct drift
        _attempt_drift_correction(pair, diff, bot_details, conn, sync_data, exchange=exchange)

    # Proportional Allocation logic removed under ADR-006. Standard GTR drift check only.

    return sync_data


def _attempt_drift_correction(pair, diff, bot_details, conn, sync_data, exchange=None):
    """
    Phase 2 — Exchange-Authoritative Position Sync (v4.1.3).

    Called when sync_pair_to_exchange() detects drift > qty_tolerance().
    Strategy:
      1. Reseal all active bots on the pair via seal_trade_state() (bot_orders as truth),
         sorted by oldest basket_start_time first.
      2. Re-fetch exchange net and recompute db_sum_qty to check if drift is resolved.
      3. If drift persists, write a manual-review flag to bots.notes (if column exists)
         and to sync_data for JSON cache — no automatic overwrite of exchange data.

    Parameters:
        exchange: The exchange instance passed through from sync_pair_to_exchange.
                  If None, post-reseal re-check is skipped with a warning.
    """
    import datetime
    from engine.ledger import seal_trade_state
    from engine.exchange_interface import normalize_symbol
    from engine.parity_gates import get_exchange_signed_net, qty_tolerance

    norm_pair = normalize_symbol(pair).upper()

    # ── Step 1: Reseal all active bots on the pair (oldest-first FIFO order) ──
    rows = conn.execute("""
        SELECT b.id, b.name, b.direction, COALESCE(t.open_qty, 0.0), t.basket_start_time
        FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1 AND b.normalized_pair = ?
        ORDER BY CASE WHEN t.basket_start_time IS NULL THEN 1 ELSE 0 END,
                 t.basket_start_time ASC
    """, (norm_pair,)).fetchall()

    logger.info(
        f"[EXCHANGE-SYNC-CORRECT] Attempting FIFO reseal for {len(rows)} bot(s) on {pair} "
        f"(diff={diff:+.8f})"
    )

    for row in rows:
        bot_id, bot_name = row[0], row[1]
        try:
            seal_trade_state(bot_id)
            logger.info(f"[EXCHANGE-SYNC-CORRECT] Resealed bot {bot_id} ({bot_name})")
        except Exception as e:
            logger.error(f"[EXCHANGE-SYNC-CORRECT] Reseal failed for bot {bot_id} ({bot_name}): {e}")

    # ── Step 2: Re-check drift post-reseal ───────────────────────────────────
    if exchange is None:
        logger.warning(
            f"[EXCHANGE-SYNC-CORRECT] No exchange instance available — skipping "
            f"post-reseal drift check for {pair}."
        )
        sync_data['post_reseal_diff'] = None
        sync_data['post_reseal_resolved'] = None
        return

    exchange_net_after = get_exchange_signed_net(exchange, pair)
    if exchange_net_after is None:
        logger.warning(
            f"[EXCHANGE-SYNC-CORRECT] Could not re-fetch exchange net after reseal for {pair} — "
            f"skipping post-reseal check."
        )
        sync_data['post_reseal_diff'] = None
        sync_data['post_reseal_resolved'] = None
        return

    rows_after = conn.execute("""
        SELECT b.direction, COALESCE(t.open_qty, 0.0)
        FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1 AND b.normalized_pair = ?
    """, (norm_pair,)).fetchall()

    db_sum_after = round(
        sum(float(r[1]) if r[0].upper() == 'LONG' else -float(r[1]) for r in rows_after),
        8
    )
    diff_after = round(exchange_net_after - db_sum_after, 8)
    tolerance = qty_tolerance()
    resolved = abs(diff_after) <= tolerance

    sync_data['post_reseal_exchange_net'] = exchange_net_after
    sync_data['post_reseal_db_sum'] = db_sum_after
    sync_data['post_reseal_diff'] = diff_after
    sync_data['post_reseal_resolved'] = resolved

    if resolved:
        logger.info(
            f"[EXCHANGE-SYNC-CORRECT] ✅ Drift resolved after reseal for {pair}. "
            f"post_reseal_diff={diff_after:+.8f}"
        )
        return

    # ── Step 3: Drift persists — write manual-review flag ────────────────────
    logger.critical(
        f"[EXCHANGE-SYNC-CORRECT] ❌ DRIFT UNRESOLVED after reseal for {pair}! "
        f"post_reseal_diff={diff_after:+.8f}. Flagging bots for manual review."
    )

    # Check whether bots.notes column exists (no schema migration required)
    try:
        conn.execute("SELECT notes FROM bots LIMIT 1")
        has_notes_col = True
    except Exception:
        has_notes_col = False

    flag_ts = datetime.datetime.utcnow().isoformat()
    for row in rows:
        bot_id, bot_name = row[0], row[1]
        flag_msg = (
            f"[MANUAL-REVIEW] Exchange sync drift unresolved after reseal "
            f"at {flag_ts} pair={pair} post_reseal_diff={diff_after:+.8f}"
        )
        if has_notes_col:
            try:
                conn.execute("UPDATE bots SET notes=? WHERE id=?", (flag_msg, bot_id))
                conn.commit()
                logger.warning(
                    f"[EXCHANGE-SYNC-CORRECT] Manual review flag written to bots.notes "
                    f"for bot {bot_id} ({bot_name})"
                )
            except Exception as e:
                logger.error(
                    f"[EXCHANGE-SYNC-CORRECT] Failed to write notes flag for bot {bot_id}: {e}"
                )
        else:
            logger.warning(
                f"[EXCHANGE-SYNC-CORRECT] bots.notes column missing — "
                f"manual review flag for bot {bot_id} ({bot_name}) written to JSON cache only."
            )

    # Always persist flag state in sync_data (written to JSON cache by caller)
    sync_data['manual_review_flag'] = {
        'flagged': True,
        'timestamp': flag_ts,
        'post_reseal_diff': diff_after,
        'bots': [{'bot_id': r[0], 'name': r[1]} for r in rows],
    }


def get_typical_position_size(conn, bot_id: int) -> float:
    """
    Returns the average filled amount of entry/grid/adoption orders for the bot,
    or 0.0 if no historical filled orders exist.
    """
    row = conn.execute("""
        SELECT AVG(filled_amount) 
        FROM bot_orders 
        WHERE bot_id = ? 
          AND order_type IN ('entry', 'grid', 'adoption', 'adoption_add', 'carry') 
          AND status IN ('filled', 'partially_filled') 
          AND filled_amount > 0
    """, (bot_id,)).fetchone()
    return float(row[0]) if (row and row[0] is not None) else 0.0


def detect_unowned_exchange_positions(conn, exchange):
    """
    INV-32: Scrapes the active exchange positions and compares them against the cumulative
    database order books for all active bots to detect unowned positions.
    """
    from engine.parity_gates import qty_tolerance
    import time

    tolerance = qty_tolerance()

    # 1. Fetch live positions from exchange
    try:
        all_positions = exchange.fetch_positions()
    except Exception as e:
        logger.error(f"[ORPHAN-DETECTOR] Failed to fetch positions: {e}")
        return

    # Map of normalized symbol -> signed net quantity on exchange
    exchange_nets = {}
    for p in all_positions:
        # fetch_positions returns net_qty which is already signed (negative for SHORT, positive for LONG)
        signed_qty = float(p.get('net_qty', 0.0) or p.get('contracts', 0.0) or 0.0)
        symbol = p.get('symbol')
        if abs(signed_qty) > 0.0001:
            norm = normalize_symbol(symbol).upper()
            exchange_nets[norm] = signed_qty

    # 2. Get all active bots' trading pairs
    active_pairs = [r[0] for r in conn.execute(
        "SELECT DISTINCT pair FROM bots WHERE is_active = 1"
    ).fetchall()]

    for pair in active_pairs:
        norm_pair = normalize_symbol(pair).upper()
        exchange_qty = exchange_nets.get(norm_pair, 0.0)

        # 3. Sum bot_orders.filled_amount across all active bots on that pair
        # using the FIFO entry minus exit fills logic (ADR-006 Pair level)
        active_bot_rows = conn.execute(
            "SELECT id, direction, status FROM bots WHERE is_active = 1 AND normalized_pair = ?",
            (norm_pair,)
        ).fetchall()
        active_bot_ids = [r[0] for r in active_bot_rows]

        if not active_bot_ids:
            continue

        placeholders = ','.join('?' for _ in active_bot_ids)

        # Calculate db quantity for each bot on the pair and sum them up signed
        pair_db_qty = 0.0
        for bot_id, direction, _ in active_bot_rows:
            # Fetch current cycle_id and cycle_floor
            row_trade = conn.execute(
                "SELECT cycle_id, wipe_wall_ts FROM trades WHERE bot_id = ?",
                (bot_id,)
            ).fetchone()
            if not row_trade:
                continue
            target_cycle = row_trade[0]
            wall_ts = int(row_trade[1] or 0)
            
            # Auto-detect cycle floor
            row_floor = conn.execute("""
                SELECT cycle_id,
                       SUM(CASE WHEN order_type IN ('entry','grid','adoption','adoption_add','carry') THEN filled_amount ELSE 0.0 END) AS entry_qty,
                       SUM(CASE WHEN order_type IN ('tp','close','dust_close','sl','adoption_reduce','flatten_close') THEN filled_amount ELSE 0.0 END) AS exit_qty
                FROM bot_orders
                WHERE bot_id = ?
                  AND cycle_id < ?
                  AND cycle_id IS NOT NULL
                  AND filled_amount > 0
                  AND status = 'filled'
                GROUP BY cycle_id
                HAVING (entry_qty - exit_qty) > 1e-6
                ORDER BY cycle_id ASC
                LIMIT 1
            """, (bot_id, target_cycle)).fetchone()
            
            if row_floor:
                cycle_floor = row_floor[0]
            else:
                cycle_floor = target_cycle

            # Run FIFO calculation for this bot starting from cycle_floor and wall_ts
            # Fetch entries
            entries = conn.execute(f"""
                SELECT filled_amount FROM bot_orders
                WHERE bot_id = ?
                  AND cycle_id >= ? AND cycle_id <= ?
                  AND (
                      status IN ('filled', 'closed', 'auto_closed', 'hedge_exited', 'partially_filled')
                      OR (status IN ('canceled', 'cancelled', 'cancelling') AND filled_amount > 0)
                  )
                  AND filled_amount > 0
                  AND order_type IN ('entry', 'grid', 'adoption', 'adoption_add', 'carry')
                  AND (? = 0 OR created_at >= ?)
                ORDER BY created_at ASC
            """, (bot_id, cycle_floor, target_cycle, wall_ts, wall_ts)).fetchall()
            
            # Fetch exits
            exits = conn.execute(f"""
                SELECT filled_amount FROM bot_orders
                WHERE bot_id = ?
                  AND cycle_id >= ? AND cycle_id <= ?
                  AND (
                      status IN ('filled', 'closed', 'auto_closed', 'hedge_exited', 'partially_filled')
                      OR (status IN ('canceled', 'cancelled', 'cancelling') AND filled_amount > 0)
                  )
                  AND filled_amount > 0
                  AND order_type IN ('adoption_reduce', 'tp', 'close', 'dust_close', 'sl', 'flatten_close')
                  AND (? = 0 OR created_at >= ?)
                ORDER BY created_at ASC
            """, (bot_id, cycle_floor, target_cycle, wall_ts, wall_ts)).fetchall()
            
            total_exit_qty = sum(float(r[0]) for r in exits)
            
            # Match FIFO
            accum_sold = total_exit_qty
            bot_open_qty = 0.0
            for r in entries:
                qty = float(r[0])
                if accum_sold > 0.0:
                    if qty <= accum_sold:
                        accum_sold = round(accum_sold - qty, 8)
                    else:
                        bot_open_qty += round(qty - accum_sold, 8)
                        accum_sold = 0.0
                else:
                    bot_open_qty += qty
            
            bot_open_qty = round(bot_open_qty, 8)
            signed_oq = bot_open_qty if direction.upper() == 'LONG' else -bot_open_qty
            pair_db_qty += signed_oq

        pair_db_qty = round(pair_db_qty, 8)
        shortfall = round(exchange_qty - pair_db_qty, 8)

        # 4. If drift exceeds tolerance, identify candidates and log alert
        if abs(shortfall) > tolerance:
            # Look for flat bots (open_qty = 0)
            flat_bots = conn.execute(f"""
                SELECT b.id, b.name, b.direction, t.cycle_id 
                FROM bots b JOIN trades t ON t.bot_id = b.id 
                WHERE b.is_active = 1 AND b.normalized_pair = ? 
                  AND t.open_qty < ? AND b.id IN ({placeholders})
            """, [norm_pair, tolerance] + active_bot_ids).fetchall()

            candidates = []
            for f_bot_id, f_name, f_dir, f_cycle in flat_bots:
                # Sign of shortfall must match the bot direction
                # If shortfall is positive (+), exchange has more contracts than DB (more LONG exposure needed) -> matches LONG bot
                # If shortfall is negative (-), exchange is more short than DB -> matches SHORT bot
                shortfall_is_long = shortfall > 0
                bot_is_long = f_dir.upper() == 'LONG'

                if shortfall_is_long == bot_is_long:
                    typical_size = get_typical_position_size(conn, f_bot_id)

                    # Matches typical size OR typical size is 0.0 (no history, accepts any shortfall size)
                    if typical_size == 0.0 or abs(abs(shortfall) - typical_size) < tolerance:
                        candidates.append((f_bot_id, f_name))

            # Determine best candidate to suggest
            suggested_bot_id = None
            if candidates:
                suggested_bot_id = candidates[0][0]
                cand_names = ", ".join(f"{c[1]} (ID: {c[0]})" for c in candidates)
                notes_msg = f"[AUTO-DETECT] Position drift of {shortfall:+.4f} detected. Candidates: {cand_names}."
            else:
                notes_msg = f"[AUTO-DETECT] Position drift of {shortfall:+.4f} detected, but no matching flat bot could be found."

            # Enforce exactly one pending alert per pair to prevent double adoption / duplicates
            exists = conn.execute("""
                SELECT id FROM unowned_position_alerts 
                WHERE normalized_pair = ? AND status = 'pending_review'
            """, (norm_pair,)).fetchone()

            if exists:
                conn.execute("""
                    UPDATE unowned_position_alerts 
                    SET bot_id = ?, exchange_qty = ?, db_qty = ?, detected_at = ?, notes = ?
                    WHERE id = ?
                """, (suggested_bot_id, exchange_qty, pair_db_qty, int(time.time()), notes_msg, exists[0]))
                conn.commit()
                logger.info(f"🔄 [ORPHAN-DETECTOR] Updated existing unowned position alert for {pair} (drift={shortfall:+.4f})")
            else:
                conn.execute("""
                    INSERT INTO unowned_position_alerts 
                    (bot_id, pair, normalized_pair, exchange_qty, db_qty, detected_at, status, notes)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending_review', ?)
                """, (
                    suggested_bot_id, pair, norm_pair, exchange_qty, pair_db_qty, int(time.time()), notes_msg
                ))
                conn.commit()
                logger.critical(f"⚠️ [ORPHAN-DETECTOR] Created new unowned position alert for {pair} (drift={shortfall:+.4f}, suggest={suggested_bot_id})")


