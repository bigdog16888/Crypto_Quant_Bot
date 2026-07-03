import os
import sys
import time
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import get_connection, init_db
from engine.migrations.migration_base import SafeMigration
from engine.ground_truth_reconciler import GroundTruthReconciler
from engine.exchange_interface import ExchangeInterface

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_inv33.db')
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


# Subclasses for testing SafeMigration
class TestMigrationTrue(SafeMigration):
    requires_flat_positions = True
    
    @classmethod
    def _run_impl(cls, conn):
        conn.execute("UPDATE bots SET name='migrated_name'")

class TestMigrationFalse(SafeMigration):
    requires_flat_positions = False
    
    @classmethod
    def _run_impl(cls, conn):
        conn.execute("UPDATE bots SET name='migrated_name'")


def _insert_order(conn, bot_id, order_type, filled_amount, amount, price, status, cycle_id=1, position_side='LONG', created_at=None):
    if created_at is None:
        created_at = int(time.time())
    conn.execute("""
        INSERT INTO bot_orders (bot_id, order_type, filled_amount, amount, price, status, cycle_id, position_side, created_at, client_order_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (bot_id, order_type, filled_amount, amount, price, status, cycle_id, position_side, created_at, f"CQB_{bot_id}_{order_type}_{cycle_id}_{created_at}"))
    conn.commit()


class TestINV33MigrationSafetyAndGTR(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        self.reconciler = GroundTruthReconciler()
        self.mock_exchange = MagicMock(spec=ExchangeInterface)
        self.mock_exchange.get_symbol_precision.return_value = {'step_size': 0.001}

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_inv33_migration_blocked_when_positions_open(self):
        # Insert active bot with open_qty > 0
        _insert_bot(self.conn, 10001, 'bot_a', 'SUI/USDC', 'SUIUSDC', 'LONG')
        _insert_trades(self.conn, 10001, open_qty=1.0)
        
        # SafeMigration subclass with requires_flat_positions=True should raise RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            TestMigrationTrue.run(self.conn)
            
        self.assertIn("INV-33 VIOLATED", str(ctx.exception))
        self.assertIn("bot_a", str(ctx.exception))
        
        # Verify no changes made to the database
        bot_name = self.conn.execute("SELECT name FROM bots WHERE id=10001").fetchone()[0]
        self.assertEqual(bot_name, "bot_a")

    def test_inv33_migration_allowed_when_flat(self):
        # Insert bot with open_qty=0
        _insert_bot(self.conn, 10001, 'bot_a', 'SUI/USDC', 'SUIUSDC', 'LONG')
        _insert_trades(self.conn, 10001, open_qty=0.0)
        
        # Migration should succeed
        TestMigrationTrue.run(self.conn)
        
        # Verify migration did run
        bot_name = self.conn.execute("SELECT name FROM bots WHERE id=10001").fetchone()[0]
        self.assertEqual(bot_name, "migrated_name")

    def test_inv33_schema_only_migration_not_blocked(self):
        # Insert bot with open_qty > 0
        _insert_bot(self.conn, 10001, 'bot_a', 'SUI/USDC', 'SUIUSDC', 'LONG')
        _insert_trades(self.conn, 10001, open_qty=1.0)
        
        # Migration with requires_flat_positions=False should run without error
        TestMigrationFalse.run(self.conn)
        
        # Verify migration did run
        bot_name = self.conn.execute("SELECT name FROM bots WHERE id=10001").fetchone()[0]
        self.assertEqual(bot_name, "migrated_name")

    def test_inv33_gtr_manual_proof_alert_after_1hour(self):
        # Bot status=REQUIRE_MANUAL_PROOF, cascade_started_at=now - 7200 (2 hours)
        now_ts = int(time.time())
        _insert_bot(self.conn, 10001, 'sui long', 'SUI/USDC', 'SUIUSDC', 'LONG', 
                    status='REQUIRE_MANUAL_PROOF', cascade_started_at=now_ts - 7200)
        _insert_trades(self.conn, 10001, open_qty=100.0, avg_entry_price=2.0)
        
        # Mock exchange positions so there's no drift trigger for in_sync check on GTR
        self.mock_exchange.fetch_positions.return_value = []
        self.mock_exchange.get_last_price.return_value = 2.5
        
        with patch('engine.ground_truth_reconciler.logger.critical') as mock_critical:
            results = self.reconciler.run(self.mock_exchange, self.conn)
            
            # Assert manual_proof is tracked
            self.assertIn(10001, results.get('manual_proof', []))
            
            # Assert CRITICAL alert fired with correct details
            mock_critical.assert_called_once()
            log_msg = mock_critical.call_args[0][0]
            self.assertIn("locked for 2h", log_msg)
            self.assertIn("$250.00 USD", log_msg)

    def test_inv33_gtr_manual_proof_no_alert_before_1hour(self):
        # Bot status=REQUIRE_MANUAL_PROOF, cascade_started_at=now - 1800 (30 mins)
        now_ts = int(time.time())
        _insert_bot(self.conn, 10001, 'sui long', 'SUI/USDC', 'SUIUSDC', 'LONG', 
                    status='REQUIRE_MANUAL_PROOF', cascade_started_at=now_ts - 1800)
        _insert_trades(self.conn, 10001, open_qty=100.0, avg_entry_price=2.0)
        
        self.mock_exchange.fetch_positions.return_value = []
        
        with patch('engine.ground_truth_reconciler.logger.critical') as mock_critical:
            results = self.reconciler.run(self.mock_exchange, self.conn)
            
            # Assert manual_proof is still tracked
            self.assertIn(10001, results.get('manual_proof', []))
            
            # Assert CRITICAL alert did NOT fire
            mock_critical.assert_not_called()

    def test_detect_bot_ghost_standard_bot(self):
        # Insert three bots on ETHUSDC:
        # bot_1: LONG, 0.409
        # bot_2: SHORT, 0.012
        # bot_3 (standard): SHORT, 0.014 (ghost)
        _insert_bot(self.conn, 10011, 'eth', 'ETH/USDC', 'ETHUSDC', 'SHORT', status='IN TRADE')
        _insert_trades(self.conn, 10011, open_qty=0.012)
        _insert_order(self.conn, 10011, 'entry', 0.012, 0.012, 1000.0, 'filled', cycle_id=1, position_side='SHORT')

        _insert_bot(self.conn, 10021, 'long eth', 'ETH/USDC', 'ETHUSDC', 'LONG', status='IN TRADE')
        _insert_trades(self.conn, 10021, open_qty=0.409)
        _insert_order(self.conn, 10021, 'entry', 0.409, 0.409, 1000.0, 'filled', cycle_id=1, position_side='LONG')

        _insert_bot(self.conn, 100002, 'short eth', 'ETH/USDC', 'ETHUSDC', 'SHORT', status='IN TRADE', bot_type='standard')
        _insert_trades(self.conn, 100002, open_qty=0.014)
        # To simulate a ghost: must have a filled entry AND a filled exit that nets to 0 (so safety check is happy but recompute returns 0)
        _insert_order(self.conn, 100002, 'entry', 0.014, 0.014, 1000.0, 'filled', cycle_id=1, position_side='SHORT')
        _insert_order(self.conn, 100002, 'tp', 0.014, 0.014, 1010.0, 'filled', cycle_id=1, position_side='SHORT')

        # Mock exchange net position is 0.397 LONG
        with patch('engine.parity_gates.get_exchange_signed_net', return_value=0.397):
            from engine.oneway_netting import detect_bot_ghost, wipe_bot_ghost
            
            # Check bot 100002 (ghost)
            is_ghost = detect_bot_ghost(self.mock_exchange, 100002, self.conn)
            self.assertTrue(is_ghost)
            
            # Wipe bot 100002 (ghost)
            wipe_bot_ghost(self.mock_exchange, 100002, self.conn)
            
            # Verify bot 100002 status is reset to Scanning and open_qty to 0.0
            bot_status = self.conn.execute("SELECT status FROM bots WHERE id=100002").fetchone()[0]
            self.assertEqual(bot_status, 'Scanning')
            open_qty = self.conn.execute("SELECT open_qty FROM trades WHERE bot_id=100002").fetchone()[0]
            self.assertEqual(open_qty, 0.0)

    def test_detect_bot_ghost_does_not_false_positive_on_legitimate_position(self):
        # Insert three bots on ETHUSDC:
        # bot_1: LONG, 0.409
        # bot_2: SHORT, 0.012
        # bot_3: SHORT, 0.014
        _insert_bot(self.conn, 10011, 'eth', 'ETH/USDC', 'ETHUSDC', 'SHORT', status='IN TRADE')
        _insert_trades(self.conn, 10011, open_qty=0.012)
        _insert_order(self.conn, 10011, 'entry', 0.012, 0.012, 1000.0, 'filled', cycle_id=1, position_side='SHORT')

        _insert_bot(self.conn, 10021, 'long eth', 'ETH/USDC', 'ETHUSDC', 'LONG', status='IN TRADE')
        _insert_trades(self.conn, 10021, open_qty=0.409)
        _insert_order(self.conn, 10021, 'entry', 0.409, 0.409, 1000.0, 'filled', cycle_id=1, position_side='LONG')

        _insert_bot(self.conn, 100002, 'short eth', 'ETH/USDC', 'ETHUSDC', 'SHORT', status='IN TRADE', bot_type='standard')
        _insert_trades(self.conn, 100002, open_qty=0.014)
        # Legitimate position has filled entry but no exit
        _insert_order(self.conn, 100002, 'entry', 0.014, 0.014, 1000.0, 'filled', cycle_id=1, position_side='SHORT')

        # Mock exchange net position is 0.383 LONG
        with patch('engine.parity_gates.get_exchange_signed_net', return_value=0.383):
            from engine.oneway_netting import detect_bot_ghost
            
            # Check bot 100002 (not a ghost)
            is_ghost = detect_bot_ghost(self.mock_exchange, 100002, self.conn)
            self.assertFalse(is_ghost)
