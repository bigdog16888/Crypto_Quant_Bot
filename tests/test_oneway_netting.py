"""One-way cross-bot netting tests."""
import pytest
import uuid
import sqlite3
from unittest.mock import MagicMock, call, patch
from engine import database
from engine.oneway_netting import (
    gate_oneway_opposite_entry,
    apply_oneway_entry_cross_reduction,
    get_pair_open_qty_net,
)


@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_oneway_{db_id}?mode=memory&cache=shared'
    persistent_conn = orig_connect(shared_uri, uri=True)

    def mock_connect(db_path, *args, **kwargs):
        kwargs['uri'] = True
        return orig_connect(shared_uri, *args, **kwargs)

    sqlite3.connect = mock_connect
    if hasattr(database._local, 'connection'):
        database._local.connection = None

    database.DB_PATH = shared_uri
    database.init_db()
    yield database.get_connection()
    sqlite3.connect = orig_connect
    database.DB_PATH = orig_db_path
    database.backup_database = orig_backup
    persistent_conn.close()


def _seed_bot(conn, bot_id, pair, direction, open_qty=0.0, cycle=1):
    conn.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (?, ?, ?, ?, ?, 1, 'IN TRADE')",
        (bot_id, f'bot_{bot_id}', pair, 'BTCUSDC', direction),
    )
    conn.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, wipe_wall_ts, position_side) "
        "VALUES (?, ?, ?, 0, ?)",
        (bot_id, cycle, open_qty, direction),
    )
    conn.commit()


def test_gate_blocks_opposite_entry(memory_db):
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.008)
    _seed_bot(memory_db, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.0)
    
    # 1. Fresh entry bot (step 0, total_invested=0) is NOT blocked
    ok, reason = gate_oneway_opposite_entry(10022, 'BTC/USDC:USDC', 'SHORT')
    assert ok
    assert reason == ''

    # 2. Bot already in trade (step 1, total_invested > 0) IS blocked
    memory_db.execute(
        "UPDATE trades SET current_step = 1, total_invested = 100.0 WHERE bot_id = 10022"
    )
    memory_db.commit()
    ok, reason = gate_oneway_opposite_entry(10022, 'BTC/USDC:USDC', 'SHORT')
    assert not ok
    assert 'LONG' in reason


def test_cross_reduction_on_short_entry(memory_db):
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.008, cycle=6)
    _seed_bot(memory_db, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.0, cycle=4)
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, "
        "filled_amount, status, cycle_id, step, position_side) "
        "VALUES (10016, 'entry', 'e6', 'CQB_10016_ENTRY_6_1', 1.0, 0.008, 0.008, 'filled', 6, 1, 'LONG')"
    )
    memory_db.commit()
    cut = apply_oneway_entry_cross_reduction(
        10022, 'BTC/USDC:USDC', 'SHORT', 0.002, '348125134', 76816.1
    )
    assert cut == pytest.approx(0.002)
    oq = memory_db.execute(
        "SELECT open_qty FROM trades WHERE bot_id=10016"
    ).fetchone()[0]
    assert oq == pytest.approx(0.006)
    assert get_pair_open_qty_net('BTC/USDC:USDC') == pytest.approx(0.006)


def test_get_pair_virtual_net_uses_open_qty(memory_db):
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.006)
    _seed_bot(memory_db, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.0)
    assert database.get_pair_virtual_net('BTC/USDC:USDC') == pytest.approx(0.006)


def test_reconcile_oneway_under_reporting_low(memory_db):
    from engine.oneway_netting import reconcile_oneway_pair_open_qty
    class MockExchange:
        def fetch_positions(self):
            return [{'symbol': 'BTC/USDC:USDC', 'contracts': 0.05, 'side': 'long'}]
    
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.0)
    
    ex = MockExchange()
    res = reconcile_oneway_pair_open_qty(ex, 'BTC/USDC:USDC')
    assert res is not None
    assert "under-report gap" in res
    
    oq = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id=10016").fetchone()[0]
    assert oq == 0.0


def test_reconcile_oneway_max_repair_qty_guard(memory_db):
    from engine.oneway_netting import reconcile_oneway_pair_open_qty
    from config.settings import config
    
    class MockExchange:
        def fetch_positions(self):
            return [{'symbol': 'BTC/USDC:USDC', 'contracts': 1.0, 'side': 'long'}]
            
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=100.0)
    
    original_max = getattr(config, 'MAX_OWAY_REPAIR_QTY', 50.0)
    config.MAX_OWAY_REPAIR_QTY = 5.0
    
    try:
        ex = MockExchange()
        reconcile_oneway_pair_open_qty(ex, 'BTC/USDC:USDC')
        
        oq = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id=10016").fetchone()[0]
        assert oq == 100.0
    finally:
        config.MAX_OWAY_REPAIR_QTY = original_max


def test_get_authoritative_close_qty_success():
    from engine.oneway_netting import get_authoritative_close_qty

    class MockExchange:
        def __init__(self, signed_net):
            self.signed_net = signed_net

        def fetch_positions(self):
            return [{'symbol': 'BTC/USDC:USDC', 'contracts': self.signed_net, 'net_qty': self.signed_net, 'side': 'long' if self.signed_net > 0 else 'short'}]

    # Case 1: Long direction, physical net positive, db_qty = 0.5, physical = 0.3
    ex = MockExchange(0.3)
    qty = get_authoritative_close_qty(ex, 'BTC/USDC:USDC', 'LONG', 0.5)
    assert qty == pytest.approx(0.3)

    # Case 2: Long direction, physical net positive, db_qty = 0.2, physical = 0.3
    qty = get_authoritative_close_qty(ex, 'BTC/USDC:USDC', 'LONG', 0.2)
    assert qty == pytest.approx(0.2)

    # Case 3: Long direction, physical net negative (short), db_qty = 0.5
    ex2 = MockExchange(-0.3)
    qty = get_authoritative_close_qty(ex2, 'BTC/USDC:USDC', 'LONG', 0.5)
    assert qty == pytest.approx(0.0)

    # Case 4: Short direction, physical net negative (short), db_qty = 0.5, physical = -0.3
    qty = get_authoritative_close_qty(ex2, 'BTC/USDC:USDC', 'SHORT', 0.5)
    assert qty == pytest.approx(0.3)

    # Case 5: Exchange fetch_positions fails (returns None)
    class MockExchangeFail:
        def fetch_positions(self):
            return None

    ex_fail = MockExchangeFail()
    qty = get_authoritative_close_qty(ex_fail, 'BTC/USDC:USDC', 'LONG', 0.5)
    assert qty == pytest.approx(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# INV-28A / INV-28B  Race condition tests (v4.0.1)
# ─────────────────────────────────────────────────────────────────────────────

def test_exact_3_second_race_scenario(memory_db):
    """
    Reproduce the June 10, 2026 BTC 0.002 orphan exactly.

    Timeline:
      t=0  short_btc resting TP: 0.044 BTC (open in bot_orders)
      t=4  long_btc_price entry fills 0.002 BTC
             → cross-reduction fires
             → INV-28A: must cancel short_btc's 0.044 TP immediately
             → INV-28B: if long_btc_price virtual zeroed AND physical net > 0, → pending_flatten
      t=7  (never happens now because Fix-A cancelled the stale TP)

    Assertions:
      - short_btc TP row is marked 'cancelled' with '[CROSS-REDUCE-CANCEL]' in notes
      - exchange.cancel_order was called with the correct exchange_order_id
      - short_btc open_qty reduced from 0.044 → 0.042
      - long_btc_price open_qty zeroed (0.002 entry fully netted)
    """
    # ── Seed short_btc (bot 20001, SHORT, open_qty=0.044) ──────────────────
    _seed_bot(memory_db, 20001, 'BTC/USDC:USDC', 'SHORT', open_qty=0.044, cycle=3)
    # Filled entry orders for short_btc (so seal_trade_state recomputes to 0.044)
    memory_db.execute(
        "INSERT INTO bot_orders "
        "(bot_id, order_type, order_id, client_order_id, "
        " price, amount, filled_amount, status, cycle_id, step, position_side) "
        "VALUES (20001, 'entry', 'SE_20001_1', 'CQB_20001_ENTRY_1', "
        "        106100.0, 0.044, 0.044, 'filled', 3, 1, 'SHORT')"
    )
    # Resting TP for short_btc at t=0 (simulates 23:46:48)
    memory_db.execute(
        "INSERT INTO bot_orders "
        "(bot_id, order_type, order_id, client_order_id, "
        " price, amount, filled_amount, status, cycle_id, step, position_side) "
        "VALUES (20001, 'tp', 'EX_TP_20001', 'CQB_20001_TP_1', "
        "        106000.0, 0.044, 0.0, 'open', 3, 1, 'SHORT')"
    )
    memory_db.commit()

    # ── Seed long_btc_price (bot 20002, LONG, open_qty=0.002) ──────────────
    _seed_bot(memory_db, 20002, 'BTC/USDC:USDC', 'LONG', open_qty=0.002, cycle=1)
    memory_db.execute(
        "INSERT INTO bot_orders "
        "(bot_id, order_type, order_id, client_order_id, price, amount, "
        " filled_amount, status, cycle_id, step, position_side) "
        "VALUES (20002, 'entry', 'E_20002_1', 'CQB_20002_ENTRY_1', "
        "        105800.0, 0.002, 0.002, 'filled', 1, 1, 'LONG')"
    )
    memory_db.commit()

    # ── Build a mock exchange ──────────────────────────────────────────────
    mock_exchange = MagicMock()
    # Physical net after cross-reduction: long_btc_price added 0.002 LONG,
    # but it netted against short_btc → net stays at 0.042 SHORT (negative).
    # For Fix-B: signed net should NOT be in LONG direction, so no pending_flatten.
    mock_exchange.fetch_positions.return_value = [
        {'symbol': 'BTC/USDC:USDC', 'contracts': -0.042,
         'net_qty': -0.042, 'side': 'short'}
    ]
    mock_exchange.cancel_order.return_value = {'id': 'EX_TP_20001', 'status': 'cancelled'}

    # ── Fire cross-reduction (simulates t=4, entry fill of long_btc_price) ─
    cut = apply_oneway_entry_cross_reduction(
        20002,           # filling_bot_id = long_btc_price
        'BTC/USDC:USDC',
        'LONG',
        0.002,           # delta
        'E_20002_1',
        105800.0,
        exchange=mock_exchange,
    )

    # ── Assertions ─────────────────────────────────────────────────────────
    assert cut == pytest.approx(0.002), "full delta must be cross-reduced"

    # Fix-A: short_btc TP must have been cancelled
    tp_row = memory_db.execute(
        "SELECT status, notes FROM bot_orders "
        "WHERE bot_id=20001 AND order_type='tp'"
    ).fetchone()
    assert tp_row is not None, "TP row must still exist"
    assert tp_row[0] == 'cancelled', f"TP must be cancelled, got: {tp_row[0]}"
    assert '[CROSS-REDUCE-CANCEL' in (tp_row[1] or ''), \
        f"TP notes must contain marker, got: {tp_row[1]}"

    # Fix-A: exchange.cancel_order called with the resting TP's exchange_order_id
    mock_exchange.cancel_order.assert_called_once()
    cancel_args = mock_exchange.cancel_order.call_args[0]
    assert cancel_args[0] == 'EX_TP_20001', \
        f"cancel_order called with wrong order id: {cancel_args[0]}"

    # short_btc open_qty reduced 0.044 → 0.042
    short_oq = memory_db.execute(
        "SELECT open_qty FROM trades WHERE bot_id=20001"
    ).fetchone()[0]
    assert short_oq == pytest.approx(0.042), \
        f"short_btc open_qty should be 0.042, got {short_oq}"

    # long_btc_price open_qty must be 0 (fully netted)
    long_oq = memory_db.execute(
        "SELECT open_qty FROM trades WHERE bot_id=20002"
    ).fetchone()[0]
    assert long_oq == pytest.approx(0.0), \
        f"long_btc_price open_qty should be 0, got {long_oq}"

    # Fix-B: physical net is SHORT → no LONG orphan → status must NOT be pending_flatten
    status = memory_db.execute(
        "SELECT status FROM bots WHERE id=20002"
    ).fetchone()[0]
    assert status != 'pending_flatten', \
        f"long_btc_price should not be pending_flatten when no orphan, got: {status}"


def test_inv28b_pending_flatten_when_physical_orphan_remains(memory_db):
    """
    INV-28B: If cross-reduction zeros the filling bot's virtual open_qty
    but the exchange still shows a physical position in that direction,
    the bot must be transitioned to pending_flatten.
    """
    # short_btc with open_qty=0.002 (will absorb the entire fill)
    _seed_bot(memory_db, 20010, 'BTC/USDC:USDC', 'SHORT', open_qty=0.002, cycle=1)

    # long_btc_price with open_qty=0.002 (will be fully netted → 0)
    _seed_bot(memory_db, 20011, 'BTC/USDC:USDC', 'LONG', open_qty=0.002, cycle=1)
    memory_db.execute(
        "INSERT INTO bot_orders "
        "(bot_id, order_type, order_id, client_order_id, price, amount, "
        " filled_amount, status, cycle_id, step, position_side) "
        "VALUES (20011, 'entry', 'E_20011_1', 'CQB_20011_ENTRY_1', "
        "        105800.0, 0.002, 0.002, 'filled', 1, 1, 'LONG')"
    )
    memory_db.commit()

    mock_exchange = MagicMock()
    # CRITICAL: physical net after reduction is LONG +0.002 → orphan detected
    mock_exchange.fetch_positions.return_value = [
        {'symbol': 'BTC/USDC:USDC', 'contracts': 0.002,
         'net_qty': 0.002, 'side': 'long'}
    ]
    mock_exchange.cancel_order.return_value = {}

    apply_oneway_entry_cross_reduction(
        20011,
        'BTC/USDC:USDC',
        'LONG',
        0.002,
        'E_20011_1',
        105800.0,
        exchange=mock_exchange,
    )

    # open_qty must be 0
    long_oq = memory_db.execute(
        "SELECT open_qty FROM trades WHERE bot_id=20011"
    ).fetchone()[0]
    assert long_oq == pytest.approx(0.0), f"Expected 0.0, got {long_oq}"

    # Fix-B: physical orphan detected → pending_flatten
    status = memory_db.execute(
        "SELECT status FROM bots WHERE id=20011"
    ).fetchone()[0]
    assert status == 'pending_flatten', \
        f"Expected pending_flatten when orphan remains, got: {status}"


def test_inv28a_no_cancel_if_no_resting_tp(memory_db):
    """
    INV-28A: If the sibling has no resting TP/dust_close order,
    cancel_order must NOT be called (avoid spurious exchange API errors).
    """
    _seed_bot(memory_db, 20020, 'BTC/USDC:USDC', 'SHORT', open_qty=0.044, cycle=3)
    # NO resting TP order for 20020

    _seed_bot(memory_db, 20021, 'BTC/USDC:USDC', 'LONG', open_qty=0.002, cycle=1)
    memory_db.execute(
        "INSERT INTO bot_orders "
        "(bot_id, order_type, order_id, client_order_id, price, amount, "
        " filled_amount, status, cycle_id, step, position_side) "
        "VALUES (20021, 'entry', 'E_20021_1', 'CQB_20021_ENTRY_1', "
        "        105800.0, 0.002, 0.002, 'filled', 1, 1, 'LONG')"
    )
    memory_db.commit()

    mock_exchange = MagicMock()
    mock_exchange.fetch_positions.return_value = [
        {'symbol': 'BTC/USDC:USDC', 'contracts': -0.042,
         'net_qty': -0.042, 'side': 'short'}
    ]

    apply_oneway_entry_cross_reduction(
        20021,
        'BTC/USDC:USDC',
        'LONG',
        0.002,
        'E_20021_1',
        105800.0,
        exchange=mock_exchange,
    )

    mock_exchange.cancel_order.assert_not_called()


def test_sync_pair_to_exchange_detects_drift(memory_db):
    from engine.oneway_netting import sync_pair_to_exchange
    import logging
    
    _seed_bot(memory_db, 30001, 'BTC/USDC:USDC', 'LONG', open_qty=0.05, cycle=1)
    _seed_bot(memory_db, 30002, 'BTC/USDC:USDC', 'SHORT', open_qty=0.02, cycle=1)
    
    # DB net should be 0.05 (LONG) + -0.02 (SHORT) = 0.03
    mock_exchange = MagicMock()
    mock_exchange.fetch_positions.return_value = [
        {'symbol': 'BTC/USDC:USDC', 'contracts': 0.04, 'net_qty': 0.04, 'side': 'long'}
    ]
    
    with patch('logging.Logger.warning') as mock_warning:
        sync_data = sync_pair_to_exchange('BTC/USDC:USDC', mock_exchange, memory_db)
        
        assert sync_data is not None
        assert sync_data['drift_detected'] is True
        assert sync_data['exchange_net'] == 0.04
        assert sync_data['db_sum_qty'] == 0.03
        assert sync_data['diff'] == 0.01
        
        # Check warning logged
        mock_warning.assert_called()
        any_drift_msg = any('[EXCHANGE-SYNC-DRIFT]' in args[0] for args, _ in mock_warning.call_args_list)
        assert any_drift_msg


def test_sync_pair_to_exchange_no_action_within_tolerance(memory_db):
    from engine.oneway_netting import sync_pair_to_exchange
    
    _seed_bot(memory_db, 30003, 'BTC/USDC:USDC', 'LONG', open_qty=0.05, cycle=1)
    
    mock_exchange = MagicMock()
    mock_exchange.fetch_positions.return_value = [
        {'symbol': 'BTC/USDC:USDC', 'contracts': 0.05, 'net_qty': 0.05, 'side': 'long'}
    ]
    
    with patch('logging.Logger.warning') as mock_warning:
        sync_data = sync_pair_to_exchange('BTC/USDC:USDC', mock_exchange, memory_db)
        
        assert sync_data is not None
        assert sync_data['drift_detected'] is False
        assert sync_data['diff'] == 0.0
        
        # No warning logged for drift
        any_drift_msg = any('[EXCHANGE-SYNC-DRIFT]' in args[0] for args, _ in mock_warning.call_args_list)
        assert not any_drift_msg


def test_sync_runs_at_startup_and_reconciler_cycle(memory_db):
    from engine.runner import BotRunner
    from engine.reconciler import StateReconciler
    
    mock_exchange = MagicMock()
    mock_exchange.fetch_positions.return_value = []
    
    # 1. Test reconciler call site
    reconciler = StateReconciler(exchanges={'future': mock_exchange})
    
    with patch('engine.oneway_netting.sync_pair_to_exchange') as mock_sync:
        _seed_bot(memory_db, 30005, 'BTC/USDC:USDC', 'LONG', open_qty=0.05, cycle=1)
        reconciler.reconcile_all()
        mock_sync.assert_called()
        
    # 2. Test runner startup call site
    runner = BotRunner()
    runner.exchanges = {'future': mock_exchange}
    runner._reconciler = StateReconciler(runner.exchanges)
    
    # Seed active bot in DB
    _seed_bot(memory_db, 30005, 'BTC/USDC:USDC', 'LONG', open_qty=0.05, cycle=1)
    
    with patch('engine.parity_gates.detect_and_repair_global_wipe', return_value={'triggered': False}), \
         patch('engine.oneway_netting.sync_pair_to_exchange') as mock_sync_startup, \
         patch('engine.runner.get_connection', return_value=memory_db), \
         patch('engine.runner.time.sleep'), \
         patch('engine.database.sync_trades_from_orders', return_value=0), \
         patch('engine.database.get_connection', return_value=memory_db), \
         patch('engine.database.audit_pair_ledger_vs_exchange', side_effect=ValueError("Abort early after startup sync check")):
        
        try:
            runner.startup_sync()
        except ValueError as e:
            # We expected the abort early exception
            assert str(e) == "Abort early after startup sync check"
            
        mock_sync_startup.assert_called()


