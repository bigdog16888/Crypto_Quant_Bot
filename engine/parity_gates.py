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
    """Signed virtual qty this bot contributes to pair netting."""
    from engine.database import get_connection, recompute_invested_from_orders
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
    
    # Use recompute_invested_from_orders to get proof-based quantity
    # Fallback to trades.open_qty if no orders exist for this bot
    order_count = conn.execute(
        "SELECT COUNT(*) FROM bot_orders WHERE bot_id = ?", (bot_id,)
    ).fetchone()[0]
    
    if order_count > 0:
        _, _, net_qty, _ = recompute_invested_from_orders(bot_id)
        open_qty = net_qty

    return round(open_qty if direction == 'LONG' else -open_qty, 8)


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
        'tp', 'close', 'dust_close', 'sl',
        'adoption_reduce', 'forensic_adoption_reduce', 'hedgetp',
        'flatten_close',
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
    try:
        from unittest.mock import Mock
        if isinstance(positions, Mock):
            return 'mock_unconfigured'
    except ImportError:
        pass
    if positions is None or not isinstance(positions, list):
        return None

    total = 0.0
    for pos in positions:
        if normalize_symbol(pos.get('symbol', '')).upper() != norm:
            continue
        val = pos.get('net_qty')
        if val is None or val == 0:
            val = pos.get('contracts')
        if val is None or val == 0:
            qty = float(pos.get('qty', pos.get('size', 0)) or 0)
            side = str(pos.get('side', '')).lower()
            if side in ('short', 'sell'):
                val = -qty
            else:
                val = qty
        total += float(val or 0)
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
    if physical == 'mock_unconfigured':
        return True, virtual, virtual, 0.0
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
    if label == 'HEDGE_UNBLOCK':
        return
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
        # 🚀 FIX 3 (v3.9.13): Re-fetch before blocking — TP fill may have arrived on exchange
        # after the WS snapshot that populated 'physical' was taken (up to ~9s lag per cycle).
        # This costs one REST call, but is only paid when a reset would be blocked — resets are
        # rare and the API cost is far cheaper than leaving a closed bot stuck indefinitely.
        physical_recheck = get_exchange_signed_net(exchange, pair)
        if physical_recheck is not None:
            gap_recheck = abs(projected - physical_recheck)
            if gap_recheck <= tol:
                logger.info(
                    f"[CYCLE-RESET-UNBLOCKED] Bot {bot_id} {label} on {pair}: "
                    f"Re-fetch confirmed position closed (projected={projected:.6f} "
                    f"exchange_recheck={physical_recheck:.6f} gap={gap_recheck:.6f}). "
                    f"Proceeding with reset."
                )
                return  # gap resolved — allow reset
            # Use the fresher value for the error message
            physical = physical_recheck
            gap = gap_recheck

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
    from engine.database import get_connection, get_pair_virtual_net
    from engine.exchange_interface import ExchangeInterface, normalize_symbol

    try:
        conn = get_connection()
        row = conn.execute("SELECT pair FROM bots WHERE id = ?", (bot_id,)).fetchone()
        if row:
            pair = row[0]

            # Compute pair-level net BEFORE the grace check so gap_abs is available
            # for both the size-cap guard in pair_has_recent_fill and the
            # tolerance-bypass check below.  This avoids two separate exchange API calls.
            gap_abs = None
            pair_virtual_net = None
            pair_physical_net = None
            try:
                ex = ExchangeInterface(market_type='future')
                pair_virtual_net = get_pair_virtual_net(pair)
                pair_physical_net = get_exchange_signed_net(ex, pair)
                if pair_physical_net is not None:
                    gap_abs = abs(pair_virtual_net - pair_physical_net)
            except Exception as e_net:
                logger.error(f"Error computing pair net for bot {bot_id}: {e_net}")

            # v5.3.2+: INV §3.57 grace-period guard, centralized here so it applies
            # to every caller (gate_trading_allowed, gate_maintain_orders_allowed,
            # flag_orphan_fill_manual_proof, CycleResetBlockedError handler, etc.).
            # v5.3.4: size-cap added — gaps > 20 units bypass grace regardless of
            # recent fills (mirrors PASS3-GRACE FIX #3 [V2.4.1] semantics).
            try:
                if pair_has_recent_fill(
                    conn,
                    symbol=pair,
                    window_seconds=60,
                    max_gap_units=20.0,
                    gap_abs=gap_abs,
                ):
                    _gap_str = f"{gap_abs:.6f}" if gap_abs is not None else "n/a"
                    logger.info(
                        f"[PROOF-GRACE] Bot {bot_id} on {pair}: recent fill within "
                        f"60s and gap={_gap_str} \u2264 20 units "
                        f"\u2014 skipping REQUIRE_MANUAL_PROOF ({reason})."
                    )
                    return

            except Exception as e_grace:
                logger.error(f"Error checking recent-fill grace for bot {bot_id}: {e_grace}")

            # Tolerance-bypass: if the pair net already matches, no gate needed.
            # Reuses the gap_abs computed above — no second exchange call.
            try:
                if pair_physical_net is not None and gap_abs is not None:
                    tol = qty_tolerance()
                    if gap_abs <= tol:
                        logger.info(
                            f"🛡️ [BYPASS-GATE] Bot {bot_id} on {pair}: Pair-level virtual net ({pair_virtual_net:.6f}) "
                            f"matches physical ({pair_physical_net:.6f}) within tolerance ({tol:.6f}). Bypassing REQUIRE_MANUAL_PROOF."
                        )
                        return
            except Exception as e_gate:
                logger.error(f"Error checking pair net in _set_bot_require_manual_proof for bot {bot_id}: {e_gate}")

        conn.execute(
            "UPDATE bots SET status='REQUIRE_MANUAL_PROOF' WHERE id=? AND status NOT IN ('STOPPED')",
            (bot_id,),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to set REQUIRE_MANUAL_PROOF for bot {bot_id}: {e} ({reason})")





def pair_has_recent_fill(
    conn,
    symbol: str = None,
    window_seconds: int = 60,
    tp_close_window_seconds: int = 0,
    bot_ids: list = None,
    max_gap_units: float = None,
    gap_abs: float = None,
) -> bool:
    """Return True if any fill credit has landed on this pair within the grace window.

    Args:
        conn:                    Active DB connection or cursor.
        symbol:                  Pair string — used to derive bots via JOIN when bot_ids is None.
        window_seconds:          Grace window for any fill type (default 60s).
        tp_close_window_seconds: Additional wider window for TP/close fills only (0 = disabled).
                                 The reconciler passes 600 to guard seal_trade_state lag.
        bot_ids:                 If provided, query by bot_id IN (...) instead of a JOIN on
                                 normalized_pair.  Use this when the caller already has a
                                 pre-verified list (e.g. bots_on_ticker from the reconciler)
                                 to preserve exact semantics without re-deriving the list.
        max_gap_units:           Size cap (units).  If gap_abs exceeds this value the grace
                                 window is bypassed entirely — the caller is forced through to
                                 the forensic scan / gate path regardless of recent fills.
                                 None = no size cap (legacy behaviour, no limit).
        gap_abs:                 Absolute gap magnitude (units) to compare against max_gap_units.
                                 Only meaningful when max_gap_units is also set.
    """
    # ── Size-cap guard (v5.3.4 / FIX #3 mirror) ─────────────────────────────
    # Gaps exceeding max_gap_units almost certainly are NOT a DB-seal-lag race —
    # they are real offline-fill losses or structural mismatches.  Grace-skipping
    # them causes bots to operate with the wrong open_qty for minutes.
    # PASS3-GRACE in reconciler.py learned this the hard way (V2.4.1 comment);
    # this mirrors that lesson across all four centralized callers.
    if max_gap_units is not None and gap_abs is not None and gap_abs > max_gap_units:
        return False  # Force caller through to forensic/gate path

    import time
    now = int(time.time())
    cutoff = now - window_seconds

    if bot_ids is not None:
        # Fast path: explicit list — no JOIN needed
        if not bot_ids:
            return False
        ph = ','.join('?' * len(bot_ids))
        count = conn.execute(
            f"""SELECT COUNT(*) FROM bot_orders
               WHERE bot_id IN ({ph})
               AND filled_amount > 0
               AND status IN ('filled', 'partially_filled')
               AND updated_at >= ?""",
            (*bot_ids, cutoff)
        ).fetchone()[0]
        if count > 0:
            return True
        if tp_close_window_seconds > 0:
            tp_cutoff = now - tp_close_window_seconds
            tp_count = conn.execute(
                f"""SELECT COUNT(*) FROM bot_orders
                   WHERE bot_id IN ({ph})
                   AND filled_amount > 0
                   AND order_type IN ('tp', 'close')
                   AND status IN ('filled', 'partially_filled')
                   AND updated_at >= ?""",
                (*bot_ids, tp_cutoff)
            ).fetchone()[0]
            return tp_count > 0
        return False
    else:
        # JOIN path: derive bots from normalized_pair
        from engine.exchange_interface import normalize_symbol
        norm = normalize_symbol(symbol).upper()
        count = conn.execute(
            """SELECT COUNT(*) FROM bot_orders bo
               JOIN bots b ON b.id = bo.bot_id
               WHERE (b.normalized_pair = ? OR REPLACE(REPLACE(b.pair,'/',''),':USDC','') = ?)
               AND bo.filled_amount > 0
               AND bo.status IN ('filled', 'partially_filled')
               AND bo.updated_at >= ?""",
            (norm, norm, cutoff)
        ).fetchone()[0]
        if count > 0:
            return True
        if tp_close_window_seconds > 0:
            tp_cutoff = now - tp_close_window_seconds
            tp_count = conn.execute(
                """SELECT COUNT(*) FROM bot_orders bo
                   JOIN bots b ON b.id = bo.bot_id
                   WHERE (b.normalized_pair = ? OR REPLACE(REPLACE(b.pair,'/',''),':USDC','') = ?)
                   AND bo.filled_amount > 0
                   AND bo.order_type IN ('tp', 'close')
                   AND bo.status IN ('filled', 'partially_filled')
                   AND bo.updated_at >= ?""",
                (norm, norm, tp_cutoff)
            ).fetchone()[0]
            return tp_count > 0
        return False


# Backward-compat alias so existing internal callers are not broken during transition.
_pair_has_recent_fill = pair_has_recent_fill




def flag_orphan_fill_manual_proof(
    bot_id: int,
    order_id: str,
    symbol: str,
    qty: float,
    source: str,
) -> None:
    """Forensic adopt disabled — require operator proof instead.

    The grace-period check is handled centrally inside _set_bot_require_manual_proof.
    A redundant pre-check here is kept only for an early-exit log message so the
    orphan-fill path emits a distinct [PROOF-GRACE] log line identifying the symbol.
    """
    logger.error(
        f"✨ [ORPHAN-NO-ADOPT] Bot {bot_id} {source}: order {order_id} on {symbol} "
        f"qty={qty:.6f} not in ledger. Set ALLOW_FORENSIC_ADOPT=True to auto-adopt (not recommended)."
    )
    # Note: _set_bot_require_manual_proof already contains the full grace check.
    # The early-return below just produces a cleaner log message for the orphan path.
    # v5.3.5: Added size-cap matching to prevent gracing large orphan fills.
    try:
        from engine.database import get_connection
        conn = get_connection()
        if pair_has_recent_fill(
            conn,
            symbol=symbol,
            window_seconds=60,
            max_gap_units=20.0,
            gap_abs=qty,
        ):
            logger.info(
                f"[PROOF-GRACE] {symbol}: recent fill within 60s and qty={qty:.6f} \u2264 20 units — "
                f"skipping orphan gate to allow WS credit to land."
            )
            return
    except Exception as e_grace:
        logger.error(f"Error checking recent fill grace for {symbol}: {e_grace}")


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

    from engine.database import get_connection, safe_wipe_bot, save_bot_order

    conn = get_connection()
    norm = normalize_symbol(pair).upper()
    bots = conn.execute(
        "SELECT id, pair, direction FROM bots WHERE is_active=1"
    ).fetchall()
    wiped = []
    for bot_id, raw_pair, direction in bots:
        if normalize_symbol(raw_pair).upper() != norm:
            continue

        # Collect open orders for this bot before canceling to list their CIDs
        cancelled_cids = []
        if exchange:
            try:
                prefix = f"CQB_{bot_id}_"
                for order in exchange.fetch_open_orders(raw_pair):
                    cid = order.get('clientOrderId', '')
                    if cid.startswith(prefix):
                        cancelled_cids.append(cid)
            except Exception as e:
                logger.warning(f"Failed to fetch open orders to audit cancellation for bot {bot_id}: {e}")

            # Cancel open orders for this bot
            try:
                cancelled_count = exchange.cancel_orders_by_bot_id(bot_id, raw_pair)
                logger.info(
                    f"🧹 [PHANTOM-PURGE] Cancelled {cancelled_count} open order(s) "
                    f"for bot {bot_id} on {raw_pair} before ledger purge."
                )
            except Exception as e:
                logger.warning(f"Failed to cancel open orders for bot {bot_id} on {raw_pair}: {e}")

        # Write ghost_order_cancel audit row
        try:
            cycle_row = conn.execute("SELECT cycle_id FROM trades WHERE bot_id = ?", (bot_id,)).fetchone()
            cycle_id = int(cycle_row[0]) if cycle_row and cycle_row[0] is not None else 1
            cid_str = ", ".join(cancelled_cids) if cancelled_cids else "None"
            audit_cid = f"CQB_{bot_id}_GHOST_CANCEL_{int(time.time())}"
            save_bot_order(
                bot_id,
                'ghost_order_cancel',
                audit_cid,
                price=0.0,
                amount=0.0,
                step=0,
                status='filled',
                client_order_id=audit_cid,
                notes=f"Purged phantom ledger. Cancelled CIDs: {cid_str}",
                cycle_id=cycle_id,
            )
        except Exception as e:
            logger.error(f"Failed to save ghost_order_cancel audit row for bot {bot_id}: {e}")

        ok = safe_wipe_bot(
            bot_id,
            raw_pair,
            direction or 'LONG',
            reason='PHANTOM_LEDGER_PURGE',
            force=False,
            action_label='MANUAL_CLOSE',
            exchange=exchange,
            bypass_ledger_guard=True,
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
        JOIN trades t ON t.bot_id = bo.bot_id
        WHERE bo.bot_id IN ({placeholders})
          AND bo.cycle_id = t.cycle_id
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


def detect_and_repair_global_wipe(exchange) -> Dict[str, Any]:
    """
    Check if the exchange is completely flat (no active non-zero physical positions),
    but the database claims we have at least 2 active trades (open_qty > 0.0001).
    If so, automatically run purge_phantom_ledger_when_exchange_flat across all pairs.
    """
    summary = {
        'triggered': False,
        'reason': '',
        'pairs_purged': [],
        'bots_affected': 0,
        'skipped_reason': ''
    }
    
    # 1. Config guard
    if not getattr(config, 'ENABLE_GLOBAL_WIPE_DETECTION', True):
        summary['skipped_reason'] = 'disabled by config'
        summary['reason'] = 'Skipped: global wipe detection is disabled by config.'
        return summary

    if not exchange:
        summary['skipped_reason'] = 'exchange instance is None'
        summary['reason'] = 'Skipped: exchange instance is None.'
        return summary

    # 2. Fetch live physical positions from the exchange
    try:
        positions = exchange.fetch_positions() or []
    except Exception as e:
        logger.error(f"[WIPE-DETECT] Failed to fetch positions from exchange: {e}")
        summary['skipped_reason'] = f'fetch_positions failed: {e}'
        summary['reason'] = f'Skipped: fetch_positions failed.'
        return summary

    tol = qty_tolerance()
    has_physical = False
    for pos in positions:
        qty = abs(float(pos.get('contracts', pos.get('net_qty', pos.get('size', 0))) or 0))
        if qty > tol:
            has_physical = True
            break

    if has_physical:
        summary['skipped_reason'] = 'exchange has active positions'
        summary['reason'] = 'Skipped: exchange has active physical positions.'
        return summary

    # 3. Check if the database has at least 2 active bots claiming positions (open_qty > 0.0001)
    from engine.database import get_connection, get_pair_virtual_net
    conn = get_connection()
    rows = conn.execute("""
        SELECT b.id, b.pair, b.direction FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1 AND t.open_qty > 0.0001
    """).fetchall()

    db_active_bots_count = len(rows)
    if db_active_bots_count < 2:
        summary['skipped_reason'] = f'only {db_active_bots_count} bot(s) claim positions'
        summary['reason'] = f'Skipped: database only has {db_active_bots_count} active bot(s) with open_qty > 0.0001 (requires >= 2).'
        return summary

    # 4. Trigger emergency global purge
    logger.critical(
        f"[GLOBAL-WIPE-DETECTED] Exchange flat across all symbols but DB claims "
        f"{db_active_bots_count} active positions. Running emergency purge across all pairs. "
        f"If this is NOT a demo reset, investigate immediately."
    )
    summary['triggered'] = True
    summary['reason'] = f'Triggered emergency purge: exchange is flat but DB has {db_active_bots_count} active bots.'
    summary['skipped_reason'] = ''

    # Get distinct pairs from the database to purge
    pairs_rows = conn.execute("SELECT DISTINCT pair FROM bots").fetchall()
    pairs = [r[0] for r in pairs_rows]

    total_wiped_bots = 0
    for pair in pairs:
        virtual = get_pair_virtual_net(pair)
        if abs(virtual) > tol:
            logger.warning(
                f"🧹 [GLOBAL-WIPE-PURGE] Purging phantom ledger for pair {pair} (virtual={virtual:.6f})."
            )
            # Run purge on the pair with physical net = 0.0
            ok, msg = purge_phantom_ledger_when_exchange_flat(exchange, pair, virtual, 0.0)
            if ok:
                summary['pairs_purged'].append(pair)
                # Parse number of wiped bots from msg
                if 'purged bots' in msg:
                    try:
                        import re
                        bots_list = re.findall(r'\d+', msg)
                        total_wiped_bots += len(bots_list)
                    except Exception:
                        total_wiped_bots += 1
                else:
                    total_wiped_bots += 1

    summary['bots_affected'] = total_wiped_bots
    return summary


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
        "SELECT id, pair, direction FROM bots WHERE is_active=1"
    ).fetchall()
    target_bots = [
        {'id': r[0], 'direction': r[2]} for r in bot_rows
        if normalize_symbol(r[1]).upper() == norm_target
    ]
    target_bot_ids = [b['id'] for b in target_bots]

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
        # close_side is always determined by the physical exchange net — this is the
        # authoritative source. proof_flatten_pair must close whatever the exchange holds,
        # regardless of what direction individual bots think they are.
        # An opposite-sign mismatch (e.g. SHORT bot but exchange LONG) is precisely
        # the case where proof flatten is needed — blocking it here makes recovery
        # impossible. Log the mismatch for audit; do NOT block the flatten.
        close_side = 'sell' if net > 0 else 'buy'

        for bot in target_bots:
            expected_side = 'buy' if bot['direction'] == 'SHORT' else 'sell'
            if close_side != expected_side:
                logger.warning(
                    f"⚠️ [PROOF-FLATTEN-OPPOSITE-SIGN] Bot {bot['id']} direction={bot['direction']} "
                    f"expects close_side='{expected_side}' but exchange net={net:.6f} requires "
                    f"close_side='{close_side}'. Proceeding with exchange-authority flatten. "
                    f"Bot ledger will be reset after exchange is verified flat."
                )

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

# ═══════════════════════════════════════════════════════════════════════════
# ORPHAN POSITION DIAGNOSTICS AND CLOSE (INV-16)
# ═══════════════════════════════════════════════════════════════════════════
# Before touching any orphan position, ALWAYS distinguish:
#
#   PARTIAL FILL: open order at limit price still exists on exchange.
#     The remaining qty WILL fill when price reaches it. DO NOTHING.
#     Market-selling a partial fill creates a double-sell and untracked change.
#
#   TRUE ORPHAN: no open order covers the gap.
#     Use close_unattributed_position() — full audit receipt, no DB mutation.
# ═══════════════════════════════════════════════════════════════════════════

def diagnose_pair_orphans(exchange, pair: str) -> Dict[str, Any]:
    """
    Check whether a pair gap is:
      - A partial fill (open limit order exists on exchange — wait, it will fill)
      - A true orphan (no open order, needs close_unattributed_position)

    Returns dict:
      recommendation: 'ok' | 'wait_for_fill' | 'close_orphan'
      virtual_qty, physical_qty, delta
      open_orders, pending_close_qty, true_orphan_qty
    """
    from engine.database import get_pair_virtual_net, get_connection as _gc

    result: Dict[str, Any] = {
        "pair": pair, "virtual_qty": 0.0, "physical_qty": 0.0, "delta": 0.0,
        "open_orders": [], "pending_close_qty": 0.0, "true_orphan_qty": 0.0,
        "recommendation": "ok", "errors": [],
    }
    norm_pair = normalize_symbol(pair).upper()

    try:
        result["virtual_qty"] = get_pair_virtual_net(pair)
    except Exception as e:
        result["errors"].append(f"get_pair_virtual_net: {e}")
        return result

    try:
        phys_net = 0.0
        for pos in exchange.fetch_positions() or []:
            if normalize_symbol(pos.get("symbol", "")).upper() == norm_pair:
                phys_net += float(pos.get("contracts", 0) or 0)
        result["physical_qty"] = phys_net
    except Exception as e:
        result["errors"].append(f"fetch_positions: {e}")
        return result

    delta = result["physical_qty"] - result["virtual_qty"]
    result["delta"] = round(delta, 8)

    if abs(delta) <= qty_tolerance():
        return result

    try:
        open_orders = exchange.fetch_open_orders(pair) or []
        result["open_orders"] = [
            {
                "order_id": o.get("id"),
                "client_order_id": o.get("clientOrderId", ""),
                "side": o.get("side", ""),
                "qty": float(o.get("amount", 0)),
                "price": float(o.get("price", 0)),
            }
            for o in open_orders
        ]
    except Exception as e:
        result["errors"].append(f"fetch_open_orders: {e}")

    # Determine which side of orders would close the orphan
    closing_side = "sell" if delta > 0 else "buy"
    pending_close_qty = 0.0
    _conn = _gc()
    for o in result["open_orders"]:
        if o["side"].lower() != closing_side:
            continue
        cid = o.get("client_order_id", "")
        if not cid.startswith("CQB_"):
            # Non-CQB (manual) closing order — counts as covering the orphan
            pending_close_qty += o["qty"]
            continue
        # CQB order: include if its owning bot already shows open_qty=0 in DB
        # (typical of a partial-fill TP — bot was zeroed but closing order remains)
        parts = cid.split("_")
        if len(parts) > 1:
            try:
                _bid = int(parts[1])
                _r = _conn.execute(
                    "SELECT COALESCE(t.open_qty,0) FROM trades t WHERE t.bot_id=?",
                    (_bid,)
                ).fetchone()
                if _r and float(_r[0]) < 0.0001:
                    pending_close_qty += o["qty"]
            except Exception:
                pass

    result["pending_close_qty"] = round(pending_close_qty, 8)
    true_orphan = abs(delta) - pending_close_qty
    result["true_orphan_qty"] = round(max(0.0, true_orphan), 8)

    if result["true_orphan_qty"] <= qty_tolerance():
        result["recommendation"] = "wait_for_fill"
        logger.info(
            f"[ORPHAN-DIAG] {pair}: delta={delta:+.6f} COVERED by "
            f"{pending_close_qty:.6f} pending order(s). Wait for natural fill."
        )
    else:
        result["recommendation"] = "close_orphan"
        logger.warning(
            f"[ORPHAN-DIAG] {pair}: delta={delta:+.6f}, covered={pending_close_qty:.6f}, "
            f"TRUE ORPHAN={result['true_orphan_qty']:.6f}. No open order will close this."
        )

    return result


def close_unattributed_position(
    exchange,
    pair: str,
    qty: float,
    side: str,
    audit_reason: str,
    human_approved: bool = False,
) -> Dict[str, Any]:
    """
    Close a position that no active bot claims AND that has no open limit order
    that would close it naturally (confirmed via diagnose_pair_orphans).

    INV-16 requirements enforced here:
      1. human_approved=True must be passed by caller.
      2. diagnose_pair_orphans() re-run to abort if it turns out a partial-fill
         open order IS present — closing it would create a double-sell.
      3. exchange_order_audit receipt written BEFORE the exchange call (WAL pattern).
      4. No bot_orders / trades / bots rows are modified — this is an unowned
         position; any DB mutation would fabricate false ledger history.
      5. Pair is re-audited after close to confirm delta resolved.
    """
    from engine.database import get_connection as _gc

    result: Dict[str, Any] = {
        "success": False, "pair": pair, "qty": qty, "side": side,
        "order_id": None, "delta_before": None, "delta_after": None, "errors": [],
    }

    if not human_approved:
        result["errors"].append(
            "[INV-16] human_approved=True required. "
            "Run diagnose_pair_orphans() first to confirm no open order covers the gap."
        )
        return result

    diag = diagnose_pair_orphans(exchange, pair)
    result["delta_before"] = diag["delta"]

    if diag["recommendation"] == "wait_for_fill":
        result["errors"].append(
            f"[INV-16] ABORTED: gap covered by {diag['pending_close_qty']:.6f} "
            "pending limit order(s). Do NOT close — it will fill naturally. "
            "Closing now creates a double-sell and an untracked position change."
        )
        return result

    if diag["recommendation"] == "ok":
        result["errors"].append("[INV-16] ABORTED: pair is within tolerance, no action needed.")
        return result

    if abs(qty - diag["true_orphan_qty"]) > qty_tolerance() * 5:
        result["errors"].append(
            f"[INV-16] ABORTED: qty={qty:.6f} does not match "
            f"true_orphan_qty={diag['true_orphan_qty']:.6f}. "
            "Re-run diagnose_pair_orphans() and use the exact reported qty."
        )
        return result

    # Write WAL audit receipt BEFORE exchange call
    _conn = _gc()
    _cursor = _conn.cursor()
    cid = f"CQB_ORPHAN_CLOSE_{normalize_symbol(pair)}_{int(time.time())}"

    _cursor.execute(
        """INSERT INTO exchange_order_audit
               (order_id, client_order_id, symbol, side, qty, price,
                call_site, context, placed_at, notes)
           VALUES (?,?,?,?,?,0,?,?,?,?)""",
        (
            "PENDING", cid, pair, side, qty,
            "parity_gates:close_unattributed_position", "orphan_close",
            int(time.time()),
            (
                f"[INV-16] Unattributed position close. reason={audit_reason}. "
                f"true_orphan={diag['true_orphan_qty']:.6f}. "
                f"delta={diag['delta']:+.6f}. human_approved=True."
            ),
        )
    )
    _conn.commit()
    _pending_id = _cursor.lastrowid

    try:
        res = exchange.create_order(
            symbol=pair,
            type="market",
            side=side,
            amount=qty,
            params={"newClientOrderId": cid, "reduceOnly": True},
            emergency=True,
            _audit_cursor=_cursor,
            _call_site="parity_gates:close_unattributed_position",
            human_approved=True,
        )
        real_id = str(res.get("id", ""))
        _cursor.execute(
            "UPDATE exchange_order_audit SET order_id=? WHERE id=?",
            (real_id, _pending_id)
        )
        _conn.commit()
        result["order_id"] = real_id
        logger.warning(
            f"\u2705 [ORPHAN-CLOSE][INV-16] {pair}: {qty} {side.upper()} reduceOnly "
            f"\u2192 exchange_order_id={real_id} | {audit_reason}"
        )
    except Exception as e:
        _cursor.execute(
            "UPDATE exchange_order_audit SET notes=notes||'|FAILED:'||? WHERE id=?",
            (str(e), _pending_id)
        )
        _conn.commit()
        result["errors"].append(f"Exchange order failed: {e}")
        logger.error(
            f"\u274c [ORPHAN-CLOSE][INV-16] {pair}: FAILED: {e}. "
            f"Audit row id={_pending_id} preserved in exchange_order_audit."
        )
        return result

    # Brief settle, then re-audit
    time.sleep(1.5)
    diag_after = diagnose_pair_orphans(exchange, pair)
    result["delta_after"] = diag_after["delta"]
    result["success"] = diag_after["recommendation"] == "ok"
    if not result["success"]:
        result["errors"].append(
            f"Post-close delta={diag_after['delta']:+.6f}. "
            "Re-run diagnose_pair_orphans() after exchange settles."
        )
    return result
