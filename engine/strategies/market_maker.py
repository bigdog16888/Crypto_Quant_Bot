import logging
import time
from engine.strategies.base import BaseStrategy

class MarketMakerStrategy(BaseStrategy):
    """
    Spread-based Market Maker with Inventory Skew.
    
    Logic:
    1. Calculate Mid Price (or Fair Value).
    2. Calculate Bid/Ask based on Target Spread.
    3. Apply Inventory Skew:
       - If Long: Shift quotes DOWN (easier to sell, harder to buy).
       - If Short: Shift quotes UP (easier to buy, harder to sell).
    4. Reconcile:
       - Only replace orders if price moves beyond `reprice_threshold`.
    """
    def __init__(self, name, params):
        super().__init__(name, params)
        self.logger = logging.getLogger(f"MM_{name}")
        
        # --- Strategy Parameters ---
        self.spread_pct = float(params.get('spread_pct', 0.002))       # 0.2% spread
        self.skew_factor = float(params.get('skew_factor', 0.0))       # Price shift per unit of inventory
        self.order_size = float(params.get('order_size', 0.01))        # Base order size
        self.max_inventory = float(params.get('max_inventory', 1.0))   # Max position size
        self.reprice_threshold = float(params.get('reprice_threshold', 0.001)) # 0.1% move needed to update
        
        # State
        self.last_bid_price = 0.0
        self.last_ask_price = 0.0

    def calculate_quotes(self, mid_price, current_inventory):
        """
        Derives ideal Bid and Ask prices.
        """
        # 1. Base Spread
        half_spread = (mid_price * self.spread_pct) / 2
        
        # 2. Inventory Skew
        # Example: Holding 0.5 BTC. Skew Factor 10. 
        # Shift = 0.5 * 10 = 5 USDT.
        # Prices shift DOWN by 5 USDT.
        skew_adjustment = current_inventory * self.skew_factor
        
        ideal_bid = mid_price - half_spread - skew_adjustment
        ideal_ask = mid_price + half_spread - skew_adjustment
        
        # 3. Sanity Checks
        if ideal_bid >= ideal_ask:
            # Spread inverted due to massive skew? Reset to min spread.
            ideal_bid = mid_price - half_spread
            ideal_ask = mid_price + half_spread
            
        return ideal_bid, ideal_ask

    def check_signals(self, df):
        """
        Market Makers don't use 'signals' like directional bots.
        They run a continuous loop. This method is kept for compatibility
        but returns None. The runner should call `execute_mm_logic`.
        """
        return False, False