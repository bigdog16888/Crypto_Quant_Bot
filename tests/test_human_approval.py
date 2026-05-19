import unittest
import os
import json
import sys
from unittest.mock import patch, MagicMock

sys.path.append(os.getcwd())

# Import necessary modules
from config.settings import config as app_config
from engine.exchange_interface import ExchangeInterface
from engine.database import safe_wipe_bot, reset_bot_after_tp
from engine.exceptions import APIError


class TestHumanApproval(unittest.TestCase):
    def set_approval_flag(self, value):
        # Propagate value to Config class and any config objects across all loaded modules
        try:
            from config.settings import Config
            Config.REQUIRE_HUMAN_APPROVAL = value
        except:
            pass
            
        for name, module in list(sys.modules.items()):
            if not module:
                continue
            # If module has a 'config' object
            if hasattr(module, 'config'):
                cfg = getattr(module, 'config')
                if hasattr(cfg, 'REQUIRE_HUMAN_APPROVAL'):
                    try:
                        cfg.REQUIRE_HUMAN_APPROVAL = value
                    except:
                        pass
            # If module has 'Config' class
            if hasattr(module, 'Config'):
                cls = getattr(module, 'Config')
                try:
                    cls.REQUIRE_HUMAN_APPROVAL = value
                    if hasattr(cls, 'config'):
                        cls.config.REQUIRE_HUMAN_APPROVAL = value
                except:
                    pass

    def setUp(self):
        # Force the safety gate to be ON for testing
        self._original_require_human_approval = app_config.REQUIRE_HUMAN_APPROVAL
        self.set_approval_flag(True)
        app_config.REQUIRE_HUMAN_APPROVAL = True
        
        # Ensure log file path exists and clean it
        self.blocked_log_path = os.path.join(app_config.ROOT_DIR, "blocked_actions.log")
        if os.path.exists(self.blocked_log_path):
            os.remove(self.blocked_log_path)
            
    def tearDown(self):
        # Restore configuration
        self.set_approval_flag(self._original_require_human_approval)
        app_config.REQUIRE_HUMAN_APPROVAL = self._original_require_human_approval
        
        # Cleanup
        if os.path.exists(self.blocked_log_path):
            os.remove(self.blocked_log_path)

    @patch('engine.exchange_interface.ExchangeInterface._raw_request')
    def test_autonomous_market_order_blocked(self, mock_raw_request):
        # Mock exchange to prevent any actual network calls
        ex = ExchangeInterface(market_type='future')
        ex.exchange = MagicMock()
        
        # 1. Test that an autonomous market order throws a ValueError
        with self.assertRaises(ValueError) as context:
            ex.create_order('BTC/USDT', 'market', 'sell', 1.0, emergency=True, _audit_cursor=MagicMock())
            
        self.assertIn("HUMAN-APPROVAL-REQUIRED", str(context.exception))
        
        # 2. Verify that the blocked_actions.log file was written to
        self.assertTrue(os.path.exists(self.blocked_log_path))
        with open(self.blocked_log_path, 'r', encoding='utf-8') as f:
            log_content = f.read()
        self.assertIn("BLOCKED-ACTION", log_content)
        self.assertIn("MARKET", log_content)
        self.assertIn("BTC/USDT", log_content)
        
    @patch('engine.exchange_interface.ExchangeInterface._raw_request')
    def test_human_approved_market_order_passes(self, mock_raw_request):
        # Mock exchange to simulate a successful API call
        mock_raw_request.return_value = {'orderId': '12345', 'status': 'open', 'clientOrderId': 'test'}
        ex = ExchangeInterface(market_type='future')
        
        # 1. Test passing human_approved via kwarg
        try:
            res = ex.create_order('ETH/USDT', 'market', 'buy', 0.5, human_approved=True, emergency=True, _audit_cursor=MagicMock())
            self.assertEqual(res.get('id'), '12345')
        except ValueError:
            self.fail("create_order raised ValueError unexpectedly when human_approved=True")
            
        # 2. Test passing human_approved inside params (as UI does)
        try:
            res = ex.create_order('ETH/USDT', 'market', 'buy', 0.5, params={'human_approved': True}, emergency=True, _audit_cursor=MagicMock())
            self.assertEqual(res.get('id'), '12345')
        except ValueError:
            self.fail("create_order raised ValueError unexpectedly when params={'human_approved': True}")

    @patch('engine.database.get_connection')
    def test_autonomous_safe_wipe_blocked(self, mock_get_connection):
        # Mock connection to return cycle_phase 'ACTIVE' and total_invested > 0
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        # Mock the Guard 1 fetchone
        mock_cursor.execute.return_value.fetchone.return_value = ('ACTIVE', 100.0)
        mock_conn.cursor.return_value = mock_cursor
        mock_get_connection.return_value = mock_conn
        
        # Call autonomous safe_wipe_bot (human_approved defaults to False)
        result = safe_wipe_bot(999, 'SOL/USDT', 'LONG', 'Test Wipe', human_approved=False)
        
        # Must return False because it was blocked by the gate
        self.assertFalse(result)
        
        # Verify blocked_actions.log
        self.assertTrue(os.path.exists(self.blocked_log_path))
        with open(self.blocked_log_path, 'r', encoding='utf-8') as f:
            log_content = f.read()
        self.assertIn("BLOCKED-ACTION", log_content)
        self.assertIn("SYSTEM_WIPE", log_content)

    @patch('engine.database._reset_bot_after_tp_internal')
    @patch('engine.database.get_connection')
    def test_autonomous_reset_bot_after_tp_destructive_blocked(self, mock_get_connection, mock_internal):
        # Call autonomous reset_bot_after_tp with a destructive action
        reset_bot_after_tp(888, 0.0, direction='SHORT', action_label='EMERGENCY_CLOSE', human_approved=False)
        
        # The internal function should NEVER be called because it was blocked
        mock_internal.assert_not_called()
        
        # Verify blocked_actions.log
        self.assertTrue(os.path.exists(self.blocked_log_path))
        with open(self.blocked_log_path, 'r', encoding='utf-8') as f:
            log_content = f.read()
        self.assertIn("BLOCKED-ACTION", log_content)
        self.assertIn("EMERGENCY_CLOSE", log_content)
        
    @patch('engine.database._reset_bot_after_tp_internal')
    @patch('engine.database.get_connection')
    def test_human_approved_reset_bot_after_tp_passes(self, mock_get_connection, mock_internal):
        # Call reset_bot_after_tp with human_approved=True
        reset_bot_after_tp(777, 0.0, direction='LONG', action_label='MANUAL_CLOSE', human_approved=True)
        
        # The internal function MUST be called
        mock_internal.assert_called_once()

    @patch('engine.bot_executor.time.sleep')
    @patch('engine.bot_executor.ExchangeInterface')
    def test_manual_gate_blocks_when_not_invested(self, mock_exchange_class, mock_sleep):
        from engine.bot_executor import BotExecutor
        
        # Instantiate BotExecutor
        mock_runner = MagicMock()
        executor = BotExecutor(runner=mock_runner)
        
        # db_invested = 0.0, REQUIRE_MANUAL in status
        bot_data = (
            123,                 # bot_id
            'test_bot',          # name
            'BTC/USDT',          # pair
            'LONG',              # direction
            'grid',              # strategy_type
            '{"market_type": "future"}', # config_json
            0.0,                 # db_invested (not in trade)
            0,                   # db_step
            30.0,                # rsi_limit
            True,                # is_active
            10.0,                # base_size
            1.5,                 # martingale_multiplier
            'REQUIRE_MANUAL_PROOF' # bot_status_str
        )
        
        res_sleep, res_trade = executor.process_bot(bot_data, exchange_snapshot={})
        # Should return None, None as it was suspended
        self.assertIsNone(res_sleep)
        self.assertIsNone(res_trade)

    @patch('engine.bot_executor.time.sleep')
    @patch('engine.bot_executor.ExchangeInterface')
    def test_manual_gate_does_not_block_when_invested(self, mock_exchange_class, mock_sleep):
        from engine.bot_executor import BotExecutor
        
        # Instantiate BotExecutor
        mock_runner = MagicMock()
        executor = BotExecutor(runner=mock_runner)
        
        # db_invested = 100.0 (in trade), REQUIRE_MANUAL in status
        bot_data = (
            123,                 # bot_id
            'test_bot',          # name
            'BTC/USDT',          # pair
            'LONG',              # direction
            'grid',              # strategy_type
            '{"market_type": "future"}', # config_json
            100.0,               # db_invested (> 0, in trade)
            0,                   # db_step
            30.0,                # rsi_limit
            True,                # is_active
            10.0,                # base_size
            1.5,                 # martingale_multiplier
            'REQUIRE_MANUAL_PROOF' # bot_status_str
        )
        
        # Define a custom exception inheriting from BaseException so it bypasses try...except Exception blocks
        class PassedGate(BaseException):
            pass
            
        # Mock exchange interface instance returned by _get_thread_exchange
        mock_exchange = MagicMock()
        # Let's make get_last_price raise an exception to prove it bypassed the manual gate check and reached the exchange interaction!
        mock_exchange.get_last_price.side_effect = PassedGate("Passed manual-gate check and reached exchange interaction!")
        mock_exchange_class.return_value = mock_exchange
        
        # We need to make sure _get_thread_exchange returns our mock_exchange
        with patch.object(executor, '_get_thread_exchange', return_value=mock_exchange):
            with self.assertRaises(PassedGate) as context:
                executor.process_bot(bot_data, exchange_snapshot={})
            
            self.assertEqual(str(context.exception), "Passed manual-gate check and reached exchange interaction!")


if __name__ == '__main__':
    unittest.main()
