"""
Pair-level parity gates: exchange net must match proof ledger before cycle reset or trading.

Invariant: after removing one bot's signed virtual contribution, pair virtual must match
exchange within tolerance — otherwise cycle reset would clear ledger while exchange still holds size.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

from config.settings import config
from engine.exchange_interface import normalize_symbol

logger = logging.getLogger(__name__)

CYCLE_RESET_CARRY_LABELS = frozenset({
    'SYSTEM_WIPE', 'MANUAL', 'MANUAL_CLOSE', 'PARTIAL_MANUAL', 'CARRY_WIPE',
    'EMERGENCY_CLOSE', 'RESET_PHANTOM_ENTRY',
})


class CycleResetBlockedError(Exception):
    """Raised when a cycle reset would widen pair-level ledger vs exchange gap."""


def qty_tolerance() -> float:
    return float(getattr(config, 'PAIR_PARITY_QTY_TOLERANCE', 0.002))


def forensic_adopt_allowed() -> bool:
    return bool(getattr(config, 'ALLOW_FORENSIC_ADOPT', False))


def get_bot_signed_contribution(bot_id: int) -> float:
    """Signed virtual qty this bot contributes to pair netting (matches get_pair_virtual_net)."""
    from engine.database import get_connection, _canonical_bot_orders_from

    conn = get_connection()
    row = conn.execute(
        "SELECT direction FROM bots WHERE id = ?", (bot_id,)
    ).fetchone()
    if not row:
        return 0.0

    direction = str(row[0] or 'LONG').upper()
    cursor = conn.cursor()
    cycle_row = cursor.execute(
        "SELECT cycle_id, COALESCE(wipe_wall_ts, 0) FROM trades WHERE bot_id = ?", (bot_id,)
    ).fetchone()
    if not cycle_row or cycle_row[0] is None:
        return 0.0

    cycle_id, wipe_wall = int(cycle_row[0]), int(cycle_row[1] or 0)

    pos_row = conn.execute(
        "SELECT COALESCE(position_side, direction) FROM trades t JOIN bots b ON b.id=t.bot_id WHERE t.bot_id=?",
        (bot_id,),
    ).fetchone()
    position_side = pos_row[0] if pos_row else direction

    res = cursor.execute(f"""
        SELECT
            COALESCE(SUM(CASE WHEN bo.cycle_id = ? AND bo.status NOT IN ('auto_closed','reset_cleared')
                AND (? = 0 OR bo.created_at >= ?)
                AND bo.order_type IN ('entry','grid','adoption_add','adoption','carry')
                THEN bo.filled_amount ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN bo.cycle_id = ? AND bo.status NOT IN ('auto_closed','reset_cleared')
                AND (? = 0 OR bo.created_at >= ?)
                AND bo.order_type IN ('adoption_reduce','tp','close','dust_close','sl','virtual_netting')
                THEN bo.filled_amount ELSE 0 END), 0),
            ROUND(COALESCE(SUM(
                CASE WHEN bo.status NOT IN ('auto_closed','reset_cleared','rejected','failed')
                     AND bo.order_type LIKE 'hedge%' AND bo.order_type NOT LIKE '%tp%'
                THEN bo.filled_amount
                WHEN bo.status NOT IN ('auto_closed','reset_cleared','rejected','failed')
                     AND (bo.order_type LIKE 'hedge%tp%' OR bo.order_type LIKE 'hedgetp%')
                THEN -bo.filled_amount ELSE 0 END
            ), 0), 8)
        {_canonical_bot_orders_from('bo')}
          AND bo.bot_id = ?
          AND (bo.order_type LIKE 'hedge%' OR bo.position_side = ? OR bo.position_side IS NULL
               OR bo.position_side = 'BOTH' OR bo.position_side = '')
          AND (bo.status IN ('filled','closed','auto_closed','hedge_exited')
               OR (bo.status IN ('canceled','cancelled') AND bo.filled_amount > 0))
          AND bo.filled_amount > 0
    """, (
        cycle_id, wipe_wall, wipe_wall,
        cycle_id, wipe_wall, wipe_wall,
        bot_id, position_side,
    )).fetchone()
    if not res:
        return 0.0

    bought, sold, hedge = float(res[0] or 0), float(res[1] or 0), float(res[2] or 0)
    bot_net = round(bought - sold, 8)
    if direction == 'LONG':
        return round(bot_net - hedge, 8)
    return round(-bot_net + hedge, 8)


def get_exchange_signed_net(exchange, pair: str) -> Optional[float]:
    """Signed exchange net for pair (positive = long). None if positions fetch failed."""
    if not exchange:
        return None
    norm = normalize_symbol(pair).upper()
    try:
        positions = exchange.fetch_positions()
    except Exception as e:
        logger.error(f"[PARITY] fetch_positions failed for {pair}: {e}")
        return None
    if positions is None:
        return None

    total = 0.0
    for pos in positions:
        if normalize_symbol(pos.get('symbol', '')).upper() != norm:
            continue
        total += float(pos.get('net_qty', pos.get('contracts', 0)) or 0)
    return round(total, 8)


def projected_pair_virtual_after_bot_flat(bot_id: int, pair: str) -> float:
    from engine.database import get_pair_virtual_net

    current = get_pair_virtual_net(pair)
    contrib = get_bot_signed_contribution(bot_id)
    return round(current - contrib, 8)


def pair_parity_ok(
    pair: str,
    exchange=None,
    virtual: Optional[float] = None,
    physical: Optional[float] = None,
    tol: Optional[float] = None,
) -> Tuple[bool, float, float, float]:
    """Returns (ok, virtual, physical, delta)."""
    from engine.database import get_pair_virtual_net

    tol = qty_tolerance() if tol is None else tol
    virtual = get_pair_virtual_net(pair) if virtual is None else virtual
    physical = get_exchange_signed_net(exchange, pair) if physical is None else physical
    if physical is None:
        return False, virtual, 0.0, 0.0
    delta = round(physical - virtual, 8)
    return abs(delta) <= tol, virtual, physical, delta


def assert_cycle_reset_allowed(
    bot_id: int,
    pair: str,
    action_label: str,
    human_approved: bool = False,
    exchange=None,
) -> None:
    """
    Block cycle reset when removing this bot's ledger would leave pair virtual != exchange.
    MANUAL_CLOSE / SYSTEM_WIPE with human_approved skip (caller must have flattened first).
    """
    label = (action_label or 'TP_HIT').upper()
    if label in CYCLE_RESET_CARRY_LABELS and human_approved:
        return

    projected = projected_pair_virtual_after_bot_flat(bot_id, pair)
    physical = get_exchange_signed_net(exchange, pair)
    if physical is None:
        raise CycleResetBlockedError(
            f"Bot {bot_id}: cannot verify exchange position for {pair} — reset blocked."
        )

    tol = qty_tolerance()
    gap = abs(projected - physical)
    if gap > tol:
        from engine.database import get_pair_virtual_net

        virtual = get_pair_virtual_net(pair)
        contrib = get_bot_signed_contribution(bot_id)
        msg = (
            f"[CYCLE-RESET-BLOCKED] Bot {bot_id} {label} on {pair}: "
            f"virtual={virtual:.6f} bot_contrib={contrib:.6f} "
            f"projected_after_reset={projected:.6f} exchange={physical:.6f} gap={gap:.6f}. "
            f"Flatten exchange to match proof before reset."
        )
        logger.error(msg)
        raise CycleResetBlockedError(msg)


def gate_trading_allowed(
    bot_id: int,
    pair: str,
    exchange=None,
) -> Tuple[bool, str]:
    """Block entries / maintain when pair ledger != exchange."""
    ok, virtual, physical, delta = pair_parity_ok(pair, exchange=exchange)
    if ok:
        return True, ''
    reason = (
        f"Pair parity gate: {pair} virtual={virtual:.6f} exchange={physical:.6f} "
        f"delta={delta:.6f}"
    )
    logger.error(f"🛑 [PAIR-PARITY-GATE] Bot {bot_id}: {reason}")
    _set_bot_require_manual_proof(bot_id, reason)
    return False, reason


def _set_bot_require_manual_proof(bot_id: int, reason: str) -> None:
    from engine.database import get_connection

    try:
        conn = get_connection()
        conn.execute(
            "UPDATE bots SET status='REQUIRE_MANUAL_PROOF' WHERE id=? AND status NOT IN ('STOPPED')",
            (bot_id,),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to set REQUIRE_MANUAL_PROOF for bot {bot_id}: {e} ({reason})")


def flag_orphan_fill_manual_proof(
    bot_id: int,
    order_id: str,
    symbol: str,
    qty: float,
    source: str,
) -> None:
    """Forensic adopt disabled — require operator proof instead."""
    logger.error(
        f"🚨 [ORPHAN-NO-ADOPT] Bot {bot_id} {source}: order {order_id} on {symbol} "
        f"qty={qty:.6f} not in ledger. Set ALLOW_FORENSIC_ADOPT=True to auto-adopt (not recommended)."
    )
    _set_bot_require_manual_proof(
        bot_id,
        f"Orphan fill {order_id} requires manual proof (forensic adopt disabled)",
    )
    try:
        from engine.database import flag_pair_ledger_mismatch, audit_pair_ledger_vs_exchange
        from engine.exchange_interface import ExchangeInterface

        ex = ExchangeInterface(market_type='future')
        mismatches = audit_pair_ledger_vs_exchange(ex, qty_tolerance=qty_tolerance())
        if mismatches:
            flag_pair_ledger_mismatch(mismatches)
    except Exception:
        pass


def purge_phantom_ledger_when_exchange_flat(
    exchange,
    pair: str,
    virtual: float,
    physical: float,
) -> Tuple[bool, str]:
    """
    Exchange is flat but ledger still shows size — purge bot ledgers via safe_wipe (no market order).
    Proof: physical net ≈ 0 from live fetch_positions.
    """
    tol = qty_tolerance()
    if abs(physical) > tol:
        return False, f'exchange not flat ({physical:.6f})'
    if abs(virtual) <= tol:
        return False, 'virtual already flat'

    from engine.database import get_connection, safe_wipe_bot

    conn = get_connection()
    norm = normalize_symbol(pair).upper()
    bots = conn.execute(
        "SELECT id, pair, direction FROM bots WHERE is_active=1"
    ).fetchall()
    wiped = []
    for bot_id, raw_pair, direction in bots:
        if normalize_symbol(raw_pair).upper() != norm:
            continue
        ok = safe_wipe_bot(
            bot_id,
            raw_pair,
            direction or 'LONG',
            reason='PHANTOM_LEDGER_PURGE',
            force=True,
            human_approved=True,
        )
        if ok:
            wiped.append(bot_id)
            logger.warning(
                f"🧹 [PHANTOM-PURGE] Bot {bot_id} on {pair}: ledger had {virtual:.4f} "
                f"but exchange=0 — safe_wipe complete."
            )
    if not wiped:
        return False, 'no bots wiped'
    return True, f'purged bots {wiped}'


def startup_repair_mismatched_pairs(exchange) -> Dict[str, Any]:
    """
    Run after CQB history scan: purge phantom ledgers (exchange flat), re-audit.
    """
    from engine.database import audit_pair_ledger_vs_exchange, flag_pair_ledger_mismatch

    summary: Dict[str, Any] = {'purged': [], 'remaining': []}
    if not exchange:
        return summary

    tol = qty_tolerance()
    mismatches = audit_pair_ledger_vs_exchange(exchange, tol)
    for pair, virtual, physical, delta in mismatches:
        if abs(physical) <= tol and abs(virtual) > tol:
            if getattr(config, 'TESTNET_PURGE_PHANTOM_LEDGER', False):
                ok, msg = purge_phantom_ledger_when_exchange_flat(
                    exchange, pair, virtual, physical,
                )
                if ok:
                    summary['purged'].append((pair, msg))
            else:
                logger.error(
                    f"🚨 [PHANTOM-LEDGER] {pair}: virtual={virtual:.4f} exchange=0. "
                    f"Set TESTNET_PURGE_PHANTOM_LEDGER=True or wipe manually."
                )

    remaining = audit_pair_ledger_vs_exchange(exchange, tol)
    if remaining:
        flag_pair_ledger_mismatch(remaining)
        summary['remaining'] = [(p, v, ph, d) for p, v, ph, d in remaining]
    return summary


def proof_flatten_pair(
    exchange,
    pair: str,
    human_approved: bool = False,
) -> Dict[str, Any]:
    """
    Proof flatten protocol:
    1. Cancel CQB open orders on pair
    2. reduceOnly market to flat exchange net
    3. Verify exchange flat
    4. reset_bot_after_tp(MANUAL_CLOSE) for all active bots on pair
    """
    if not human_approved:
        return {'success': False, 'error': 'human_approved required for proof flatten'}

    from engine.database import get_connection, reset_bot_after_tp

    result: Dict[str, Any] = {
        'success': False,
        'pair': pair,
        'cancelled_orders': 0,
        'close_order': None,
        'bots_reset': [],
        'errors': [],
    }

    norm_target = normalize_symbol(pair).upper()
    conn = get_connection()
    bot_rows = conn.execute(
        "SELECT id, pair FROM bots WHERE is_active=1"
    ).fetchall()
    target_bot_ids = [
        r[0] for r in bot_rows
        if normalize_symbol(r[1]).upper() == norm_target
    ]

    # 1. Cancel CQB orders
    try:
        open_orders = exchange.fetch_open_orders(pair) or []
        for o in open_orders:
            cid = o.get('clientOrderId', '') or ''
            if cid.startswith('CQB_'):
                try:
                    exchange.cancel_order(o['id'], pair)
                    result['cancelled_orders'] += 1
                except Exception as e:
                    result['errors'].append(f"cancel {o['id']}: {e}")
    except Exception as e:
        result['errors'].append(f"fetch_open_orders: {e}")

    # 2. Market reduceOnly flatten
    net = get_exchange_signed_net(exchange, pair)
    if net is None:
        return {**result, 'error': 'Could not read exchange positions'}

    tol = qty_tolerance()
    if abs(net) > tol:
        close_side = 'sell' if net > 0 else 'buy'
        close_qty = abs(net)
        try:
            prec = exchange.get_symbol_precision(pair)
            step = float(prec.get('amount_step', prec.get('step_size', 0)) or 0)
            if step > 0:
                close_qty = exchange.round_to_step(close_qty, step)
        except Exception:
            pass

        if close_qty <= 0:
            result['errors'].append('close_qty rounded to zero')
        else:
            ts = int(time.time() * 1000)
            client_id = f"CQB_FLATTEN_{norm_target.replace('/', '')}_{ts}"
            try:
                close_order = exchange.create_order(
                    symbol=pair,
                    type='market',
                    side=close_side,
                    amount=close_qty,
                    price=None,
                    params={
                        'reduceOnly': True,
                        'clientOrderId': client_id,
                        'human_approved': True,
                    },
                )
                result['close_order'] = close_order
            except Exception as e:
                result['errors'].append(f"market close: {e}")
                return {**result, 'error': str(e)}

    # 3. Verify flat
    time.sleep(0.5)
    net_after = get_exchange_signed_net(exchange, pair)
    if net_after is None or abs(net_after) > tol:
        return {
            **result,
            'error': f'Exchange still not flat: net={net_after}',
            'exchange_net': net_after,
        }

    # 4. Reset bots only when exchange is flat
    for bid in target_bot_ids:
        try:
            reset_bot_after_tp(
                bid,
                exit_price=0.0,
                action_label='MANUAL_CLOSE',
                notes=f'proof_flatten_pair: exchange flat verified',
                human_approved=True,
            )
            result['bots_reset'].append(bid)
        except CycleResetBlockedError as e:
            result['errors'].append(f"reset bot {bid}: {e}")
        except Exception as e:
            result['errors'].append(f"reset bot {bid}: {e}")

    result['success'] = (
        net_after is not None
        and abs(net_after) <= tol
        and not result['errors']
    )
    return result
