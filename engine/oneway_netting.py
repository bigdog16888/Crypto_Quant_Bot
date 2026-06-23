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


def apply_oneway_entry_cross_reduction(
    filling_bot_id: int,
    pair: str,
    direction: str,
    delta: float,
    source_order_id: str,
    avg_price: float = 0.0,
    exchange = None,
) -> float:
    from engine.write_queue import WriteQueue
    return WriteQueue().put_and_wait(
        _apply_oneway_entry_cross_reduction_internal,
        filling_bot_id,
        pair,
        direction,
        delta,
        source_order_id,
        avg_price=avg_price,
        exchange=exchange,
    )

def _apply_oneway_entry_cross_reduction_internal(
    filling_bot_id: int,
    pair: str,
    direction: str,
    delta: float,
    source_order_id: str,
    avg_price: float = 0.0,
    exchange = None,
) -> float:
    """
    When a bot credits entry/grid qty on a one-way pair, reduce opposite-direction
    siblings' open_qty by the same amount (exchange already netted the position).
    Returns total qty reduced on siblings.
    """
    if delta <= 1e-12:
        return 0.0

    from engine.database import get_connection, save_bot_order
    from engine.ledger import seal_trade_state

    norm = _pair_norm(pair)
    filler_dir = str(direction).upper()
    target_dir = 'SHORT' if filler_dir == 'LONG' else 'LONG'
    conn = get_connection()

    neighbors: List[Tuple[int, float]] = []
    # Fix 4 (v3.5.8): Also fetch b.status so we can skip bots that are not actively
    # trading. A SCANNING or REQUIRE_MANUAL_PROOF bot may have a stale open_qty
    # residual from a prior cycle — cross-reducing against it creates phantom
    # virtual_netting rows and inflates the SHORT bot's open_qty reduction count.
    _INACTIVE_STATUSES = frozenset({
        'scanning', '\U0001f7e2 scanning',   # 🟢 SCANNING
        'stopped',
        'hedge_standby',
    })
    for bid, bdir, raw_pair, bot_norm, oq, b_status in conn.execute(
        """
        SELECT b.id, b.direction, b.pair, b.normalized_pair,
               COALESCE(t.open_qty, 0), b.status
        FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1 AND b.bot_type != 'hedge_child'
          AND b.id != ?
          AND b.id != COALESCE((SELECT hedge_child_bot_id FROM bots WHERE id = ?), -1)
          AND b.id != COALESCE((SELECT parent_bot_id FROM bots WHERE id = ?), -1)
        """,
        (filling_bot_id, filling_bot_id, filling_bot_id),
    ).fetchall():
        if (bot_norm or normalize_symbol(raw_pair)).upper() != norm:
            continue
        if str(bdir).upper() != target_dir:
            continue
        # Skip bots that are not actively holding a position.
        if str(b_status or '').lower() in _INACTIVE_STATUSES:
            continue
        oqf = float(oq or 0)
        if oqf > 1e-12:
            neighbors.append((bid, oqf))

    if not neighbors:
        return 0.0

    # INV-28B Constraint Check: Fetch source bot open_qty BEFORE reduction
    pre_reduction_source_qty = 0.0
    try:
        _pre_row = conn.execute(
            "SELECT COALESCE(open_qty, 0.0) FROM trades WHERE bot_id = ?",
            (filling_bot_id,)
        ).fetchone()
        if _pre_row:
            pre_reduction_source_qty = float(_pre_row[0])
    except Exception as _pre_err:
        logger.warning(f"Failed to fetch pre-reduction source qty: {_pre_err}")

    # Map old open_qty for siblings
    old_qty_map = {bid: oq for bid, oq in neighbors}

    remaining = delta
    total_cut = 0.0
    ts = int(time.time())
    reduced_bots = []
    cuts_map = {}
    for nb_id, oq in sorted(neighbors, key=lambda x: -x[1]):
        if remaining <= 1e-12:
            break
        cut = round(min(oq, remaining), 8)
        if cut <= 0:
            continue
        cycle_row = conn.execute(
            "SELECT cycle_id FROM trades WHERE bot_id = ?", (nb_id,)
        ).fetchone()
        cycle_id = int(cycle_row[0] or 1) if cycle_row else 1

        # 1. RECENCY CHECK FIRST (do NOT insert claim yet)
        recency_row = conn.execute(
            """
            SELECT MAX(filled_at) FROM bot_orders 
            WHERE bot_id = ? AND cycle_id = ? AND order_type IN ('entry', 'grid') AND status = 'filled'
            """,
            (nb_id, cycle_id)
        ).fetchone()
        max_filled_at = recency_row[0] if recency_row and recency_row[0] is not None else 0
        if max_filled_at > 0 and (time.time() - max_filled_at < 30):
            logger.warning(
                f"⚠️ [ONEWAY-RECENCY] Skipping cross-reduction for bot {nb_id} cycle {cycle_id} "
                f"due to recent entry fill ({int(time.time() - max_filled_at)}s ago < 30s)."
            )
            continue

        # 2. CLAIM INSERT SECOND (only if recency check passed)
        res = conn.execute(
            """
            INSERT OR IGNORE INTO cross_reduction_claims 
            (source_order_id, source_bot_id, target_bot_id, reduction_qty, claimed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(source_order_id or 'manual'), filling_bot_id, nb_id, cut, int(time.time()))
        )
        if res.rowcount == 0:
            logger.warning(
                f"⚠️ [ONEWAY-IDEMPOTENCY] Skip: cross-reduction from source_order_id {source_order_id} "
                f"to target_bot_id {nb_id} already claimed."
            )
            continue

        # Generate a unique order_id and client_order_id for the virtual netting row
        _src = str(source_order_id or 'manual')
        vn_order_id = f"VN_{nb_id}_{_src}_{int(time.time())}"
        vn_client_order_id = f"CQB_{nb_id}_VNET_{_src}_{int(time.time())}"
        save_bot_order(
            nb_id,
            'virtual_netting',
            vn_order_id,
            avg_price or 0.0,
            cut,
            step=0,
            status='filled',
            client_order_id=vn_client_order_id,
            notes=(
                f"ONEWAY_CROSS: bot {filling_bot_id} {filler_dir} entry -{cut:.6f} "
                f"from shared position (src {source_order_id})"
            ),
            cycle_id=cycle_id,
        )
        conn.execute(
            "UPDATE bot_orders SET filled_amount = ? WHERE client_order_id = ? AND bot_id = ?",
            (cut, vn_client_order_id, nb_id),
        )
        
        # Write virtual_netting exit on the filling bot as well (since this portion of the fill netted instead of adding exposure)
        filling_cycle_row = conn.execute(
            "SELECT cycle_id FROM trades WHERE bot_id = ?", (filling_bot_id,)
        ).fetchone()
        filling_cycle_id = int(filling_cycle_row[0] or 1) if filling_cycle_row else 1
        vn_order_id_src = f"VN_{filling_bot_id}_{nb_id}_{int(time.time())}"
        vn_client_order_id_src = f"CQB_{filling_bot_id}_VNET_SRC_{nb_id}_{int(time.time())}"
        save_bot_order(
            filling_bot_id,
            'virtual_netting',
            vn_order_id_src,
            avg_price or 0.0,
            cut,
            step=0,
            status='filled',
            client_order_id=vn_client_order_id_src,
            notes=(
                f"ONEWAY_CROSS_SRC: netting entry against bot {nb_id} ({target_dir}) "
                f"position (src {source_order_id})"
            ),
            cycle_id=filling_cycle_id,
        )
        conn.execute(
            "UPDATE bot_orders SET filled_amount = ? WHERE client_order_id = ? AND bot_id = ?",
            (cut, vn_client_order_id_src, filling_bot_id),
        )
        
        remaining -= cut
        total_cut += cut
        cuts_map[nb_id] = cut
        reduced_bots.append(nb_id)
        if filling_bot_id not in reduced_bots:
            reduced_bots.append(filling_bot_id)
        logger.warning(
            f"⚖️ [ONEWAY-CROSS] Pair {norm}: bot {filling_bot_id} {filler_dir} entry "
            f"−{cut:.6f} from bot {nb_id} ({target_dir}) open_qty "
            f"(source order {source_order_id})."
        )

    if total_cut > 0:
        conn.commit()
        for nb_id in reduced_bots:
            try:
                seal_trade_state(nb_id, force_recompute=True)
            except Exception as e_seal:
                logger.error(f"Failed to seal cross-reduced bot {nb_id}: {e_seal}")

            # Fix A (INV-28A) — Stale TP cancellation in oneway_netting.py:
            if nb_id != filling_bot_id:
                try:
                    # 1. Query bot_orders for any row WHERE bot_id=nb_id AND status IN ('open','new') AND order_type IN ('tp','dust_close')
                    _nb_tps = conn.execute(
                        "SELECT id, order_id, client_order_id "
                        "FROM bot_orders WHERE bot_id=? "
                        "AND order_type IN ('tp','dust_close') "
                        "AND status IN ('open','new')",
                        (nb_id,)
                    ).fetchall()
                    
                    if _nb_tps:
                        # Fetch the sibling bot's pair
                        _nb_pair_row = conn.execute("SELECT pair FROM bots WHERE id = ?", (nb_id,)).fetchone()
                        _nb_pair = _nb_pair_row[0] if _nb_pair_row else pair
                        
                        _local_exchange = exchange
                        if not _local_exchange:
                            try:
                                from engine.runner import BotRunner
                                _runner = BotRunner.get_instance()
                                if _runner and hasattr(_runner, 'exchange') and _runner.exchange:
                                    _local_exchange = _runner.exchange
                            except Exception:
                                pass

                        if not _local_exchange:
                            _cfg_row = conn.execute("SELECT config FROM bots WHERE id = ?", (nb_id,)).fetchone()
                            _mtype = 'future'
                            if _cfg_row and _cfg_row[0]:
                                try:
                                    import json
                                    _cfg = json.loads(_cfg_row[0])
                                    _mtype = _cfg.get('market_type', 'future')
                                except Exception:
                                    pass
                            from engine.exchange_interface import ExchangeInterface
                            _local_exchange = ExchangeInterface(market_type=_mtype)

                        for _tp_row in _nb_tps:
                            _row_id, _order_id, _client_order_id = _tp_row
                            # 2. Call exchange.cancel_order wrapped in try/except
                            try:
                                _local_exchange.cancel_order(_order_id, _nb_pair)
                            except Exception as _ex_cancel_err:
                                logger.warning(
                                    f"[CROSS-REDUCE-CANCEL] Failed to cancel order {_order_id} "
                                    f"on exchange for bot {nb_id}: {_ex_cancel_err}"
                                )
                            
                            # 3. UPDATE bot_orders
                            conn.execute(
                                "UPDATE bot_orders SET status='cancelled', "
                                "notes = COALESCE(notes,'') || ' [CROSS-REDUCE-CANCEL: stale TP after open_qty reduction]', "
                                "updated_at = unixepoch() "
                                "WHERE id = ?", (_row_id,)
                            )
                            conn.commit()

                            # 4. Log the cancellation
                            old_qty = old_qty_map.get(nb_id, 0.0)
                            new_qty = old_qty - cuts_map.get(nb_id, 0.0)
                            logger.warning(
                                f"[INV-28A] Bot {nb_id}: cancelled stale TP {_order_id} after cross-reduction "
                                f"reduced open_qty {old_qty:.6f} → {new_qty:.6f}. maintain_orders will resize next cycle."
                            )
                except Exception as _cr_err:
                    logger.error(f"[CROSS-REDUCE-CANCEL] Failed for bot {nb_id}: {_cr_err}")

        # Fix B (INV-28B) — Physical orphan check in oneway_netting.py:
        if pre_reduction_source_qty > 1e-8:
            try:
                # 1. Read trades.open_qty WHERE bot_id = source_bot_id
                filling_oq_row = conn.execute(
                    "SELECT COALESCE(t.open_qty, 0.0) FROM trades t WHERE t.bot_id = ?",
                    (filling_bot_id,)
                ).fetchone()
                filling_oq = float(filling_oq_row[0]) if filling_oq_row else 0.0
                
                # If virtual open_qty <= 1e-8:
                if filling_oq <= 1e-8:
                    # 2. Read bots.direction WHERE id = source_bot_id
                    _dir_row = conn.execute("SELECT direction FROM bots WHERE id = ?", (filling_bot_id,)).fetchone()
                    _direction = _dir_row[0] if _dir_row else filler_dir
                    
                    # 3. Resolve exchange and call get_exchange_signed_net
                    _local_exchange = exchange
                    if not _local_exchange:
                        try:
                            from engine.runner import BotRunner
                            _runner = BotRunner.get_instance()
                            if _runner and hasattr(_runner, 'exchange') and _runner.exchange:
                                _local_exchange = _runner.exchange
                        except Exception:
                            pass

                    if not _local_exchange:
                        _cfg_row = conn.execute("SELECT config FROM bots WHERE id = ?", (filling_bot_id,)).fetchone()
                        _mtype = 'future'
                        if _cfg_row and _cfg_row[0]:
                            try:
                                import json
                                _cfg = json.loads(_cfg_row[0])
                                _mtype = _cfg.get('market_type', 'future')
                            except Exception:
                                pass
                        from engine.exchange_interface import ExchangeInterface
                        _local_exchange = ExchangeInterface(market_type=_mtype)

                    from engine.parity_gates import get_exchange_signed_net
                    # 4. signed_net = result
                    signed_net = get_exchange_signed_net(_local_exchange, pair)
                    
                    if signed_net is not None:
                        # 5. source_is_long = direction.upper() == 'LONG'
                        source_is_long = _direction.upper() == 'LONG'
                        # 6. orphan_exists
                        orphan_exists = (source_is_long and signed_net > 0.0001) or \
                                        (not source_is_long and signed_net < -0.0001)
                        # 7. If virtual open_qty <= 1e-8 AND orphan_exists: UPDATE and Log CRITICAL
                        if orphan_exists:
                            conn.execute(
                                "UPDATE bots SET status='pending_flatten' WHERE id = ?",
                                (filling_bot_id,)
                            )
                            conn.commit()
                            logger.critical(
                                f"[INV-28B] Bot {filling_bot_id}: virtual open_qty=0 but physical "
                                f"net={signed_net:.6f} on {_direction} side. Setting pending_flatten. "
                                f"Runner will close orphan next cycle."
                            )
            except Exception as _ob_err:
                logger.error(f"[CROSS-REDUCE-ORPHAN-CHECK] Failed: {_ob_err}")

    return total_cut


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


def detect_hedge_child_ghost(exchange, child_bot_id, conn) -> bool:
    """
    Returns True if hedge child has open_qty > 0 but exchange shows
    no position on that side for that pair.
    """
    child_row = conn.execute(
        "SELECT b.pair, b.direction, t.open_qty, b.parent_bot_id "
        "FROM bots b JOIN trades t ON t.bot_id = b.id "
        "WHERE b.id = ?", (child_bot_id,)
    ).fetchone()
    if not child_row or float(child_row[2] or 0) <= 0.0001:
        return False  # no position to check

    pair, direction, open_qty, parent_bot_id = child_row
    open_qty = float(open_qty)

    # Get parent info
    parent_row = conn.execute(
        "SELECT b.direction, t.open_qty "
        "FROM bots b JOIN trades t ON t.bot_id = b.id "
        "WHERE b.id = ?", (parent_bot_id,)
    ).fetchone()

    parent_direction = parent_row[0] if parent_row else ('SHORT' if direction == 'LONG' else 'LONG')
    parent_open_qty = float(parent_row[1] or 0.0) if parent_row else 0.0

    from engine.parity_gates import get_exchange_signed_net, qty_tolerance
    exchange_net = get_exchange_signed_net(exchange, pair)
    if exchange_net is None:
        # If API call failed, do not assume ghost to be safe
        return False

    parent_contribution = parent_open_qty if parent_direction == 'LONG' else -parent_open_qty

    # Deduct signed net contribution of all other active bots on the same pair
    from engine.exchange_interface import normalize_symbol
    norm_pair = normalize_symbol(pair).upper()
    other_contribution = 0.0
    _exclude_ids = (child_bot_id, parent_bot_id or -1)
    other_rows = conn.execute(
        "SELECT b.direction, COALESCE(t.open_qty, 0) FROM bots b JOIN trades t ON t.bot_id = b.id "
        "WHERE b.is_active = 1 AND b.normalized_pair = ? AND b.id NOT IN (?, ?)",
        (norm_pair, _exclude_ids[0], _exclude_ids[1])
    ).fetchall()
    for _other_dir, _other_qty in other_rows:
        oq = float(_other_qty or 0.0)
        if _other_dir.upper() == 'LONG':
            other_contribution += oq
        else:
            other_contribution -= oq

    tolerance = qty_tolerance()
    if len(other_rows) > 0:
        expected_exchange_net_without_child = parent_contribution + other_contribution
        if abs(expected_exchange_net_without_child - exchange_net) < tolerance:
            # Exchange matches parent + other bots (i.e. child is gone) -> child position is gone!
            return True
    else:
        pair_net_without_child = parent_contribution
        if abs(pair_net_without_child - exchange_net) < tolerance:
            # Exchange matches parent-only contribution -> child position is gone!
            return True

    return False


def wipe_hedge_child_ghost(exchange, child_bot_id, conn):
    # 1. Fetch details
    row = conn.execute(
        "SELECT b.name, b.pair, b.direction, t.open_qty, t.cycle_id "
        "FROM bots b JOIN trades t ON t.bot_id = b.id "
        "WHERE b.id = ?", (child_bot_id,)
    ).fetchone()
    if not row:
        return
    name, pair, direction, open_qty, cycle_id = row

    # 1. Cancel any open orders for this child on exchange
    if exchange:
        try:
            exchange.cancel_orders_by_bot_id(child_bot_id, pair)
            logger.info(f"🧹 [HEDGE-GHOST] Cancelled open orders for hedge child {child_bot_id} on {pair}.")
        except Exception as e:
            logger.error(f"Failed to cancel open orders for hedge child {child_bot_id} on {pair}: {e}")

    # 2. Set status to hedge_standby (will be committed atomically in step 3)
    conn.execute(
        "UPDATE bots SET status = 'hedge_standby' WHERE id = ?", (child_bot_id,)
    )

    # 3. Cancel open internal orders and archive filled orders to prevent zombie revival
    conn.execute(
        "UPDATE bot_orders SET status='cancelled' "
        "WHERE bot_id=? AND status IN ('open', 'new', 'placing', 'cancelling')",
        (child_bot_id,)
    )
    conn.execute(
        "UPDATE bot_orders SET status='reset_cleared' "
        "WHERE bot_id=? AND (status NOT IN ('open', 'new', 'placing', 'cancelling', 'auto_closed', 'reset_cleared', 'cancelled') OR (status IN ('cancelled', 'canceled') AND filled_amount > 0))",
        (child_bot_id,)
    )
    conn.commit()

    # 4. Seal the bot using its new 'reset_cleared' state (reads 0.0 open_qty)
    from engine.ledger import seal_trade_state
    seal_trade_state(child_bot_id, force_recompute=True)

    # Force status to hedge_standby if seal overwrote it to Scanning
    conn.execute(
        "UPDATE bots SET status = 'hedge_standby' WHERE id = ?",
        (child_bot_id,)
    )
    conn.commit()

    # 5. Write a drift_note audit row
    from engine.database import save_bot_order
    ts_now = int(time.time())
    drift_cid = f"CQB_{child_bot_id}_DRIFT_GHOST_WIPE_{ts_now}"
    try:
        save_bot_order(
            child_bot_id, 'drift_note', f'GHOST_WIPE_{child_bot_id}_{ts_now}',
            price=0.0, amount=0.0, step=0, status='audit',
            client_order_id=drift_cid,
            notes=f"[HEDGE-GHOST] DB claims {open_qty} but exchange is flat on this side. Auto-wiped to hedge_standby.",
            cycle_id=cycle_id
        )
    except Exception as e:
        logger.error(f"Failed to save drift_note audit row for hedge child ghost {child_bot_id}: {e}")

    # 5. Log CRITICAL: [HEDGE-GHOST] Child {id} ({name}): DB claims {open_qty} but exchange is flat. Auto-wiped to hedge_standby.
    logger.critical(
        f"[HEDGE-GHOST] Child {child_bot_id} ({name}): DB claims "
        f"{open_qty} but exchange is flat. Auto-wiped to hedge_standby."
    )


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
