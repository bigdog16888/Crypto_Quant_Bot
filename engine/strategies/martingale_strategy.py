from typing import Optional, Dict, Any, List, Tuple
from .base import BaseStrategy
import pandas as pd
import engine.indicators as ta_custom
import logging

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
    MIN_INVESTMENT = 2.0 # USDT/USDC - Dust threshold

    def __init__(self, params: Optional[dict] = None):
        super().__init__("Martingale_Grid", params if params is not None else {})
        self.params = params or {}
        self.max_steps = int(self.params.get('max_steps', 10))
        # 🚀 FUNDAMENTAL FIX: Pair-Agnostic Dynamic Precision
        self.qty_precision = 3
        self.price_precision = 2
        self.step_size = 0.001
        self.tick_size = 0.01

    def set_precision_metadata(self, metadata: Dict[str, Any]):
        """Sets the precision and step metadata dynamically."""
        self.qty_precision = metadata.get('qty_precision', 3)
        self.price_precision = metadata.get('price_precision', 2)
        self.step_size = metadata.get('step_size', 0.001)
        self.tick_size = metadata.get('tick_size', 0.01)

    def check_signals(self, market_data: pd.DataFrame, current_price_float: float = None) -> tuple[bool, bool]:
        """User Logic: Triggers based on price thresholds."""
        current_price = 0.0
        
        if not market_data.empty:
            current_price = float(market_data['close'].iloc[-1])
        elif current_price_float is not None:
             current_price = current_price_float
        else:
             return False, False
        
        mode_price = int(self.params.get('mode_price', 0))
        threshold = float(self.params.get('price_threshold', 0.0))
        buy_signal = False
        sell_signal = False
        
        # DEBUG SIGNAL
        if self.params.get('direction') == 'SHORT' or self.params.get('direction') == 'LONG':
             # logger.warning(f"DEBUG_SIG: Sym={self.name} Mode={mode_price} Thresh={threshold} Curr={current_price} SellSig={sell_signal} BuySig={buy_signal}")
             pass

        if mode_price == 2: # Price < threshold
            buy_signal = current_price < threshold
        elif mode_price == 1: # Price > threshold
            sell_signal = current_price > threshold
            
        return buy_signal, sell_signal

    @staticmethod
    def get_empty_df() -> pd.DataFrame:
        return pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    def decide_action(self, bot_status: Dict, current_price: float, market_data: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """Core logic to determine the bot's next action based on market conditions"""
        logger.warning(f"DECIDE: {self.name} Invested={bot_status['total_invested']} Price={current_price}")
        
        # 🚀 DUST PROTECTION: If total invested is below threshold, treat as IDLE
        if bot_status['total_invested'] > self.MIN_INVESTMENT:
            # Already in a trade, maintain orders (TP, Grid)
            return {'action': 'maintain_orders'}
        else:
            # 1. Not in a trade: Check for entry signals
            buy_signal, sell_signal = self.check_signals(market_data, current_price)

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
        if avg_price == 0: avg_price = current_price
        tp_pct = float(self.params.get('tp_pct', 1.5))
        direction = self.params.get('direction', 'LONG').upper()
        price = avg_price * (1 + tp_pct/100) if direction == 'LONG' else avg_price * (1 - tp_pct/100)
        # 🚀 DYNAMIC ROUNDING
        return round(price, self.price_precision)

    def calculate_take_profit_amount(self, bot_status: Dict, current_price: float, pair: str, exchange: Any) -> float:
        avg_price = float(bot_status.get('avg_entry_price', 1.0))
        if avg_price == 0: avg_price = current_price
        # 🚀 DYNAMIC ROUNDING
        return round(float(bot_status.get('total_invested', 0)) / avg_price, self.qty_precision)

    def calculate_grid_order_price(self, bot_status: Dict, current_price: float) -> float:
        dist_pct = float(self.params.get('grid_dist_pct', 1.0))
        direction = self.params.get('direction', 'LONG').upper()
        price = current_price * (1 - dist_pct/100) if direction == 'LONG' else current_price * (1 + dist_pct/100)
        # 🚀 DYNAMIC ROUNDING
        return round(price, self.price_precision)

    def calculate_grid_order_amount(self, bot_status: Dict, current_price: float, pair: str, exchange: Any) -> float:
        step = int(bot_status.get('current_step', 0))
        return self.calculate_lot_size(step + 1, 10000, market_data=current_price)

    def calculate_lot_size(self, current_step: int, balance: float, market_data=None) -> float:
        base_size_usd = float(self.params.get('base_size', 150.0))
        multiplier = float(self.params.get('martingale_multiplier', 2.0))
        size_usd = base_size_usd * (multiplier ** current_step)
        
        calc_price = 0.0
        if market_data is not None:
            if isinstance(market_data, pd.DataFrame) and not market_data.empty:
                calc_price = float(market_data['close'].iloc[-1])
            elif isinstance(market_data, (int, float)):
                calc_price = float(market_data)
            
        if calc_price <= 100.0: calc_price = 68000.0
        
        # 🚀 DYNAMIC ROUNDING
        return round(size_usd / calc_price, self.qty_precision)
