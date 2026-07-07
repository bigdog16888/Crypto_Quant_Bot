"""
engine/recovery.py — Universal resolution for gated bots.

Replaces per-incident resolve_*.py scripts. Any REQUIRE_MANUAL_PROOF or
STUCK_DUST_NO_EXIT case should call resolve_gated_bot(bot_id, exchange, ...).

Invariants preserved:
- Exchange close always precedes DB wipe (INV-15).
- safe_wipe_bot Guard 2.0 live-verifies exchange flat for direction before clearing DB.
- Parity gates remain consistent at every commit boundary.
"""
from __future__ import annotations

import logging
import time as _time
from typing import Optional

logger = logging.getLogger(__name__)

RESOLVABLE_STATUSES = frozenset({
    'REQUIRE_MANUAL_PROOF',
    'STUCK_DUST_NO_EXIT',
    'pending_flatten',
})


def compute_closeable_qty(
    direction: str,
    virtual_qty: float,
    live_signed_net: float,
) -> float:
    """
    Compute how much of virtual_qty is physically backed by exchange net exposure
    in the correct close direction.

    A LONG bot closes via SELL (reduceOnly). A SELL reduceOnly can only reduce a
    net LONG exchange position. If the net is SHORT, closeable_qty is 0.

    A SHORT bot closes via BUY (reduceOnly). A BUY reduceOnly can only reduce a
    net SHORT exchange position. If the net is LONG, closeable_qty is 0.

    Returns value in [0, virtual_qty].
    """
    if direction.upper() == 'LONG':
        # SELL reduceOnly: can reduce up to max(0, live_signed_net)
        backed = max(0.0, live_signed_net)
    else:
        # BUY reduceOnly: can reduce up to max(0, -live_signed_net)
        backed = max(0.0, -live_signed_net)
    return round(min(float(virtual_qty), backed), 8)


def resolve_gated_bot(
    bot_id: int,
    exchange,
    action_label: str = 'AUTO_NET_CLOSE',
    reason: str = '',
    human_approved: bool = True,
    cursor=None,
) -> dict:
    """
    Universal resolution function for REQUIRE_MANUAL_PROOF / STUCK_DUST_NO_EXIT bots.

    Algorithm:
    1. Look up bot context from DB (pair, direction, open_qty, status).
    2. Validate status is resolvable.
    3. Fetch live signed net position for the pair.
    4. Compute closeable_qty = min(virtual_qty, physically-backed-qty in close direction).
    5. If closeable_qty > 0: place reduceOnly market close for closeable_qty first.
       DB is NOT touched until exchange confirms.
    6. Call safe_wipe_bot to clear the full virtual position from DB.
       safe_wipe_bot Guard 2.0 re-verifies exchange flat for the direction after the close.
    7. Return result dict.

    Returns dict: {status, bot_id, pair, direction, virtual_qty, live_net,
                   closeable_qty, unphysical_remainder, wiped}
    Raises ValueError on invalid state.
    Raises RuntimeError on exchange failure (bot remains gated, DB untouched).
    """
    from engine.database import get_connection, safe_wipe_bot
    from engine.parity_gates import get_exchange_signed_net

    conn = get_connection()
    row = conn.execute(
        """
        SELECT b.pair, b.direction, b.status, t.open_qty, t.cycle_id
        FROM bots b
        LEFT JOIN trades t ON t.bot_id = b.id
        WHERE b.id = ?
        """,
        (bot_id,),
    ).fetchone()

    if not row:
        raise ValueError(f"Bot {bot_id} not found.")

    pair, direction, current_status, open_qty, cycle_id = row
    open_qty = float(open_qty or 0.0)

    if current_status not in RESOLVABLE_STATUSES:
        raise ValueError(
            f"Bot {bot_id} has status '{current_status}' which is not in "
            f"resolvable set {RESOLVABLE_STATUSES}. Will not resolve."
        )

    if not exchange:
        raise ValueError(
            f"Bot {bot_id}: exchange object required for live position check."
        )

    logger.warning(
        f"[RECOVERY] resolve_gated_bot: bot={bot_id} pair={pair} direction={direction} "
        f"status={current_status} open_qty={open_qty:.6f} action={action_label}"
    )

    # Step 3: Fetch live signed net for this pair
    live_net = get_exchange_signed_net(exchange, pair)
    if live_net is None:
        raise RuntimeError(
            f"Bot {bot_id}: fetch_positions failed for {pair}. Cannot resolve without "
            f"live exchange data — aborting to prevent silent ledger corruption."
        )

    # Step 4: Compute closeable quantity
    closeable_qty = compute_closeable_qty(direction, open_qty, live_net)
    unphysical_remainder = round(open_qty - closeable_qty, 8)

    logger.warning(
        f"[RECOVERY] Bot {bot_id}: live_net={live_net:.6f} virtual={open_qty:.6f} "
        f"closeable={closeable_qty:.6f} unphysical_remainder={unphysical_remainder:.6f}"
    )

    # Step 5: Place exchange order for the physical portion (INV-15: exchange first)
    if closeable_qty > 1e-8:
        close_side = 'sell' if direction.upper() == 'LONG' else 'buy'
        cid = f"CQB_{bot_id}_RECOVERY_{int(_time.time())}"
        try:
            # Write WAL receipt before exchange call
            conn.execute(
                """
                INSERT INTO bot_orders
                (bot_id, order_type, status, amount, filled_amount, price,
                 client_order_id, cycle_id, created_at, updated_at)
                VALUES (?, 'flatten_close', 'placing', ?, 0, 0, ?, ?, ?, ?)
                """,
                (bot_id, closeable_qty, cid, cycle_id or 1,
                 int(_time.time()), int(_time.time())),
            )
            conn.commit()

            order = exchange.create_order(
                pair, 'market', close_side, closeable_qty,
                params={
                    'reduceOnly': True,
                    'newClientOrderId': cid,
                    'human_approved': True,
                },
            )
            filled = float(order.get('filled') or closeable_qty)
            fill_price = float(order.get('average') or order.get('price') or 0)

            conn.execute(
                """
                UPDATE bot_orders SET status='filled', filled_amount=?,
                price=?, order_id=?, updated_at=?
                WHERE client_order_id=? AND bot_id=?
                """,
                (filled, fill_price, str(order.get('id', '')),
                 int(_time.time()), cid, bot_id),
            )
            conn.commit()
            logger.warning(
                f"[RECOVERY] Bot {bot_id}: exchange close confirmed "
                f"{filled:.6f} @ {fill_price:.4f}"
            )
        except Exception as e:
            # Mark WAL receipt failed; leave bot gated — do not wipe DB
            conn.execute(
                """
                UPDATE bot_orders SET status='failed', notes=?, updated_at=?
                WHERE client_order_id=? AND bot_id=?
                """,
                (f"recovery close failed: {e}", int(_time.time()), cid, bot_id),
            )
            conn.commit()
            raise RuntimeError(
                f"Bot {bot_id}: exchange close for {closeable_qty:.6f} failed: {e}. "
                f"Bot remains gated. DB not wiped."
            ) from e
    else:
        # closeable_qty == 0: virtual position has no physical backing.
        # No exchange order needed. safe_wipe_bot Guard 2.0 will confirm
        # there is no LONG/SHORT position in this direction before clearing DB.
        logger.info(
            f"[RECOVERY] Bot {bot_id}: closeable_qty=0 — virtual position is unphysical. "
            f"No exchange order needed. Proceeding to safe_wipe."
        )

    # Step 6: Wipe DB via safe_wipe_bot (bypass_ledger_guard=True because
    # the exchange close above already cleared the physical portion)
    full_reason = reason or f"resolve_gated_bot: {action_label}"
    wipe_ok = safe_wipe_bot(
        bot_id=bot_id,
        pair=pair,
        direction=direction,
        reason=full_reason,
        bypass_ledger_guard=True,
        human_approved=human_approved,
        cursor=cursor,
    )


    if not wipe_ok:
        raise RuntimeError(
            f"Bot {bot_id}: safe_wipe_bot refused to clear DB state after exchange close. "
            f"Check live exchange position manually."
        )

    logger.warning(
        f"[RECOVERY] Bot {bot_id} ({pair}): fully resolved. "
        f"closeable={closeable_qty:.6f} unphysical={unphysical_remainder:.6f} "
        f"action={action_label}"
    )

    return {
        'status': 'resolved',
        'bot_id': bot_id,
        'pair': pair,
        'direction': direction,
        'virtual_qty': open_qty,
        'live_net': live_net,
        'closeable_qty': closeable_qty,
        'unphysical_remainder': unphysical_remainder,
        'wiped': True,
    }
