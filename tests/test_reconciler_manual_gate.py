import pytest
import uuid
import sqlite3
from unittest.mock import MagicMock, patch
from engine import database
from engine.reconciler import StateReconciler, BotState, ExchangePosition, ExchangeOrder

@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_reconciler_{db_id}?mode=memory&cache=shared'
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


def _seed_bot(conn, bot_id, name, pair, direction, total_invested=0.0, open_qty=0.0, status='IN TRADE'):
    conn.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (bot_id, name, pair, pair.split(':')[0].replace('/', '').upper(), direction, status),
    )
    conn.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, total_invested, avg_entry_price, current_step, entry_confirmed, cycle_phase, wipe_wall_ts, position_side) "
        "VALUES (?, 1, ?, ?, ?, 1, 1, 'ACTIVE', 0, ?)",
        (bot_id, open_qty, total_invested, 1.0 if total_invested > 0 else 0.0, direction),
    )
    conn.commit()


def test_global_flatten_skips_require_manual_proof(memory_db):
    # Seed a bot that is gated with REQUIRE_MANUAL_PROOF
    _seed_bot(
        memory_db, 
        bot_id=10017, 
        name="xrp long", 
        pair="XRP/USDC:USDC", 
        direction="LONG", 
        total_invested=6.8177, 
        open_qty=5.1, 
        status="REQUIRE_MANUAL_PROOF"
    )

    # Initialize StateReconciler with a mock exchange
    mock_exchange = MagicMock()
    mock_exchange.fetch_positions.return_value = [] # Physical position is 0
    exchanges = {'future': mock_exchange}
    reconciler = StateReconciler(exchanges)

    # Prepare input for resolve_net_mismatch
    bot_states = reconciler.get_bot_states()
    
    # Assert bot_status is loaded correctly
    assert len(bot_states) == 1
    assert bot_states[0].bot_status == "REQUIRE_MANUAL_PROOF"
    assert bot_states[0].in_trade is True

    # Run resolve_net_mismatch through mock of safe_wipe_bot
    with patch('engine.reconciler.safe_wipe_bot') as mock_wipe:
        reconciler.resolve_net_mismatch(bot_states, positions={}, all_orders={})
        
        # Verify safe_wipe_bot was NOT called on this bot
        mock_wipe.assert_not_called()


def test_adopt_from_physical_positions_skips_when_in_sync(memory_db):
    # Seed a bot that has the correct open_qty matching exchange position
    _seed_bot(
        memory_db, 
        bot_id=10017, 
        name="xrp long", 
        pair="XRP/USDC:USDC", 
        direction="LONG", 
        total_invested=6.8177, 
        open_qty=5.1, 
        status="IN TRADE"
    )
    # Insert the manual restore order
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, cycle_id, position_side) "
        "VALUES (10017, 1, 'entry', 'RESTORE_10017_1', 1.3368, 5.1, 5.1, 'filled', 1, 'LONG')"
    )
    memory_db.commit()

    # Mock exchange returns 5.1 contracts physical position
    mock_exchange = MagicMock()
    mock_exchange.fetch_positions.return_value = [{'symbol': 'XRP/USDC:USDC', 'contracts': 5.1, 'side': 'long', 'entryPrice': 1.3368}]
    # Mock exchange returns a large recent fill from a different cycle (e.g. cycle 60)
    mock_exchange.fetch_my_trades.return_value = [
        {'id': 63258796, 'order': '116621111', 'orderId': 116621111, 'symbol': 'XRPUSDC', 'side': 'buy', 'price': 1.3368, 'amount': 17.2, 'clientOrderId': 'CQB_10017_GRID_60_3', 'timestamp': 1779824066763}
    ]
    
    exchanges = {'future': mock_exchange}
    reconciler = StateReconciler(exchanges)

    # Run adopt_from_physical_positions
    results = reconciler.adopt_from_physical_positions()

    # The result for bot 10017 should not show any adopted fills or errors since it is already in sync
    bot_status = database.get_bot_status(10017)
    assert bot_status['status'] == "IN TRADE" # Status was NOT set to REQUIRE_MANUAL_PROOF
    # Verify no orders were moved or modified to cycle 1 from cycle 60
    db_row = memory_db.execute("SELECT cycle_id FROM bot_orders WHERE order_id = '116621111'").fetchone()
    assert db_row is None # The 17.2 order was not adopted/aligned


def test_global_flatten_skips_gated_bots(memory_db):
    # Seed a bot that is gated with REQUIRE_MANUAL_PROOF
    _seed_bot(
        memory_db, 
        bot_id=10018, 
        name="sol bot", 
        pair="SOL/USDC:USDC", 
        direction="LONG", 
        total_invested=50.0, 
        open_qty=50.0, 
        status="REQUIRE_MANUAL_PROOF"
    )

    # Initialize StateReconciler with a mock exchange
    mock_exchange = MagicMock()
    # Return a physical position of LONG 40.0 contracts
    mock_exchange.fetch_positions.return_value = [
        {'symbol': 'SOL/USDC:USDC', 'contracts': 40.0, 'side': 'long', 'entryPrice': 1.0}
    ]
    mock_exchange.get_last_price.return_value = 1.0
    mock_exchange.fetch_open_orders.return_value = []
    
    exchanges = {'future': mock_exchange}
    reconciler = StateReconciler(exchanges)

    # Prepare input for resolve_net_mismatch
    bot_states = reconciler.get_bot_states()
    
    # Assert bot_status is loaded correctly
    assert len(bot_states) == 1
    assert bot_states[0].bot_status == "REQUIRE_MANUAL_PROOF"
    assert bot_states[0].in_trade is True

    # Patch reconstruct methods and check if flag_bot_manual_proof is called
    with patch.object(reconciler, 'reconstruct_offline_fills') as mock_recon, \
         patch.object(reconciler, '_align_memory_to_ledger') as mock_align, \
         patch.object(mock_exchange, 'create_order') as mock_create_order, \
         patch('engine.reconciler.flag_bot_manual_proof') as mock_flag:
        
        reconciler.resolve_net_mismatch(bot_states, positions={'SOL/USDC:USDC': [
            ExchangePosition(symbol='SOL/USDC:USDC', side='LONG', size=40.0, entry_price=1.0, mark_price=1.0, unrealized_pnl=0.0)
        ]}, all_orders={})
        
        # Verify flag_bot_manual_proof was called on this bot
        mock_flag.assert_called_once_with(10018, reason='Global flatten blocked — bot is gated')
        # Verify flatten order was NOT placed
        mock_create_order.assert_not_called()


def test_b4_forensic_proof_prevents_flatten(memory_db):
    # Seed an active bot (not gated)
    _seed_bot(
        memory_db, 
        bot_id=10018, 
        name="sol bot", 
        pair="SOL/USDC:USDC", 
        direction="LONG", 
        total_invested=50.0, 
        open_qty=50.0, 
        status="IN TRADE"
    )

    # Initialize StateReconciler with a mock exchange
    mock_exchange = MagicMock()
    # Return a physical position of LONG 40.0 contracts
    mock_exchange.fetch_positions.return_value = [
        {'symbol': 'SOL/USDC:USDC', 'contracts': 40.0, 'side': 'long', 'entryPrice': 1.0}
    ]
    mock_exchange.get_last_price.return_value = 1.0
    mock_exchange.fetch_open_orders.return_value = []
    
    exchanges = {'future': mock_exchange}
    reconciler = StateReconciler(exchanges)

    # Prepare input for resolve_net_mismatch
    bot_states = reconciler.get_bot_states()
    
    # Assert bot_status is loaded correctly
    assert len(bot_states) == 1
    assert bot_states[0].bot_status == "IN TRADE"
    assert bot_states[0].in_trade is True

    # Prepare a mock TP order to trigger B.4 proof
    mock_tp_order = ExchangeOrder(
        order_id='ord_123',
        symbol='SOL/USDC:USDC',
        side='sell',
        order_type='limit',
        price=1.0,
        amount=50.0,
        status='open',
        client_order_id='CQB_10018_TP_123'
    )
    all_orders = {'SOLUSDC': [mock_tp_order]}


    # Patch reconstruct methods and check create_order behavior
    with patch.object(reconciler, 'reconstruct_offline_fills') as mock_recon, \
         patch.object(reconciler, '_align_memory_to_ledger') as mock_align, \
         patch.object(mock_exchange, 'create_order') as mock_create_order:
        
        reconciler.resolve_net_mismatch(bot_states, positions={'SOL/USDC:USDC': [
            ExchangePosition(symbol='SOL/USDC:USDC', side='LONG', size=40.0, entry_price=1.0, mark_price=1.0, unrealized_pnl=0.0)
        ]}, all_orders=all_orders)
        
        # Verify flatten order was NOT placed because b4_ran prevented it
        mock_create_order.assert_not_called()



