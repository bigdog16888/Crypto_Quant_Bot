import os
import sys
import time
import sqlite3
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import get_connection, init_db
from engine.ground_truth_reconciler import GroundTruthReconciler
from engine.exchange_interface import ExchangeInterface

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_inv31.db')
    database.DB_PATH = db_path
    database._local = database.threading.local()
    init_db()
    return d, db_path

def _insert_bot(conn, bot_id, name, pair, norm_pair, direction,
                status='IN TRADE', bot_type='standard', is_active=1, cascade_started_at=0):
    conn.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction,
                          status, bot_type, is_active,
                          rsi_limit, martingale_multiplier, base_size, strategy_type, cascade_started_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0, 0, 'Martingale', ?)
    """, (bot_id, name, pair, norm_pair, direction, status, bot_type, is_active, cascade_started_at))
    conn.commit()

def _insert_trades(conn, bot_id, open_qty=0.0, cycle_id=1, position_side='LONG',
                   avg_entry_price=0.0, basket_start_time=None):
    if basket_start_time is None:
        basket_start_time = int(time.time())
    conn.execute("""
        INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                            total_invested, avg_entry_price, current_step, entry_confirmed, basket_start_time)
        VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)
    """, (bot_id, open_qty, cycle_id, position_side, open_qty * avg_entry_price, avg_entry_price, basket_start_time))
    conn.commit()

class TestINV31GroundTruthReconciler(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        self.reconciler = GroundTruthReconciler()
        self.mock_exchange = MagicMock(spec=ExchangeInterface)
        self.mock_exchange.get_symbol_precision.return_value = {'step_size': 0.001}

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_inv31_ghost_virtual_healed(self):
        """INV-31: virtual open_qty > 0 but physical = 0, no open orders -> cleared to Scanning, cycle incremented"""
        _insert_bot(self.conn, 100314, 'BNB short_hedge', 'BNB/USDC:USDC', 'BNBUSDC', 'SHORT', status='IN TRADE')
        _insert_trades(self.conn, 100314, open_qty=0.3, cycle_id=84, avg_entry_price=580.0, basket_start_time=int(time.time()))
        
        # Insert a non-open order to show there are no active orders preventing reset
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (100314, 'tp', 'CQB_100314_TP_84_1', 580.0, 0.3, 0.0, 'cancelled', 84, 0)
        """)
        self.conn.commit()

        # Mock physical positions to show BNBUSDC position is flat
        self.mock_exchange.fetch_positions.return_value = []

        # Run GTR
        res = self.reconciler.run(self.mock_exchange, self.conn)
        self.assertIn(100314, res['ghost_virtual'])

        # Verify DB updates
        bot_row = self.conn.execute("SELECT status FROM bots WHERE id=100314").fetchone()
        trade_row = self.conn.execute("SELECT open_qty, cycle_id, current_step FROM trades WHERE bot_id=100314").fetchone()
        self.assertEqual(bot_row[0], 'Scanning')
        self.assertEqual(trade_row[0], 0.0)
        self.assertEqual(trade_row[1], 85)
        self.assertEqual(trade_row[2], 0)

    def test_inv31_ghost_not_healed_when_orders_exist(self):
        """INV-31: pair physical=0, bot open_qty>0, but HAS open tp order -> NOT healed (tp order may still fill)"""
        _insert_bot(self.conn, 100315, 'BNB short_hedge', 'BNB/USDC:USDC', 'BNBUSDC', 'SHORT', status='IN TRADE')
        _insert_trades(self.conn, 100315, open_qty=0.3, cycle_id=84, avg_entry_price=580.0, basket_start_time=int(time.time()))
        
        # Insert an open order
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (100315, 'tp', 'CQB_100315_TP_84_1', 580.0, 0.3, 0.0, 'open', 84, 0)
        """)
        self.conn.commit()

        # Mock physical positions to show BNBUSDC position is flat
        self.mock_exchange.fetch_positions.return_value = []

        res = self.reconciler.run(self.mock_exchange, self.conn)
        self.assertEqual(len(res['ghost_virtual']), 0)

        # Verify bot is still in IN TRADE status
        bot_row = self.conn.execute("SELECT status FROM bots WHERE id=100315").fetchone()
        self.assertEqual(bot_row[0], 'IN TRADE')

    def test_inv31_pair_level_in_sync(self):
        """INV-31: 3 bots on ETHUSDC: one has open_qty=0.072, two have open_qty=0. Physical = 0.072 -> PAIR_IN_SYNC, no orphan/ghost flags"""
        _insert_bot(self.conn, 10021, 'long eth', 'ETH/USDC:USDC', 'ETHUSDC', 'LONG', status='IN TRADE')
        _insert_trades(self.conn, 10021, open_qty=0.072, cycle_id=38, avg_entry_price=1600.0)

        _insert_bot(self.conn, 10011, 'eth', 'ETH/USDC:USDC', 'ETHUSDC', 'SHORT', status='Scanning')
        _insert_trades(self.conn, 10011, open_qty=0.0, cycle_id=1, avg_entry_price=0.0)

        _insert_bot(self.conn, 100002, 'short eth', 'ETH/USDC:USDC', 'ETHUSDC', 'SHORT', status='Scanning')
        _insert_trades(self.conn, 100002, open_qty=0.0, cycle_id=1, avg_entry_price=0.0)

        # Mock physical position as LONG 0.072
        self.mock_exchange.fetch_positions.return_value = [
            {'symbol': 'ETH/USDC:USDC', 'contracts': 0.072, 'side': 'LONG', 'positionAmt': 0.072}
        ]

        res = self.reconciler.run(self.mock_exchange, self.conn)
        self.assertEqual(res['in_sync_count'], 1)
        self.assertEqual(len(res['ghost_virtual']), 0)
        self.assertEqual(len(res['orphan_physical']), 0)

    def test_inv31_in_sync_no_action(self):
        """INV-31: virtual matches physical -> no action taken"""
        _insert_bot(self.conn, 10022, 'short btc', 'BTC/USDC:USDC', 'BTCUSDC', 'SHORT', status='IN TRADE')
        _insert_trades(self.conn, 10022, open_qty=0.002, cycle_id=47, avg_entry_price=60867.0)

        # Mock physical position as SHORT 0.002
        self.mock_exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDC:USDC', 'contracts': 0.002, 'side': 'SHORT', 'positionAmt': -0.002}
        ]

        res = self.reconciler.run(self.mock_exchange, self.conn)
        self.assertEqual(res['in_sync_count'], 1)
        self.assertEqual(len(res['ghost_virtual']), 0)

        bot_row = self.conn.execute("SELECT status FROM bots WHERE id=10022").fetchone()
        trade_row = self.conn.execute("SELECT open_qty, cycle_id FROM trades WHERE bot_id=10022").fetchone()
        self.assertEqual(bot_row[0], 'IN TRADE')
        self.assertEqual(trade_row[0], 0.002)
        self.assertEqual(trade_row[1], 47)

    @patch('engine.database.reset_bot_after_tp')
    def test_inv31_stuck_cascade_pending_close_flat(self, mock_reset):
        """INV-31: status='pending_close' and open_qty=0, cascade_started_at is old -> reset_bot_after_tp called"""
        _insert_bot(self.conn, 10001, 'stuck bot', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', status='pending_close', cascade_started_at=int(time.time()) - 400)
        _insert_trades(self.conn, 10001, open_qty=0.0, cycle_id=10, avg_entry_price=60000.0)

        self.mock_exchange.fetch_positions.return_value = []

        res = self.reconciler.run(self.mock_exchange, self.conn)
        self.assertIn(10001, res['stuck_cascade'])
        mock_reset.assert_called_once_with(10001, exit_price=0.0, action_label='GTR_STUCK_CASCADE_RECOVERY')

    def test_inv31_stuck_cascade_pending_close_with_qty(self):
        """INV-31: status='pending_close' and open_qty > 0, cascade_started_at is old -> status set to pending_flatten"""
        _insert_bot(self.conn, 10002, 'stuck bot with qty', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', status='pending_close', cascade_started_at=int(time.time()) - 400)
        _insert_trades(self.conn, 10002, open_qty=0.3, cycle_id=10, avg_entry_price=60000.0)

        self.mock_exchange.fetch_positions.return_value = []

        res = self.reconciler.run(self.mock_exchange, self.conn)
        self.assertIn(10002, res['stuck_cascade'])
        
        bot_row = self.conn.execute("SELECT status FROM bots WHERE id=10002").fetchone()
        self.assertEqual(bot_row[0], 'pending_flatten')

    def test_inv31_stuck_pending_hedge_close(self):
        """INV-31: status='pending_hedge_close' and open_qty=0, cascade_started_at is old -> reset to Scanning, cycle_id incremented"""
        _insert_bot(self.conn, 10007, 'BNB short', 'BNB/USDC:USDC', 'BNBUSDC', 'SHORT', status='pending_hedge_close', cascade_started_at=int(time.time()) - 400)
        _insert_trades(self.conn, 10007, open_qty=0.0, cycle_id=84, avg_entry_price=580.0)

        self.mock_exchange.fetch_positions.return_value = []

        res = self.reconciler.run(self.mock_exchange, self.conn)
        self.assertIn(10007, res['stuck_cascade'])

        bot_row = self.conn.execute("SELECT status FROM bots WHERE id=10007").fetchone()
        trade_row = self.conn.execute("SELECT open_qty, cycle_id, current_step FROM trades WHERE bot_id=10007").fetchone()
        self.assertEqual(bot_row[0], 'Scanning')
        self.assertEqual(trade_row[0], 0.0)
        self.assertEqual(trade_row[1], 85)
        self.assertEqual(trade_row[2], 0)

    def test_inv31_orphan_physical_logged_not_healed(self):
        """INV-31: virtual=0 but physical > 0 -> logged in results['orphan_physical'], no DB change"""
        _insert_bot(self.conn, 10003, 'Scanning bot', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', status='Scanning')
        _insert_trades(self.conn, 10003, open_qty=0.0, cycle_id=1, avg_entry_price=0.0)

        self.mock_exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDC:USDC', 'contracts': 0.002, 'side': 'LONG', 'positionAmt': 0.002}
        ]

        res = self.reconciler.run(self.mock_exchange, self.conn)
        self.assertEqual(len(res['orphan_physical']), 1)
        self.assertEqual('BTCUSDC:0.002000', res['orphan_physical'][0])

        bot_row = self.conn.execute("SELECT status FROM bots WHERE id=10003").fetchone()
        self.assertEqual(bot_row[0], 'Scanning')

    @patch('engine.database.reset_bot_after_tp')
    def test_inv31_cascade_not_triggered_before_timeout(self, mock_reset):
        """INV-31: cascade_started_at is recent -> no stuck cascade action taken"""
        _insert_bot(self.conn, 10004, 'recent cascade bot', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', status='pending_close', cascade_started_at=int(time.time()) - 100)
        _insert_trades(self.conn, 10004, open_qty=0.0, cycle_id=10, avg_entry_price=60000.0)

        self.mock_exchange.fetch_positions.return_value = []

        res = self.reconciler.run(self.mock_exchange, self.conn)
        self.assertEqual(len(res['stuck_cascade']), 0)
        mock_reset.assert_not_called()

    def test_inv31_cascade_started_at_used_for_timeout(self):
        """INV-31: basket_start_time is old (1 day ago) but cascade_started_at is recent -> NO stuck cascade action triggered"""
        _insert_bot(self.conn, 10005, 'old bot recent cascade', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', status='pending_close', cascade_started_at=int(time.time()) - 100)
        _insert_trades(self.conn, 10005, open_qty=0.0, cycle_id=10, avg_entry_price=60000.0, basket_start_time=int(time.time()) - 86400)

        self.mock_exchange.fetch_positions.return_value = []

        res = self.reconciler.run(self.mock_exchange, self.conn)
        self.assertEqual(len(res['stuck_cascade']), 0)

    def test_inv31_cascade_timeout_correct(self):
        """INV-31: cascade_started_at is old (400s ago) -> stuck cascade triggered"""
        _insert_bot(self.conn, 10006, 'old cascade bot', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', status='pending_close', cascade_started_at=int(time.time()) - 400)
        _insert_trades(self.conn, 10006, open_qty=0.3, cycle_id=10, avg_entry_price=60000.0)

        self.mock_exchange.fetch_positions.return_value = []

        res = self.reconciler.run(self.mock_exchange, self.conn)
        self.assertIn(10006, res['stuck_cascade'])

        bot_row = self.conn.execute("SELECT status FROM bots WHERE id=10006").fetchone()
        self.assertEqual(bot_row[0], 'pending_flatten')

    def test_inv32_safe_wipe_not_blocked_by_sibling(self):
        """INV-32: Sibling bot has active position on pair, but target bot has 0 position. Target bot safe wipe should not be blocked."""
        # Insert target bot (100318)
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', status='pending_close')
        _insert_trades(self.conn, 100318, open_qty=0.0, cycle_id=128)
        
        # Insert sibling bot (100000) on same pair SUIUSDC
        _insert_bot(self.conn, 100000, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', status='IN TRADE')
        _insert_trades(self.conn, 100000, open_qty=14.7, cycle_id=12)

        # Mock active_positions: sibling has open short, target bot has no position
        self.conn.execute("""
            INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked, last_updated)
            VALUES (100000, 'SUIUSDC', 'SHORT', 14.7, 0.68, ?, '2026-06-25 05:03:04')
        """, (int(time.time()),))
        self.conn.commit()

        # Call safe_wipe_bot on target bot
        with patch('engine.database.get_connection', return_value=self.conn):
            res = database.safe_wipe_bot(100318, 'SUI/USDC:USDC', 'SHORT', 'test sibling bot check', force=False, cursor=self.conn.cursor(), human_approved=True)
            self.assertTrue(res)

    def test_inv32_safe_wipe_blocked_when_own_position_exists(self):
        """INV-32: Target bot has active position on pair. Target bot safe wipe should be blocked."""
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', status='pending_close')
        _insert_trades(self.conn, 100318, open_qty=1474.5, cycle_id=128)

        # Mock active_positions: target bot itself has an open position
        self.conn.execute("""
            INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked, last_updated)
            VALUES (100318, 'SUIUSDC', 'SHORT', 1474.5, 0.68, ?, '2026-06-25 05:03:04')
        """, (int(time.time()),))
        self.conn.commit()

        # Call safe_wipe_bot on target bot
        with patch('engine.database.get_connection', return_value=self.conn):
            res = database.safe_wipe_bot(100318, 'SUI/USDC:USDC', 'SHORT', 'test own bot check', force=False, cursor=self.conn.cursor(), human_approved=True)
            self.assertFalse(res)

    def test_gtr_oneway_short_position_sign(self):
        """INV-31: Exchange returns SHORT position under One-Way mode. contracts is negative or positionAmt is negative. side says LONG or BOTH. Verifies net is negative."""
        mock_positions = [
            {
                'symbol': 'SOL/USDC:USDC',
                'contracts': -1.9,
                'side': 'LONG',
                'info': {'positionAmt': '-1.9'}
            }
        ]
        net = self.reconciler._build_physical_net(mock_positions)
        self.assertEqual(net.get('SOLUSDC'), -1.9)

    def test_gtr_oneway_sign_fallback(self):
        """INV-31: Fallback case where contracts/positionAmt is not signed or is None/zero, but side is SHORT. Verifies negative net quantity."""
        mock_positions = [
            {
                'symbol': 'BTC/USDC:USDC',
                'contracts': 0.002,
                'side': 'SHORT',
                'info': {}
            }
        ]
        net = self.reconciler._build_physical_net(mock_positions)
        self.assertEqual(net.get('BTCUSDC'), -0.002)

if __name__ == '__main__':
    unittest.main()
