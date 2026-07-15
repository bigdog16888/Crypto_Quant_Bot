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
from engine.database import get_connection, sync_trades_from_orders, update_active_positions_snapshot
from engine.ledger import seal_all_active_bots
from engine.exchange_interface import ExchangeInterface
from config.settings import config


class MockExchangeForRace(ExchangeInterface):
    def __init__(self, market_type='future'):
        import logging
        self.logger = logging.getLogger('MockExchange')
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
        
    def validate_order(self, symbol, side, amount, price, is_closing=False):
        return True, amount, price, ""
        
    def load_markets(self):
        return self.markets
        
    def get_symbol_precision(self, symbol):
        return {'qty_precision': 2, 'price_precision': 4, 'step_size': 0.01, 'tick_size': 0.01, 'min_notional': 1.0}
        
    def get_best_bid_ask(self, symbol):
        return 99.9, 100.1
        
    def fetch_open_orders(self, symbol=None):
        return [o for o in self.orders if o['status'] == 'open']
        
    def fetch_closed_orders(self, symbol=None, since=None, limit=None, params=None):
        return [o for o in self.orders if o['status'] in ('closed', 'filled')]
        
    def fetch_ticker(self, symbol):
        return {'last': 100.0, 'symbol': symbol}
        
    def get_last_price(self, symbol):
        return 100.0
        
    def fetch_order(self, order_id, symbol=None):
        for o in self.orders:
            if o['id'] == order_id or o.get('clientOrderId') == order_id:
                return o
        return None
        
    def create_order(self, symbol, type, side, amount, price=None, params=None):
        order_id = f"mock_ex_{len(self.orders)+1}"
        p = params or {}
        client_order_id = p.get('clientOrderId') or p.get('newClientOrderId') or f"CQB_mock_{int(time.time()*1000)}"
        order = {
            'id': order_id,
            'clientOrderId': client_order_id,
            'symbol': symbol,
            'type': type,
            'side': side.upper(),
            'price': price or 100.0,
            'amount': amount,
            'filled': amount,
            'remaining': 0.0,
            'status': 'closed',
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


class TestStartupBarrierRace(unittest.TestCase):
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
        self.mock_exchange = MockExchangeForRace()
        
        # Setup Runner with mocked exchanges and initialization bypassed
        with patch('engine.runner.startup.StartupMixin._initialize_exchanges'), \
             patch('engine.database.check_and_fix_integrity'), \
             patch('engine.migrations.migration_001_v2_schema.run'), \
             patch('engine.runner.startup.StartupMixin._post_init'):
            self.runner = BotRunner()
            
        self.runner.exchanges = {'future': self.mock_exchange}
        self.runner.exchange = self.mock_exchange
        
        # Set up real reconciler instance on the runner
        from engine.reconciler import StateReconciler
        self.runner._reconciler = StateReconciler(self.runner.exchanges)
        StateReconciler._last_global_offline_scan = 0.0
        
        # Patch BotExecutor._get_thread_exchange to return self.mock_exchange
        self.patcher_gte = patch('engine.bot_executor.BotExecutor._get_thread_exchange', return_value=self.mock_exchange)
        self.patcher_gte.start()
        
    def tearDown(self):
        self.patcher_gte.stop()
        
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

    def test_startup_barrier_race_prevention(self):
        """
        Simulate the exact startup race:
          - Parent bot (10011, LONG) has an unsynced offline fill of 0.5.
          - Child bot (10012, SHORT, parent_bot_id=10011) has not caught up.
          - If the startup barrier runs, it must sync the parent fills and seal the ledger
            BEFORE any cycle runs.
          - Assert that no premature child catch-up orders are placed based on stale DB status,
            and that once the barrier completes, the child correctly catches up to 0.5.
        """
        # 1. Seed Parent Bot in DB
        self.conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status, bot_type, rsi_limit, base_size, martingale_multiplier, hedge_trigger_step)
            VALUES (10011, 'Parent_LONG', 'SOL/USDC:USDC', 'SOLUSDC', 'LONG', 1, 'IN TRADE', 'parent', 30.0, 10.0, 2.0, 1)
        """)
        self.conn.execute("""
            INSERT INTO trades (bot_id, current_step, total_invested, open_qty, avg_entry_price, entry_confirmed, cycle_id)
            VALUES (10011, 1, 50.0, 0.0, 100.0, 1, 1)
        """)
        # Seed an unsynced placing order in DB
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, status, amount, filled_amount, price, client_order_id, cycle_id, step, created_at, updated_at)
            VALUES (10011, 'entry', 'placing', 0.5, 0.0, 100.0, 'CQB_10011_GRID_1', 1, 1, 0, 0)
        """)

        # 2. Seed Hedge Child Bot in DB
        self.conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status, bot_type, parent_bot_id, rsi_limit, base_size, martingale_multiplier, hedge_trigger_step)
            VALUES (10012, 'Child_Hedge', 'SOL/USDC:USDC', 'SOLUSDC', 'SHORT', 1, 'SCANNING', 'hedge_child', 10011, 30.0, 10.0, 2.0, 1)
        """)
        self.conn.execute("""
            INSERT INTO trades (bot_id, current_step, total_invested, open_qty, avg_entry_price, entry_confirmed, cycle_id)
            VALUES (10012, 0, 0.0, 0.0, 0.0, 0, 1)
        """)
        self.conn.commit()

        # 3. Configure Exchange state
        # The parent order filled while offline
        self.mock_exchange.orders.append({
            'id': 'ex_order_1',
            'clientOrderId': 'CQB_10011_GRID_1',
            'symbol': 'SOL/USDC:USDC',
            'type': 'limit',
            'side': 'BUY',
            'price': 100.0,
            'amount': 0.5,
            'filled': 0.5,
            'remaining': 0.0,
            'status': 'closed',
            'timestamp': int(time.time() * 1000) - 10000,
            'datetime': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'lastTradeTimestamp': int(time.time() * 1000) - 10000,
        })
        # Exchange physical net is +0.5 (parent is +0.5, child is 0.0)
        self.mock_exchange.positions = [
            {'symbol': 'SOL/USDC:USDC', 'contracts': 0.5, 'side': 'long'}
        ]

        # Write a mock last_shutdown.ts file to trigger offline fills reconstruction
        with open('last_shutdown.ts', 'w') as f:
            f.write(str(int(time.time() - 3600)))

        # 4. Trigger runner startup sync (the barrier)
        # Bypassing the 20s WebSocket warmup sleep to keep the test fast
        with patch('time.sleep', return_value=None):
            self.runner.startup_sync()

        # 5. Assert database has synced correctly BEFORE the cycle runs
        # Parent order status is promoted to filled
        order_status = self.conn.execute("SELECT status, filled_amount FROM bot_orders WHERE client_order_id='CQB_10011_GRID_1'").fetchone()
        self.assertEqual(order_status[0], 'filled')
        self.assertEqual(order_status[1], 0.5)

        # Parent bot open_qty in trades is now 0.5
        parent_qty = self.conn.execute("SELECT open_qty FROM trades WHERE bot_id=10011").fetchone()[0]
        self.assertEqual(parent_qty, 0.5)

        # Child bot trades has not caught up yet (remains at 0.0 because cycle has not run)
        child_qty_before = self.conn.execute("SELECT open_qty FROM trades WHERE bot_id=10012").fetchone()[0]
        self.assertEqual(child_qty_before, 0.0)

        # No order has been placed by the child yet
        child_orders_before = [o for o in self.mock_exchange.orders if 'CQB_10012_' in o['clientOrderId']]
        self.assertEqual(len(child_orders_before), 0)

        # 6. Execute one runner cycle to let the child bot catch up
        # This simulates the loop starting after the barrier has cleared
        with patch('engine.database.update_active_positions_snapshot'):
            self.runner.run_cycle()

        # 7. Assert that the child bot correctly placed a hedge order of 0.5 matching the synced parent qty
        child_orders_after = [o for o in self.mock_exchange.orders if 'CQB_10012_' in o['clientOrderId']]
        self.assertEqual(len(child_orders_after), 1)
        self.assertEqual(child_orders_after[0]['amount'], 0.5)
        self.assertEqual(child_orders_after[0]['side'], 'SELL')  # Child hedges LONG parent by going SHORT
