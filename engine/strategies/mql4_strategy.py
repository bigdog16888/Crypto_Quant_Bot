from .base import BaseStrategy
import pandas as pd
import engine.indicators as ta_custom

# Helper functions for MQL4-style indicators (kept local or could be moved to utils)
def iRSI(data: pd.Series, period: int) -> float:
    rsi_series = ta_custom.rsi(data, period=period)
    if rsi_series is None or rsi_series.empty:
        return 50.0
    return rsi_series.iloc[-1]

def iCCI(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> float:
    cci_series = ta_custom.cci(high, low, close, period=period)
    if cci_series is None or cci_series.empty:
        return 0.0
    return cci_series.iloc[-1]

def iBands(close: pd.Series, period: int, deviation: float):
    upper_s, mid_s, lower_s = ta_custom.bollinger_bands(close, period=period, deviation=deviation)
    if upper_s is None or upper_s.empty:
        return 0.0, 0.0, 0.0
    return upper_s.iloc[-1], mid_s.iloc[-1], lower_s.iloc[-1]

class MQL4Strategy(BaseStrategy):
    """
    Strategy porting the original MQL4 translation logic.
    Focuses on CCI, Bollinger Bands, and other indicators defined in the original EA.
    """
    def __init__(self, name: str = "MQL4_Legacy", params: dict = None):
        super().__init__(name, params)
        
        # Default MQL4 settings (can be overridden by params)
        self.use_any_entry = self.params.get('use_any_entry', False)
        self.force_market_cond = self.params.get('force_market_cond', 3)
        
        # Entry Switches (0=Off, 1=Standard, 2=Reverse)
        self.cci_entry = self.params.get('cci_entry', 0)
        self.bollinger_entry = self.params.get('bollinger_entry', 0)
        
        # Parameters
        self.cci_period = self.params.get('cci_period', 14)
        self.boll_period = self.params.get('boll_period', 10)
        self.boll_deviation = self.params.get('boll_deviation', 2.0)
        self.boll_distance = self.params.get('boll_distance', 10)

        # Stochastic
        self.stoch_entry = self.params.get('stoch_entry', 0)
        self.stoch_k = self.params.get('stoch_k', 5)
        self.stoch_d = self.params.get('stoch_d', 3)
        self.stoch_slowing = self.params.get('stoch_slowing', 3)
        self.stoch_lvl_up = self.params.get('stoch_lvl_up', 80)
        self.stoch_lvl_dn = self.params.get('stoch_lvl_dn', 20)
        
        # MACD
        self.macd_entry = self.params.get('macd_entry', 0)
        self.macd_fast = self.params.get('macd_fast', 12)
        self.macd_slow = self.params.get('macd_slow', 26)
        self.macd_sig = self.params.get('macd_sig', 9)

        # Timeframes (Default to None = Use Base)
        self.cci_tf = self.params.get('cci_tf', None)
        self.boll_tf = self.params.get('boll_tf', None)
        self.stoch_tf = self.params.get('stoch_tf', None)
        self.macd_tf = self.params.get('macd_tf', None)

    def _resample(self, data: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        """
        Resamples micro-timeframe data (e.g. 1m) to target timeframe (e.g. 1h).
        Expected data index is DateTimeIndex.
        """
        if timeframe is None or timeframe == '1m': # Assuming base is 1m for simplicity
            return data
            
        # Map string tf to pandas alias
        tf_map = {'1m': '1T', '5m': '5T', '15m': '15T', '30m': '30T', '1h': '1H', '4h': '4H', '1d': '1D'}
        pd_tf = tf_map.get(timeframe, '1H')
        
        try:
            # Ensure index is datetime
            df = data.copy()
            if not isinstance(df.index, pd.DatetimeIndex):
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                df.set_index('timestamp', inplace=True)
                
            resampled = df.resample(pd_tf).agg({
                'open': 'first',
                'high': 'max',
                'low': 'min',
                'close': 'last',
                'volume': 'sum'
            }).dropna()
            
            return resampled
        except Exception as e:
            # Fallback to original if resampling fails
            return data



    def check_signals(self, market_data: pd.DataFrame) -> tuple[bool, bool]:
        """
        Mimics the specific MQL4 signal aggregation logic.
        """
        buy_me = False
        sell_me = False
        ind_entry_count = 0
        
        # --- CCI ---
        if self.cci_entry > 0:
            # Resample if needed
            df_cci = self._resample(market_data, self.cci_tf)
            
            if 'high' in df_cci and 'low' in df_cci:
                cci_val = iCCI(df_cci['high'], df_cci['low'], df_cci['close'], self.cci_period)
            else:
                cci_val = iCCI(df_cci['close'], df_cci['close'], df_cci['close'], self.cci_period)
            
            is_uptrend = cci_val > 0 
            is_downtrend = cci_val < 0
            
            buy_signal = False
            sell_signal = False
            
            if is_uptrend:
                if self.cci_entry == 1: buy_signal = True
                elif self.cci_entry == 2: sell_signal = True 
            elif is_downtrend:
                if self.cci_entry == 1: sell_signal = True
                elif self.cci_entry == 2: buy_signal = True
                
            buy_me, sell_me, ind_entry_count = self._aggregate_signal(buy_me, sell_me, buy_signal, sell_signal, ind_entry_count)

        # --- Bollinger ---
        if self.bollinger_entry > 0:
            df_bb = self._resample(market_data, self.boll_tf)
            upper, mid, lower = iBands(df_bb['close'], self.boll_period, self.boll_deviation)
            
            ask = market_data['close'].iloc[-1] # Current Price always executes on CURRENT execution TF
            bid = market_data['close'].iloc[-1] 
            
            # Logic: Check if Current Price broke bands calculated on HIGHER TF? 
            # OR check if Higher TF Close broke Higher TF Bands?
            # Standard MTF: Current price relative to MTF Bands.
            
            # NOTE: Comparing realtime tick (execution TF) vs 1H Band.
            
            buy_signal = ask < (lower - (self.boll_distance * 0.0001)) 
            sell_signal = bid > (upper + (self.boll_distance * 0.0001))
            
            final_buy = False
            final_sell = False
            
            if buy_signal:
                if self.bollinger_entry == 1: final_buy = True
                elif self.bollinger_entry == 2: final_sell = True
            elif sell_signal:
                if self.bollinger_entry == 1: final_sell = True
                elif self.bollinger_entry == 2: final_buy = True
                
            buy_me, sell_me, ind_entry_count = self._aggregate_signal(buy_me, sell_me, final_buy, final_sell, ind_entry_count)

        # --- Stochastic ---
        if self.stoch_entry > 0:
            df_st = self._resample(market_data, self.stoch_tf)
            if 'high' in df_st and 'low' in df_st:
                k, d = iStochastic(df_st['high'], df_st['low'], df_st['close'], self.stoch_k, self.stoch_d, self.stoch_slowing)
            else:
                k, d = iStochastic(df_st['close'], df_st['close'], df_st['close'], self.stoch_k, self.stoch_d, self.stoch_slowing)
                
            is_oversold = (k < self.stoch_lvl_dn) and (d < self.stoch_lvl_dn)
            is_overbought = (k > self.stoch_lvl_up) and (d > self.stoch_lvl_up)
            
            buy_signal = is_oversold 
            sell_signal = is_overbought
            
            final_buy = False; final_sell = False
            if buy_signal:
                if self.stoch_entry == 1: final_buy = True
                elif self.stoch_entry == 2: final_sell = True
            elif sell_signal:
                if self.stoch_entry == 1: final_sell = True
                elif self.stoch_entry == 2: final_buy = True
                
            buy_me, sell_me, ind_entry_count = self._aggregate_signal(buy_me, sell_me, final_buy, final_sell, ind_entry_count)

        # --- MACD ---
        if self.macd_entry > 0:
            df_macd = self._resample(market_data, self.macd_tf)
            main, sig = iMACD(df_macd['close'], self.macd_fast, self.macd_slow, self.macd_sig)

            
            buy_signal = main > sig
            sell_signal = main < sig
            
            final_buy = False; final_sell = False
            if buy_signal:
                if self.macd_entry == 1: final_buy = True
                elif self.macd_entry == 2: final_sell = True
            elif sell_signal:
                if self.macd_entry == 1: final_sell = True
                elif self.macd_entry == 2: final_buy = True
                
            buy_me, sell_me, ind_entry_count = self._aggregate_signal(buy_me, sell_me, final_buy, final_sell, ind_entry_count)

        
        # Fallback
        if ind_entry_count == 0 and self.force_market_cond == 3:
            pass
            
        return buy_me, sell_me

    def _aggregate_signal(self, current_buy, current_sell, new_buy, new_sell, count):
        if self.use_any_entry:
            current_buy = current_buy or new_buy
            current_sell = current_sell or new_sell
        else:
            if count == 0:
                current_buy = new_buy
                current_sell = new_sell
            else:
                current_buy = current_buy and new_buy
                current_sell = current_sell and new_sell
                
        if current_buy and current_sell:
            current_buy = False
            current_sell = False
            
        return current_buy, current_sell, count + 1

def iStochastic(high, low, close, k, d, slowing):
    k_s, d_s = ta_custom.stochastic(high, low, close, k_period=k, d_period=d, slowing=slowing)
    if k_s is None or k_s.empty: return 50.0, 50.0
    return k_s.iloc[-1], d_s.iloc[-1]

def iMACD(close, fast, slow, signal):
    macd_l, sig_l = ta_custom.macd(close, fast=fast, slow=slow, signal=signal)
    if macd_l is None or macd_l.empty: return 0.0, 0.0
    return macd_l.iloc[-1], sig_l.iloc[-1]

