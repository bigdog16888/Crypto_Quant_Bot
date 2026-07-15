import unittest
import sqlite3
import tempfile
import os
import time
import logging
from unittest.mock import MagicMock, patch, call

import sys
sys.path.append(os.getcwd())

from engine import database, ledger
from engine.runner import BotRunner
from engine.bot_executor import BotExecutor
from engine.database import get_connection, save_bot_order, get_bot_status
from engine.exchange_interface import ExchangeInterface
from config.settings import config


class MockExchange(ExchangeInterface):
    def __init__(self):
        self.logger = logging.getLogger("MockExchange")
        self.market_type = 'future'
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

    def validate_order(self, symbol, side, amount, price=None, is_closing=False):
        return True, amount, price, ""


class TestEntryDedupGuard(unittest.TestCase):
    def setUp(self):
        self.orig_backup = database.backup_database
        database.backup_database = lambda: None
        self.orig_db_path = database.DB_PATH
        
        self.db_fd, self.db_temp_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        database.DB_PATH = self.db_temp_path
        
        if hasattr(database._local, 'connection'):
            database._local.connection = None
            
        database.init_db()
        self.conn = database.get_connection()
        
        self.orig_trading_enabled = config.TRADING_ENABLED
        self.orig_dry_run = config.DRY_RUN
        config.TRADING_ENABLED = True
        config.DRY_RUN = False
        
        self.mock_exchange = MockExchange()
        
        with patch('engine.runner.startup.StartupMixin._initialize_exchanges'), \
             patch('engine.database.check_and_fix_integrity'), \
             patch('engine.migrations.migration_001_v2_schema.run'), \
             patch('engine.runner.startup.StartupMixin._post_init'):
            self.runner = BotRunner()
            
        self.runner.exchanges = {'future': self.mock_exchange}
        self.runner.exchange = self.mock_exchange
        self.executor = BotExecutor(self.runner)

    def tearDown(self):
        if hasattr(database._local, 'connection') and database._local.connection:
            database._local.connection.close()
            database._local.connection = None
            
        database.DB_PATH = self.orig_db_path
        database.backup_database = self.orig_backup
        
        config.TRADING_ENABLED = self.orig_trading_enabled
        config.DRY_RUN = self.orig_dry_run
        
        try:
            os.remove(self.db_temp_path)
        except Exception:
            pass

    def setup_test_bot(self, bot_id=10008, name="SOL_LONG_Bot", pair="SOL/USDC:USDC", direction="LONG"):
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

    @patch('engine.bot_executor.logger')
    def test_entry_placement_skipped_when_cid_exists(self, mock_logger):
        bot_id = 10008
        pair = "SOL/USDC:USDC"
        direction = "LONG"
        
        self.setup_test_bot(bot_id=bot_id, pair=pair, direction=direction)
        
        # Save a duplicate entry order matching the generated CID: CQB_10008_ENTRY_1_1
        client_order_id = "CQB_10008_ENTRY_1_1"
        save_bot_order(
            bot_id=bot_id,
            order_type='entry',
            exchange_order_id="existing_exchange_oid_111",
            price=100.0,
            amount=0.5,
            step=1,
            status='expired',
            client_order_id=client_order_id,
            notes='pre-existing-for-dedup-check'
        )
        
        # Execute the entry function
        bot_status = get_bot_status(bot_id)
        bot_config = {
            'market_type': 'future',
            'post_exit_stop': False
        }
        
        # Mock _place_gtx_order_with_retry to check if it's called
        self.executor._place_gtx_order_with_retry = MagicMock()
        
        res = self.executor.execute_entry(
            bot_id=bot_id,
            name="SOL_LONG_Bot",
            pair=pair,
            side="buy",
            amount=0.5,
            direction=direction,
            price=100.0,
            params={},
            exchange=self.mock_exchange,
            market_snapshot=None,
            bot_config=bot_config,
            bot_status=bot_status
        )
        
        # Verify the order placement was skipped (returned None)
        self.assertIsNone(res)
        
        # Verify _place_gtx_order_with_retry was NOT called
        self.executor._place_gtx_order_with_retry.assert_not_called()
        
        # Verify the warning was logged
        warn_msg = f"🛡️ [DEDUP-GUARD] Entry already exists for this CID: {client_order_id}. Skipping placement."
        mock_logger.warning.assert_any_call(warn_msg)

    @patch('engine.bot_executor.logger')
    def test_entry_placed_when_previous_failed(self, mock_logger):
        bot_id = 10008
        pair = "SOL/USDC:USDC"
        direction = "LONG"
        
        self.setup_test_bot(bot_id=bot_id, pair=pair, direction=direction)
        
        # Save a duplicate entry order with a FAILED status
        client_order_id = "CQB_10008_ENTRY_1_1"
        save_bot_order(
            bot_id=bot_id,
            order_type='entry',
            exchange_order_id="failed_exchange_oid",
            price=100.0,
            amount=0.5,
            step=1,
            status='failed',
            client_order_id=client_order_id,
            notes='pre-existing-failed-for-dedup-check'
        )
        
        bot_status = get_bot_status(bot_id)
        bot_config = {
            'market_type': 'future',
            'post_exit_stop': False
        }
        
        # Mock _place_gtx_order_with_retry
        mock_order = {
            'id': 'new_mock_order_id',
            'clientOrderId': client_order_id,
            'status': 'open',
            'filled': 0.0,
            'amount': 0.5
        }
        self.executor._place_gtx_order_with_retry = MagicMock(return_value=mock_order)
        
        res = self.executor.execute_entry(
            bot_id=bot_id,
            name="SOL_LONG_Bot",
            pair=pair,
            side="buy",
            amount=0.5,
            direction=direction,
            price=100.0,
            params={},
            exchange=self.mock_exchange,
            market_snapshot=None,
            bot_config=bot_config,
            bot_status=bot_status
        )
        
        # Verify the place was NOT skipped by DEDUP-GUARD
        self.executor._place_gtx_order_with_retry.assert_called_once()
        
        # Verify DEDUP-GUARD warning was NOT logged
        for call_args in mock_logger.warning.call_args_list:
            msg = call_args[0][0]
            self.assertNotIn("[DEDUP-GUARD]", msg)

    @patch('engine.bot_executor.logger')
    def test_entry_placed_when_previous_excluded(self, mock_logger):
        for idx, excluded_status in enumerate(('reset_cleared', 'auto_closed', 'rejected')):
            bot_id = 10009 + idx
            pair = "SOL/USDC:USDC"
            direction = "LONG"
            
            self.setup_test_bot(bot_id=bot_id, pair=pair, direction=direction)
            
            # Save a duplicate entry order with the excluded status
            client_order_id = f"CQB_{bot_id}_ENTRY_1_1"
            save_bot_order(
                bot_id=bot_id,
                order_type='entry',
                exchange_order_id=f"ex_oid_{excluded_status}",
                price=100.0,
                amount=0.5,
                step=1,
                status=excluded_status,
                client_order_id=client_order_id,
                notes=f'pre-existing-{excluded_status}-for-dedup-check'
            )
            
            bot_status = get_bot_status(bot_id)
            bot_config = {
                'market_type': 'future',
                'post_exit_stop': False
            }
            
            # Mock _place_gtx_order_with_retry
            mock_order = {
                'id': f'new_mock_order_{excluded_status}',
                'clientOrderId': client_order_id,
                'status': 'open',
                'filled': 0.0,
                'amount': 0.5
            }
            self.executor._place_gtx_order_with_retry = MagicMock(return_value=mock_order)
            
            res = self.executor.execute_entry(
                bot_id=bot_id,
                name=f"SOL_{excluded_status}_Bot",
                pair=pair,
                side="buy",
                amount=0.5,
                direction=direction,
                price=100.0,
                params={},
                exchange=self.mock_exchange,
                market_snapshot=None,
                bot_config=bot_config,
                bot_status=bot_status
            )
            
            # Verify the place was NOT skipped by DEDUP-GUARD
            self.executor._place_gtx_order_with_retry.assert_called_once()
            
            # Verify DEDUP-GUARD warning was NOT logged
            for call_args in mock_logger.warning.call_args_list:
                msg = call_args[0][0]
                self.assertNotIn("[DEDUP-GUARD]", msg)

