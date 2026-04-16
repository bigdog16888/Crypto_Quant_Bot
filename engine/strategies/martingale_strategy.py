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
        """Round to exchange tick_size — the ONLY correct way to prepare a price for the exchange."""
        if not self.tick_size or self.tick_size <= 0:
            return price
        import math
        precision = int(-math.log10(self.tick_size)) if self.tick_size < 1 else 0
        return round(math.floor(price / self.tick_size) * self.tick_size, precision)

    def _round_qty(self, qty: float) -> float:
        """Round to exchange step_size for quantities."""
        if not self.step_size or self.step_size <= 0:
            return qty
        import math
        precision = int(-math.log10(self.step_size)) if self.step_size < 1 else 0
        return round(math.floor(qty / self.step_size) * self.step_size, precision)

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
        In Binance One-Way Mode, multiple bots across opposing directions mathematically Net out
        on the exchange orderbook. Using the aggregate physical position size would erroneously
        clip identical Long and Short bots down to 0, trapping their Take Profits.
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
            # 🚀 HARMONIZED KEYS: Support both UI 'StepPct' and engine 'grid_dist_pct'
            dist_pct = float(self.params.get('grid_dist_pct', self.params.get('StepPct', 1.0)))
            # 🛡️ SAFETY FLOOR: Prevent 0.0 or negative distance which causes 'Same Price' ordering
            dist_pct = max(0.1, dist_pct)
            
            direction = self.params.get('direction', 'LONG').upper()
            base_grid = float(self.params.get('base_grid', 100.0))
            use_atr = self.params.get('UseATRGrid', False)
            step = int(bot_status.get('current_step', 0))
            avg_entry = float(bot_status.get('avg_entry_price', 0))
            
            explanation = []
            
            # 1. Calculate Base Distance
            if use_atr:
                 try:
                     atr_period = int(self.params.get('ATRPeriods', 14))
                     atr_factor = float(self.params.get('ATRGridFactor', 1.0))

                     # 🚀 FUNDAMENTAL FIX: Use the CONFIGURED ATR timeframe, not just the 1-min feed.
                     # The bot config has 'ATR_Timeframe' (e.g. '15m', '1h'). If multi_tf_data is provided,
                     # select the correct candle series. Otherwise fall back to the 1-min market_data.
                     # Note: support 'ATR_Timeframe' (UI key), 'ATRTimeframe', and 'atr_tf' as fallbacks.
                     atr_tf = (self.params.get('ATR_Timeframe')
                               or self.params.get('ATRTimeframe')
                               or self.params.get('atr_tf', ''))
                     atr_df = None
                     if multi_tf_data and atr_tf and atr_tf in multi_tf_data:
                         candidate = multi_tf_data[atr_tf]
                         if hasattr(candidate, 'empty') and not candidate.empty and len(candidate) >= atr_period:
                             atr_df = candidate
                     if atr_df is None and hasattr(market_data, 'empty') and not market_data.empty:
                         atr_df = market_data

                     # Smart display: use enough decimal places to show a non-zero value
                     def _fmt(v): return f"{v:.6f}".rstrip('0').rstrip('.') if v < 0.01 else f"{v:.4f}" if v < 1.0 else f"{v:.2f}"

                     if atr_df is not None and len(atr_df) >= atr_period:
                         atr_val = iATR(atr_df['high'], atr_df['low'], atr_df['close'], atr_period)
                         # Safety floor: if ATR is too small (< 0.01% of price), fall back to price-relative distance
                         min_atr_floor = current_price * 0.0001  # 0.01% of price
                         if atr_val <= 0 or atr_val < min_atr_floor:
                             # Fallback to percentage-relative distance (safe for any asset)
                             pct_fallback = current_price * (dist_pct / 100.0)
                             logger.warning(f"ATR({atr_period},{atr_tf or '1m'})={_fmt(atr_val)} below floor ({_fmt(min_atr_floor)}), using {dist_pct}% = {_fmt(pct_fallback)} instead.")
                             grid_dist = pct_fallback
                             explanation.append(f"ATR({atr_period},{atr_tf or '1m'})={_fmt(atr_val)} [FLOOR->{dist_pct}%={_fmt(pct_fallback)}]")
                         else:
                             grid_dist = atr_val * atr_factor
                             explanation.append(f"ATR({atr_period},{atr_tf or '1m'})={_fmt(atr_val)} * {atr_factor}")
                     else:
                         # Not enough candle data yet — fall back to % distance
                         pct_fallback = current_price * (dist_pct / 100.0)
                         logger.warning(f"ATR: insufficient {atr_tf or '1m'} data, using {dist_pct}% = {_fmt(pct_fallback)}.")
                         grid_dist = pct_fallback
                         explanation.append(f"ATR no-data [FALLBACK {dist_pct}%]")
                 except Exception as e:
                     logger.error(f"ATR Calc failed: {e}")
                     grid_dist = current_price * (dist_pct / 100.0)  # Safe fallback for any asset
                     explanation.append(f"ATR Fail (Fallback {dist_pct}%)")
            else:
                 # Standard Percentage or Fixed Pips?
                 # Old logic used 'grid_dist_pct' (Percentage).
                 # UI Projection used 'base_grid' (Pips/Raw).
                 # We need to support BOTH or standardize.
                 # Let's standardize on PERCENTAGE for default, but if 'UseATRGrid' is OFF, check if 'base_grid' is preferred?
                 # Param `grid_dist_pct` suggests % based.
                 # Let's check `calculate_next_grid_price` (UI algo). It prefers `base_grid` (which implies price delta, not %).
                 # BUT default settings usually use %.
                 # Let's stick to PERCENTAGE if ATR is OFF, to avoid breaking existing users who rely on %?
                 # Wait, `calculate_next_grid_price` uses `base_grid` as fallback.
                 # I will preserve `grid_dist_pct` as the primary non-ATR logic for now, to be safe.
                 
                 grid_dist = current_price * (dist_pct/100.0)
                 explanation.append(f"Fixed {dist_pct}%")

            # 2. Calculate NEXT-STEP distance only (not cumulative)
            # Each grid order is placed 1 step-distance from the avg_entry, not a sum of all steps.
            # Cumulative approach caused the grid price to overshoot the market, triggering the GAP RECOVERY
            # loop which then re-placed at the same invalid offset every cycle.
            grid_mult = float(self.params.get('GridMultiplier', 1.0))
            # Apply multiplier for the NEXT step only (step is already the current step count)
            next_step_dist = grid_dist * (grid_mult ** step)
            final_dist = next_step_dist
            if grid_mult != 1.0:
                explanation.append(f"Step {step} dist (mult={grid_mult}^{step})")

            # 3. Calculate Price from reference (avg_entry is most stable, falls back to current_price)
            ref_price = avg_entry if avg_entry > 0 else current_price
            
            # Calculate next grid order price: 1 step beyond the current avg entry
            if direction == 'LONG':
                price = ref_price - final_dist
            else:
                price = ref_price + final_dist

            # 🚀 GAP RECOVERY: If calculated grid crossed market (e.g. after a fast move),
            # use 1 step-distance from current_price instead (still a valid, BETTER price for the bot).
            is_invalid = False
            if direction == 'LONG' and price > current_price:
                 is_invalid = True  # Buy Order ABOVE Market → use recovery
            elif direction == 'SHORT' and price < current_price:
                 is_invalid = True  # Sell Order BELOW Market → use recovery
            
            if is_invalid:
                def _p(v): return f"{v:.6f}".rstrip('0').rstrip('.') if v < 1.0 else f"{v:.4f}" if v < 10.0 else f"{v:,.2f}"
                # Recovery: use a SINGLE-step offset from the current market price
                # This produces a valid limit order that is 1 ATR-step beyond current price
                if direction == 'LONG':
                    price = current_price - final_dist
                else:
                    price = current_price + final_dist
                logger.info(f"📐 Gap Recovery: Grid {_p(ref_price - final_dist if direction == 'LONG' else ref_price + final_dist)} crossed Market {_p(current_price)}. Recovery → {_p(price)}.")
                explanation.append("GapRecovery")
                
                # Final safety: if recovery price still invalid (e.g. extreme volatility), skip
                if direction == 'LONG' and price > current_price:
                    logger.warning(f"⛔ Gap Recovery still invalid for {direction}. Skipping grid placement.")
                    return 0.0, "GapRecovery-INVALID"
                elif direction == 'SHORT' and price < current_price:
                    logger.warning(f"⛔ Gap Recovery still invalid for {direction}. Skipping grid placement.")
                    return 0.0, "GapRecovery-INVALID"

            # 🚀 UNIVERSAL PRECISION: Use exchange tick_size for all grid prices
            final_price = self._round_price(price)

            def _fmt_dist(v): return f"{v:.6f}".rstrip('0').rstrip('.') if v < 0.01 else f"{v:.4f}" if v < 1.0 else f"{v:.2f}"
            explain_str = f"Grid: {' | '.join(explanation)} -> Dist {_fmt_dist(final_dist)}"
            return final_price, explain_str

        except Exception as e:
            logger.error(f"Error calculating grid price: {e}")
            return 0.0, "Error"

    def calculate_grid_order_amount(self, bot_status: Dict, current_price: float, pair: str, exchange: Any) -> float:
        step = int(bot_status.get('current_step', 0))
        # Reuse lot size logic
        # 🚀 BUG FIX: Pass 'step' exactly to ensure the exponent computes M^step. 
        # Previously passed step + 1, skipping M^1 and jumping straight from M^0 (Entry) to M^2.
        return self.calculate_lot_size(step, 10000, market_data=current_price)

    def _apply_volatility_sizing(self, base_size: float, market_data: Any) -> float:
        """
        Adjusts the position size based on relative volatility.
        Ratio = ATR(100) / ATR(14). 
        - High Vol (ATR > AVG) -> Smaller Size
        - Low Vol (ATR < AVG) -> Larger Size
        """
        if not self.params.get('UseVolSizing', False):
            return base_size
            
        if not isinstance(market_data, pd.DataFrame) or market_data.empty:
            return base_size
            
        try:
            atr_period = int(self.params.get('ATRPeriods', 14))
            # Baseline (SMA 100) vs Short-term (SMA 14)
            baseline_atr = iATR(market_data['high'], market_data['low'], market_data['close'], 100)
            current_atr = iATR(market_data['high'], market_data['low'], market_data['close'], atr_period)
            
            if baseline_atr > 0 and current_atr > 0:
                vol_mult = baseline_atr / current_atr
                # Safety Clamp: don't let size swing more than 5x or less than 0.2x
                vol_mult = max(0.2, min(vol_mult, 5.0))
                
                adjusted_size = base_size * vol_mult
                logger.info(f"📊 Vol Sizing: Base ${base_size:.2f} * {vol_mult:.2f}x (ATR100/ATR{atr_period}) -> ${adjusted_size:.2f}")
                return adjusted_size
        except Exception as e:
            logger.error(f"Volatility sizing calculation failed: {e}")
            
        return base_size

    def calculate_lot_size(self, current_step: int, balance: float, market_data=None) -> float:
        base_size_usd = float(self.params.get('base_size', 150.0))
        
        # Apply Volatility Sizing if enabled (only affects the base calculation)
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
        
        # 🚀 DYNAMIC ROUNDING
        return round(size_usd / calc_price, self.qty_precision)

    def calculate_next_grid_price(self, direction: str, current_price: float, avg_entry: float, step: int, market_data: Any, **kwargs) -> float:
        """Helper for UI to predict next grid order price."""
        dist_pct = float(self.params.get('grid_dist_pct', self.params.get('StepPct', 1.0)))
        base_grid = float(self.params.get('base_grid', 100.0))
        use_atr = self.params.get('UseATRGrid', False)
        
        # Calculate distance
        if use_atr and hasattr(market_data, 'empty') and not market_data.empty:
             try:
                 atr_period = int(self.params.get('ATRPeriods', 14))
                 atr_val = iATR(market_data['high'], market_data['low'], market_data['close'], atr_period)
                 grid_dist = atr_val * float(self.params.get('ATRGridFactor', 1.0))
             except:
                 grid_dist = base_grid # Fallback
        else:
             # If not ATR, use percentage of entry if StepPct is set, otherwise base_grid
             if self.params.get('StepPct') is not None or self.params.get('grid_dist_pct') is not None:
                 ref_price = avg_entry if avg_entry > 0 else current_price
                 grid_dist = ref_price * (dist_pct / 100.0)
             else:
                 grid_dist = base_grid # Default fixed grid
                 
        # Dynamic Multiplier logic if enabled
        grid_mult = float(self.params.get('GridMultiplier', 1.0))
        if step > 0 and grid_mult > 1.0:
            grid_dist = grid_dist * (grid_mult ** step)
             
        # Direction Logic
        last_grid_price = kwargs.get('last_grid_price', 0)
        # Using last_grid_price provides absolute origin anchoring instead of drifting avg_entry
        ref_price = last_grid_price if last_grid_price > 0 else (avg_entry if avg_entry > 0 else current_price)
        
        if direction.upper() == 'LONG':
             price = ref_price - grid_dist
        else:
             price = ref_price + grid_dist
             
        return round(price, self.price_precision)

    def calculate_projections(self, base_price: float, current_atr: float = 0.0) -> List[Dict[str, Any]]:
        """
        Generates a projection of trade steps (Grid Orders) based on current parameters.
        Used by the UI to visualize risk and potential order placements.
        """
        try:
            projections = []
            
            # Parameters
            base_size = float(self.params.get('base_size', 10.0))
            mm_mult = float(self.params.get('martingale_multiplier', 2.0))
            max_steps = int(self.params.get('max_steps', 10))
            direction = self.params.get('direction', 'LONG').upper()
            
            # Grid Spacing
            use_atr = self.params.get('UseATRGrid', False)
            base_grid = float(self.params.get('base_grid', 100.0)) # Pips/Price
            grid_mult = float(self.params.get('GridMultiplier', 1.0))

            # Initial State
            current_price_level = base_price
            total_invested = 0.0
            total_qty = 0.0
            avg_price = 0.0
            
            # Hedge Config
            use_hedge = self.params.get('UseHedge', False)
            hedge_start_step = int(self.params.get('HedgeStartStep', 5))
            
            for step in range(max_steps + 1):
                # 1. Calculate Order Size for this step
                # Step 0 is Base Order
                step_mult = mm_mult ** step
                order_size = base_size * step_mult
                
                # 2. Calculate Price Level for this step
                if step == 0:
                    price = base_price
                else:
                    # Calculate distance for this specific step
                    # Distance = Base * (GridMult ^ (step-1))
                    # Note: Simplified logic matching standard Martingale
                    if use_atr and current_atr > 0:
                        dist = current_atr * float(self.params.get('ATRGridFactor', 1.0))
                    else:
                        dist = base_grid
                        
                    # Apply expansion
                    dist = dist * (grid_mult ** (step - 1))
                    
                    if direction == 'LONG':
                        current_price_level -= dist
                    else:
                        current_price_level += dist
                    
                    price = current_price_level
                
                # Rounding
                price = round(price, self.price_precision)
                qty = order_size / price if price > 0 else 0
                
                # Update Accumulators
                total_invested += order_size
                total_qty += qty
                if total_qty > 0:
                    avg_price = total_invested / total_qty
                
                # 3. Calculate Take Profit
                # For UI projection, use simple TP logic
                tp_pct = float(self.params.get('tp_pct', 1.0))
                tp_dist = avg_price * (tp_pct / 100.0)
                
                if direction == 'LONG':
                    tp_price = avg_price + tp_dist
                else:
                    tp_price = avg_price - tp_dist
                    
                tp_price = round(tp_price, self.price_precision)
                
                # 4. Hedge Logic
                is_hedge = False
                hedge_size = 0.0
                if use_hedge and step == hedge_start_step:
                    is_hedge = True
                    hedge_size = total_invested # 1:1 Hedge usually
                
                projections.append({
                    'step': step,
                    'price': price,
                    'order_size_usdc': round(order_size, 2),
                    'total_invested': round(total_invested, 2),
                    'avg_price': round(avg_price, self.price_precision),
                    'tp_price': tp_price,
                    'is_hedge': is_hedge,
                    'hedge_size_usdc': round(hedge_size, 2)
                })
                
            return projections
            
        except Exception as e:
            logger.error(f"Error calculating projections: {e}")
            return []
