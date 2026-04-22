from typing import Optional, Dict, Any, List, Tuple
from .base import BaseStrategy
import pandas as pd
import engine.indicators as ta_custom
import logging
import math

logger = logging.getLogger(__name__)

# Helper functions for standard indicators

def iATR(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> float:
    atr_series = ta_custom.atr(high, low, close, period=period)
    if atr_series is None or atr_series.empty: return 0.0
    return atr_series.iloc[-1]

def iRSI(data: pd.Series, period: int) -> float:
    rsi_series = ta_custom.rsi(data, period=period)
    if rsi_series is None or rsi_series.empty: return 50.0
    return rsi_series.iloc[-1]

class MartingaleStrategy(BaseStrategy):
    MIN_INVESTMENT = 0.01 # USDT/USDC - Support fractional micro-altcoin dust

    def __init__(self, params: Optional[dict] = None):
        super().__init__("Martingale_Grid", params if params is not None else {})
        self.params = params or {}
        self.max_steps = int(self.params.get('max_steps', 10))
        # 🚀 UNIVERSAL PRECISION: Never hardcode to 2. Fallback=4 decimals until exchange metadata injected.
        self.qty_precision = 4
        self.price_precision = 4
        self.step_size = 0.0001
        self.tick_size = 0.0001

    def set_precision_metadata(self, metadata: Dict[str, Any]):
        """Sets the precision and step metadata dynamically from real exchange data."""
        self.qty_precision = metadata.get('qty_precision', 4)
        self.price_precision = metadata.get('price_precision', 4)
        self.step_size = metadata.get('step_size', 0.0001)
        self.tick_size = metadata.get('tick_size', 0.0001)

    def _round_price(self, price: float) -> float:
        """Round to exchange tick_size using Decimal precision."""
        if not self.tick_size or self.tick_size <= 0:
            return price
        from decimal import Decimal, ROUND_FLOOR
        d_val = Decimal(format(price, '.15g'))
        d_step = Decimal(format(self.tick_size, '.15g'))
        rounded = (d_val / d_step).quantize(Decimal('1'), rounding=ROUND_FLOOR) * d_step
        return float(rounded)

    def _round_qty(self, qty: float) -> float:
        """Round to exchange step_size for quantities using Decimal precision."""
        if not self.step_size or self.step_size <= 0:
            return qty
        from decimal import Decimal, ROUND_FLOOR
        d_val = Decimal(format(qty, '.15g'))
        d_step = Decimal(format(self.step_size, '.15g'))
        rounded = (d_val / d_step).quantize(Decimal('1'), rounding=ROUND_FLOOR) * d_step
        return float(rounded)

    def check_signals(self, market_data: pd.DataFrame, current_price_float: float = None,
                       multi_tf_data: dict = None) -> tuple[bool, bool]:
        """
        Returns (entry_signal, exit_signal).
        Evaluates ALL enabled triggers (confluence). Entry is only allowed when
        every active trigger passes. mode=0 means OFF for that trigger.
        """
        current_price = 0.0

        if not market_data.empty:
            current_price = float(market_data['close'].iloc[-1])
        elif current_price_float is not None:
            current_price = current_price_float
        else:
            return False, False

        if multi_tf_data is None:
            multi_tf_data = {}

        # ── Evaluate each trigger ──────────────────────────────────
        results = []  # list of (name, passed)

        # 1. Price Threshold
        r = self._check_price(current_price)
        if r is not None:
            results.append(('Price', r))

        # 2. CCI
        r = self._check_cci(market_data, multi_tf_data)
        if r is not None:
            results.append(('CCI', r))

        # 3. RSI
        r = self._check_rsi(market_data, multi_tf_data)
        if r is not None:
            results.append(('RSI', r))

        # 4. Bollinger Bands
        r = self._check_bollinger(market_data, multi_tf_data, current_price)
        if r is not None:
            results.append(('Bollinger', r))

        # 5. Stochastic
        r = self._check_stochastic(market_data, multi_tf_data)
        if r is not None:
            results.append(('Stochastic', r))

        # 6-9. Pattern Slots 1-4
        for slot in range(1, 5):
            r = self._check_pattern(slot, market_data, multi_tf_data)
            if r is not None:
                results.append((f'Pattern_{slot}', r))

        # 10. ATR Percentile
        r = self._check_atrp(market_data)
        if r is not None:
            results.append(('ATRP', r))

        # 11. ATR Expansion
        r = self._check_atre(market_data)
        if r is not None:
            results.append(('ATRE', r))

        # 12. MA Filter
        r = self._check_ma(market_data, current_price)
        if r is not None:
            results.append(('MA', r))

        # 13. MTF Confluence
        r = self._check_mtf(multi_tf_data, current_price)
        if r is not None:
            results.append(('MTF', r))

        # ── Confluence decision ────────────────────────────────────
        if not results:
            # No triggers enabled → always enter (backward-compatible)
            entry_signal = True
        else:
            entry_signal = all(passed for _, passed in results)

        # Log trigger results for debugging
        if results:
            summary = ' | '.join(f"{n}={'✅' if p else '❌'}" for n, p in results)
            logger.info(f"🔍 [{self.params.get('bot_name', self.name)}] Triggers: {summary} → Entry={'YES' if entry_signal else 'NO'}")

        return entry_signal, entry_signal

    # ── Private trigger helpers ─────────────────────────────────────

    def _get_tf_data(self, tf_key: str, market_data: pd.DataFrame,
                     multi_tf_data: dict) -> pd.DataFrame:
        """Get DataFrame for a specific timeframe, falling back to 1m data."""
        tf = self.params.get(tf_key, '1m')
        if tf and tf in multi_tf_data and not multi_tf_data[tf].empty:
            return multi_tf_data[tf]
        # Fallback: if the requested TF is '1m' or unavailable, use default market_data
        return market_data

    def _check_price(self, current_price: float):
        """Trigger 9: Price Threshold. Returns None if OFF."""
        mode = int(self.params.get('mode_price', 0))
        if mode == 0:
            return None
        threshold = float(self.params.get('price_threshold', 0.0))
        if mode == 1:
            return current_price > threshold
        if mode == 2:
            return current_price < threshold
        return None

    def _check_cci(self, market_data: pd.DataFrame, multi_tf_data: dict):
        """Trigger 1: CCI. Returns None if OFF."""
        mode = int(self.params.get('mode_cci', 0))
        if mode == 0:
            return None
        df = self._get_tf_data('cci_tf', market_data, multi_tf_data)
        if df.empty or len(df) < 14:
            return None  # Not enough data — skip (don't block)
        period = int(self.params.get('cci_period', 14))
        level = float(self.params.get('cci_level', 100))
        cci_val = float(ta_custom.cci(df['high'], df['low'], df['close'], period=period).iloc[-1])
        if mode == 1:  # Above level
            return cci_val > level
        if mode == 2:  # Below level
            return cci_val < level
        return None

    def _check_rsi(self, market_data: pd.DataFrame, multi_tf_data: dict):
        """Trigger 4: RSI. Returns None if OFF."""
        mode = int(self.params.get('mode_rsi', 0))
        if mode == 0:
            return None
        df = self._get_tf_data('rsi_tf', market_data, multi_tf_data)
        if df.empty or len(df) < 14:
            return None
        period = int(self.params.get('rsi_period', 14))
        level = float(self.params.get('rsi_level', 30))
        rsi_val = iRSI(df['close'], period)
        if mode == 1:  # Below level (oversold entry)
            return rsi_val < level
        if mode == 2:  # Above level (overbought entry)
            return rsi_val > level
        return None

    def _check_bollinger(self, market_data: pd.DataFrame, multi_tf_data: dict,
                         current_price: float):
        """Trigger 2: Bollinger Bands. Returns None if OFF."""
        mode = int(self.params.get('mode_boll', 0))
        if mode == 0:
            return None
        df = self._get_tf_data('boll_tf', market_data, multi_tf_data)
        if df.empty or len(df) < 20:
            return None
        period = int(self.params.get('boll_period', 20))
        dev = float(self.params.get('boll_dev', 2.0))
        upper, mid, lower = ta_custom.bollinger_bands(df['close'], period=period, deviation=dev)
        if mode == 1:  # Outside Lower — price BELOW lower band (entry: price dropped to oversold)
            return current_price < float(lower.iloc[-1])
        if mode == 2:  # Outside Upper — price ABOVE upper band (entry: price broke above overbought)
            return current_price > float(upper.iloc[-1])
        return None

    def _check_stochastic(self, market_data: pd.DataFrame, multi_tf_data: dict):
        """Trigger 3: Stochastic %K. Returns None if OFF."""
        mode = int(self.params.get('mode_stoch', 0))
        if mode == 0:
            return None
        df = self._get_tf_data('stoch_tf', market_data, multi_tf_data)
        if df.empty or len(df) < 14:
            return None
        level = float(self.params.get('stoch_level', 20))
        k_val, _ = ta_custom.stochastic(df['high'], df['low'], df['close'])
        k_last = float(k_val.iloc[-1])
        if mode == 1:  # Above level
            return k_last > level
        if mode == 2:  # Below level
            return k_last < level
        return None

    def _check_pattern(self, slot: int, market_data: pd.DataFrame, multi_tf_data: dict):
        """Trigger 5-8: Consecutive candle pattern. Returns None if OFF."""
        mode = int(self.params.get(f'pat_{slot}_mode', 0))
        if mode == 0:
            return None
        count = int(self.params.get(f'pat_{slot}_count', 3))
        df = self._get_tf_data(f'pat_{slot}_tf', market_data, multi_tf_data)
        if df.empty or len(df) < count + 1:
            return None
        # Check last `count` candles
        closes = df['close'].values
        if mode == 1:  # Consecutive Up (each close > previous close)
            for i in range(-count, 0):
                if closes[i] <= closes[i - 1]:
                    return False
            return True
        if mode == 2:  # Consecutive Down (each close < previous close)
            for i in range(-count, 0):
                if closes[i] >= closes[i - 1]:
                    return False
            return True
        return None

    def _check_atrp(self, market_data: pd.DataFrame):
        """Trigger 10: ATR Percentile. Returns None if OFF."""
        mode = int(self.params.get('mode_atrp', 0))
        if mode == 0:
            return None
        if market_data.empty or len(market_data) < 20:
            return None
        level = float(self.params.get('atrp_level', 50))
        atrp_val = ta_custom.atr_percentile(market_data['high'], market_data['low'],
                                            market_data['close'])
        if mode == 1:  # Above percentile
            return atrp_val > level
        if mode == 2:  # Below percentile
            return atrp_val < level
        return None

    def _check_atre(self, market_data: pd.DataFrame):
        """Trigger 11: ATR Expansion/Contraction. Returns None if OFF."""
        mode = int(self.params.get('mode_atre', 0))
        if mode == 0:
            return None
        if market_data.empty or len(market_data) < 28:
            return None
        period = int(self.params.get('atre_period', 14))
        atr_series = ta_custom.atr(market_data['high'], market_data['low'],
                                   market_data['close'], period=period)
        if len(atr_series) < 2:
            return None
        current_atr = float(atr_series.iloc[-1])
        prev_atr = float(atr_series.iloc[-2])
        if mode == 1:  # Expanding (current > previous)
            return current_atr > prev_atr
        if mode == 2:  # Contracting (current < previous)
            return current_atr < prev_atr
        return None

    def _check_ma(self, market_data: pd.DataFrame, current_price: float):
        """Trigger 12: Moving Average Price Filter. Returns None if OFF."""
        mode = int(self.params.get('mode_ma', 0))
        if mode == 0:
            return None
        period = int(self.params.get('ma_period', 20))
        if market_data.empty or len(market_data) < period:
            return None
        ma_val = float(market_data['close'].rolling(window=period).mean().iloc[-1])
        if mode == 1:  # Price above MA
            return current_price > ma_val
        if mode == 2:  # Price below MA
            return current_price < ma_val
        return None

    def _check_mtf(self, multi_tf_data: dict, current_price: float):
        """Trigger 13: Multi-Timeframe Confluence. Returns None if OFF."""
        if not self.params.get('UseMTFConfluence', False):
            return None
        tf = self.params.get('MTF_Timeframe', '1h')
        period = int(self.params.get('MTF_MA_Period', 50))
        df = multi_tf_data.get(tf)
        if df is None or df.empty or len(df) < period:
            return None
        ma_val = float(df['close'].rolling(window=period).mean().iloc[-1])
        direction = self.params.get('direction', 'LONG').upper()
        if direction == 'LONG':
            return current_price > ma_val  # Price above higher-TF MA
        else:
            return current_price < ma_val  # Price below higher-TF MA

    @staticmethod
    def get_empty_df() -> pd.DataFrame:
        return pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    def decide_action(self, bot_status: Dict, current_price: float, market_data: pd.DataFrame,
                       multi_tf_data: dict = None) -> Optional[Dict[str, Any]]:
        """Core logic to determine the bot's next action based on market conditions"""
        logger.debug(f"DECIDE: {self.name} Invested={bot_status['total_invested']} Price={current_price}")
        
        # 🚀 DUST PROTECTION: If total invested is below threshold, treat as IDLE
        if bot_status['total_invested'] > self.MIN_INVESTMENT:
            # 🚀 FIX: Don't maintain orders until entry fill is confirmed via WS
            if not bot_status.get('entry_confirmed', 0):
                logger.info(f"{self.name}: Invested but entry NOT confirmed. Waiting for WS fill.")
                return None
                
            # Already in a trade, maintain orders (TP, Grid)
            return {'action': 'maintain_orders'}
        else:
            # 1. Not in a trade: Check for entry signals
            buy_signal, sell_signal = self.check_signals(market_data, current_price,
                                                         multi_tf_data=multi_tf_data)

            # 2. Next Action is Entry
            direction = self.params.get('direction', 'LONG').upper()
            
            if direction == 'LONG' and buy_signal:
                logger.info(f"{self.name} Entry LONG at {current_price}")
                amount = self.calculate_lot_size(0, 10000, market_data)
                return {'action': 'entry', 'side': 'buy', 'amount': amount, 'price': current_price}

            elif direction == 'SHORT' and sell_signal:
                logger.info(f"{self.name} Entry SHORT at {current_price}")
                amount = self.calculate_lot_size(0, 10000, market_data)
                return {'action': 'entry', 'side': 'sell', 'amount': amount, 'price': current_price}
        
        return None

    def calculate_take_profit_price(self, bot_status: Dict, current_price: float) -> float:
        avg_price = float(bot_status.get('avg_entry_price', 0))
        if avg_price == 0:
            avg_price = current_price
        # 🚀 UNIVERSAL: Always read direction from params; SHORT bots have direction='SHORT'
        direction = (self.params.get('direction') or 'LONG').upper()
        tp_type = self.params.get('TakeProfitType', 'Percent')

        if tp_type == 'Percent':
            tp_pct = float(self.params.get('TakeProfitPct', self.params.get('tp_pct', 1.5)))
            tp_pct = max(0.1, tp_pct)
            price = avg_price * (1 + tp_pct / 100) if direction == 'LONG' else avg_price * (1 - tp_pct / 100)
        else:
            # Fixed USD profit target
            target_usd = float(self.params.get('TakeProfitBase', 10.0))
            total_invested = float(bot_status.get('total_invested', avg_price))
            est_qty = total_invested / avg_price if avg_price > 0 else 0
            if est_qty > 0:
                dist = target_usd / est_qty
                price = avg_price + dist if direction == 'LONG' else avg_price - dist
            else:
                price = avg_price

        # 🚀 UNIVERSAL PRECISION: Use exchange tick_size, NOT Python round(price, 2)
        return self._round_price(price)

    def calculate_take_profit_amount(self, bot_status: Dict, current_price: float, pair: str, exchange: Any) -> float:
        """
        Calculates the quantity (amount) for the Take Profit order.
        Strictly relies on the mathematically perfect Virtual Ledger.
        """
        avg_price = float(bot_status.get('avg_entry_price', 1.0))
        if avg_price <= 0:
            avg_price = current_price
            
        raw_qty = float(bot_status.get('total_invested', 0)) / avg_price
        return self._round_qty(raw_qty)

    def calculate_grid_order_price(self, bot_status: Dict, current_price: float, market_data: Any = None, multi_tf_data: dict = None) -> Tuple[float, str]:
        """
        Calculates the next grid price using configured logic (Fixed or ATR).
        Returns: (price, explanation_string)
        """
        try:
            dist_pct = float(self.params.get('grid_dist_pct', self.params.get('StepPct', 1.0)))
            dist_pct = max(0.1, dist_pct)
            
            direction = self.params.get('direction', 'LONG').upper()
            base_grid = float(self.params.get('base_grid', 100.0))
            use_atr = self.params.get('UseATRGrid', False)
            step = int(bot_status.get('current_step', 0))
            avg_entry = float(bot_status.get('avg_entry_price', 0))
            
            explanation = []
            
            if use_atr:
                 try:
                     atr_period = int(self.params.get('ATRPeriods', 14))
                     atr_factor = float(self.params.get('ATRGridFactor', 1.0))
                     atr_tf = (self.params.get('ATR_Timeframe') or self.params.get('ATRTimeframe') or self.params.get('atr_tf', ''))
                     atr_df = None
                     if multi_tf_data and atr_tf and atr_tf in multi_tf_data:
                         candidate = multi_tf_data[atr_tf]
                         if hasattr(candidate, 'empty') and not candidate.empty and len(candidate) >= atr_period:
                             atr_df = candidate
                     if atr_df is None and hasattr(market_data, 'empty') and not market_data.empty:
                         atr_df = market_data

                     def _fmt(v): return f"{v:.6f}".rstrip('0').rstrip('.') if v < 0.01 else f"{v:.4f}" if v < 1.0 else f"{v:.2f}"

                     if atr_df is not None and len(atr_df) >= atr_period:
                          atr_val = iATR(atr_df['high'], atr_df['low'], atr_df['close'], atr_period)
                          # 🚀 STRICT STRATEGY: No fallbacks.
                          if atr_val > 0:
                              grid_dist = atr_val * atr_factor
                              explanation.append(f"ATR({atr_period},{atr_tf or '1m'})={_fmt(atr_val)} * {atr_factor}")
                          else:
                              logger.warning(f"ATR is 0 for {atr_tf or '1m'}. Aborting.")
                              return 0.0, "ERROR_ATR_ZERO"
                     else:
                          logger.warning(f"ATR: insufficient {atr_tf or '1m'} data. Aborting.")
                          return 0.0, "ERROR_INSUFFICIENT_DATA"
                 except Exception as e:
                      logger.error(f"ATR Calc failed: {e}")
                      return 0.0, f"ERROR_ATR_EXCEPTION"
            else:
                 grid_dist = current_price * (dist_pct/100.0)
                 explanation.append(f"Fixed {dist_pct}%")

            grid_mult = float(self.params.get('GridMultiplier', 1.0))
            final_dist = grid_dist * (grid_mult ** step)
            if grid_mult != 1.0:
                explanation.append(f"Step {step} dist (mult={grid_mult}^{step})")

            ref_price = avg_entry if avg_entry > 0 else current_price
            price = ref_price - final_dist if direction == 'LONG' else ref_price + final_dist

            # Gap Recovery
            is_invalid = (direction == 'LONG' and price > current_price) or (direction == 'SHORT' and price < current_price)
            if is_invalid:
                price = current_price - final_dist if direction == 'LONG' else current_price + final_dist
                explanation.append("GapRecovery")
                
                if (direction == 'LONG' and price > current_price) or (direction == 'SHORT' and price < current_price):
                    return 0.0, "GapRecovery-INVALID"

            final_price = self._round_price(price)
            return final_price, f"Grid: {' | '.join(explanation)} -> Dist {final_dist:.6f}"

        except Exception as e:
            logger.error(f"Error calculating grid price: {e}")
            return 0.0, "Error"

    def calculate_grid_order_amount(self, bot_status: Dict, current_price: float, pair: str, exchange: Any) -> float:
        step = int(bot_status.get('current_step', 0))
        return self.calculate_lot_size(step, 10000, market_data=current_price)

    def _apply_volatility_sizing(self, base_size: float, market_data: Any) -> float:
        if not self.params.get('UseVolSizing', False):
            return base_size
        if not isinstance(market_data, pd.DataFrame) or market_data.empty:
            return base_size
        try:
            atr_period = int(self.params.get('ATRPeriods', 14))
            baseline_atr = iATR(market_data['high'], market_data['low'], market_data['close'], 100)
            current_atr = iATR(market_data['high'], market_data['low'], market_data['close'], atr_period)
            if baseline_atr > 0 and current_atr > 0:
                vol_mult = baseline_atr / current_atr
                vol_mult = max(0.2, min(vol_mult, 5.0))
                return base_size * vol_mult
        except:
            pass
        return base_size

    def calculate_lot_size(self, current_step: int, balance: float, market_data=None) -> float:
        base_size_usd = float(self.params.get('base_size', 150.0))
        if market_data is not None:
             base_size_usd = self._apply_volatility_sizing(base_size_usd, market_data)
        multiplier = float(self.params.get('martingale_multiplier', 2.0))
        size_usd = base_size_usd * (multiplier ** current_step)
        calc_price = 0.0
        if market_data is not None:
            if isinstance(market_data, pd.DataFrame) and not market_data.empty:
                calc_price = float(market_data['close'].iloc[-1])
            elif isinstance(market_data, (int, float)):
                calc_price = float(market_data)
        if calc_price <= 0: calc_price = 68000.0 # Fallback
        return self._round_qty(size_usd / calc_price)

    def calculate_next_grid_price(self, direction: str, current_price: float, avg_entry: float, step: int, market_data: Any, **kwargs) -> Optional[float]:
        dist_pct = float(self.params.get('grid_dist_pct', self.params.get('StepPct', 0)))
        base_grid = float(self.params.get('base_grid', 100.0))
        use_atr = self.params.get('UseATRGrid', False)
        grid_dist = 0
        if use_atr:
            if hasattr(market_data, 'empty') and not market_data.empty:
                try:
                    atr_period = int(self.params.get('ATRPeriods', 14))
                    atr_val = iATR(market_data['high'], market_data['low'], market_data['close'], atr_period)
                    grid_dist = atr_val * float(self.params.get('ATRGridFactor', 1.0))
                except:
                    return None
            else:
                return None
        elif dist_pct > 0:
            ref_price = avg_entry if avg_entry > 0 else current_price
            grid_dist = ref_price * (dist_pct / 100.0)
        elif base_grid > 0:
            grid_dist = base_grid
        else:
            return None
                 
        grid_mult = float(self.params.get('GridMultiplier', 1.0))
        if step > 0 and grid_mult > 1.0:
            grid_dist = grid_dist * (grid_mult ** step)
             
        last_grid_price = kwargs.get('last_grid_price', 0)
        ref_price = last_grid_price if last_grid_price > 0 else (avg_entry if avg_entry > 0 else current_price)
        price = ref_price - grid_dist if direction.upper() == 'LONG' else ref_price + grid_dist
        if price <= 0: return None
        return round(price, self.price_precision)

    def calculate_projections(self, base_price: float, current_atr: float = 0.0) -> List[Dict[str, Any]]:
        try:
            projections = []
            base_size = float(self.params.get('base_size', 10.0))
            mm_mult = float(self.params.get('martingale_multiplier', 2.0))
            max_steps = int(self.params.get('max_steps', 10))
            direction = self.params.get('direction', 'LONG').upper()
            use_atr = self.params.get('UseATRGrid', False)
            base_grid = float(self.params.get('base_grid', 100.0))
            grid_mult = float(self.params.get('GridMultiplier', 1.0))
            current_price_level = base_price
            total_invested = 0.0
            total_qty = 0.0
            avg_price = 0.0
            use_hedge = self.params.get('UseHedge', False)
            hedge_start_step = int(self.params.get('HedgeStartStep', 5))
            
            for step in range(max_steps + 1):
                order_size = base_size * (mm_mult ** step)
                if step == 0:
                    price = base_price
                else:
                    dist = current_atr * float(self.params.get('ATRGridFactor', 1.0)) if (use_atr and current_atr > 0) else base_grid
                    dist = dist * (grid_mult ** (step - 1))
                    current_price_level = current_price_level - dist if direction == 'LONG' else current_price_level + dist
                    price = current_price_level
                
                price = self._round_price(price)
                qty = order_size / price if price > 0 else 0
                total_invested += order_size
                total_qty += qty
                if total_qty > 0:
                    avg_price = total_invested / total_qty
                
                tp_pct = float(self.params.get('tp_pct', 1.0))
                tp_dist = avg_price * (tp_pct / 100.0)
                tp_price = self._round_price(avg_price + tp_dist if direction == 'LONG' else avg_price - tp_dist)
                
                is_hedge = use_hedge and step == hedge_start_step
                projections.append({
                    'step': step, 'price': price, 'order_size_usdc': round(order_size, 2),
                    'total_invested': round(total_invested, 2), 'avg_price': self._round_price(avg_price),
                    'tp_price': tp_price, 'is_hedge': is_hedge, 'hedge_size_usdc': round(total_invested, 2) if is_hedge else 0.0
                })
            return projections
        except:
            return []
