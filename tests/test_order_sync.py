"""
Unit Tests for v3.9.4 Stale Open Order Sync.
"""

import os
import sys
import time
import tempfile
import shutil
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import get_connection, init_db
from engine.bot_executor import sync_stale_open_orders

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_order_sync.db')
    database.DB_PATH = db_path
    database._local = database.threading.local()
    init_db()
    return d, db_path

def _insert_bot(conn, bot_id, name, pair, norm_pair, direction,
                status='IN TRADE', bot_type='standard', is_active=1):
    conn.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction,
                          status, bot_type, is_active,
                          rsi_limit, martingale_multiplier, base_size, strategy_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0, 0, 'Martingale')
    """, (bot_id, name, pair, norm_pair, direction, status, bot_type, is_active))
    conn.commit()

def _insert_trades(conn, bot_id, open_qty=0.0,
                   cycle_id=1, position_side='LONG', avg_entry_price=0.0, total_invested=0.0):
    conn.execute("""
        INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                            total_invested, avg_entry_price, current_step, entry_confirmed)
        VALUES (?, ?, ?, ?, ?, ?, 1, 1)
    """, (bot_id, open_qty, cycle_id, position_side, total_invested, avg_entry_price))
    conn.commit()

def _insert_bot_order(conn, bot_id, order_id, client_order_id, amount, filled_amount, status, created_at, order_type='grid', price=100.0, step=1, cycle_id=1):
    conn.execute("""
        INSERT INTO bot_orders (bot_id, order_id, client_order_id, amount, filled_amount, status, created_at, order_type, price, step, cycle_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (bot_id, order_id, client_order_id, amount, filled_amount, status, created_at, order_type, price, step, cycle_id))
    conn.commit()

class TestOrderSync(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        # Insert test bot
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG')
        _insert_trades(self.conn, 10018, open_qty=134.3, total_invested=200.0, cycle_id=88)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sync_detects_missed_fill(self):
        # 1. stale 'open' order, created 150s ago
        now = int(time.time())
        _insert_bot_order(
            self.conn, 10018, '12345', 'CQB_10018_GRID_88_1', 
            amount=185.2, filled_amount=0.0, status='open', 
            created_at=now - 150, order_type='grid', price=1.2, step=1, cycle_id=88
        )

        class MockExchange:
            def fetch_order(self, order_id, symbol):
                return {
                    'id': '12345',
                    'status': 'filled',
                    'filled': 185.2,
                    'amount': 185.2,
                    'average': 1.2,
                    'price': 1.2,
                    'clientOrderId': 'CQB_10018_GRID_88_1'
                }

        ex = MockExchange()
        
        with patch('engine.ledger.credit_fill') as mock_credit_fill, \
             patch('engine.ledger.seal_trade_state') as mock_seal:
            
            synced = sync_stale_open_orders(10018, ex, self.conn, max_age_seconds=120)
            
            self.assertEqual(synced, 1)
            mock_credit_fill.assert_called_once_with(
                bot_id=10018,
                order_id='12345',
                cumulative_qty=185.2,
                avg_price=1.2,
                order_type='grid',
                is_cumulative=True,
                sync_to_exchange=True
            )
            mock_seal.assert_called_once_with(10018)

            # Check that database was updated
            row = self.conn.execute("SELECT status, filled_amount, price FROM bot_orders WHERE order_id='12345'").fetchone()
            self.assertEqual(row[0], 'filled')
            self.assertEqual(row[1], 185.2)
            self.assertEqual(row[2], 1.2)

    def test_sync_detects_missed_cancel(self):
        # 2. stale 'open' order, created 150s ago, cancelled on exchange
        now = int(time.time())
        _insert_bot_order(
            self.conn, 10018, '12346', 'CQB_10018_GRID_88_2', 
            amount=185.2, filled_amount=0.0, status='open', 
            created_at=now - 150, order_type='grid', price=1.2, step=2, cycle_id=88
        )

        class MockExchange:
            def fetch_order(self, order_id, symbol):
                return {
                    'id': '12346',
                    'status': 'canceled',
                    'filled': 0.0,
                    'amount': 185.2,
                    'average': 0.0,
                    'price': 1.2,
                    'clientOrderId': 'CQB_10018_GRID_88_2'
                }

        ex = MockExchange()
        
        with patch('engine.ledger.credit_fill') as mock_credit_fill, \
             patch('engine.ledger.seal_trade_state') as mock_seal:
            
            synced = sync_stale_open_orders(10018, ex, self.conn, max_age_seconds=120)
            
            self.assertEqual(synced, 1)
            mock_credit_fill.assert_not_called()
            mock_seal.assert_not_called()

            # Check DB updated
            row = self.conn.execute("SELECT status FROM bot_orders WHERE order_id='12346'").fetchone()
            self.assertEqual(row[0], 'cancelled')

    def test_sync_skips_recent_orders(self):
        # 3. order created 30s ago -> not synced (max_age_seconds=120 guard)
        now = int(time.time())
        _insert_bot_order(
            self.conn, 10018, '12347', 'CQB_10018_GRID_88_3', 
            amount=185.2, filled_amount=0.0, status='open', 
            created_at=now - 30, order_type='grid', price=1.2, step=3, cycle_id=88
        )

        class MockExchange:
            def fetch_order(self, order_id, symbol):
                return {
                    'id': '12347',
                    'status': 'filled',
                    'filled': 185.2,
                    'amount': 185.2,
                    'average': 1.2,
                    'price': 1.2,
                    'clientOrderId': 'CQB_10018_GRID_88_3'
                }

        ex = MockExchange()
        
        with patch('engine.ledger.credit_fill') as mock_credit_fill, \
             patch('engine.ledger.seal_trade_state') as mock_seal:
            
            synced = sync_stale_open_orders(10018, ex, self.conn, max_age_seconds=120)
            
            self.assertEqual(synced, 0)
            mock_credit_fill.assert_not_called()
            mock_seal.assert_not_called()

            # DB remains unchanged
            row = self.conn.execute("SELECT status FROM bot_orders WHERE order_id='12347'").fetchone()
            self.assertEqual(row[0], 'open')

    def test_sync_handles_not_found(self):
        # 4. exchange raises NotFound -> marked cancelled
        now = int(time.time())
        _insert_bot_order(
            self.conn, 10018, '12348', 'CQB_10018_GRID_88_4', 
            amount=185.2, filled_amount=0.0, status='open', 
            created_at=now - 150, order_type='grid', price=1.2, step=4, cycle_id=88
        )

        class MockExchange:
            def fetch_order(self, order_id, symbol):
                raise Exception("Order not found or invalid symbol (NotFound, -2013)")

        ex = MockExchange()
        
        with patch('engine.ledger.credit_fill') as mock_credit_fill, \
             patch('engine.ledger.seal_trade_state') as mock_seal:
            
            synced = sync_stale_open_orders(10018, ex, self.conn, max_age_seconds=120)
            
            self.assertEqual(synced, 1)
            mock_credit_fill.assert_not_called()
            mock_seal.assert_not_called()

            # Check DB updated to cancelled
            row = self.conn.execute("SELECT status FROM bot_orders WHERE order_id='12348'").fetchone()
            self.assertEqual(row[0], 'cancelled')

    def test_sync_calls_seal_after_fills(self):
        # 5. any fill synced -> seal_trade_state called once (even if multiple fills)
        now = int(time.time())
        _insert_bot_order(
            self.conn, 10018, '12349', 'CQB_10018_GRID_88_5', 
            amount=10.0, filled_amount=0.0, status='open', 
            created_at=now - 150, order_type='grid', price=1.2, step=5, cycle_id=88
        )
        _insert_bot_order(
            self.conn, 10018, '12350', 'CQB_10018_GRID_88_6', 
            amount=20.0, filled_amount=0.0, status='open', 
            created_at=now - 150, order_type='grid', price=1.2, step=6, cycle_id=88
        )

        class MockExchange:
            def fetch_order(self, order_id, symbol):
                if order_id == '12349':
                    return {
                        'id': '12349', 'status': 'filled', 'filled': 10.0,
                        'amount': 10.0, 'average': 1.2, 'price': 1.2,
                        'clientOrderId': 'CQB_10018_GRID_88_5'
                    }
                else:
                    return {
                        'id': '12350', 'status': 'filled', 'filled': 20.0,
                        'amount': 20.0, 'average': 1.2, 'price': 1.2,
                        'clientOrderId': 'CQB_10018_GRID_88_6'
                    }

        ex = MockExchange()
        
        with patch('engine.ledger.credit_fill') as mock_credit_fill, \
             patch('engine.ledger.seal_trade_state') as mock_seal:
            
            synced = sync_stale_open_orders(10018, ex, self.conn, max_age_seconds=120)
            
            self.assertEqual(synced, 2)
            self.assertEqual(mock_credit_fill.call_count, 2)
            mock_seal.assert_called_once_with(10018)

    def test_sync_tp_fill_triggers_handle_tp_completion(self):
        # Setup: Stale TP order fully filled on exchange
        now = int(time.time())
        _insert_bot_order(
            self.conn, 10018, '99991', 'CQB_10018_TP_88_1', 
            amount=10.0, filled_amount=0.0, status='open', 
            created_at=now - 150, order_type='tp', price=1.2, step=1, cycle_id=88
        )

        class MockExchange:
            def fetch_order(self, order_id, symbol):
                return {
                    'id': '99991', 'status': 'filled', 'filled': 10.0,
                    'amount': 10.0, 'average': 1.2, 'price': 1.2,
                    'clientOrderId': 'CQB_10018_TP_88_1'
                }

        ex = MockExchange()
        with patch('engine.ledger.credit_fill') as mock_credit_fill, \
             patch('engine.ledger.handle_tp_completion') as mock_tp_comp:
            
            synced = sync_stale_open_orders(10018, ex, self.conn, max_age_seconds=120)
            self.assertEqual(synced, 1)
            mock_credit_fill.assert_called_once()
            mock_tp_comp.assert_called_once_with(
                bot_id=10018,
                exit_price=1.2,
                pair='SUI/USDC:USDC',
                exchange=ex
            )

    def test_sync_partial_tp_fill_no_cascade(self):
        # Setup: Stale TP order partially filled (not complete)
        now = int(time.time())
        _insert_bot_order(
            self.conn, 10018, '99992', 'CQB_10018_TP_88_2', 
            amount=10.0, filled_amount=0.0, status='open', 
            created_at=now - 150, order_type='tp', price=1.2, step=1, cycle_id=88
        )

        class MockExchange:
            def fetch_order(self, order_id, symbol):
                return {
                    'id': '99992', 'status': 'partially_filled', 'filled': 5.0,
                    'amount': 10.0, 'average': 1.2, 'price': 1.2,
                    'clientOrderId': 'CQB_10018_TP_88_2'
                }

        ex = MockExchange()
        with patch('engine.ledger.credit_fill') as mock_credit_fill, \
             patch('engine.ledger.handle_tp_completion') as mock_tp_comp:
            
            synced = sync_stale_open_orders(10018, ex, self.conn, max_age_seconds=120)
            self.assertEqual(synced, 0)
            mock_credit_fill.assert_not_called()
            mock_tp_comp.assert_not_called()

    def test_sync_sl_fill_triggers_sl_handler(self):
        # Setup: Stale SL order fully filled
        now = int(time.time())
        _insert_bot_order(
            self.conn, 10018, '99993', 'CQB_10018_SL_88_1', 
            amount=10.0, filled_amount=0.0, status='open', 
            created_at=now - 150, order_type='sl', price=1.2, step=1, cycle_id=88
        )

        class MockExchange:
            def fetch_order(self, order_id, symbol):
                return {
                    'id': '99993', 'status': 'filled', 'filled': 10.0,
                    'amount': 10.0, 'average': 1.2, 'price': 1.2,
                    'clientOrderId': 'CQB_10018_SL_88_1'
                }

        ex = MockExchange()
        with patch('engine.ledger.credit_fill') as mock_credit_fill, \
             patch('engine.ledger.handle_flatten') as mock_flatten:
            
            synced = sync_stale_open_orders(10018, ex, self.conn, max_age_seconds=120)
            self.assertEqual(synced, 1)
            mock_credit_fill.assert_called_once()
            mock_flatten.assert_called_once_with(
                bot_id=10018,
                pair='SUI/USDC:USDC',
                exchange=ex,
                close_price=1.2,
                close_qty=10.0,
                reason='sync_sl_fill'
            )


if __name__ == '__main__':
    unittest.main()
