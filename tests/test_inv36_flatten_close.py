"""Tests for INV-36: FLATTEN_CLOSE and CLOSE order handling, reconciler integration, and gating bypass."""
import pytest
import sqlite3
import time
import uuid
import sys
import os
from unittest.mock import MagicMock, patch

# Add root directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import database
from engine.ledger import credit_fill, seal_trade_state, handle_tp_completion
from engine.ws_event_handlers import start_db_worker, stop_db_worker
from engine.oneway_netting import gate_oneway_opposite_entry


@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_inv36_{db_id}?mode=memory&cache=shared'
    persistent_conn = orig_connect(shared_uri, uri=True)

    def mock_connect(db_path, *args, **kwargs):
        kwargs['uri'] = True
        return orig_connect(shared_uri, *args, **kwargs)

    sqlite3.connect = mock_connect
    if hasattr(database._local, 'connection'):
        database._local.connection = None

    database.DB_PATH = shared_uri
    database.init_db()
    conn = database.get_connection()
    conn.commit()
    yield conn

    persistent_conn.close()
    sqlite3.connect = orig_connect
    database.backup_database = orig_backup
    database.DB_PATH = orig_db_path
    if hasattr(database._local, 'connection'):
        database._local.connection = None


def test_inv36_credit_fill_close_and_flatten_close(memory_db):
    bot_id = 9991
    pair = 'SOL/USDC:USDC'

    # Setup database state
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, status, normalized_pair) "
        "VALUES (?, 'sol bot close', ?, 'LONG', 1, 'ACTIVE', 'SOLUSDC')",
        (bot_id, pair)
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price, cycle_phase, position_side) "
        "VALUES (?, 1, 1, 1.0, 100.0, 100.0, 'ACTIVE', 'LONG')",
        (bot_id,)
    )
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, created_at, updated_at) "
        "VALUES (?, 'tp', 'CQB_9991_CLOSE_1_12345', 105.0, 1.0, 0.0, 'open', 1, 0, 0)",
        (bot_id,)
    )
    memory_db.commit()

    # Credit fill for CQB_9991_CLOSE_1_12345 (ends with _CLOSE_)
    credited = credit_fill(
        bot_id=bot_id,
        order_id='CQB_9991_CLOSE_1_12345',
        cumulative_qty=1.0,
        avg_price=105.0,
        order_type='tp',
        is_cumulative=True
    )
    assert credited is True

    # Check bot_orders is filled
    row_order = memory_db.execute(
        "SELECT filled_amount, status FROM bot_orders WHERE client_order_id = 'CQB_9991_CLOSE_1_12345'"
    ).fetchone()
    assert float(row_order[0]) == 1.0
    assert row_order[1] == 'filled'

    # Check open_qty is now 0 in trades table
    row_trade = memory_db.execute(
        "SELECT open_qty, cycle_phase FROM trades WHERE bot_id = ?", (bot_id,)
    ).fetchone()
    assert float(row_trade[0]) == 0.0


def test_inv36_credit_fill_flatten_close(memory_db):
    bot_id = 9992
    pair = 'SOL/USDC:USDC'

    # Setup database state
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, status, normalized_pair) "
        "VALUES (?, 'sol bot flatten', ?, 'LONG', 1, 'ACTIVE', 'SOLUSDC')",
        (bot_id, pair)
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price, cycle_phase, position_side) "
        "VALUES (?, 1, 1, 2.0, 200.0, 100.0, 'ACTIVE', 'LONG')",
        (bot_id,)
    )
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, created_at, updated_at) "
        "VALUES (?, 'tp', 'CQB_9992_FLATTEN_CLOSE_1_12345', 105.0, 2.0, 0.0, 'open', 1, 0, 0)",
        (bot_id,)
    )
    memory_db.commit()

    # Credit fill for CQB_9992_FLATTEN_CLOSE_1_12345
    credited = credit_fill(
        bot_id=bot_id,
        order_id='CQB_9992_FLATTEN_CLOSE_1_12345',
        cumulative_qty=2.0,
        avg_price=105.0,
        order_type='tp',
        is_cumulative=True
    )
    assert credited is True

    # Check open_qty is now 0
    row_trade = memory_db.execute(
        "SELECT open_qty FROM trades WHERE bot_id = ?", (bot_id,)
    ).fetchone()
    assert float(row_trade[0]) == 0.0


def test_inv36_reconciler_pending_reduce_close(memory_db):
    from engine.reconciler import StateReconciler, BotState, ExchangePosition, ExchangeOrder
    bot_id = 9993
    pair = 'SOL/USDC:USDC'

    # Setup database state
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, status, normalized_pair) "
        "VALUES (?, 'reconciler test bot', ?, 'LONG', 1, 'ACTIVE', 'SOLUSDC')",
        (bot_id, pair)
    )
    # Target state: open_qty = 0 (we think we TP'd), but physical = 1.0 (exchange not filled/still open)
    # The reconciler will see: virtual = 0.0, physical = 1.0. Gap = 1.0.
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price, cycle_phase, position_side) "
        "VALUES (?, 1, 1, 0.0, 0.0, 0.0, 'ACTIVE', 'LONG')",
        (bot_id,)
    )
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, created_at, updated_at) "
        "VALUES (?, 'tp', 'CQB_9993_CLOSE_1_12345', 105.0, 1.0, 0.0, 'open', 1, 0, 0)",
        (bot_id,)
    )
    memory_db.commit()

    # Define inputs for resolve_net_mismatch
    bot_states = [
        BotState(
            bot_id=bot_id,
            name='reconciler test bot',
            pair=pair,
            direction='LONG',
            is_active=True,
            in_trade=True,
            total_invested=0.0,
            avg_entry_price=100.0,
            target_tp_price=105.0,
            current_step=1,
            basket_start_time=0,
            entry_order_id=None,
            tp_order_id=None,
            has_confirmed_entry=True,
            cycle_id=1,
            cycle_start_time=0,
            base_size=100.0,
            martingale_multiplier=1.0,
            bot_status='ACTIVE',
            cycle_phase='ACTIVE'
        )
    ]
    
    # 1.0 contracts LONG physical position on the exchange
    positions = {
        'SOLUSDC': [
            ExchangePosition(
                symbol='SOLUSDC',
                side='LONG',
                size=1.0,
                entry_price=100.0,
                mark_price=100.0,
                unrealized_pnl=0.0
            )
        ]
    }
    
    # Sell limit order on the exchange matching our CLOSE clientOrderId
    all_orders = {
        'SOLUSDC': [
            ExchangeOrder(
                order_id='12345',
                symbol='SOLUSDC',
                side='sell',
                order_type='limit',
                price=105.0,
                amount=1.0,
                status='open',
                client_order_id='CQB_9993_CLOSE_1_12345'
            )
        ]
    }

    mock_exchange = MagicMock()
    # Stub fetch_positions and fetch_open_orders
    mock_exchange.fetch_positions.return_value = [{'symbol': 'SOLUSDC', 'contracts': 1.0, 'side': 'LONG'}]
    mock_exchange.fetch_open_orders.return_value = [{
        'symbol': 'SOLUSDC',
        'side': 'sell',
        'amount': 1.0,
        'clientOrderId': 'CQB_9993_CLOSE_1_12345',
        'price': 105.0,
        'id': '12345'
    }]
    mock_exchange.create_order = MagicMock()

    # Construct StateReconciler
    reconciler = StateReconciler(exchanges={'future': mock_exchange})

    # Call resolve_net_mismatch
    with patch.object(reconciler, '_align_memory_to_ledger'):
        results = reconciler.resolve_net_mismatch(bot_states, positions, all_orders)

    # Verify:
    # 1. create_order was NOT called (meaning no drift correction order was placed)
    # 2. Results does not contain REQUIRE_MANUAL actions (it was healed/processed cleanly)
    mock_exchange.create_order.assert_not_called()
    for r in results:
        assert r.requires_manual_intervention is False


def test_inv36_parity_gate_close_bypass(memory_db):
    # Test that check_one_way_gate returns correct status and doesn't block if there is a flat state or netting order.
    # Let's test check_one_way_gate.
    # Let's setup two bots on the same pair: one LONG (bot A) and one SHORT (bot B).
    bot_a = 9994
    bot_b = 9995
    pair = 'SOL/USDC:USDC'

    # Bot A (LONG) is Scanning, has open_qty = 0.0
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, status, normalized_pair) "
        "VALUES (?, 'bot A', ?, 'LONG', 1, 'Scanning', 'SOLUSDC')",
        (bot_a, pair)
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price, cycle_phase, position_side) "
        "VALUES (?, 1, 0, 0.0, 0.0, 0.0, 'IDLE', 'LONG')",
        (bot_a,)
    )

    # Bot B (SHORT) is ACTIVE, has open_qty = 2.0
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, status, normalized_pair) "
        "VALUES (?, 'bot B', ?, 'SHORT', 1, 'ACTIVE', 'SOLUSDC')",
        (bot_b, pair)
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price, cycle_phase, position_side) "
        "VALUES (?, 1, 1, 2.0, 200.0, 100.0, 'ACTIVE', 'SHORT')",
        (bot_b,)
    )
    memory_db.commit()

    # Call gate_oneway_opposite_entry for bot B (SHORT) to open a new grid.
    # Since bot A is LONG but status is 'Scanning' and open_qty = 0, the gate should return True (allowed).
    allowed, msg = gate_oneway_opposite_entry(bot_b, pair, 'SHORT')
    assert allowed is True
