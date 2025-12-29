import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Ensure root is in path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from engine.runner import BotRunner

class TestBotRunner(unittest.TestCase):
    
    @patch('engine.runner.get_connection')
    @patch('engine.runner.ExchangeInterface')
    def test_run_cycle_dry_run(self, MockExchange, mock_get_conn):
        """
        Verifies that the runner:
        1. Fetches active bots.
        2. Initializes the strategy.
        3. Fetches market data.
        4. Calls execute_entry when a signal is forced.
        """
        
        # 1. Setup Mock DB
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        
        # Returns one bot: (id, name, pair, direction, type, config, size, mm, rsi)
        # We inject a specific config to force a simple CCI entry if possible, 
        # or we just rely on the Strategy Mocking. 
        # Actually, let's just let the strategy run but Mock the DATA to force a signal?
        # Easier: Mock the strategy class itself or its result? 
        # Let's mock the process_bot's internal strategy creation or just let it run with "perfect" data.
        
        # Let's stick to system integration:
        fake_config = '{"timeframe": "1h", "cci_entry": 1, "cci_period": 14}' 
        mock_cursor.fetchall.return_value = [
            (1, "TestBot", "BTC/USDT", "LONG", "MQL4", fake_config, 10.0, 1.5, 30.0)
        ]

        # 2. Setup Mock Exchange
        mock_exchange_instance = MockExchange.return_value
        # Return 100 rows of data where price is going UP for Buy Signal?
        # Or easier: Mock the Strategy Signal check? 
        # Let's Mock fetch_ohlcv to return data.
        
        dates = pd.date_range(start='2024-01-01', periods=100, freq='H')
        data = [[t.value//10**6, 100, 105, 95, 102, 1000] for t in dates] # Valid OHLCV structure
        # Manipulate last candle to be bullish for CCI? 
        # Actually, let's just assert that fetch_ohlcv WAS called. 
        # Generating precise indicator signals via mock data is complex.
        mock_exchange_instance.fetch_ohlcv.return_value = data
        
        # 3. Initialize Runner
        runner = BotRunner()
        
        # 4. Run Cycle
        # We utilize a Spy or Mock on 'execute_entry' to see if it gets triggered if we could force it.
        # But without forcing specific data, we might not get a signal. 
        # Let's Mock check_signals on the strategy.
        
        with patch('engine.runner.MQL4Strategy') as MockStrategy:
            mock_strategy_instance = MockStrategy.return_value
            # Force a BUY signal
            mock_strategy_instance.check_signals.return_value = (True, False)
            
            # Watch execute_entry
            with patch.object(runner, 'execute_entry') as mock_execute:
                runner.run_cycle()
                
                # Asserts
                mock_cursor.execute.assert_called() # DB Query ran
                mock_exchange_instance.fetch_ohlcv.assert_called_with(symbol="BTC/USDT", timeframe="1h", limit=100) # Data fetched
                mock_strategy_instance.check_signals.assert_called() # Strategy checked
                
                # Did we execute?
                mock_execute.assert_called_with(1, "TestBot", "BTC/USDT", 'buy', 10.0)
                print("Test passed: Runner cycle executed entry on signal.")

if __name__ == '__main__':
    unittest.main()
