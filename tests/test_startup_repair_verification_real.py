"""
Regression tests for startup repair verification (CID-based, real implementations).

Covers:
  1. Orphan check: physical position with NO bots → orphaned
  2. Orphan check: bot exists but no CID fills → orphaned
  3. Orphan check: CID-verified fills fully explain physical → NOT orphaned
  4. XAU incident: resting grid order filled while engine down → delta explainable
  5. Sign conflict with no CID fills → NOT explainable
  6. Delta exists but exchange shows no fills for our CIDs → NOT explainable

Symbol convention (CODEBASE_GUIDE.md §3.36):
  bots.pair = 'XAU/USDT:USDT' (CCXT), bots.normalized_pair = 'XAUUSDT' (WS).
"""
import sqlite3
import pytest

import engine.database
from engine.startup_repair_verification import (
    _pair_has_unexplained_orphan,
    _mismatch_explainable_by_cid,
)


class MockExchange:
    """Mock exchange: fetch_order by CID, fetch_closed_orders history, fetch_positions."""

    def __init__(self, positions_dict=None, orders_dict=None, closed_orders=None):
        self.positions_data = positions_dict or {}
        self.orders_data = orders_dict or {}      # {cid: ccxt order dict}
        self.closed_orders_data = closed_orders or []  # list of ccxt order dicts

    def fetch_positions(self):
        result = []
        for symbol, data in self.positions_data.items():
            result.append({
                'symbol': symbol,
                'contracts': data.get('contracts', 0),
                'net_qty': data.get('net_qty', 0),
                'qty': abs(data.get('contracts', 0)),
                'side': data.get('side', 'long'),
                'unrealizedPnl': data.get('unrealizedPnl', 0),
                'entryPrice': data.get('entryPrice', 0),
            })
        return result

    def fetch_order(self, cid, pair):
        return self.orders_data.get(cid)

    def fetch_closed_orders(self, pair, since=None, limit=1000):
        return self.closed_orders_data


def create_test_db():
    conn = sqlite3.connect(':memory:')
    conn.execute("""
        CREATE TABLE bots (
            id INTEGER PRIMARY KEY, name TEXT, pair TEXT, normalized_pair TEXT,
            direction TEXT, is_active INTEGER DEFAULT 1, status TEXT DEFAULT 'Scanning',
            bot_type TEXT DEFAULT 'standard', parent_bot_id INTEGER,
            hedge_child_bot_id INTEGER, hedge_trigger_step INTEGER
        )""")
    conn.execute("""
        CREATE TABLE trades (
            bot_id INTEGER PRIMARY KEY, open_qty REAL DEFAULT 0,
            cycle_phase TEXT DEFAULT 'IDLE', position_side TEXT DEFAULT 'LONG',
            cycle_id INTEGER DEFAULT 1, total_invested REAL DEFAULT 0,
            avg_entry_price REAL DEFAULT 0, wipe_wall_ts INTEGER DEFAULT 0
        )""")
    conn.execute("""
        CREATE TABLE bot_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, order_type TEXT,
            step INTEGER, client_order_id TEXT, order_id TEXT, price REAL,
            amount REAL, filled_amount REAL, status TEXT, cycle_id INTEGER,
            created_at INTEGER
        )""")
    return conn


def run_with_test_db(conn, test_func):
    original = engine.database.get_connection
    engine.database.get_connection = lambda: conn
    try:
        return test_func(conn)
    finally:
        engine.database.get_connection = original


# ---------------------------------------------------------------------------
# Orphan check tests
# ---------------------------------------------------------------------------

def test_orphan_true_no_bots():
    """Physical position exists, zero active bots → orphaned."""
    def _test(conn):
        exchange = MockExchange(positions_dict={
            'XAU/USDT:USDT': {'contracts': -0.2, 'net_qty': -0.2, 'side': 'short'}
        })
        result = _pair_has_unexplained_orphan('XAU/USDT:USDT', -0.2, exchange)
        assert result is True
    run_with_test_db(create_test_db(), _test)


def test_orphan_true_no_cid_fills():
    """Bot exists but has NO filled orders → physical unexplained → orphaned."""
    def _test(conn):
        conn.execute(
            "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status, bot_type) "
            "VALUES (1001, 'test bot', 'XAU/USDT:USDT', 'XAUUSDT', 'SHORT', 1, 'IN TRADE', 'standard')")
        conn.execute(
            "INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) "
            "VALUES (1001, 0.1, 'ACTIVE', 'SHORT', 1)")
        exchange = MockExchange(positions_dict={
            'XAU/USDT:USDT': {'contracts': -0.2, 'net_qty': -0.2, 'side': 'short'}
        })
        result = _pair_has_unexplained_orphan('XAU/USDT:USDT', -0.2, exchange)
        assert result is True
    run_with_test_db(create_test_db(), _test)


def test_orphan_false_cid_fills_explain_physical():
    """Bot's CID-verified fills sum to the physical net → NOT orphaned."""
    def _test(conn):
        conn.execute(
            "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status, bot_type) "
            "VALUES (1002, 'test bot', 'XAU/USDT:USDT', 'XAUUSDT', 'SHORT', 1, 'IN TRADE', 'standard')")
        conn.execute(
            "INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id, wipe_wall_ts) "
            "VALUES (1002, 0.2, 'ACTIVE', 'SHORT', 1, 1000000)")
        # Two filled grid orders, 0.1 each → total 0.2 short
        conn.execute(
            "INSERT INTO bot_orders (bot_id, order_type, step, client_order_id, amount, filled_amount, status, cycle_id, created_at) "
            "VALUES (1002, 'grid', 1, 'CQB_1002_GRID_1_1000001', 0.1, 0.1, 'filled', 1, 1000001)")
        conn.execute(
            "INSERT INTO bot_orders (bot_id, order_type, step, client_order_id, amount, filled_amount, status, cycle_id, created_at) "
            "VALUES (1002, 'grid', 2, 'CQB_1002_GRID_2_1000002', 0.1, 0.1, 'filled', 1, 1000002)")

        exchange = MockExchange(
            positions_dict={'XAU/USDT:USDT': {'contracts': -0.2, 'net_qty': -0.2, 'side': 'short'}},
            orders_dict={
                'CQB_1002_GRID_1_1000001': {'id': '1', 'clientOrderId': 'CQB_1002_GRID_1_1000001',
                                            'filled': '0.1', 'status': 'FILLED', 'side': 'SELL'},
                'CQB_1002_GRID_2_1000002': {'id': '2', 'clientOrderId': 'CQB_1002_GRID_2_1000002',
                                            'filled': '0.1', 'status': 'FILLED', 'side': 'SELL'},
            })
        result = _pair_has_unexplained_orphan('XAU/USDT:USDT', -0.2, exchange)
        assert result is False
    run_with_test_db(create_test_db(), _test)


# ---------------------------------------------------------------------------
# Mismatch explainability tests
# ---------------------------------------------------------------------------

def test_mismatch_explainable_xau_incident():
    """
    EXACT 2026-08-21 XAU incident numbers:
      parent 10019 SHORT: step-7 entry 0.063 filled, step-8 grid 0.124 resting
      child 100319 LONG: entry 0.063 filled
      virtual net = +0.063 - 0.063 - 0.124... wait: parent open_qty 0.125 (0.063+0.062?)

    Actual incident state:
      DB virtual net = child +0.063, parent -0.063 (only step 7 credited) → net ≈ -0.062
        (parent open_qty 0.125 includes step-8 intent; credited fills = 0.063+0.062)
      Exchange net = -0.186 (step-8's 0.124 filled while engine dead)
      delta = -0.186 - (-0.062) = -0.124

    The step-8 grid order is 'open' in bot_orders but FILLED on exchange.
    → uncredited CID fill of -0.124 (SELL) exactly closes the delta → REPARABLE.
    """
    def _test(conn):
        conn.execute(
            "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status, bot_type, hedge_child_bot_id) "
            "VALUES (10019, 'short gold', 'XAU/USDT:USDT', 'XAUUSDT', 'SHORT', 1, 'IN TRADE', 'standard', 100319)")
        conn.execute(
            "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status, bot_type, parent_bot_id) "
            "VALUES (100319, 'short gold_hedge', 'XAU/USDT:USDT', 'XAUUSDT', 'LONG', 1, 'IN TRADE', 'hedge_child', 10019)")
        conn.execute(
            "INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id, wipe_wall_ts) "
            "VALUES (10019, 0.125, 'ACTIVE', 'SHORT', 1, 1000000)")
        conn.execute(
            "INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id, wipe_wall_ts) "
            "VALUES (100319, 0.063, 'ACTIVE', 'LONG', 1, 1000000)")
        # Step-8 grid order: resting in DB (status 'open'), filled on exchange
        conn.execute(
            "INSERT INTO bot_orders (bot_id, order_type, step, client_order_id, amount, filled_amount, status, cycle_id, created_at) "
            "VALUES (10019, 'grid', 8, 'CQB_10019_GRID_8_1000001', 0.124, 0.0, 'open', 1, 1000001)")
        # Child hedge entry already filled & credited
        conn.execute(
            "INSERT INTO bot_orders (bot_id, order_type, step, client_order_id, amount, filled_amount, status, cycle_id, created_at) "
            "VALUES (100319, 'entry', 1, 'CQB_100319_ENTRY_1_1000002', 0.063, 0.063, 'filled', 1, 1000002)")

        exchange = MockExchange(
            positions_dict={'XAU/USDT:USDT': {'contracts': -0.186, 'net_qty': -0.186,
                                              'side': 'short', 'entryPrice': 4540.46}},
            orders_dict={
                'CQB_10019_GRID_8_1000001': {
                    'id': '574831556', 'clientOrderId': 'CQB_10019_GRID_8_1000001',
                    'filled': '0.124', 'status': 'FILLED', 'side': 'SELL',
                    'symbol': 'XAUUSDT', 'timestamp': 1787286112000,
                },
            })

        virtual = -0.062   # DB state before crediting the offline fill
        physical = -0.186  # exchange truth
        result = _mismatch_explainable_by_cid('XAU/USDT:USDT', virtual, physical, exchange)
        assert result is True, (
            "XAU incident delta -0.124 must be explained by the step-8 CID fill")
    run_with_test_db(create_test_db(), _test)


def test_mismatch_not_explainable_sign_conflict():
    """Virtual long vs physical short, no unresolved CIDs → NOT explainable."""
    def _test(conn):
        conn.execute(
            "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status, bot_type) "
            "VALUES (2001, 'long btc', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', 1, 'IN TRADE', 'standard')")
        conn.execute(
            "INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) "
            "VALUES (2001, 0.05, 'ACTIVE', 'LONG', 1)")
        exchange = MockExchange(positions_dict={
            'BTC/USDC:USDC': {'contracts': -0.1, 'net_qty': -0.1, 'side': 'short'}
        })
        result = _mismatch_explainable_by_cid('BTC/USDC:USDC', 0.05, -0.1, exchange)
        assert result is False
    run_with_test_db(create_test_db(), _test)


def test_mismatch_not_explainable_no_exchange_fills():
    """Delta exists, unresolved CIDs in DB, but exchange shows them unfilled → NOT explainable."""
    def _test(conn):
        conn.execute(
            "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status, bot_type) "
            "VALUES (3001, 'short eth', 'ETH/USDC:USDC', 'ETHUSDC', 'SHORT', 1, 'IN TRADE', 'standard')")
        conn.execute(
            "INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) "
            "VALUES (3001, 0.1, 'ACTIVE', 'SHORT', 1)")
        conn.execute(
            "INSERT INTO bot_orders (bot_id, order_type, step, client_order_id, amount, filled_amount, status, cycle_id, created_at) "
            "VALUES (3001, 'grid', 3, 'CQB_3001_GRID_3_1000001', 0.2, 0.0, 'open', 1, 1000001)")
        # Exchange: order exists but NOT filled (still open / cancelled empty)
        exchange = MockExchange(
            positions_dict={'ETH/USDC:USDC': {'contracts': -0.3, 'net_qty': -0.3, 'side': 'short'}},
            orders_dict={
                'CQB_3001_GRID_3_1000001': {'id': '999', 'clientOrderId': 'CQB_3001_GRID_3_1000001',
                                            'filled': '0.0', 'status': 'CANCELED', 'side': 'SELL'},
            })
        result = _mismatch_explainable_by_cid('ETH/USDC:USDC', -0.1, -0.3, exchange)
        assert result is False
    run_with_test_db(create_test_db(), _test)


def test_mismatch_trivial_parity():
    """Virtual == physical within tolerance → trivially explainable."""
    def _test(conn):
        exchange = MockExchange()
        result = _mismatch_explainable_by_cid('SOL/USDC:USDC', -0.5, -0.5, exchange)
        assert result is True
    run_with_test_db(create_test_db(), _test)
