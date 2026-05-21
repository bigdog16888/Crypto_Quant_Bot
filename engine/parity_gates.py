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
    'ORPHAN_EXCHANGE_REPAIR',  # Caller verified exchange flat before reset
})


class CycleResetBlockedError(Exception):
    """Raised when a cycle reset would widen pair-level ledger vs exchange gap."""


def qty_tolerance() -> float:
    return float(getattr(config, 'PAIR_PARITY_QTY_TOLERANCE', 0.002))


def _repair_client_order_id(prefix: str, pair: str) -> str:
    """Binance futures clientOrderId max length is 36."""
    norm = normalize_symbol(pair).upper().replace('/', '').replace(':', '')[:10]
    ts = int(time.time() * 1000) % 10**12
    cid = f"{prefix}_{norm}_{ts}"
    return cid[:36]


def forensic_adopt_allowed() -> bool:
    return bool(getattr(config, 'ALLOW_FORENSIC_ADOPT', False))


def get_bot_signed_contribution(bot_id: int) -> float:
    """Signed virtual qty this bot contributes to pair netting (matches get_pair_virtual_net)."""
    from engine.database import get_connection, get_bot_hedge_qty

    conn = get_connection()
    row = conn.execute(
        "SELECT b.direction, COALESCE(t.open_qty, 0) FROM bots b "
        "JOIN trades t ON t.bot_id = b.id WHERE b.id = ?",
        (bot_id,),
    ).fetchone()
    if not row:
        return 0.0

    direction = str(row[0] or 'LONG').upper()
    open_qty = float(row[1] or 0)
    hedge_qty = get_bot_hedge_qty(bot_id)
    if direction == 'LONG':
        return round(open_qty - hedge_qty, 8)
    return round(-open_qty + hedge_qty, 8)


def pair_heal_budget(exchange, pair: str) -> float:
    """
    How much additional |qty| the ledger may absorb for this pair without exceeding
    exchange net (same sign). Used by startup heal paths to prevent double-counting.
    """
    from engine.database import get_pair_virtual_net

    physical = get_exchange_signed_net(exchange, pair)
    if physical is None:
        return 0.0
    virtual = get_pair_virtual_net(pair)
    tol = qty_tolerance()
    if abs(virtual) <= tol and abs(physical) <= tol:
        return 0.0
    # Opposite signs: do not auto-heal (requires manual flatten)
    if (virtual > tol and physical < -tol) or (virtual < -tol and physical > tol):
        return 0.0
    if abs(virtual) >= abs(physical) - tol:
        return 0.0
    return round(abs(physical) - abs(virtual), 8)


def gate_heal_exit_without_entry(bot_id: int, order_type: str, proposed_qty: float) -> bool:
    """
    Block startup heal from crediting exit-only fills when the bot cycle has no entries.
    Prevents orphan TP credits (e.g. short bot scanning but TP row gets 0.11 fill).
    """
    otype = (order_type or '').lower()
    exit_types = frozenset({
        'tp', 'close', 'dust_close', 'sl', 'virtual_netting',
        'adoption_reduce', 'forensic_adoption_reduce', 'hedgetp',
    })
    if otype not in exit_types:
        return True
    if proposed_qty <= 0:
        return False

    from engine.database import get_connection, _canonical_bot_orders_from

    conn = get_connection()
    row = conn.execute(
        "SELECT cycle_id, COALESCE(wipe_wall_ts, 0) FROM trades WHERE bot_id = ?", (bot_id,)
    ).fetchone()
    if not row or row[0] is None:
        logger.warning(
            f"[HEAL-BLOCKED] Bot {bot_id}: exit heal {proposed_qty:.6f} with no active trade row."
        )
        return False

    cycle_id, wipe_wall = int(row[0]), int(row[1] or 0)
    bought = conn.execute(f"""
        SELECT COALESCE(SUM(bo.filled_amount), 0)
        {_canonical_bot_orders_from('bo')}
          AND bo.bot_id = ?
          AND bo.cycle_id = ?
          AND bo.status NOT IN ('auto_closed', 'reset_cleared')
          AND (? = 0 OR bo.created_at >= ?)
          AND bo.order_type IN ('entry', 'grid', 'adoption_add', 'adoption', 'carry')
          AND bo.filled_amount > 0
    """, (bot_id, cycle_id, wipe_wall, wipe_wall)).fetchone()[0]

    if float(bought or 0) <= qty_tolerance():
        logger.warning(
            f"[HEAL-BLOCKED] Bot {bot_id}: exit-type heal {proposed_qty:.6f} blocked — "
            f"no entry/grid fills in cycle {cycle_id}."
        )
        return False
    return True


def gate_heal_fill_qty(pair: str, proposed_qty: float, exchange=None) -> float:
    """Return qty allowed to credit (0 if pair ledger already at/above exchange)."""
    if proposed_qty <= 0:
        return 0.0
    if exchange is None:
        try:
            from engine.exchange_interface import ExchangeInterface
            exchange = ExchangeInterface(market_type='future')
        except Exception:
            return proposed_qty
    budget = pair_heal_budget(exchange, pair)
    if budget <= 0:
        logger.warning(
            f"[HEAL-BLOCKED] {pair}: no heal budget (ledger already >= exchange). "
            f"Skipping {proposed_qty:.6f} qty credit."
        )
        return 0.0
    allowed = min(proposed_qty, budget)
    if allowed < proposed_qty - 1e-12:
        logger.warning(
            f"[HEAL-CAP] {pair}: capping heal {proposed_qty:.6f} → {allowed:.6f} "
            f"to match exchange net."
        )
    return allowed


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
    """Block new entries when pair ledger != exchange."""
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


def gate_maintain_orders_allowed(
    bot_id: int,
    pair: str,
    exchange=None,
    total_invested: float = 0.0,
) -> Tuple[bool, str]:
    """
    TP/Grid maintenance for bots already in trade must continue even when pair
    parity is slightly off (ledger over-count). Blocking maintain leaves only TP
    on exchange and triggers false MISSING GRIDS alerts.
    New entries still use gate_trading_allowed (strict).
    """
    ok, virtual, physical, delta = pair_parity_ok(pair, exchange=exchange)
    if ok:
        return True, ''
    tol = qty_tolerance()
    invested = float(total_invested or 0)
    # Opposite-sign mismatch: never maintain (true orphan / wrong side)
    if (virtual > tol and physical < -tol) or (virtual < -tol and physical > tol):
        reason = (
            f"Pair parity (opposite signs): {pair} virtual={virtual:.6f} "
            f"exchange={physical:.6f}"
        )
        logger.error(f"🛑 [MAINTAIN-BLOCKED] Bot {bot_id}: {reason}")
        _set_bot_require_manual_proof(bot_id, reason)
        return False, reason
    # In-trade bot: allow TP/grid maintenance while startup repair deflates ledger
    if invested > 0.01:
        logger.warning(
            f"⚠️ [MAINTAIN-PARITY-WARN] Bot {bot_id} {pair}: parity off "
            f"(v={virtual:.6f} ex={physical:.6f} Δ={delta:.6f}) but in-trade — "
            f"allowing TP/grid maintenance."
        )
        return True, ''
    return gate_trading_allowed(bot_id, pair, exchange=exchange)


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


def _same_sign_qty(a: float, b: float, tol: float) -> bool:
    if abs(a) <= tol and abs(b) <= tol:
        return True
    return (a > tol and b > tol) or (a < -tol and b < -tol)


def deflate_pair_ledger_overcount(exchange, pair: str) -> Optional[str]:
    """
    Trim entry/grid filled_amount when pair virtual exceeds exchange (same sign).
    Repairs prior startup [HEALING] double-credits without market orders or wipes.
    """
    from engine.database import (
        get_connection,
        get_pair_virtual_net,
        sync_trades_from_orders,
    )
    from engine.ledger import seal_trade_state

    physical = get_exchange_signed_net(exchange, pair)
    virtual = get_pair_virtual_net(pair)
    tol = qty_tolerance()
    if physical is None or not _same_sign_qty(virtual, physical, tol):
        return None
    excess = round(abs(virtual) - abs(physical), 8)
    if excess <= tol:
        return None
    norm = normalize_symbol(pair).upper()
    conn = get_connection()
    target_bids = []
    for bot_id, raw_pair, bot_norm in conn.execute(
        "SELECT id, pair, normalized_pair FROM bots WHERE is_active=1"
    ).fetchall():
        if (bot_norm or normalize_symbol(raw_pair)).upper() == norm:
            target_bids.append(bot_id)
    if not target_bids:
        return None

    placeholders = ','.join('?' * len(target_bids))
    rows = conn.execute(
        f"""
        SELECT bo.id, bo.bot_id, bo.filled_amount
        FROM bot_orders bo
        WHERE bo.bot_id IN ({placeholders})
          AND bo.order_type IN ('entry','grid','adoption_add','adoption','carry')
          AND bo.status NOT IN ('reset_cleared','auto_closed')
          AND bo.filled_amount > 0
        ORDER BY bo.updated_at DESC, bo.id DESC
        """,
        target_bids,
    ).fetchall()

    trimmed = 0.0
    remaining = excess
    touched_bots = set()
    for db_id, bid, fill in rows:
        if remaining <= 1e-12:
            break
        fill_f = float(fill or 0)
        cut = min(fill_f, remaining)
        new_fill = round(fill_f - cut, 8)
        conn.execute(
            "UPDATE bot_orders SET filled_amount=?, updated_at=? WHERE id=?",
            (new_fill, int(time.time()), db_id),
        )
        remaining -= cut
        trimmed += cut
        touched_bots.add(bid)
    if trimmed <= 0:
        return None

    conn.commit()
    for bid in touched_bots:
        seal_trade_state(bid)
        sync_trades_from_orders(bid)

    logger.warning(
        f"🔧 [LEDGER-DEFLATE] {pair}: trimmed {trimmed:.6f} from entry/grid rows "
        f"(virtual {virtual:.6f} → target exchange {physical:.6f})."
    )
    return f"trimmed {trimmed:.6f}"


def _flatten_exchange_net_market(exchange, pair: str, net: float) -> Tuple[bool, str]:
    """reduceOnly market close for signed net qty. Returns (ok, message)."""
    tol = qty_tolerance()
    if abs(net) <= tol:
        return True, 'already flat'
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
        return False, 'close_qty rounded to zero'
    client_id = _repair_client_order_id('CQB_OR', pair)
    try:
        exchange.create_order(
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
    except Exception as e:
        return False, str(e)
    time.sleep(0.5)
    net_after = get_exchange_signed_net(exchange, pair)
    if net_after is not None and abs(net_after) <= tol:
        return True, f'flattened {close_qty:.6f}'
    return False, f'still open after close (net={net_after})'


def _try_adopt_orphan_exchange_to_ledger(
    exchange,
    pair: str,
    physical: float,
) -> Optional[str]:
    """
    Ledger is flat but exchange has size: credit all matching CQB closed-order proof
    until ledger net matches exchange within tolerance.
    """
    from engine.database import get_connection, get_pair_virtual_net
    from engine.ledger import credit_fill, seal_trade_state

    tol = qty_tolerance()
    norm = normalize_symbol(pair).upper()
    want_short = physical < -tol
    want_long = physical > tol
    if not want_short and not want_long:
        return None

    conn = get_connection()
    candidates = []
    for bot_id, raw_pair, direction, bot_norm in conn.execute(
        "SELECT id, pair, direction, normalized_pair FROM bots WHERE is_active=1"
    ).fetchall():
        if (bot_norm or normalize_symbol(raw_pair)).upper() != norm:
            continue
        d = (direction or 'LONG').upper()
        if want_short and d != 'SHORT':
            continue
        if want_long and d != 'LONG':
            continue
        candidates.append(bot_id)

    if len(candidates) != 1:
        logger.warning(
            f"[ORPHAN-ADOPT] {pair}: expected 1 direction-matched bot, found {len(candidates)}."
        )
        return None

    bot_id = candidates[0]
    try:
        closed = exchange.fetch_closed_orders(pair, limit=100) or []
    except Exception:
        return None

    credited_total = 0.0
    credited_cids = []
    for o in sorted(closed, key=lambda x: x.get('timestamp') or 0):
        cid = o.get('clientOrderId') or (o.get('info') or {}).get('clientOrderId') or ''
        if not cid.startswith(f'CQB_{bot_id}_'):
            continue
        filled = float(o.get('filled') or o.get('amount') or 0)
        if filled <= 0:
            continue
        avg = float(o.get('average') or o.get('price') or 0)
        oid = str(o.get('id') or cid)
        if credit_fill(
            bot_id=bot_id,
            order_id=oid,
            cumulative_qty=filled,
            avg_price=avg,
            order_type='adoption',
            is_cumulative=True,
        ):
            credited_total += filled
            credited_cids.append(cid)
        seal_trade_state(bot_id)

    if credited_cids:
        virtual_after = get_pair_virtual_net(pair)
        logger.warning(
            f"🔗 [ORPHAN-ADOPT] {pair}: credited {credited_total:.6f} to bot {bot_id} "
            f"({len(credited_cids)} CQB order(s)); virtual now {virtual_after:.6f}."
        )
        ok, _, physical_now, _ = pair_parity_ok(pair, exchange=exchange)
        if ok:
            return f'adopted {credited_total:.6f} ({len(credited_cids)} orders)'
    return None


def repair_exchange_orphan_when_ledger_flat(
    exchange,
    pair: str,
    virtual: float,
    physical: float,
) -> Optional[str]:
    """
    Ledger ≈ 0 but exchange holds size (wiped ledger / residual testnet position).
    Exchange is flattened to match the proof ledger (ledger is authoritative).
    Adoption into a wiped cycle is not used — it cannot affect get_pair_virtual_net.
    """
    if not getattr(config, 'AUTO_REPAIR_ORPHAN_EXCHANGE', False):
        logger.error(
            f"🚨 [ORPHAN-EXCHANGE] {pair}: ledger={virtual:.6f} exchange={physical:.6f}. "
            f"Enable AUTO_REPAIR_ORPHAN_EXCHANGE or flatten manually."
        )
        return None

    tol = qty_tolerance()
    if abs(virtual) > tol or abs(physical) <= tol:
        return None

    net = get_exchange_signed_net(exchange, pair)
    if net is None or abs(net) <= tol:
        return None

    logger.warning(
        f"🔧 [ORPHAN-EXCHANGE] {pair}: ledger≈0, exchange={net:.6f} — "
        f"flattening exchange to match proof ledger."
    )
    ok, msg = _flatten_exchange_net_market(exchange, pair, net)
    if ok:
        from engine.database import get_connection, reset_bot_after_tp

        norm = normalize_symbol(pair).upper()
        conn = get_connection()

        # ── STALE-SNAPSHOT FIX ────────────────────────────────────────────────────
        # active_positions is populated from the last WS/REST snapshot, which predates
        # the reduceOnly market close we just sent. If we call reset_bot_after_tp before
        # the WS fill arrives, _fetch_pos_wrapper still sees the old size and raises
        # WipeBlockedError. Clearing the row here tells the wipe-proof guard that the
        # position is gone — we already confirmed it via net_after check in
        # _flatten_exchange_net_market (which verified abs(net_after) <= tol).
        # ─────────────────────────────────────────────────────────────────────────────
        clean_pair_key = pair.split(':')[0].replace('/', '').upper()
        try:
            conn.execute(
                "DELETE FROM active_positions WHERE pair=?",
                (clean_pair_key,)
            )
            conn.commit()
            logger.info(f"[ORPHAN-EXCHANGE] Cleared stale active_positions for {clean_pair_key} after flatten.")
        except Exception as _ap_err:
            logger.warning(f"[ORPHAN-EXCHANGE] Could not clear active_positions for {clean_pair_key}: {_ap_err}")

        time.sleep(1.0)  # Extra wait for Binance to process the market close

        for bot_id, raw_pair, raw_dir in conn.execute(
            "SELECT id, pair, direction FROM bots WHERE is_active=1"
        ).fetchall():
            if normalize_symbol(raw_pair).upper() != norm:
                continue
            try:
                reset_bot_after_tp(
                    bot_id,
                    exit_price=0.0,
                    action_label='ORPHAN_EXCHANGE_REPAIR',
                    exchange=exchange,
                    human_approved=True,
                )
                logger.info(f"[ORPHAN-EXCHANGE] Bot {bot_id} ({raw_pair}) ledger cleared after orphan flatten.")
            except Exception as e:
                logger.warning(f"[ORPHAN-EXCHANGE] Bot {bot_id} reset after flatten: {e}")
        logger.warning(
            f"🔧 [ORPHAN-EXCHANGE] {pair}: ledger flat, exchange had {physical:.6f} — {msg}."
        )
        return msg
    logger.error(f"🚨 [ORPHAN-EXCHANGE] {pair}: flatten failed: {msg}")
    return None


def reconcile_pair_to_exchange(exchange, pair: str) -> Optional[str]:
    """
    Single pair repair: deflate over-count, flatten orphan exchange, or purge phantom ledger.
    Returns action message or None if already in parity.
    """
    from engine.database import get_pair_virtual_net

    tol = qty_tolerance()
    virtual = get_pair_virtual_net(pair)
    physical = get_exchange_signed_net(exchange, pair)
    if physical is None:
        return None
    if abs(virtual - physical) <= tol:
        return None

    if abs(physical) <= tol and abs(virtual) > tol:
        ok, msg = purge_phantom_ledger_when_exchange_flat(exchange, pair, virtual, physical)
        return msg if ok else None

    if abs(virtual) <= tol and abs(physical) > tol:
        return repair_exchange_orphan_when_ledger_flat(exchange, pair, virtual, physical)

    if _same_sign_qty(virtual, physical, tol) and abs(virtual) > abs(physical) + 1e-12:
        return deflate_pair_ledger_overcount(exchange, pair)

    return None


def startup_repair_mismatched_pairs(exchange) -> Dict[str, Any]:
    """
    Run after CQB history scan: purge phantom ledgers (exchange flat), re-audit.
    """
    from engine.database import audit_pair_ledger_vs_exchange, flag_pair_ledger_mismatch

    summary: Dict[str, Any] = {
        'purged': [], 'deflated': [], 'orphan_repaired': [], 'remaining': [],
    }
    if not exchange:
        return summary

    tol = qty_tolerance()
    mismatches = audit_pair_ledger_vs_exchange(exchange, qty_tolerance())
    for pair, virtual, physical, delta in mismatches:
        msg = reconcile_pair_to_exchange(exchange, pair)
        if not msg:
            continue
        if 'purged' in msg or 'wipe' in msg.lower():
            summary['purged'].append((pair, msg))
        elif 'deflat' in msg or 'trimmed' in msg:
            summary['deflated'].append((pair, msg))
        elif 'flatten' in msg or 'adopted' in msg:
            summary['orphan_repaired'].append((pair, msg))
        else:
            summary['orphan_repaired'].append((pair, msg))

    remaining = audit_pair_ledger_vs_exchange(exchange, qty_tolerance())
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
            client_id = _repair_client_order_id('CQB_FL', pair)
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
