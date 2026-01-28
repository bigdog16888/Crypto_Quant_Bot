from .base import BaseStrategy
import pandas as pd
import engine.indicators as ta_custom

# Helper functions for standard indicators (kept local or could be moved to utils)

def iATR(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> float:
    atr_series = ta_custom.atr(high, low, close, period=period)
    if atr_series is None or atr_series.empty:
        return 0.0
    return atr_series.iloc[-1]

def iATRPercentile(high: pd.Series, low: pd.Series, close: pd.Series, period_atr: int, period_lookback: int) -> float:
    return ta_custom.atr_percentile(high, low, close, period_atr=period_atr, period_lookback=period_lookback)

def iATRExpansion(open_p: float, current_p: float, high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> float:
    """Calculates move from open as a percentage of ATR."""
    atr_val = iATR(high, low, close, period)
    if atr_val == 0: return 0.0
    return (current_p - open_p) / atr_val * 100.0

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

def iMA(close: pd.Series, period: int, ma_type: str) -> float:
    """
    Moving Average Helper (SMA/EMA).
    Returns the last value.
    """
    if close is None or close.empty: return 0.0
    
    if ma_type.upper() == 'EMA':
        ma_s = close.ewm(span=period, adjust=False).mean()
    else: # SMA
        ma_s = close.rolling(window=period).mean()
        
    if ma_s is None or ma_s.empty: return 0.0
    return ma_s.iloc[-1]

class MartingaleStrategy(BaseStrategy):
    """
    Martingale Grid DCA Strategy.
    Uses confluence of indicators (CCI, Bollinger, RSI, etc.) for entry timing.
    Originally ported from strategy logic.
    """
    def __init__(self, name: str = "Martingale_Grid", params: dict = None):
        super().__init__(name, params)
        
        # Default settings (can be overridden by params)
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
        self.stoch_tf = self.params.get('stoch_tf', None)
        self.macd_tf = self.params.get('macd_tf', None)

        # MA Trigger (Trigger 12)
        self.mode_ma = self.params.get('mode_ma', 0)
        self.ma_period = self.params.get('ma_period', 200)
        self.ma_tf = self.params.get('ma_tf', None)
        self.ma_type = self.params.get('ma_type', 'SMA')

        # Advanced Blessing 3 Features
        self.use_atr_grid = self.params.get('UseATRGrid', False)
        self.atr_grid_factor = self.params.get('ATRGridFactor', 1.0)
        self.atr_period = self.params.get('ATRPeriods', 21)
        self.atr_tf = self.params.get('ATR_Timeframe', None) # Specific TF for Grid ATR
        
        # Flexible Grid System (NEW)
        # ATR Mode: 'dynamic' (recalculate each cycle) or 'locked' (capture at first entry)
        self.atr_mode = self.params.get('ATRMode', 'dynamic')
        self.locked_atr = None  # Will store ATR value when mode is 'locked'
        
        # Step-based Grid Rules: List of rules applied by step range
        # Format: [{"start": 1, "end": 4, "type": "atr", "multiplier": 1.0},
        #          {"start": 5, "end": 7, "type": "atr", "multiplier": 1.1},
        #          {"start": 8, "end": 10, "type": "fixed", "value": 500}]
        # type: 'atr' (uses ATR * multiplier) or 'fixed' (uses fixed $ value)
        self.grid_step_rules = self.params.get('GridStepRules', [])
        
        self.use_hedge = self.params.get('UseHedge', False)
        self.hedge_start = self.params.get('HedgeStart', 20.0)
        self.lot_mult_hedge = self.params.get('LotMultHedge', 0.8)

        # MTF Confluence & Trigger Logic (New)
        self.use_mtf_confluence = self.params.get('UseMTFConfluence', False)
        self.mtf_tf = self.params.get('MTF_Timeframe', '1h') # The higher TF to check
        self.trigger_candles = self.params.get('TriggerCandles', 1) # Consecutive candles needed
        self.candle_counter = 0 # volatile state usually handled by checking back scan

    def _resample(self, data: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        """
        Resamples micro-timeframe data (e.g. 1m) to target timeframe (e.g. 1h).
        Expected data index is DateTimeIndex.
        """
        if timeframe is None or timeframe == '1m': # Assuming base is 1m for simplicity
            return data
            
        # Map string tf to pandas alias
        tf_map = {
            '1m': '1min', '5m': '5min', '15m': '15min', '30m': '30min', 
            '1h': '1h', '4h': '4h', '1d': '1D', '3d': '3D', '5d': '5D'
        }
        pd_tf = tf_map.get(timeframe, '1h')

        
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



    def check_pattern(self, series: pd.Series, mode: int, count: int) -> bool:
        """
        Generic pattern checker for any indicator series.
        mode: 1 (Consecutive Up), 2 (Consecutive Down)
        count: number of consecutive candles
        """
        if series is None or len(series) < count:
            return False
            
        last_vals = series.iloc[-count:]
        
        if mode == 1: # Consecutive Up
            # Check if each value is greater than the previous
            return all(last_vals.iloc[i] > last_vals.iloc[i-1] for i in range(1, len(last_vals)))
        elif mode == 2: # Consecutive Down
            return all(last_vals.iloc[i] < last_vals.iloc[i-1] for i in range(1, len(last_vals)))
            
        return False

    def check_signals(self, market_data: pd.DataFrame) -> tuple[bool, bool]:
        """
        Refined 8-Trigger Confluence logic.
        Validates 4 indicators + 4 patterns. ALL enabled triggers must pass.
        """
        # Directional allow flags
        buy_allow = True
        sell_allow = True
        triggers_active = 0
        
        # --- 1. Indicator Triggers (1-4) ---
        # CCI
        mode_cci = self.params.get('mode_cci', 0) # 0=Off, 1=Above, 2=Below
        if mode_cci > 0:
            triggers_active += 1
            df_cci = self._resample(market_data, self.params.get('cci_tf'))
            val = iCCI(df_cci['high'], df_cci['low'], df_cci['close'], self.params.get('cci_period', 14))
            lvl = self.params.get('cci_level', 100)
            if mode_cci == 1: # Above
                if val < lvl: buy_allow = sell_allow = False
            else: # Below
                if val > -lvl: buy_allow = sell_allow = False

        # Bollinger
        mode_boll = self.params.get('mode_boll', 0) # 0=Off, 1=Outside Lower (Buy-ish), 2=Outside Upper (Sell-ish)
        if mode_boll > 0:
            triggers_active += 1
            df_bb = self._resample(market_data, self.params.get('boll_tf'))
            upper, mid, lower = iBands(df_bb['close'], self.params.get('boll_period', 20), self.params.get('boll_deviation', 2.0))
            price = market_data['close'].iloc[-1]
            if mode_boll == 1: # Price < Lower (Buy entry)
                if price >= lower: buy_allow = False
            else: # Price > Upper (Sell entry)
                if price <= upper: sell_allow = False

        # Stochastic
        mode_stoch = self.params.get('mode_stoch', 0) # 0=Off, 1=Below DN (Oversold), 2=Above UP (Overbought)
        if mode_stoch > 0:
            triggers_active += 1
            df_st = self._resample(market_data, self.params.get('stoch_tf'))
            k, d = iStochastic(df_st['high'], df_st['low'], df_st['close'], self.params.get('stoch_k', 5), self.params.get('stoch_d', 3), self.params.get('stoch_slowing', 3))
            if mode_stoch == 1: # K & D < LVL_DN
                if k >= self.params.get('stoch_lvl_dn', 20) or d >= self.params.get('stoch_lvl_dn', 20): buy_allow = False
            else: # K & D > LVL_UP
                if k <= self.params.get('stoch_lvl_up', 80) or d <= self.params.get('stoch_lvl_up', 80): sell_allow = False

        # RSI (New/Refined)
        mode_rsi = self.params.get('mode_rsi', 0) # 0=Off, 1=Below, 2=Above
        if mode_rsi > 0:
            triggers_active += 1
            df_rsi = self._resample(market_data, self.params.get('rsi_tf'))
            val = iRSI(df_rsi['close'], self.params.get('rsi_period', 14))
            lvl = self.params.get('rsi_level', 30 if mode_rsi==1 else 70)
            if mode_rsi == 1: # Below
                if val >= lvl: buy_allow = False
            else: # Above
                if val <= lvl: sell_allow = False

        # --- 2. Pattern Triggers (5-8) (Refined) ---
        for p_idx in range(1, 5):
            p_mode = self.params.get(f'pat_{p_idx}_mode', 0) # 0=Off, 1=Consecutive Up, 2=Consecutive Down
            if p_mode > 0:
                triggers_active += 1
                p_tf = self.params.get(f'pat_{p_idx}_tf')
                df_p = self._resample(market_data, p_tf)
                
                # Indicator Awareness: watch_indicator param (e.g. 'Price', 'RSI', 'CCI')
                watch = self.params.get(f'pat_{p_idx}_source', 'Price')
                count = self.params.get(f'pat_{p_idx}_count', 3)
                
                series = None
                if watch == 'RSI':
                    series = ta_custom.rsi(df_p['close'], period=self.params.get('rsi_period', 14))
                elif watch == 'CCI':
                    series = ta_custom.cci(df_p['high'], df_p['low'], df_p['close'], period=self.params.get('cci_period', 14))
                else: # Default to Price
                    series = df_p['close']
                
                # Use helper method
                if not self.check_pattern(series, p_mode, count):
                    buy_allow = sell_allow = False

        # --- 3. Price Threshold Trigger (Trigger 9) ---
        mode_price = self.params.get('mode_price', 0) # 0=Off, 1=Above, 2=Below
        if mode_price > 0:
            triggers_active += 1
            current_price = market_data['close'].iloc[-1]
            threshold = self.params.get('price_threshold', 0.0)
            if mode_price == 1: # Above
                if current_price < threshold: buy_allow = sell_allow = False
            else: # Below
                if current_price > threshold: buy_allow = sell_allow = False

        # --- 4. ATR Percentile Trigger (Trigger 10) ---
        mode_atrp = self.params.get('mode_atrp', 0) # 0=Off, 1=Below Level (Low Vol), 2=Above Level (High Vol)
        if mode_atrp > 0:
            triggers_active += 1
            atrp_tf = self.params.get('atrp_tf', '1h')
            df_atrp = self._resample(market_data, atrp_tf)
            val = iATRPercentile(df_atrp['high'], df_atrp['low'], df_atrp['close'], 
                                self.params.get('atrp_period', 14), 
                                self.params.get('atrp_lookback', 100))
            lvl = self.params.get('atrp_level', 50.0)
            if mode_atrp == 1: # Below (Low Volatility filter)
                if val > lvl: buy_allow = sell_allow = False
            else: # Above (High Volatility filter)
                if val < lvl: buy_allow = sell_allow = False

        # --- 5. ATR Expansion Trigger (Trigger 11) ---
        mode_atre = self.params.get('mode_atre', 0) # 0=Off, 1=Move Up >= X%, 2=Move Down >= X%
        if mode_atre > 0:
            triggers_active += 1
            atre_tf = self.params.get('atre_tf', '1h')
            df_atre = self._resample(market_data, atre_tf)
            if not df_atre.empty:
                open_p = df_atre['open'].iloc[-1]
                curr_p = market_data['close'].iloc[-1]
                expansion = iATRExpansion(open_p, curr_p, df_atre['high'], df_atre['low'], df_atre['close'], self.params.get('atre_period', 14))
                target = self.params.get('atre_level', 100.0)
                
                if mode_atre == 1: # Move Up (Long trigger or Sell block)
                    if expansion < target: buy_allow = False
                else: # Move Down (Short trigger or Buy block)
                    if expansion > -target: sell_allow = False

        # --- 6. MA Trigger (Trigger 12) ---
        mode_ma = self.params.get('mode_ma', 0) # 0=Off, 1=Price > MA (Bullish), 2=Price < MA (Bearish)
        if mode_ma > 0:
            triggers_active += 1
            ma_tf = self.params.get('ma_tf', '1h')
            df_ma = self._resample(market_data, ma_tf)
            
            if not df_ma.empty:
                ma_val = iMA(df_ma['close'], self.params.get('ma_period', 200), self.params.get('ma_type', 'SMA'))
                curr_p = market_data['close'].iloc[-1]
                
                # Logic: Filter entries based on price relation to MA
                if mode_ma == 1: # Bullish Bias (Price MUST be > MA)
                    if curr_p <= ma_val: buy_allow = sell_allow = False
                elif mode_ma == 2: # Bearish Bias (Price MUST be < MA)
                    if curr_p >= ma_val: buy_allow = sell_allow = False

        # Final Confluence: All enabled must be true. 
        # For a Buy signal, buy_allow must be True and triggers_active > 0
        if triggers_active == 0:
            return False, False
            
        return buy_allow, sell_allow

    def calculate_lot_size(self, current_step: int, account_balance: float) -> float:
        """
        Calculates the order size ($USDC) for the next Martingale level.
        Based on Blessing 3 Multiplier or LotAdd logic.
        """
        base_size = self.params.get('base_size', 10.0)
        multiplier = self.params.get('martingale_multiplier', 1.5)
        
        if current_step == 0:
            return base_size
            
        # Standard Multiplier scaling
        return base_size * (multiplier ** current_step)

    def _calculate_atr(self, market_data) -> float:
        """
        Calculate ATR value from market data.
        Returns ATR in price units (e.g., $500 for BTC).
        """
        if market_data is None or market_data.empty:
            return 0.0
            
        atr_tf = self.atr_tf or '1h'
        atr_periods = getattr(self, 'atr_period', 14)
        atr_periods = min(max(atr_periods, 3), 240)
        
        df_atr = self._resample(market_data, atr_tf)
        
        if df_atr.empty or len(df_atr) < atr_periods:
            return 0.0
            
        # Calculate True Range
        tr1 = df_atr['high'] - df_atr['low']
        tr2 = (df_atr['high'] - df_atr['close'].shift()).abs()
        tr3 = (df_atr['low'] - df_atr['close'].shift()).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        return float(true_range.iloc[-atr_periods:].mean())
    
    def _get_grid_spacing_for_step(self, step: int, atr_value: float, current_price: float) -> float:
        """
        Get grid spacing for a specific step based on GridStepRules.
        
        Rules format: [
            {"start": 1, "end": 4, "type": "atr", "multiplier": 1.0},
            {"start": 5, "end": 7, "type": "atr", "multiplier": 1.1},
            {"start": 8, "end": 10, "type": "fixed", "value": 500}
        ]
        
        If no rule matches, falls back to default ATR * ATRGridFactor or 1.0% of current price.
        """
        # Check step-based rules first
        for rule in self.grid_step_rules:
            start = rule.get('start', 1)
            end = rule.get('end', 999)
            rule_type = rule.get('type', 'atr')
            
            if start <= step <= end:
                if rule_type == 'fixed':
                    # Fixed $ grid spacing
                    return float(rule.get('value', 100.0))
                elif rule_type == 'atr':
                    # ATR-based with custom multiplier
                    multiplier = float(rule.get('multiplier', 1.0))
                    if atr_value > 0:
                        return atr_value * multiplier
                    # Fallback if ATR is 0: 1.0% of current price
                    return current_price * 0.01 
        
        # No rule matched - use default behavior
        if self.use_atr_grid and atr_value > 0:
            return atr_value * self.atr_grid_factor
        
        # Final Fallback: 1.0% of current price
        return current_price * 0.01 
    
    def lock_atr(self, market_data) -> float:
        """
        Lock ATR value at first entry. Called when entering a new trade.
        Returns the locked ATR value.
        """
        atr_val = self._calculate_atr(market_data)
        self.locked_atr = atr_val
        return atr_val
    
    def reset_locked_atr(self):
        """Reset locked ATR when trade closes."""
        self.locked_atr = None

    def calculate_next_grid_price(self, direction: str, current_price: float, avg_entry: float, current_step: int, market_data=None) -> float:
        """
        Calculate next grid order price using flexible step-based rules.
        
        Supports:
        1. ATR Mode: 'locked' (use ATR captured at entry) or 'dynamic' (recalculate each cycle)
        2. Step-based Rules: Different spacing rules for different step ranges
           - type: 'atr' with multiplier (e.g., ATR * 1.1)
           - type: 'fixed' with value (e.g., $500 fixed spacing)
        
        Example GridStepRules:
        [
            {"start": 1, "end": 4, "type": "atr", "multiplier": 1.0},    # Steps 1-4: 100% ATR
            {"start": 5, "end": 7, "type": "atr", "multiplier": 1.1},    # Steps 5-7: ATR * 1.1
            {"start": 8, "end": 10, "type": "fixed", "value": 500}       # Steps 8-10: Fixed $500
        ]
        """
        next_step = current_step + 1
        
        # Determine which ATR value to use
        atr_value = 0.0
        if self.use_atr_grid or self.grid_step_rules:
            if self.atr_mode == 'locked' and self.locked_atr is not None:
                # Use locked ATR from first entry
                atr_value = self.locked_atr
            else:
                # Dynamic: recalculate ATR each cycle
                atr_value = self._calculate_atr(market_data)
                
                # Auto-lock on first entry if mode is 'locked' and not yet locked
                if self.atr_mode == 'locked' and self.locked_atr is None and atr_value > 0:
                    self.locked_atr = atr_value
        
        # Get spacing for this step
        grid_spacing = self._get_grid_spacing_for_step(next_step, atr_value, current_price)
        
        # Apply direction
        if direction == 'LONG':
            return current_price - grid_spacing
        else:
            return current_price + grid_spacing

    def calculate_projections(self, base_price: float, current_atr: float = None) -> list:
        """
        Generates a list of dictionaries representing the risk/investment at each step.
        Includes absolute price levels for grid, TP, and hedging.
        Uses step-based grid spacing rules for flexible grid calculation.
        """
        projections = []
        total_invested = 0
        total_qty = 0
        total_cost_basis = 0
        
        base_size = self.params.get('base_size', 10.0)
        multiplier = self.params.get('martingale_multiplier', 1.5)
        hedge_step = self.params.get('HedgeStartStep', 7)
        tp_type = self.params.get('TakeProfitType', 'USD')
        tp_target_usd = self.params.get('TakeProfitBase', 10.0)
        
        direction = 1 if self.params.get('direction', 'LONG').upper() == 'LONG' else -1
        fee_rate = self.params.get('fee_rate', 0.001)
        slippage_rate = self.params.get('slippage_rate', 0.0005)
        cost_factor = 1.0 + fee_rate + slippage_rate
        
        # Early Exit logic parameters
        ee_enabled = self.params.get('UseEarlyExit', False)
        decay_pct = self.params.get('DecayPercentPerInterval', 30.0) / 100.0
        
        # Determine ATR to use for projections
        atr_value = 0.0
        if self.use_atr_grid or self.grid_step_rules:
            if current_atr is not None:
                atr_value = current_atr
            else:
                # Default fallback ATR for projection
                atr_value = base_price * 0.01  # 1% of price as default
        
        # Track previous price for incremental grid calculation
        current_grid_price = base_price
        
        for i in range(self.params.get('max_steps', 10)):
            # 1. Determine Order Price
            if i == 0:
                order_price = base_price
            else:
                # Get spacing for this specific step (i is current step index 1..N)
                grid_spacing = self._get_grid_spacing_for_step(i, atr_value, current_grid_price)
                
                if direction == 1:  # LONG: Price goes down
                    order_price = current_grid_price - grid_spacing
                else:  # SHORT: Price goes up
                    order_price = current_grid_price + grid_spacing
                
                current_grid_price = order_price

            if order_price <= 0: order_price = 0.01 # Safety

            # 2. Determine Size
            step_size = base_size * (multiplier ** i)
            
            # 3. Update Position Totals
            invested_amount = step_size * cost_factor
            total_invested += invested_amount
            
            qty = step_size / order_price
            total_qty += qty
            total_cost_basis += (qty * order_price)
            
            avg_price = total_cost_basis / total_qty if total_qty > 0 else 0
            
            # 4. Calculate Take Profit Price
            if tp_type == 'Percent':
                # TP = AvgPrice * (1 + pct)
                tp_pct = self.params.get('TakeProfitPct', 1.0) / 100.0
                if direction == 1: # LONG
                    tp_price = avg_price * (1 + tp_pct)
                else: # SHORT
                    tp_price = avg_price * (1 - tp_pct)
            else:
                # USD Target
                if direction == 1: # LONG
                    tp_price = (total_cost_basis + tp_target_usd) / total_qty
                else: # SHORT
                    # For shorts: Sell Price < Avg Price. Profit = (Avg - Exit) * Qty
                    # Target = (Avg - Exit) * Qty => Exit = Avg - (Target / Qty)
                    # Note: Using total_cost_basis approx implies simplistic PnL, accurate enough for projection
                    tp_price = avg_price - (tp_target_usd / total_qty)
            
            # Apply Early Exit Decay (Simulated: 1 interval per step after step 0)
            if ee_enabled and i > 0:
                dist_to_be = tp_price - avg_price
                decay_factor = (1.0 - decay_pct) ** i
                tp_price = avg_price + (dist_to_be * decay_factor)
            
            # 5. Hedge Logic
            is_hedge = (i + 1) >= hedge_step if self.params.get('UseHedge') else False
            hedge_size = round(total_invested, 2) if is_hedge else 0.0

            # 6. Precision & Formatting
            prec = 2
            if base_price < 1.0: prec = 6
            elif base_price < 1000.0: prec = 4
            
            projection = {
                'step': i + 1,
                'price': round(order_price, prec),
                'order_size_usdc': round(step_size, 2),
                'total_invested_usdc': round(total_invested, 2),
                'tp_price': round(tp_price, prec),
                'hedge_size_usdc': hedge_size,
                'is_hedge': is_hedge
            }
            projections.append(projection)
            
        return projections

    def calculate_grid_distance(self, current_step: int, market_data: pd.DataFrame) -> float:
        """
        Calculates distance in pips for the next grid order.
        Now uses step-based grid rules for flexible spacing.
        
        Delegates to _get_grid_spacing_for_step which handles:
        - Step-based rules (ATR with multiplier or fixed values)
        - ATR mode (dynamic vs locked)
        - Fallback to base_grid
        """
        next_step = current_step + 1
        
        # Determine which ATR value to use
        atr_value = 0.0
        if self.use_atr_grid or self.grid_step_rules:
            if self.atr_mode == 'locked' and self.locked_atr is not None:
                atr_value = self.locked_atr
            else:
                atr_value = self._calculate_atr(market_data)
                
                # Auto-lock on first entry if mode is 'locked' and not yet locked
                if self.atr_mode == 'locked' and self.locked_atr is None and atr_value > 0:
                    self.locked_atr = atr_value
        
        # Get current price from market data for fallback calculation
        current_price = 0.0
        if market_data is not None and not market_data.empty:
            current_price = float(market_data['close'].iloc[-1])
            
        return self._get_grid_spacing_for_step(next_step, atr_value, current_price)

    def get_atr_foundation(self, market_data: pd.DataFrame):
        """
        Fetches ATR and Percentile for the configured ATR timeframe and lookback.
        
        ATR Calculation:
        - ATR_Timeframe: The candle timeframe to use (1m, 5m, 15m, 1h, 4h, 1d, etc.)
        - ATRPeriods: Number of candles to average (3 to 240)
        
        Formula: ATR = Average(True Range of last N candles at the selected timeframe)
        """
        results = {}
        
        # Get ATR configuration
        atr_tf = self.atr_tf or '1h'
        atr_periods = getattr(self, 'atr_period', 14)
        atr_periods = min(max(atr_periods, 3), 240)
        
        # Resample to ATR timeframe
        df = self._resample(market_data, atr_tf)
        
        if df.empty or len(df) < atr_periods:
            return {'error': f'Not enough data for {atr_tf} timeframe'}
        
        # Calculate True Range
        tr1 = df['high'] - df['low']
        tr2 = (df['high'] - df['close'].shift()).abs()
        tr3 = (df['low'] - df['close'].shift()).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # Calculate ATR as average of True Range over N periods
        atr_val = true_range.iloc[-atr_periods:].mean()
        
        # Get current price for move percentage
        open_p = df['open'].iloc[-1]
        curr_p = df['close'].iloc[-1]
        
        # Move as % of ATR
        if float(atr_val) > 0:
            move_pct = (curr_p - open_p) / float(atr_val) * 100.0
        else:
            move_pct = 0.0
        
        # Calculate percentile
        rolling_atrs = true_range.rolling(window=atr_periods).mean()
        atr_history = rolling_atrs.dropna()
        if len(atr_history) >= 10:
            percentile = (atr_history < float(atr_val)).sum() / len(atr_history) * 100
        else:
            percentile = 50
        
        return {
            'atr': float(atr_val),
            'move_pct': float(move_pct),
            'percentile': float(percentile),
            'timeframe': atr_tf,
            'periods': atr_periods
        }

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

