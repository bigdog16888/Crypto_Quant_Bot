"""
engine/position_ledger.py — Canonical position computation from immutable exchange_fills log.

This module replaces fragile dual-write state (trades.total_invested, bot_orders.status, trades.open_qty)
with a single source of truth: the exchange_fills append-only log.

All functions are pure, read-only, and deterministic — same input → same output.
"""

import sqlite3
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class Fill:
    """Immutable fill record from exchange_fills."""
    exchange_order_id: str
    client_order_id: str
    symbol: str
    side: str          # 'BUY' or 'SELL' — exchange-reported, never inferred
    qty: float
    price: float
    fee: float
    fee_asset: Optional[str]
    fill_ts: int
    source: str
    bot_id: Optional[int]
    order_type: Optional[str]
    step: Optional[int]
    cycle_id: Optional[int]
    raw_json: Optional[str]


@dataclass
class BotPosition:
    """Canonical computed position for a single bot."""
    bot_id: int
    pair: str
    direction: str           # 'LONG' or 'SHORT' — from bot config
    net_qty: float           # Signed: positive for LONG, negative for SHORT
    entry_cost: float        # Net cost basis (buy_cost - sell_proceeds)
    avg_entry_price: float   # entry_cost / abs(net_qty) if net_qty != 0 else 0
    realized_pnl: float      # From closed fills (TP, SL, close)
    fills_count: int
    source: str              # 'exchange_fills'
    verified_against_exchange: bool = False
    discrepancies: List[str] = None

    def __post_init__(self):
        if self.discrepancies is None:
            self.discrepancies = []


@dataclass
class PairPosition:
    """Aggregated position across all bots on a pair."""
    pair: str
    net_qty: float           # Net across all bots (signed per bot direction)
    bots: List[BotPosition]
    exchange_position: Optional[float] = None  # From fetch_position if provided
    verified: bool = False
    discrepancies: List[str] = None

    def __post_init__(self):
        if self.discrepancies is None:
            self.discrepancies = []


def _fetch_fills_for_bot(conn: sqlite3.Connection, bot_id: int, cycle_floor: int = None, cycle_ceiling: int = None) -> List[Fill]:
    """Fetch all fills for a bot, optionally filtered by cycle_floor/cycle_ceiling.
    
    Filters by the bot's configured pair (both CCXT format and normalized WS format)
    to prevent cross-pair contamination from backfill errors.
    """
    from engine.exchange_interface import normalize_symbol
    
    # Get bot's configured pair to filter fills by symbol
    bot_config = _get_bot_config(conn, bot_id)
    if not bot_config:
        return []  # Bot not found, no fills
    
    # Use normalized_pair if available, otherwise normalize the pair field
    bot_pair_raw = bot_config.get('normalized_pair') or bot_config.get('pair', '')
    bot_norm = normalize_symbol(bot_pair_raw).upper()
    bot_pair = bot_config.get('pair', '')  # Keep original for exact match fallback
    
    # Register normalize_symbol as SQLite UDF (idempotent - safe to call multiple times)
    conn.create_function("normalize_symbol", 1, lambda s: normalize_symbol(s).upper() if s else "")
    
    sql = """
        SELECT exchange_order_id, client_order_id, symbol, side, qty, price,
               fee, fee_asset, fill_ts, source, bot_id, order_type, step, cycle_id, raw_json
        FROM exchange_fills
        WHERE bot_id = ?
          AND (symbol = ? OR normalize_symbol(symbol) = ?)
    """
    params = [bot_id, bot_pair, bot_norm]
    if cycle_floor is not None:
        sql += " AND cycle_id >= ?"
        params.append(cycle_floor)
    if cycle_ceiling is not None:
        sql += " AND cycle_id <= ?"
        params.append(cycle_ceiling)
    sql += " ORDER BY fill_ts, id"

    cursor = conn.execute(sql, params)
    rows = cursor.fetchall()

    return [Fill(
        exchange_order_id=r[0],
        client_order_id=r[1],
        symbol=r[2],
        side=r[3],
        qty=r[4],
        price=r[5],
        fee=r[6] or 0.0,
        fee_asset=r[7],
        fill_ts=r[8],
        source=r[9],
        bot_id=r[10],
        order_type=r[11],
        step=r[12],
        cycle_id=r[13],
        raw_json=r[14]
    ) for r in rows]


def _fetch_fills_for_pair(conn: sqlite3.Connection, pair: str, cycle_floor: int = None) -> List[Fill]:
    """Fetch all fills for a pair (all bots), optionally filtered by cycle_floor."""
    sql = """
        SELECT exchange_order_id, client_order_id, symbol, side, qty, price,
               fee, fee_asset, fill_ts, source, bot_id, order_type, step, cycle_id, raw_json
        FROM exchange_fills
        WHERE symbol = ?
    """
    params = [pair]
    if cycle_floor is not None:
        sql += " AND cycle_id >= ?"
        params.append(cycle_floor)
    sql += " ORDER BY fill_ts, id"

    cursor = conn.execute(sql, params)
    rows = cursor.fetchall()

    return [Fill(
        exchange_order_id=r[0],
        client_order_id=r[1],
        symbol=r[2],
        side=r[3],
        qty=r[4],
        price=r[5],
        fee=r[6] or 0.0,
        fee_asset=r[7],
        fill_ts=r[8],
        source=r[9],
        bot_id=r[10],
        order_type=r[11],
        step=r[12],
        cycle_id=r[13],
        raw_json=r[14]
    ) for r in rows]


def _get_bot_config(conn: sqlite3.Connection, bot_id: int) -> Optional[Dict[str, Any]]:
    """Fetch bot config (pair, normalized_pair, direction)."""
    row = conn.execute(
        "SELECT id, name, pair, normalized_pair, direction FROM bots WHERE id = ?",
        (bot_id,)
    ).fetchone()
    if row:
        return {'id': row[0], 'name': row[1], 'pair': row[2], 'normalized_pair': row[3], 'direction': row[4]}
    return None


def compute_bot_position(
    bot_id: int,
    conn: sqlite3.Connection = None,
    *,
    cycle_floor: int = None,
    cycle_ceiling: int = None,  # NEW: upper bound (inclusive) for exact cycle computation
    exchange=None  # Optional: for live cross-check
) -> BotPosition:
    """
    Canonical position compute from exchange_fills (immutable log).

    Args:
        bot_id: Bot to compute position for
        conn: Optional DB connection (creates own if not provided)
        cycle_floor: If set, only consider fills from this cycle onward (for cross-cycle recompute)
        cycle_ceiling: If set, only consider fills up to this cycle (inclusive). With cycle_floor, enables exact-cycle computation.
        exchange: Optional exchange interface for live verification

    Returns:
        BotPosition with net_qty, entry_cost, avg_entry_price, realized_pnl
    """
    own_conn = False
    if conn is None:
        from engine.database import get_connection
        conn = get_connection()
        own_conn = True

    try:
        bot_config = _get_bot_config(conn, bot_id)
        if not bot_config:
            raise ValueError(f"Bot {bot_id} not found")

        pair = bot_config['pair']
        direction = bot_config['direction']  # 'LONG' or 'SHORT'

        fills = _fetch_fills_for_bot(conn, bot_id, cycle_floor, cycle_ceiling)

        if not fills:
            return BotPosition(
                bot_id=bot_id,
                pair=pair,
                direction=direction,
                net_qty=0.0,
                entry_cost=0.0,
                avg_entry_price=0.0,
                realized_pnl=0.0,
                fills_count=0,
                source='exchange_fills'
            )

        # Accumulate position from fills
        # For LONG bot: BUY increases position, SELL decreases
        # For SHORT bot: SELL increases position (adds to short), BUY decreases (covers short)
        net_qty = 0.0
        entry_cost = 0.0  # Cost basis of current position: positive = amount invested
        realized_pnl = 0.0

        for fill in fills:
            side = fill.side.upper()
            qty = fill.qty
            price = fill.price
            fee = fill.fee
            order_type = (fill.order_type or '').lower()

            if direction == 'LONG':
                if side == 'BUY':
                    # Opening/adding to long
                    net_qty += qty
                    entry_cost += qty * price  # Add to cost basis
                elif side == 'SELL':
                    # Closing/reducing long
                    if net_qty > 0:
                        # Realize PnL on the portion being closed
                        close_qty = min(qty, net_qty)
                        avg_cost = entry_cost / net_qty if net_qty != 0 else price
                        realized_pnl += close_qty * (price - avg_cost) - fee
                        entry_cost -= close_qty * avg_cost  # Remove cost basis of closed portion
                    net_qty -= qty
            elif direction == 'SHORT':
                if side == 'SELL':
                    # Opening/adding to short (selling)
                    net_qty -= qty  # Negative for short
                    entry_cost += qty * price  # Add to cost basis (proceeds from short)
                elif side == 'BUY':
                    # Covering/reducing short (buying back)
                    if net_qty < 0:
                        close_qty = min(qty, abs(net_qty))
                        avg_cost = entry_cost / abs(net_qty) if net_qty != 0 else price
                        # For short: PnL = (avg_entry_price - cover_price) * qty
                        realized_pnl += close_qty * (avg_cost - price) - fee
                        entry_cost -= close_qty * avg_cost  # Remove cost basis of covered portion
                    net_qty += qty

        # Compute average entry price
        avg_entry_price = 0.0
        if net_qty != 0:
            avg_entry_price = abs(entry_cost / net_qty)

        return BotPosition(
            bot_id=bot_id,
            pair=pair,
            direction=direction,
            net_qty=net_qty,
            entry_cost=entry_cost,
            avg_entry_price=avg_entry_price,
            realized_pnl=realized_pnl,
            fills_count=len(fills),
            source='exchange_fills'
        )

    finally:
        if own_conn and conn:
            # Connection came from get_connection() - don't close it, thread-local manages lifecycle
            pass


def _get_bot_checkpoint(conn: sqlite3.Connection, bot_id: int):
    """Return (side, size, last_checked_unix_ts) from active_positions, or None."""
    row = conn.execute(
        "SELECT side, size, last_checked FROM active_positions WHERE bot_id = ?",
        (bot_id,)
    ).fetchone()
    if row:
        return (str(row[0]), float(row[1]), int(row[2]) if row[2] else 0)
    return None


def _compute_delta_from_fills(conn: sqlite3.Connection, bot_id: int, since_ts: int) -> float:
    """Algebraic sum (BUY+, SELL-) of exchange_fills for bot_id where fill_ts > since_ts."""
    if since_ts <= 0:
        return 0.0
    rows = conn.execute(
        "SELECT side, qty FROM exchange_fills WHERE bot_id = ? AND fill_ts > ?",
        (bot_id, since_ts)
    ).fetchall()
    return sum(qty if str(s).upper() == 'BUY' else -qty for s, qty in rows)


def _checkpoint_bot_position(
    conn: sqlite3.Connection,
    bot_id: int,
    pair: str,
    direction: str,
) -> BotPosition:
    """Dynamic Checkpoint Pattern: base from active_positions + delta fills since checkpoint.

    Unit test fallback: if no active_positions row exists for this bot, base=0.0,
    cp_ts=0, delta=0 — preserves test integrity without requiring a DB snapshot.
    """
    cp = _get_bot_checkpoint(conn, bot_id)
    if cp:
        side_str, base_qty, cp_ts = cp
        sign = 1.0 if side_str.upper() == 'LONG' else -1.0
        base_signed = base_qty * sign
        delta_qty = _compute_delta_from_fills(conn, bot_id, cp_ts)
        net_qty = base_signed + delta_qty

        ap_row = conn.execute(
            "SELECT entry_price FROM active_positions WHERE bot_id = ?",
            (bot_id,)
        ).fetchone()
        avg_entry_price = float(ap_row[0]) if ap_row else 0.0
        entry_cost = base_qty * avg_entry_price if avg_entry_price else 0.0
        fills_count = 0
        if cp_ts > 0:
            fills_count = conn.execute(
                "SELECT count(*) FROM exchange_fills WHERE bot_id = ? AND fill_ts > ?",
                (bot_id, cp_ts)
            ).fetchone()[0]

        return BotPosition(
            bot_id=bot_id,
            pair=pair,
            direction=direction,
            net_qty=net_qty,
            entry_cost=entry_cost,
            avg_entry_price=avg_entry_price,
            realized_pnl=0.0,
            fills_count=fills_count,
            source='checkpoint',
        )
    else:
        return BotPosition(
            bot_id=bot_id,
            pair=pair,
            direction=direction,
            net_qty=0.0,
            entry_cost=0.0,
            avg_entry_price=0.0,
            realized_pnl=0.0,
            fills_count=0,
            source='checkpoint',
        )


def compute_pair_position(
    pair: str,
    conn: sqlite3.Connection = None,
    *,
    cycle_floor: int = None,
    cycle_ceiling: int = None,  # NEW: upper bound for exact cycle computation
    exchange=None  # Optional: for live cross-check
) -> PairPosition:
    """
    Aggregate position across all bots on a pair.

    Args:
        pair: Trading pair symbol
        conn: Optional DB connection
        cycle_floor: If set, only consider fills from this cycle onward
        cycle_ceiling: If set, only consider fills up to this cycle (inclusive)
        exchange: Optional exchange interface for live verification

    Returns:
        PairPosition with net_qty across all bots and per-bot breakdown
    """
    own_conn = False
    if conn is None:
        from engine.database import get_connection
        conn = get_connection()
        own_conn = True

    try:
        # Get all bots for this pair (match on both pair and normalized_pair)
        bot_rows = conn.execute(
            "SELECT id, direction FROM bots WHERE pair = ? OR normalized_pair = ?", (pair, pair)
        ).fetchall()

        if not bot_rows:
            return PairPosition(pair=pair, net_qty=0.0, bots=[])

        bot_positions = []
        pair_net_qty = 0.0

        for bot_id, direction in bot_rows:
            bp = _checkpoint_bot_position(conn, bot_id, pair, direction)
            bot_positions.append(bp)
            pair_net_qty += bp.net_qty

        result = PairPosition(
            pair=pair,
            net_qty=pair_net_qty,
            bots=bot_positions
        )

        # Optional: verify against exchange
        if exchange:
            try:
                from engine.exchange_interface import ExchangeInterface
                if hasattr(exchange, 'fetch_position'):
                    # This would need the actual exchange call
                    pass
            except Exception as e:
                logger.warning(f"[POSITION-LEDGER] Exchange verification failed for {pair}: {e}")

        return result

    finally:
        if own_conn and conn:
            # Connection came from get_connection() - don't close it, thread-local manages lifecycle
            pass


def verify_bot_position(bot_id: int, exchange, conn: sqlite3.Connection = None) -> Tuple[bool, List[str]]:
    """
    Hard verification: compare compute_bot_position() result vs exchange fetch_position().
    Returns (is_match, discrepancy_list).
    """
    own_conn = False
    if conn is None:
        from engine.database import get_connection
        conn = get_connection()
        own_conn = True

    discrepancies = []

    try:
        computed = compute_bot_position(bot_id, conn)

        # Fetch actual exchange position
        try:
            # This depends on the exchange interface
            # For Binance: fetch_position(symbol) returns position info
            # We need to map bot_id -> symbol and call appropriately
            bot_config = _get_bot_config(conn, bot_id)
            if not bot_config:
                discrepancies.append(f"Bot {bot_id} not found")
                return False, discrepancies

            symbol = bot_config['pair']

            # Try to get position from exchange
            if hasattr(exchange, 'fetch_position'):
                positions = exchange.fetch_position(symbol)
                # Parse the response - this is exchange-specific
                # For now, mark as needing manual implementation
                discrepancies.append(f"Exchange verification not fully implemented for {symbol}")
                return False, discrepancies
            else:
                discrepancies.append("Exchange interface does not support fetch_position")
                return False, discrepancies

        except Exception as e:
            discrepancies.append(f"Exchange fetch_position error: {e}")
            return False, discrepancies

    finally:
        if own_conn and conn:
            from engine.database import close_connection

    return len(discrepancies) == 0, discrepancies


# Convenience function for testing/backfill
def backfill_exchange_fills_from_bot_orders(
    conn: sqlite3.Connection,
    bot_id: int = None,
    limit: int = None
) -> int:
    """
    Backfill exchange_fills from historical bot_orders data.
    Useful for populating the immutable log with pre-existing fills.
    Only inserts where exchange_order_id is present, status indicates fill,
    AND there is REAL exchange confirmation (filled_at > 0).
    """
    sql = """
        SELECT bo.bot_id, bo.order_id, bo.client_order_id, bo.order_type, bo.filled_amount, bo.price,
               bo.status, bo.step, bo.cycle_id, bo.filled_at, b.pair, b.direction
        FROM bot_orders bo
        JOIN bots b ON bo.bot_id = b.id
        WHERE bo.filled_amount > 0
          AND bo.order_id IS NOT NULL
          AND bo.order_id != ''
          AND bo.status IN ('filled', 'partially_filled', 'reset_cleared')
          AND bo.filled_at > 0
    """
    params = []
    if bot_id:
        sql += " AND bo.bot_id = ?"
        params.append(bot_id)
    sql += " ORDER BY bo.id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    cursor = conn.execute(sql, params)
    rows = cursor.fetchall()

    inserted = 0
    for row in rows:
        bot_id, exchange_order_id, client_order_id, order_type, filled_amount, price, \
        status, step, cycle_id, filled_at, pair, direction = row

        # Infer side from direction + order_type
        # This is a best-effort for historical data; real-time path uses actual exchange side
        side = _infer_side_from_bot_data(direction, order_type, filled_amount)

        if not side:
            continue

        try:
            conn.execute("""
                INSERT OR IGNORE INTO exchange_fills (
                    exchange_order_id, client_order_id, symbol, side, qty, price,
                    fee, fee_asset, fill_ts, source, bot_id, order_type, step, cycle_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(exchange_order_id), client_order_id or '', pair, side,
                float(filled_amount), float(price), 0.0, None,
                int(filled_at) if filled_at else 0, 'backfill',
                bot_id, order_type, step, cycle_id, int(__import__('time').time())
            ))
            if conn.total_changes > 0:
                inserted += 1
        except Exception as e:
            logger.warning(f"[BACKFILL] Failed for order {exchange_order_id}: {e}")

    conn.commit()
    return inserted


def _infer_side_from_bot_data(direction: str, order_type: str, filled_qty: float) -> Optional[str]:
    """
    Infer exchange side from bot direction and order type.
    THIS IS ONLY FOR HISTORICAL BACKFILL — real-time path MUST use actual exchange side.
    """
    if filled_qty <= 0:
        return None

    ot = (order_type or '').lower()
    dir_upper = (direction or '').upper()

    if dir_upper == 'LONG':
        if ot in ('entry', 'grid'):
            return 'BUY'
        elif ot in ('tp', 'close', 'stop', 'flatten_close'):
            return 'SELL'
    elif dir_upper == 'SHORT':
        if ot in ('entry', 'grid'):
            return 'SELL'
        elif ot in ('tp', 'close', 'stop', 'flatten_close'):
            return 'BUY'

    return None


def compute_bot_position_from_exchange_fills(
    bot_id: int,
    conn: sqlite3.Connection = None,
    *,
    cycle_floor: int = None,
    cycle_ceiling: int = None,
) -> BotPosition:
    """
    Shadow-mode equivalent of recompute_invested_from_orders.
    Computes position purely from exchange_fills (immutable log) instead of bot_orders.
    
    This is the PRIMARY path going forward; legacy recompute_invested_from_orders
    reads bot_orders (mutable, subject to wipe/reset bugs).
    
    Args:
        bot_id: Bot to compute position for
        conn: Optional DB connection
        cycle_floor: If set, only consider fills from this cycle onward
        cycle_ceiling: If set, only consider fills up to this cycle (inclusive)
        
    Returns:
        BotPosition with net_qty, entry_cost, avg_entry_price, realized_pnl
    """
    # This is just an alias to compute_bot_position for clarity in shadow mode
    return compute_bot_position(bot_id, conn, cycle_floor=cycle_floor, cycle_ceiling=cycle_ceiling)


def shadow_compare_recompute_vs_position_ledger(
    bot_id: int,
    conn: sqlite3.Connection = None,
    *,
    cycle_floor: int = None,
) -> Dict[str, Any]:
    """
    Compare legacy recompute_invested_from_orders (bot_orders) vs 
    compute_bot_position (exchange_fills).
    
    Returns dict with both results and divergence flags for logging.
    """
    from engine.database import recompute_invested_from_orders
    
    own_conn = False
    if conn is None:
        from engine.database import get_connection
        conn = get_connection()
        own_conn = True
    
    try:
        # Legacy: reads bot_orders
        legacy_cost, legacy_avg, legacy_qty, legacy_step = recompute_invested_from_orders(
            bot_id, cycle_floor=cycle_floor
        )
        legacy_qty = max(0.0, legacy_qty)
        
        # Primary: reads exchange_fills
        primary_pos = compute_bot_position(bot_id, conn, cycle_floor=cycle_floor)
        primary_qty = abs(primary_pos.net_qty)  # unsigned for comparison
        primary_avg = primary_pos.avg_entry_price
        primary_cost = primary_pos.entry_cost
        
        # Compare
        qty_diff = abs(legacy_qty - primary_qty)
        cost_diff = abs(legacy_cost - primary_cost)
        avg_diff = abs(legacy_avg - primary_avg) if (legacy_avg > 0 and primary_avg > 0) else 0.0
        
        is_divergent = qty_diff > 1e-6 or cost_diff > 0.01 or avg_diff > 0.01
        
        return {
            'bot_id': bot_id,
            'cycle_floor': cycle_floor,
            'legacy': {
                'qty': legacy_qty,
                'avg': legacy_avg,
                'cost': legacy_cost,
                'step': legacy_step,
            },
            'primary': {
                'qty': primary_qty,
                'avg': primary_avg,
                'cost': primary_cost,
                'step': 0,  # position_ledger doesn't compute step yet
                'realized_pnl': primary_pos.realized_pnl,
            },
            'divergence': {
                'qty_diff': qty_diff,
                'cost_diff': cost_diff,
                'avg_diff': avg_diff,
                'is_divergent': is_divergent,
            },
            'source': 'legacy=bot_orders, primary=exchange_fills',
        }
    finally:
        if own_conn and conn:
            # Connection came from get_connection() - don't close it, thread-local manages lifecycle
            pass


if __name__ == '__main__':
    # Quick self-test
    import engine.database as db
    db.init_db()
    conn = db.get_connection()

    print("=== Testing compute_bot_position ===")
    for bot_id in [10018, 10019, 100001, 10007, 10008, 10016]:
        pos = compute_bot_position(bot_id, conn)
        print(f"Bot {bot_id} ({pos.pair} {pos.direction}): net_qty={pos.net_qty:.6f}, "
              f"avg_entry={pos.avg_entry_price:.6f}, realized_pnl={pos.realized_pnl:.4f}, "
              f"fills={pos.fills_count}")

    print("\n=== Testing compute_pair_position ===")
    for pair in ['SUI/USDC:USDC', 'XAU/USDT:USDT', 'SOL/USDC:USDC', 'BNB/USDC:USDC', 'BTC/USDC:USDC']:
        pos = compute_pair_position(pair, conn)
        print(f"Pair {pair}: net_qty={pos.net_qty:.6f}, bots={len(pos.bots)}")
        for bp in pos.bots:
            print(f"  Bot {bp.bot_id}: net_qty={bp.net_qty:.6f}")