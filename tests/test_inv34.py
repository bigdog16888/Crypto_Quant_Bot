"""Tests for INV-34: safety gate bypass for live fills and TP completion resets."""
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
from engine.ledger import credit_fill, seal_trade_state, register_tp_cascade, handle_tp_completion
from engine.ws_event_handlers import start_db_worker, stop_db_worker


@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_inv34_{db_id}?mode=memory&cache=shared'
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


def test_inv34_fill_recorded_when_gated(memory_db):
    bot_id = 123
    pair = 'SOL/USDC:USDC'

    # Setup database state
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, status, normalized_pair) "
        "VALUES (?, 'sol bot', ?, 'LONG', 1, 'REQUIRE_MANUAL_PROOF', 'SOLUSDC')",
        (bot_id, pair)
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price, cycle_phase, position_side) "
        "VALUES (?, 1, 1, 0.0, 0.0, 0.0, 'ACTIVE', 'LONG')",
        (bot_id,)
    )
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, created_at, updated_at) "
        "VALUES (?, 'entry', 'CQB_123_ENTRY_1_12345', 100.0, 1.0, 0.0, 'open', 1, 0, 0)",
        (bot_id,)
    )
    memory_db.commit()

    # Call credit_fill. Since status is REQUIRE_MANUAL_PROOF, it must NOT block recording of fill or updating trades.open_qty
    credited = credit_fill(
        bot_id=bot_id,
        order_id='CQB_123_ENTRY_1_12345',
        cumulative_qty=1.0,
        avg_price=100.0,
        order_type='entry',
        is_cumulative=True
    )
    assert credited is True

    # Assert bot_orders.filled_amount updated and status changed to filled
    row_order = memory_db.execute("SELECT filled_amount, status FROM bot_orders WHERE bot_id = ?", (bot_id,)).fetchone()
    assert float(row_order[0]) == 1.0
    assert row_order[1] == 'filled'

    # Assert open_qty updated in trades table
    row_trade = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id = ?", (bot_id,)).fetchone()
    assert float(row_trade[0]) == 1.0

    # Assert bot status still 'REQUIRE_MANUAL_PROOF' (not cleared during normal entry fill)
    row_bot = memory_db.execute("SELECT status FROM bots WHERE id = ?", (bot_id,)).fetchone()
    assert row_bot[0] == 'REQUIRE_MANUAL_PROOF'


def test_inv34_tp_cascade_runs_when_gated(memory_db):
    bot_id = 456
    pair = 'SOL/USDC:USDC'

    # Setup database state
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, status, normalized_pair) "
        "VALUES (?, 'sol bot 2', ?, 'LONG', 1, 'REQUIRE_MANUAL_PROOF', 'SOLUSDC')",
        (bot_id, pair)
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price, cycle_phase, position_side) "
        "VALUES (?, 1, 2, 1.0, 100.0, 100.0, 'ACTIVE', 'LONG')",
        (bot_id,)
    )
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, created_at, updated_at) "
        "VALUES (?, 'tp', 'CQB_456_TP_1_12345', 102.0, 1.0, 0.0, 'open', 1, 0, 0)",
        (bot_id,)
    )
    memory_db.commit()

    # Call register_tp_cascade (bypassing the REQUIRE_MANUAL_PROOF gate)
    from engine.ledger import drain_tp_cascade
    drain_tp_cascade() # Clear registry
    register_tp_cascade(bot_id, pair, 102.0, exit_fill_ts=1700000000)

    # Drain TP cascades to verify register_tp_cascade was successfully invoked (not skipped)
    cascades = drain_tp_cascade()
    assert len(cascades) == 1
    item = list(cascades)[0]
    assert item[0] == bot_id
    assert item[1] == pair

    # Setup mock exchange for handle_tp_completion
    mock_exchange = MagicMock()
    # Physical position is now flat (contracts = 0)
    mock_exchange.fetch_positions.return_value = [{'symbol': 'SOLUSDC', 'contracts': 0.0}]
    mock_exchange.cancel_all_orders.return_value = True

    # Call handle_tp_completion. It should run to completion and reset the bot to Scanning
    res = handle_tp_completion(
        bot_id=bot_id,
        pair=pair,
        exit_price=102.0,
        exit_fill_ts=item[3],
        exchange=mock_exchange
    )
    assert res is True

    # Bot should be reset to Scanning (since TP is flat-close and gate is resolved)
    row_bot = memory_db.execute("SELECT status FROM bots WHERE id = ?", (bot_id,)).fetchone()
    assert row_bot[0] == 'Scanning'


def test_inv34_hedge_signal_blocked_when_gated(memory_db):
    bot_id = 789
    child_id = 790
    pair = 'SOL/USDC:USDC'

    # Setup database state
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, status, normalized_pair, hedge_child_bot_id, hedge_trigger_step) "
        "VALUES (?, 'parent bot', ?, 'LONG', 1, 'REQUIRE_MANUAL_PROOF', 'SOLUSDC', ?, 3)",
        (bot_id, pair, child_id)
    )
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, status, normalized_pair) "
        "VALUES (?, 'child bot', ?, 'SHORT', 1, 'hedge_standby', 'SOLUSDC')",
        (child_id, pair)
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price, cycle_phase, position_side) "
        "VALUES (?, 1, 2, 2.0, 200.0, 100.0, 'ACTIVE', 'LONG')",
        (bot_id,)
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price, cycle_phase, position_side) "
        "VALUES (?, 1, 0, 0.0, 0.0, 0.0, 'IDLE', 'SHORT')",
        (child_id,)
    )
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step, created_at, updated_at) "
        "VALUES (?, 'grid', 'CQB_789_GRID_1_12345', 90.0, 1.0, 0.0, 'open', 1, 3, 0, 0)",
        (bot_id,)
    )
    memory_db.commit()

    # Call credit_fill for step 3 entry order.
    # While bot is gated, child signal should NOT trigger.
    mock_exchange = MagicMock()
    with patch('engine.runner.BotRunner.get_instance') as mock_get_instance:
        mock_runner = MagicMock()
        mock_runner.get_thread_exchange.return_value = mock_exchange
        mock_get_instance.return_value = mock_runner

        credited = credit_fill(
            bot_id=bot_id,
            order_id='CQB_789_GRID_1_12345',
            cumulative_qty=1.0,
            avg_price=90.0,
            order_type='grid',
            is_cumulative=True,
            exchange=mock_exchange
        )
        assert credited is True

        # Assert no order was placed on the child bot (it did not get triggered)
        mock_exchange.create_order_with_receipt.assert_not_called()


def test_inv34_gate_cleared_after_tp_when_flat(memory_db):
    bot_id = 111
    pair = 'SOL/USDC:USDC'

    # Setup database state
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, status, normalized_pair) "
        "VALUES (?, 'sol bot 3', ?, 'LONG', 1, 'REQUIRE_MANUAL_PROOF', 'SOLUSDC')",
        (bot_id, pair)
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price, cycle_phase, position_side) "
        "VALUES (?, 1, 2, 0.0, 0.0, 0.0, 'ACTIVE', 'LONG')",
        (bot_id,)
    )
    memory_db.commit()

    # Call seal_trade_state with flat position (total_invested=0, avg=0, open_qty=0)
    # The gate status 'REQUIRE_MANUAL_PROOF' should be automatically cleared to 'Scanning'.
    res = seal_trade_state(bot_id)
    assert res.get('status') == 'Scanning'

    row_bot = memory_db.execute("SELECT status FROM bots WHERE id = ?", (bot_id,)).fetchone()
    assert row_bot[0] == 'Scanning'


def test_inv34_gate_preserved_after_entry_fill(memory_db):
    bot_id = 222
    pair = 'SOL/USDC:USDC'

    # Setup database state
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, status, normalized_pair) "
        "VALUES (?, 'sol bot 4', ?, 'LONG', 1, 'REQUIRE_MANUAL_PROOF', 'SOLUSDC')",
        (bot_id, pair)
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price, cycle_phase, position_side) "
        "VALUES (?, 1, 1, 1.0, 100.0, 100.0, 'ACTIVE', 'LONG')",
        (bot_id,)
    )
    # Put a filled entry in bot_orders so seal_trade_state recomputes main_open_qty > 0
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, created_at, updated_at) "
        "VALUES (?, 'entry', 'CQB_222_ENTRY_1_12345', 100.0, 1.0, 1.0, 'filled', 1, 0, 0)",
        (bot_id,)
    )
    memory_db.commit()

    # Call seal_trade_state. Since position is still active (open_qty > 0), the gate status 'REQUIRE_MANUAL_PROOF' must be preserved.
    res = seal_trade_state(bot_id)
    assert res.get('qty') == 1.0
    
    row_bot = memory_db.execute("SELECT status FROM bots WHERE id = ?", (bot_id,)).fetchone()
    assert row_bot[0] == 'REQUIRE_MANUAL_PROOF'
