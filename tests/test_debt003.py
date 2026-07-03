import pytest
import sqlite3
import time
from engine.database import get_connection, reset_bot_after_tp
from engine.ledger import credit_fill, seal_trade_state, register_tp_cascade, handle_tp_completion

def setup_test_bot():
    conn = get_connection()
    cursor = conn.cursor()
    # Create a test bot
    cursor.execute("INSERT OR REPLACE INTO bots (id, name, pair, direction, is_active, bot_type) VALUES (99999, 'DEBT-003 Bot', 'BTC/USDC:USDC', 'LONG', 1, 'standard')")
    # Setup its trade state
    cursor.execute("INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, total_invested, avg_entry_price, target_tp_price, current_step, entry_confirmed, position_side) VALUES (99999, 1, 0.0, 0.0, 0.0, 0.0, 0, 0, 'LONG')")
    # Add a mock order that we can fill
    cursor.execute(
        "INSERT OR REPLACE INTO bot_orders (id, bot_id, step, order_type, order_id, price, amount, status, created_at, client_order_id, cycle_id, filled_amount) "
        "VALUES (999991, 99999, 1, 'grid', 'mock_order_999991', 50000.0, 0.01, 'open', ?, 'CQB_99999_GRID_1', 1, 0.0)",
        (int(time.time()),)
    )
    cursor.execute(
        "INSERT OR REPLACE INTO bot_orders (id, bot_id, step, order_type, order_id, price, amount, status, created_at, client_order_id, cycle_id, filled_amount) "
        "VALUES (999992, 99999, 2, 'tp', 'mock_order_999992', 51000.0, 0.01, 'open', ?, 'CQB_99999_TP_1', 1, 0.0)",
        (int(time.time()),)
    )
    conn.commit()
    conn.close()

def teardown_test_bot():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM fill_claims WHERE bot_id = 99999")
    cursor.execute("DELETE FROM bot_orders WHERE bot_id = 99999")
    cursor.execute("DELETE FROM trades WHERE bot_id = 99999")
    cursor.execute("DELETE FROM bots WHERE id = 99999")
    conn.commit()
    conn.close()

def test_credit_fill_while_gated():
    setup_test_bot()
    try:
        conn = get_connection()
        
        # Set bot status to REQUIRE_MANUAL_PROOF
        conn.execute("UPDATE bots SET status = 'REQUIRE_MANUAL_PROOF' WHERE id = 99999")
        conn.commit()
        
        # 1. Process grid fill - should credit and update open_qty
        ok = credit_fill(
            bot_id=99999,
            order_id='mock_order_999991',
            cumulative_qty=0.01,
            avg_price=50000.0,
            order_type='grid',
            caller='test'
        )
        assert ok is True
        
        # Verify order in DB is filled (gated status does NOT suppress cascade/fill recording)
        row_order = conn.execute("SELECT status, filled_amount FROM bot_orders WHERE id = 999991").fetchone()
        assert row_order[0] == 'filled'
        assert float(row_order[1]) == 0.01
        
        # Verify trades.open_qty accumulator updated
        row_trade = conn.execute("SELECT open_qty, total_invested, avg_entry_price FROM trades WHERE bot_id = 99999").fetchone()
        assert float(row_trade[0]) == 0.01
        
        # 2. Reseal trade state - status must remain REQUIRE_MANUAL_PROOF because open_qty > tolerance
        seal_trade_state(99999, force_recompute=True)
        row_bot = conn.execute("SELECT status FROM bots WHERE id = 99999").fetchone()
        assert row_bot[0] == 'REQUIRE_MANUAL_PROOF'
        
        # 3. Process TP fill - should credit normally (status = 'filled')
        ok_tp = credit_fill(
            bot_id=99999,
            order_id='mock_order_999992',
            cumulative_qty=0.01,
            avg_price=51000.0,
            order_type='tp',
            caller='test'
        )
        assert ok_tp is True
        
        # Verify order status is filled
        row_tp = conn.execute("SELECT status, filled_amount FROM bot_orders WHERE id = 999992").fetchone()
        assert row_tp[0] == 'filled'
        
        # Call register_tp_cascade - should register it
        from engine.ledger import drain_tp_cascade
        drain_tp_cascade()  # clear registry first
        register_tp_cascade(99999, 'BTC/USDC:USDC', 51000.0, int(time.time()))
        
        # Call handle_tp_completion directly - should succeed and clear the gate
        class MockExchange:
            def fetch_positions(self):
                # Position is now flat (contracts = 0)
                return [{'symbol': 'BTCUSDC', 'contracts': 0.0}]
            def cancel_order(self, *args, **kwargs):
                return True
            def fetch_open_orders(self, *args, **kwargs):
                return []
        
        success = handle_tp_completion(
            bot_id=99999,
            exit_price=51000.0,
            pair='BTC/USDC:USDC',
            exchange=MockExchange()
        )
        assert success is True
        
        # Bot status must be reset to Scanning because position is flat
        row_bot_final = conn.execute("SELECT status FROM bots WHERE id = 99999").fetchone()
        assert row_bot_final[0] == 'Scanning'
        
    finally:
        teardown_test_bot()
