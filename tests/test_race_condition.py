import unittest
from unittest.mock import MagicMock, patch
import threading
import time
import sys
import os

# Add project root to sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

# --- Mock Infrastructure ---

class MockExchangeInterface:
    def __init__(self, market_type='future'):
        self.market_type = market_type
        self.orders = {} 
        self.lock = threading.Lock()
        
        # Mock ccxt exchange object
        self.exchange = MagicMock()
        self.exchange.amount_to_precision = lambda s, a: a
        self.exchange.price_to_precision = lambda s, p: p
        self.exchange.fetch_order = self.fetch_order

    def cancel_orders_by_bot_id(self, bot_id, symbol):
        return 0

    def validate_order(self, pair, side, qty, price):
        return True, qty, price, ""

    def fetch_open_orders(self, pair, force_refresh=False):
        # Return a list of orders for this pair
        with self.lock:
            return list(self.orders.values())

    def create_order(self, pair, type, side, amount, price=None, params=None, bot_id=None, order_type=None):
        params = params or {}
        client_oid = params.get('clientOrderId')
        
        # Simulate Network Latency to encourage Race Conditions
        time.sleep(0.05) 
        
        with self.lock:
            # Check for Duplicate Client Order ID (Simulating Exchange Behavior)
            if client_oid:
                for o in self.orders.values():
                    if o.get('clientOrderId') == client_oid:
                        # Exchange raises error on duplicate ID
                        raise Exception(f"Duplicate Order ID: {client_oid}")
            
            # Create Order
            order_id = f"ord_{len(self.orders) + 1}"
            order = {
                'id': order_id,
                'symbol': pair,
                'status': 'open',
                'price': price,
                'amount': amount,
                'side': side,
                'type': type,
                'clientOrderId': client_oid,
                'filled': 0.0,
                'average': 0.0,
                'info': {'mock': True}
            }
            self.orders[order_id] = order
            return order

    def cancel_order(self, order_id, pair):
        with self.lock:
            if order_id in self.orders:
                del self.orders[order_id]

    def fetch_order(self, order_id, symbol=None):
        with self.lock:
            return self.orders.get(order_id)

    def wait_for_fill(self, order, timeout_seconds=5):
        return order 

    def get_last_price(self, pair):
        return 50000.0
    
    def _ensure_markets(self):
        pass

# Mock Exceptions
class MockAPIError(Exception): pass
class MockInsufficientFundsError(Exception): pass
class MockOrderNotFoundError(Exception): pass
class MockNetworkError(Exception): pass

# --- Test Case ---
class TestRaceCondition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Mock dependencies
        cls.mocks = {
            'engine.database': MagicMock(),
            'engine.strategies': MagicMock(),
            'engine.strategies.martingale_strategy': MagicMock(),
            'engine.risk_manager': MagicMock(),
            'engine.manager': MagicMock(),
            'engine.bot_management': MagicMock(),
            'config': MagicMock(),
            'config.settings': MagicMock(),
            'config.constants': MagicMock(),
            'engine.exceptions': MagicMock(),
            'ccxt': MagicMock(),
            'pandas': MagicMock(),
        }
        
        # Configure specific mocks
        cls.mocks['config.settings'].config.DRY_RUN = False
        cls.mocks['config.settings'].config.MARKET_TYPE = 'future'
        cls.mocks['config.constants'].MAX_ORDERS_PER_CYCLE = 100
        cls.mocks['config.constants'].MAX_ORDERS_PER_BOT_DAILY = 100
        
        # Define Exception Mocks on the exceptions module
        cls.mocks['engine.exceptions'].APIError = MockAPIError
        cls.mocks['engine.exceptions'].InsufficientFundsError = MockInsufficientFundsError
        cls.mocks['engine.exceptions'].OrderNotFoundError = MockOrderNotFoundError
        cls.mocks['engine.exceptions'].NetworkError = MockNetworkError
        
        # Apply patches
        cls.patcher = patch.dict('sys.modules', cls.mocks)
        cls.patcher.start()

        # Import BotExecutor
        from engine.bot_executor import BotExecutor
        cls.BotExecutorClass = BotExecutor
        
    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()

    def setUp(self):
        # Shared State
        self.shared_orders = {}
        self.shared_lock = threading.Lock()
        
        # Mock get_thread_exchange
        self.mock_exchange = MockExchangeInterface()
        self.mock_exchange.orders = self.shared_orders
        self.mock_exchange.lock = self.shared_lock
        
        self.runner = MagicMock()
        self.runner.exchange = self.mock_exchange
        self.runner.strategies = {} 
        
        # Create instance
        self.bot_executor = self.BotExecutorClass(self.runner)
        
        # Mock deterministic IDs
        self.bot_executor._gen_id_v2 = lambda bot_id, type_str, step_index: f"CQB_{bot_id}_{type_str}_{step_index}"
        self.bot_executor._generate_deterministic_id = lambda bot_id, type_str, step_index: f"CQB_{bot_id}_{type_str}_{step_index}"

    def test_maintain_orders_race_condition(self):
        """
        Simulate concurrent calls to maintain_orders.
        Verify that exactly ONE Grid order and ONE TP order are created.
        """
        bot_id = 44
        bot_name = "TestBot"
        pair = "BTC/USDT"
        direction = "LONG"
        
        mission = {
            'action': 'maintain_orders',
            'bot_id': bot_id,
            'bot_name': bot_name,
            'pair': pair,
            'direction': direction,
            'grid_price': 49000.0,
            'grid_qty': 0.001,
            'grid_step': 1,
            'tp_price': 51000.0,
            'tp_qty': 0.001,
            'step': 1
        }
        
        mock_status = {
            'id': bot_id,
            'name': bot_name,
            'pair': pair,
            'current_step': 1,
            'total_invested': 100.0,
            'avg_entry_price': 50000.0,
            'target_tp_price': 51000.0,
            'last_exit_time': 0,
            'basket_start_time': 0
        }
        
        with patch('engine.bot_executor.get_bot_status', return_value=mock_status):
            def worker():
                self.bot_executor.execute_mission(mission, exchange=self.mock_exchange, open_orders_snapshot=None)

            threads = []
            for _ in range(10):
                t = threading.Thread(target=worker)
                threads.append(t)
                t.start()
            
            for t in threads:
                t.join()

        # ASSERTIONS
        grid_orders = [o for o in self.shared_orders.values() if '_GRID_' in o.get('clientOrderId', '')]
        tp_orders = [o for o in self.shared_orders.values() if '_TP_' in o.get('clientOrderId', '')]
        
        self.assertEqual(len(grid_orders), 1)
        self.assertEqual(len(tp_orders), 1)

if __name__ == '__main__':
    unittest.main()
