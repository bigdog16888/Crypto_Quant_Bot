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

    norm = _pair_norm(pair)
    my_dir = str(direction).upper()
    opp_dir = 'SHORT' if my_dir == 'LONG' else 'LONG'
    tol = _qty_tol()

    conn = get_connection()
    opp_open = 0.0
    for _bid, bdir, raw_pair, bot_norm, oq in conn.execute(
        """
        SELECT b.id, b.direction, b.pair, b.normalized_pair, COALESCE(t.open_qty, 0)
        FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1 AND b.id != ?
        """,
        (bot_id,),
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
    for bid, bdir, raw_pair, bot_norm, oq in conn.execute(
        """
        SELECT b.id, b.direction, b.pair, b.normalized_pair, COALESCE(t.open_qty, 0)
        FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1 AND b.id != ?
        """,
        (filling_bot_id,),
    ).fetchall():
        if (bot_norm or normalize_symbol(raw_pair)).upper() != norm:
            continue
        if str(bdir).upper() != target_dir:
            continue
        oqf = float(oq or 0)
        if oqf > 1e-12:
            neighbors.append((bid, oqf))

    if not neighbors:
        return 0.0

    remaining = delta
    total_cut = 0.0
    ts = int(time.time())
    for nb_id, oq in sorted(neighbors, key=lambda x: -x[1]):
        if remaining <= 1e-12:
            break
        cut = round(min(oq, remaining), 8)
        if cut <= 0:
            continue
        conn.execute(
            "UPDATE trades SET open_qty = MAX(0, ROUND(COALESCE(open_qty, 0) - ?, 8)) "
            "WHERE bot_id = ?",
            (cut, nb_id),
        )
        cycle_row = conn.execute(
            "SELECT cycle_id FROM trades WHERE bot_id = ?", (nb_id,)
        ).fetchone()
        cycle_id = int(cycle_row[0] or 1) if cycle_row else 1
        audit_cid = f"CQB_{nb_id}_OWAY_{filling_bot_id}_{ts}"
        save_bot_order(
            nb_id,
            'virtual_netting',
            source_order_id or audit_cid,
            avg_price or 0.0,
            cut,
            step=0,
            status='filled',
            client_order_id=audit_cid,
            notes=(
                f"ONEWAY_CROSS: bot {filling_bot_id} {filler_dir} entry -{cut:.6f} "
                f"from shared position (src {source_order_id})"
            ),
            cycle_id=cycle_id,
        )
        conn.execute(
            "UPDATE bot_orders SET filled_amount = ? WHERE client_order_id = ? AND bot_id = ?",
            (cut, audit_cid, nb_id),
        )
        remaining -= cut
        total_cut += cut
        logger.warning(
            f"⚖️ [ONEWAY-CROSS] Pair {norm}: bot {filling_bot_id} {filler_dir} entry "
            f"−{cut:.6f} from bot {nb_id} ({target_dir}) open_qty "
            f"(source order {source_order_id})."
        )

    if total_cut > 0:
        conn.commit()

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
        for target_dir in ('LONG', 'SHORT'):
            bots: List[Tuple[int, float]] = []
            for bid, bdir, raw_pair, bot_norm, oq in conn.execute(
                """
                SELECT b.id, b.direction, b.pair, b.normalized_pair, COALESCE(t.open_qty, 0)
                FROM bots b JOIN trades t ON t.bot_id = b.id WHERE b.is_active = 1
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
                conn.execute(
                    "UPDATE trades SET open_qty = MAX(0, ROUND(open_qty - ?, 8)) WHERE bot_id=?",
                    (cut, bid),
                )
                cycle_row = conn.execute(
                    "SELECT cycle_id FROM trades WHERE bot_id = ?", (bid,)
                ).fetchone()
                cycle_id = int(cycle_row[0] or 1) if cycle_row else 1
                audit_cid = f"CQB_{bid}_OWAY_REPAIR_{int(time.time())}"
                save_bot_order(
                    bid,
                    'virtual_netting',
                    audit_cid,
                    0.0,
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
                logger.warning(
                    f"🔧 [ONEWAY-REPAIR] {norm}: trimmed bot {bid} open_qty −{cut:.6f} "
                    f"(virtual {virtual:.6f} → exchange {physical:.6f})"
                )
        conn.commit()
        return f"trimmed virtual excess {diff:.6f} on {norm}"

    return None
