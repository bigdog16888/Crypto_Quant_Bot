"""
Startup repair verification — CID-based checks for the O-9 startup barrier.

Two functions gate whether a pair-level mismatch is REPARABLE (auto-fix, let
startup proceed) or TRULY AMBIGUOUS (block startup, require manual review).

Design contract (John's spec, 2026-08-25):
  - reparable  = every open/resting order's status can be confirmed via exchange
                 lookup by client_order_id (CID) and the resulting fills account
                 for the virtual/physical delta.
  - ambiguous  = an exchange position exists with NO corresponding bot_orders/CID
                 at all (genuinely orphaned), or a divergence that no combination
                 of CID-verified fills can account for.

Both functions are READ-ONLY against the DB.  They never write, credit, or
mutate state — that is `reconstruct_offline_fills`' job at startup Step 2.
These functions only VERDICT.

Symbol convention (CODEBASE_GUIDE.md §3.36):
  - bots.pair            = CCXT format  "XAU/USDT:USDT"
  - bots.normalized_pair = WS format    "XAUUSDT"  (no slash, no suffix)
  - normalize_symbol() converts CCXT → WS.  All DB lookups on normalized_pair
    must use the WS format.
"""
import time
import logging
from typing import Optional, List, Tuple

from engine.parity_gates import qty_tolerance, get_exchange_signed_net
from engine.exchange_interface import normalize_symbol
from engine import database as engine_database

logger = logging.getLogger(__name__)


def _get_conn():
    """Get DB connection dynamically to support test monkey-patching."""
    return engine_database.get_connection()


# ---------------------------------------------------------------------------
# Shared helper: fetch exchange order by CID with fallback to closed-order scan
# ---------------------------------------------------------------------------

def _lookup_order_by_cid(exchange, cid: str, pair: str,
                         closed_orders_cache: Optional[list] = None) -> Optional[dict]:
    """
    Look up a single order on the exchange by client_order_id.

    Tries direct fetch first; if that fails (Binance sometimes strips CIDs from
    direct lookup), falls back to scanning the pre-fetched closed-order history.

    Returns the ccxt order dict, or None if not found.
    """
    # 1. Direct lookup
    try:
        order = exchange.fetch_order(cid, pair)
        if order:
            return order
    except Exception:
        pass  # fall through to history scan

    # 2. Fallback: scan pre-fetched closed orders for matching CID
    if closed_orders_cache is not None:
        for o in closed_orders_cache:
            o_cid = (o.get('clientOrderId')
                     or (o.get('info') or {}).get('clientOrderId')
                     or '')
            if o_cid == cid:
                return o

    return None


def _fetch_closed_orders(exchange, pair: str, since_hours: int = 48) -> list:
    """
    Fetch up to 10 pages of closed orders for a pair from the exchange.
    Returns a list of ccxt order dicts, sorted by timestamp ascending.
    """
    hist: list = []
    try:
        since_ms = int((time.time() - since_hours * 3600) * 1000)
        current_since = since_ms
        for _ in range(10):
            page = exchange.fetch_closed_orders(pair, since=current_since, limit=1000)
            if not page:
                break
            hist.extend(page)
            last_ts = max((o.get('timestamp') or 0) for o in page)
            if last_ts <= current_since:
                break
            current_since = last_ts + 1
    except Exception as e:
        logger.warning(f"[CID-VERIFY] fetch_closed_orders failed for {pair}: {e}")
    return sorted(hist, key=lambda x: x.get('timestamp') or 0)


def _order_filled_qty(order: dict) -> float:
    """Extract the filled quantity from a ccxt order dict, with Binance fallbacks."""
    o_status = (order.get('status') or '').lower()
    o_filled = float(order.get('filled', order.get('filled_amount', 0.0)) or 0)
    # Binance FAPI sometimes omits 'filled' on fully-executed orders
    if o_status in ('filled', 'closed') and o_filled <= 0:
        o_filled = float(order.get('amount') or 0.0)
    return o_filled


def _order_is_filled(order: dict) -> bool:
    """True if the order has any fill (even partial on a cancelled order)."""
    return _order_filled_qty(order) > 1e-12


# ---------------------------------------------------------------------------
# 1. Orphan check: does the physical position have ANY CID-owning bot?
# ---------------------------------------------------------------------------

def _pair_has_unexplained_orphan(pair: str, physical_net: float, exchange) -> bool:
    """
    Returns True if the exchange net position CANNOT be attributed to any active
    bot's CID-verified fills — i.e. it is genuinely orphaned.

    Logic:
      1. No physical position → not orphaned (False).
      2. No exchange → can't verify → assume orphaned (True, conservative).
      3. No active bots on pair → orphaned (True).
      4. For each active bot, collect its filled bot_orders CIDs for the current
         cycle, verify each CID on the exchange, and sum the signed fill qty.
      5. If the CID-verified signed sum matches the physical net within tolerance
         → the position IS explained → not orphaned (False).
      6. Otherwise → orphaned (True).
    """
    tol = qty_tolerance()

    if physical_net is None or abs(physical_net) <= tol:
        return False  # No meaningful physical position

    if not exchange:
        return True  # Can't verify → conservative: treat as orphaned

    conn = _get_conn()
    norm = normalize_symbol(pair).upper()

    # All active bots on this pair (match on normalized_pair OR raw pair)
    bot_rows = conn.execute(
        "SELECT b.id, b.direction, b.name FROM bots b "
        "WHERE b.is_active=1 AND (b.normalized_pair=? OR b.pair=?)",
        (norm, norm)
    ).fetchall()

    if not bot_rows:
        logger.warning(f"[ORPHAN-CHECK] {pair}: physical {physical_net:.6f} but ZERO active bots")
        return True

    # Pre-fetch closed-order history once for CID fallback lookups
    closed_hist = _fetch_closed_orders(exchange, pair, since_hours=48)

    # Accumulate CID-verified signed fills across ALL bots on this pair
    cid_verified_net = 0.0

    for bot_id, bot_dir, bot_name in bot_rows:
        bot_sign = 1.0 if str(bot_dir).upper() == 'LONG' else -1.0

        trade_row = conn.execute(
            "SELECT cycle_id, COALESCE(wipe_wall_ts, 0) FROM trades WHERE bot_id = ?",
            (bot_id,)
        ).fetchone()
        if not trade_row:
            continue

        target_cycle = trade_row[0]
        wipe_wall = int(trade_row[1] or 0)

        # All filled position-affecting orders for this bot's current cycle
        filled_rows = conn.execute(
            "SELECT bo.client_order_id, bo.filled_amount, bo.order_type "
            "FROM bot_orders bo "
            "WHERE bo.bot_id = ? AND bo.cycle_id = ? "
            "  AND bo.status IN ('filled','partially_filled','closed') "
            "  AND bo.filled_amount > 0 "
            "  AND bo.order_type IN ('entry','grid','adoption','adoption_add','carry') "
            "  AND bo.created_at >= ? "
            "ORDER BY bo.created_at ASC",
            (bot_id, target_cycle, wipe_wall)
        ).fetchall()

        for cid, filled_amt, otype in filled_rows:
            if not cid or not cid.startswith('CQB_'):
                continue

            # Verify this CID actually exists on the exchange with a fill
            exch_order = _lookup_order_by_cid(exchange, cid, pair, closed_hist)
            if exch_order is None:
                continue
            if not _order_is_filled(exch_order):
                continue

            verified_qty = _order_filled_qty(exch_order)
            # Sign: entry/grid fills ADD to the bot's directional position
            cid_verified_net += bot_sign * verified_qty

    # Verdict: does the CID-verified net match the physical net?
    if abs(cid_verified_net - physical_net) <= max(tol, abs(physical_net) * 0.02):
        logger.info(
            f"[ORPHAN-CHECK] {pair}: physical {physical_net:.6f} explained by "
            f"CID-verified net {cid_verified_net:.6f} — NOT orphaned"
        )
        return False

    logger.warning(
        f"[ORPHAN-CHECK] {pair}: physical {physical_net:.6f} vs CID-verified net "
        f"{cid_verified_net:.6f} — UNEXPLAINED, treating as orphaned"
    )
    return True


# ---------------------------------------------------------------------------
# 2. Mismatch explainability: can CID-verified fills account for the delta?
# ---------------------------------------------------------------------------

def _mismatch_explainable_by_cid(pair: str, virtual: float, physical: float,
                                 exchange) -> bool:
    """
    Returns True if the virtual/physical divergence is fully accounted for by
    real, identifiable exchange fills matching our bot_orders CIDs.

    This is a READ-ONLY verdict.  It does NOT credit fills or mutate the DB.
    It scans the exchange's closed-order history for CIDs that belong to our
    bots, sums the fills that the DB has NOT yet credited (i.e. bot_orders rows
    still in 'open'/'placing'/'new' status), and checks whether those uncredited
    fills close the gap.

    No sign or magnitude heuristics — the verdict comes entirely from whether
    identifiable CID fills sum to the delta.
    """
    if physical is None or virtual is None:
        return False

    tol = qty_tolerance()
    delta = physical - virtual  # what the exchange has that the DB doesn't

    if abs(delta) <= tol:
        return True  # Already in parity — trivially explainable

    if not exchange:
        return False  # Can't verify

    conn = _get_conn()
    norm = normalize_symbol(pair).upper()

    # Get all active bots on this pair
    bot_rows = conn.execute(
        "SELECT b.id, b.direction FROM bots b "
        "WHERE b.is_active=1 AND (b.normalized_pair=? OR b.pair=?)",
        (norm, norm)
    ).fetchall()

    if not bot_rows:
        return False  # No bots → nothing to explain the delta

    # Pre-fetch closed-order history for CID matching
    closed_hist = _fetch_closed_orders(exchange, pair, since_hours=48)

    # For each bot, find UNRESOLVED orders (open/placing/new) whose CIDs
    # actually filled on the exchange.  These are the missed fills.
    uncredited_net = 0.0

    for bot_id, bot_dir in bot_rows:
        bot_sign = 1.0 if str(bot_dir).upper() == 'LONG' else -1.0

        # Unresolved orders: DB says open/placing/new but exchange may have filled them
        unresolved = conn.execute(
            "SELECT bo.client_order_id, bo.amount, bo.order_type "
            "FROM bot_orders bo "
            "WHERE bo.bot_id = ? "
            "  AND bo.status IN ('open','new','placing','cancelling') "
            "  AND bo.client_order_id LIKE 'CQB_%'",
            (bot_id,)
        ).fetchall()

        for cid, amount, otype in unresolved:
            exch_order = _lookup_order_by_cid(exchange, cid, pair, closed_hist)
            if exch_order is None:
                continue
            if not _order_is_filled(exch_order):
                continue

            verified_qty = _order_filled_qty(exch_order)

            # Determine the exchange side of this fill.
            # For entry/grid orders the side matches the bot's direction.
            # For TP/exit orders the side is opposite.
            o_side = str(exch_order.get('side', '')).upper()
            if o_side == 'BUY':
                exchange_signed = +verified_qty
            elif o_side == 'SELL':
                exchange_signed = -verified_qty
            else:
                # Fallback: infer from order type
                if otype in ('tp', 'take_profit', 'exit', 'dust_close'):
                    exchange_signed = -bot_sign * verified_qty
                else:
                    exchange_signed = bot_sign * verified_qty

            uncredited_net += exchange_signed
            logger.debug(
                f"[CID-EXPLAIN] {pair} bot {bot_id}: CID {cid} filled "
                f"{verified_qty:.6f} (side={o_side}, signed={exchange_signed:+.6f})"
            )

    # Verdict: do the uncredited CID fills account for the delta?
    residual = abs(delta - uncredited_net)
    if residual <= max(tol, abs(delta) * 0.05):
        logger.info(
            f"[CID-EXPLAIN] {pair}: delta {delta:+.6f} explained by uncredited "
            f"CID fills {uncredited_net:+.6f} (residual {residual:.6f}) — REPARABLE"
        )
        return True

    logger.info(
        f"[CID-EXPLAIN] {pair}: delta {delta:+.6f} vs uncredited CID fills "
        f"{uncredited_net:+.6f} (residual {residual:.6f}) — NOT explainable"
    )
    return False
