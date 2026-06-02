import unittest
import sqlite3
import tempfile
import os
import time
from unittest.mock import MagicMock, patch

import sys
sys.path.append(os.getcwd())

from engine import database, ledger
from engine.runner import BotRunner
from engine.bot_executor import BotExecutor
from engine.database import get_connection, sync_trades_from_orders, reset_bot_after_tp, save_bot_order, update_active_positions_snapshot
from engine.ledger import credit_fill, seal_trade_state
from engine.exchange_interface import ExchangeInterface
from config.settings import config



class MockExchange(ExchangeInterface):
    """
    Inherits from ExchangeInterface to reuse static utility methods,
    but skips CCXT initialization and overrides all API calling methods
    with pure in-memory mock implementations.
    """
    def __init__(self, market_type='future'):
        self.market_type = market_type
        self.orders = []
        self.positions = []
        self.markets = {
            'SOL/USDC:USDC': {
                'id': 'SOLUSDC',
                'symbol': 'SOL/USDC:USDC',
                'base': 'SOL',
                'quote': 'USDC',
                'settle': 'USDC',
                'type': 'swap',
                'linear': True,
                'precision': {'price': 4, 'amount': 2, 'tick_size': 0.01, 'step_size': 0.01},
                'limits': {'amount': {'min': 0.01}}
            }
        }
        
    def load_markets(self):
        return self.markets
        
    def get_symbol_precision(self, symbol):
        return {'qty_precision': 2, 'price_precision': 4, 'step_size': 0.01, 'tick_size': 0.01}
        
    def get_best_bid_ask(self, symbol):
        return 99.9, 100.1
        
    def fetch_open_orders(self, symbol=None):
        return [o for o in self.orders if o['status'] == 'open']
        
    def fetch_order(self, order_id, symbol=None):
        for o in self.orders:
            if o['id'] == order_id or o.get('clientOrderId') == order_id:
                return o
        return None
        
    def create_order(self, symbol, type, side, amount, price=None, params=None):
        order_id = f"mock_ex_{len(self.orders)+1}"
        client_order_id = (params or {}).get('clientOrderId') or f"CQB_mock_{int(time.time()*1000)}"
        order = {
            'id': order_id,
            'clientOrderId': client_order_id,
            'symbol': symbol,
            'type': type,
            'side': side.upper(),
            'price': price,
            'amount': amount,
            'filled': 0.0,
            'remaining': amount,
            'status': 'open',
            'timestamp': int(time.time() * 1000),
            'datetime': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'lastTradeTimestamp': int(time.time() * 1000),
        }
        self.orders.append(order)
        return order
        
    def cancel_order(self, order_id, symbol=None):
        for o in self.orders:
            if o['id'] == order_id or o.get('clientOrderId') == order_id:
                o['status'] = 'cancelled'
                return o
        return None
        
    def fetch_positions(self, symbols=None):
        return self.positions

    def fetch_my_trades(self, symbol, since=None, limit=None, params=None):
        trades = []
        for o in self.orders:
            if o['symbol'] == symbol and o['status'] in ('filled', 'closed'):
                trades.append({
                    'id': f"trade_{o['id']}",
                    'order': o['id'],
                    'timestamp': o['timestamp'],
                    'price': o['price'],
                    'amount': o['amount'],
                    'side': o['side'].lower(),
                })
        return trades


class TestBotLifecycle(unittest.TestCase):
    def setUp(self):
        # 1. Store and disable production DB configurations
        self.orig_backup = database.backup_database
        database.backup_database = lambda: None
        self.orig_db_path = database.DB_PATH
        
        # 2. Create isolated SQLite database in a temporary file
        self.db_fd, self.db_temp_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        database.DB_PATH = self.db_temp_path
        
        # 3. Clear thread-local DB connections to force freshness
        if hasattr(database._local, 'connection'):
            database._local.connection = None
            
        # 4. Initialize schema
        database.init_db()
        self.conn = database.get_connection()
        
        # 5. Enable trading configuration for testing
        self.orig_trading_enabled = config.TRADING_ENABLED
        self.orig_dry_run = config.DRY_RUN
        config.TRADING_ENABLED = True
        config.DRY_RUN = False
        
        # 6. Mock exchange interface and runner
        self.mock_exchange = MockExchange()
        
        # Setup Runner with mocked exchanges and initialization bypassed
        with patch('engine.runner.BotRunner._initialize_exchanges'), \
             patch('engine.database.check_and_fix_integrity'), \
             patch('engine.migrations.migration_001_v2_schema.run'), \
             patch('engine.runner.BotRunner._post_init'):
            self.runner = BotRunner()
            
        self.runner.exchanges = {'future': self.mock_exchange}
        self.runner.exchange = self.mock_exchange
        
        # Set up real reconciler instance on the runner
        from engine.reconciler import StateReconciler
        self.runner._reconciler = StateReconciler(self.runner.exchanges)
        
    def tearDown(self):
        # 1. Close current connection
        if hasattr(database._local, 'connection') and database._local.connection:
            database._local.connection.close()
            database._local.connection = None
            
        # 2. Restore settings and clean up temporary database file
        database.DB_PATH = self.orig_db_path
        database.backup_database = self.orig_backup
        
        config.TRADING_ENABLED = self.orig_trading_enabled
        config.DRY_RUN = self.orig_dry_run
        
        try:
            os.remove(self.db_temp_path)
        except Exception:
            pass
            
        if os.path.exists('last_shutdown.ts'):
            try:
                os.remove('last_shutdown.ts')
            except Exception:
                pass

    def get_bot_execution_status(self, bot_id):
        cursor = self.conn.cursor()
        return cursor.execute("SELECT status FROM bots WHERE id=?", (bot_id,)).fetchone()[0]

    def setup_test_bot(self, bot_id=10008, name="SOL_LONG_Bot", pair="SOL/USDC:USDC", direction="LONG"):
        """Seeds the temporary database with a test bot and corresponding trade state."""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO bots (
                id, name, pair, normalized_pair, direction, rsi_limit, 
                martingale_multiplier, base_size, strategy_type, config, 
                is_active, status, manual_close_pct, pos_limit_hit
            ) VALUES (?, ?, ?, ?, ?, 30.0, 2.0, 10.0, 'Martingale', ?, 1, 'Scanning', 100.0, 0)
        """, (
            bot_id, name, pair, pair.split(':')[0].replace('/', '').upper(), direction,
            '{"market_type": "future", "post_exit_stop": false}'
        ))
        cursor.execute("""
            INSERT INTO trades (
                bot_id, current_step, total_invested, avg_entry_price, target_tp_price,
                last_exit_price, last_exit_time, basket_start_time, entry_confirmed,
                entry_order_id, tp_order_id, bot_position_id, close_type, cycle_id,
                cycle_phase, open_qty, wipe_wall_ts, position_side, cycle_start_time
            ) VALUES (?, 0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0, NULL, NULL, NULL, NULL, 1, 'SCANNING', 0.0, 0, ?, 0)
        """, (bot_id, direction))
        self.conn.commit()

    def test_complete_bot_lifecycle(self):
        bot_id = 10008
        pair = "SOL/USDC:USDC"
        direction = "LONG"
        
        # --- Seed the test bot ---
        self.setup_test_bot(bot_id=bot_id, pair=pair, direction=direction)
        
        # Assert initial state is clean
        bot_status = database.get_bot_status(bot_id)
        self.assertEqual(self.get_bot_execution_status(bot_id), 'Scanning')
        self.assertEqual(bot_status['current_step'], 0)
        self.assertEqual(bot_status['open_qty'], 0.0)
        self.assertEqual(bot_status['total_invested'], 0.0)
        
        # =====================================================================
        # PHASE 1: PLACE ENTRY & CREDIT FILL
        # =====================================================================
        # Place Entry order in DB as 'open'
        entry_order_id = "mock_entry_oid_123"
        client_entry_cid = "CQB_10008_ENTRY_1_1"
        save_bot_order(
            bot_id=bot_id,
            order_type='entry',
            exchange_order_id=entry_order_id,
            price=100.0,
            amount=0.5,
            step=1,
            status='open',
            client_order_id=client_entry_cid,
            notes='test-bot-lifecycle-entry'
        )
        
        # Verify order row exists in DB
        cursor = self.conn.cursor()
        order_row = cursor.execute("SELECT price, amount, status FROM bot_orders WHERE order_id=?", (entry_order_id,)).fetchone()
        self.assertIsNotNone(order_row)
        self.assertEqual(order_row[0], 100.0)
        self.assertEqual(order_row[1], 0.5)
        self.assertEqual(order_row[2], 'open')
        
        # Simulate fill on exchange & credit in ledger
        credited = credit_fill(
            bot_id=bot_id,
            order_id=entry_order_id,
            cumulative_qty=0.5,
            avg_price=100.0,
            order_type='entry'
        )
        self.assertTrue(credited)
        
        # Seal trade state to recalculate and cache trade metrics
        seal_trade_state(bot_id)
        
        # Verify transition to 'IN TRADE' at Step 1
        bot_status = database.get_bot_status(bot_id)
        self.assertEqual(self.get_bot_execution_status(bot_id), 'IN TRADE')
        self.assertEqual(bot_status['current_step'], 1)
        self.assertEqual(bot_status['open_qty'], 0.5)
        self.assertEqual(bot_status['total_invested'], 50.0)
        self.assertEqual(bot_status['avg_entry_price'], 100.0)
        self.assertEqual(bot_status['cycle_phase'], 'SCANNING')
        
        # =====================================================================
        # PHASE 2: PLACE GRID ORDER & CREDIT FILL (STEP 2)
        # =====================================================================
        grid_order_id = "mock_grid_oid_456"
        client_grid_cid = "CQB_10008_GRID_1_2"
        save_bot_order(
            bot_id=bot_id,
            order_type='grid',
            exchange_order_id=grid_order_id,
            price=90.0,
            amount=1.0,
            step=2,
            status='open',
            client_order_id=client_grid_cid,
            notes='test-bot-lifecycle-grid'
        )
        
        # Simulate grid fill in ledger
        credited = credit_fill(
            bot_id=bot_id,
            order_id=grid_order_id,
            cumulative_qty=1.0,
            avg_price=90.0,
            order_type='grid'
        )
        self.assertTrue(credited)
        seal_trade_state(bot_id)
        
        # Verify step increment and size updates
        bot_status = database.get_bot_status(bot_id)
        self.assertEqual(bot_status['current_step'], 2)
        self.assertEqual(bot_status['open_qty'], 1.5)
        self.assertEqual(bot_status['total_invested'], 140.0)  # (0.5 * 100.0) + (1.0 * 90.0)
        self.assertAlmostEqual(bot_status['avg_entry_price'], 93.33333333, places=5)
        
        # =====================================================================
        # PHASE 3: PLACE TP ORDER & CREDIT FILL
        # =====================================================================
        tp_order_id = "mock_tp_oid_789"
        client_tp_cid = "CQB_10008_TP_1_3"
        save_bot_order(
            bot_id=bot_id,
            order_type='tp',
            exchange_order_id=tp_order_id,
            price=110.0,
            amount=1.5,
            step=3,
            status='open',
            client_order_id=client_tp_cid,
            notes='test-bot-lifecycle-tp'
        )
        
        # Simulate TP fill in ledger
        credited = credit_fill(
            bot_id=bot_id,
            order_id=tp_order_id,
            cumulative_qty=1.5,
            avg_price=110.0,
            order_type='tp'
        )
        self.assertTrue(credited)
        seal_trade_state(bot_id)
        
        # Verify size drops to zero
        bot_status = database.get_bot_status(bot_id)
        self.assertEqual(bot_status['open_qty'], 0.0)
        self.assertEqual(bot_status['total_invested'], 0.0)
        
        # =====================================================================
        # PHASE 4: CASCADE RESET BOT TO SCANNING
        # =====================================================================
        # Wipe-Proof safety checks require a flat active position snapshot,
        # which we configure here in the database or leave empty.
        cursor.execute("DELETE FROM active_positions")
        self.conn.commit()
        
        # Execute cycle reset (flat exchange required by pair parity gate)
        self.mock_exchange.positions = []
        reset_bot_after_tp(bot_id, exit_price=110.0, action_label='TP_HIT', exchange=self.mock_exchange)
        
        # Assert bot returns to 'Scanning'
        bot_status = database.get_bot_status(bot_id)
        self.assertEqual(self.get_bot_execution_status(bot_id), 'Scanning')
        self.assertEqual(bot_status['current_step'], 0)
        self.assertEqual(bot_status['open_qty'], 0.0)
        self.assertEqual(bot_status['cycle_id'], 2)  # Cycle ID increments
        
        # Assert previous active rows marked 'reset_cleared' or 'auto_closed'
        non_cleared_orders = cursor.execute(
            "SELECT COUNT(*) FROM bot_orders WHERE bot_id=? AND status NOT IN ('reset_cleared', 'auto_closed', 'cancelled')",
            (bot_id,)
        ).fetchone()[0]
        self.assertEqual(non_cleared_orders, 0)
        
        # =====================================================================
        # PHASE 5 & 6: ENGINE RESTART & STARTUP SYNC
        # =====================================================================
        # Record a mock last shutdown timestamp
        shutdown_ts = int(time.time() - 3600)  # 1 hour ago
        with open('last_shutdown.ts', 'w') as f:
            f.write(str(shutdown_ts))
            
        # Re-set physical position to flat on MockExchange
        self.mock_exchange.positions = []
        
        # Execute runner startup sync
        self.runner.startup_sync()
        
        # Assert virtual net remains zero after reset and startup sync
        bot_status = database.get_bot_status(bot_id)
        self.assertEqual(bot_status['open_qty'], 0.0)
        self.assertEqual(bot_status['total_invested'], 0.0)
        
        # Assert no orphan bot_orders rows remain active
        active_orders_count = cursor.execute(
            "SELECT COUNT(*) FROM bot_orders WHERE bot_id=? AND status IN ('open', 'new')",
            (bot_id,)
        ).fetchone()[0]
        self.assertEqual(active_orders_count, 0)
