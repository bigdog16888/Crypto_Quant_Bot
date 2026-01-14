from .base import BaseStrategy
import pandas as pd
from datetime import datetime, timedelta
import pytz

class MagicHourStrategy(BaseStrategy):
    """
    Magic Hour Mean Reversion Strategy.
    Based on the concept that breakouts from specific hourly ranges (e.g. 07:00 NY)
    tend to revert to the range midpoint (50% mean reversion).
    """
    def __init__(self, name: str = "MagicHour", params: dict = None):
        super().__init__(name, params)
        
        # Strategy Parameters
        self.magic_hour = int(self.params.get('magic_hour', 7)) 
        self.timezone_str = self.params.get('timezone', 'America/New_York') # Configurable Timezone
        self.analysis_duration = int(self.params.get('analysis_duration', 3)) 
        self.stop_loss_ext = float(self.params.get('stop_loss_ext', 1.0)) 
        
        # State tracking
        self.daily_high = None
        self.daily_low = None
        self.daily_mid = None

    def _get_target_time(self, timestamp: datetime | float | int) -> datetime:
        """Converts timestamp to Target Strategy Timezone."""
        if isinstance(timestamp, (int, float)):
            dt = datetime.fromtimestamp(timestamp / 1000, tz=pytz.UTC)
        else:
            dt = timestamp.replace(tzinfo=pytz.UTC) if timestamp.tzinfo is None else timestamp
            
        try:
            target_tz = pytz.timezone(self.timezone_str)
        except:
            target_tz = pytz.timezone('America/New_York')
            
        return dt.astimezone(target_tz)

    def _get_magic_range(self, df: pd.DataFrame, magic_start: datetime, magic_end: datetime):
        """Calculates High, Low, Mid for the Magic Hour."""
        # Convert window to UTC to match dataframe index
        magic_start_utc = magic_start.astimezone(pytz.UTC)
        magic_end_utc = magic_end.astimezone(pytz.UTC)
        
        mask = (df.index >= magic_start_utc) & (df.index < magic_end_utc)
        magic_data = df.loc[mask]
        
        if magic_data.empty:
            return None
            
        m_high = magic_data['high'].max()
        m_low = magic_data['low'].min()
        
        return {
            'high': m_high,
            'low': m_low,
            'mid': (m_high + m_low) / 2,
            'range': m_high - m_low
        }

    def check_signals(self, market_data: pd.DataFrame) -> tuple[bool, bool]:
        """
        Checks for Magic Hour breakouts and reversion signals.
        """
        if market_data.empty:
            return False, False

        # Ensure index is datetime
        df = market_data.copy()
        if not isinstance(df.index, pd.DatetimeIndex):
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)

        # Get current candle info
        current_candle = df.iloc[-1]
        current_time = self._get_target_time(current_candle.name)
        current_price = current_candle['close']
        
        # Determine relevant times for today
        magic_start = current_time.replace(hour=self.magic_hour, minute=0, second=0, microsecond=0)
        magic_end = magic_start + timedelta(hours=1)
        analysis_end = magic_end + timedelta(hours=self.analysis_duration)
        
        # Check if we are in the analysis window
        if not (magic_end <= current_time <= analysis_end):
            return False, False

        # Calculate Magic Range
        range_data = self._get_magic_range(df, magic_start, magic_end)
        if not range_data:
            return False, False
            
        m_high = range_data['high']
        m_low = range_data['low']
        m_range = range_data['range']
        
        # Update state
        self.daily_high = m_high
        self.daily_low = m_low
        self.daily_mid = range_data['mid']
        
        # Check Signals
        buffer = m_range * 0.05
        buy_signal = False
        sell_signal = False
        
        # SELL Signal: Price > High (Extension) but < Invalid
        if current_price > (m_high + buffer):
            max_extension = m_high + (m_range * self.stop_loss_ext)
            if current_price < max_extension:
                sell_signal = True
                
        # BUY Signal: Price < Low (Extension) but > Invalid
        if current_price < (m_low - buffer):
            max_extension = m_low - (m_range * self.stop_loss_ext)
            if current_price > max_extension:
                buy_signal = True
                
        return buy_signal, sell_signal

    def calculate_next_grid_price(self, direction: str, current_price: float, avg_entry: float, current_step: int, market_data=None) -> float:
        """
        For Martingale/Grid compatibility. 
        """
        spacing = self.params.get('base_grid', 100.0)
        if direction == 'LONG':
            return current_price - spacing
        return current_price + spacing
