
import unittest
from unittest.mock import MagicMock, patch
import os
import sys

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from config.settings import config

class TestExchangeIntegration(unittest.TestCase):
    def setUp(self):
        # Save original config values
        self.orig_testnet = config.TESTNET
        self.orig_market_type = config.MARKET_TYPE
        
    def tearDown(self):
        # Restore config
        config.TESTNET = self.orig_testnet
        config.MARKET_TYPE = self.orig_market_type

    @patch('engine.exchange_interface.ccxt.binance')
    def test_initialization_futures_testnet(self, mock_ccxt_binance):
        """Test that ExchangeInterface initializes correctly for Futures Testnet"""
        # Setup Config
        config.TESTNET = True
        config.MARKET_TYPE = 'future'
        
        # Mock the exchange instance
        mock_exchange_instance = MagicMock()
        mock_exchange_instance.urls = {'api': {}} # Initialize with empty dict to allow update
        mock_ccxt_binance.return_value = mock_exchange_instance
        
        # Initialize
        ex = ExchangeInterface(market_type='future')
        
        # Verify defaultType option
        # CCXT constructor takes a single dict argument
        args, _ = mock_ccxt_binance.call_args
        config_dict = args[0]
        self.assertEqual(config_dict['options']['defaultType'], 'future')
        
        # Verify Testnet URL Overrides (Crucial for the fix)
        # Check if urls['api'] was updated with fapi endpoints
        # Note: In the actual code, we do self.exchange.urls['api'].update(...)
        # So we check if update was called or the dict has the keys
        # Since we mocked the instance, we can check the dict state if logic ran
        self.assertTrue('fapiPublic' in mock_exchange_instance.urls['api'], 
                       "Should have added fapiPublic to API URLs")
        self.assertIn('testnet.binancefuture.com', mock_exchange_instance.urls['api']['fapiPublic'],
                     "Should point to testnet URL")

    @patch('engine.exchange_interface.ccxt.binance')
    def test_fetch_balance_params(self, mock_ccxt_binance):
        """Test that fetch_balance passes type='future' param"""
        config.TESTNET = True
        
        # Mock
        mock_exchange_instance = MagicMock()
        # Setup options to simulate futures mode
        mock_exchange_instance.options = {'defaultType': 'future'}
        mock_ccxt_binance.return_value = mock_exchange_instance
        
        ex = ExchangeInterface(market_type='future')
        
        # Call fetch_balance
        ex.fetch_balance()
        
        # Verify underlying call
        mock_exchange_instance.fetch_balance.assert_called_with(params={'type': 'future'})

if __name__ == '__main__':
    unittest.main()
