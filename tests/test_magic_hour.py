
import unittest
from unittest.mock import MagicMock
import pandas as pd
from datetime import datetime, timedelta
import pytz
import sys
import os

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.strategies.magic_hour_strategy import MagicHourStrategy

class TestMagicHourStrategy(unittest.TestCase):
    def setUp(self):
        self.strategy = MagicHourStrategy(params={
            'magic_hour': 7,          # 07:00 NY
            'analysis_duration': 3,   # 3 Hours window
            'stop_loss_ext': 1.0      # 100% Extension Stop
        })
        
        # Create base time: Today 07:00 NY
        ny_tz = pytz.timezone('America/New_York')
        now_ny = datetime.now(ny_tz).replace(hour=7, minute=0, second=0, microsecond=0)
        self.magic_start = now_ny
        self.magic_end = now_ny + timedelta(hours=1)
        
    def create_mock_data(self, price_pattern):
        """Helper to create OHLCV dataframe from price pattern."""
        # Generate timestamps starting from 06:00 NY (Pre-Magic)
        start_time = self.magic_start - timedelta(hours=1)
        timestamps = [start_time + timedelta(minutes=i) for i in range(len(price_pattern))]
        
        data = []
        for ts, price in zip(timestamps, price_pattern):
            # Convert to UTC timestamp ms for index compatibility
            utc_ts = ts.astimezone(pytz.UTC)
            data.append({
                'timestamp': utc_ts,
                'open': price,
                'high': price + 1,
                'low': price - 1,
                'close': price,
                'volume': 100
            })
            
        df = pd.DataFrame(data)
        df.set_index('timestamp', inplace=True)
        return df

    def test_range_identification(self):
        """Verify the strategy correctly identifies the Magic Hour range."""
        # 06:00 - 07:00: Pre-market (100)
        # 07:00 - 08:00: Magic Hour (Range 100-110)
        # 08:00 - 09:00: Post (105)
        
        # 60 mins pre, 60 mins magic, 60 mins post
        prices = [100]*60 + [100]*30 + [110]*30 + [105]*60
        # In magic hour (indices 60-120), low is ~99 (price-1), high is ~111 (price+1)
        # Actually our mock sets high=price+1, low=price-1
        # So range high = 111, low = 99. Range = 12. Mid = 105.
        
        df = self.create_mock_data(prices)
        
        # Run check at a time inside analysis window (e.g. 08:30)
        # 60+60+30 = 150 minutes in
        subset = df.iloc[:150]
        
        buy, sell = self.strategy.check_signals(subset)
        
        # Verify stored range
        self.assertIsNotNone(self.strategy.daily_high)
        self.assertEqual(self.strategy.daily_high, 111)
        self.assertEqual(self.strategy.daily_low, 99)
        self.assertEqual(self.strategy.daily_mid, 105)

    def test_bullish_reversion_signal(self):
        """Test BUY signal when price breaks LOW (extension) and reverts."""
        # Range: 100-110 (Mid 105)
        # Breakout Low: 95 (Extension)
        # Stop Loss Ext: 1.0 * 10 = 10. Invalid line = 90.
        # Price 95 is < Low (100) and > Invalid (90) -> SIGNAL BUY
        
        prices = [105]*60 + [100]*30 + [110]*30 # Magic Hour (100-110)
        prices += [95] # Breakout Low
        
        df = self.create_mock_data(prices)
        buy, sell = self.strategy.check_signals(df)
        
        self.assertTrue(buy, "Should generate BUY signal on breakdown")
        self.assertFalse(sell)

    def test_bearish_reversion_signal(self):
        """Test SELL signal when price breaks HIGH (extension)."""
        # Range: 100-110
        # Breakout High: 115
        # Invalid Line: 110 + 10 = 120.
        # Price 115 is > High (110) and < Invalid (120) -> SIGNAL SELL
        
        prices = [105]*60 + [100]*30 + [110]*30 # Magic Hour
        prices += [115] # Breakout High
        
        df = self.create_mock_data(prices)
        buy, sell = self.strategy.check_signals(df)
        
        self.assertTrue(sell, "Should generate SELL signal on breakout")
        self.assertFalse(buy)

    def test_invalidation(self):
        """Test NO signal if price goes too deep (Graveyard)."""
        # Range: 100-110 (Size 10)
        # Breakout High: 125 (Extension 1.5x)
        # Invalid Line: 120
        # Price 125 > 120 -> NO TRADE (Too risky)
        
        prices = [105]*60 + [100]*30 + [110]*30
        prices += [125]
        
        df = self.create_mock_data(prices)
        buy, sell = self.strategy.check_signals(df)
        
        self.assertFalse(sell, "Should NOT signal if price invalidates logic")
        self.assertFalse(buy)

if __name__ == '__main__':
    unittest.main()
