from typing import Optional, Tuple
from .base import BaseStrategy
import pandas as pd
import logging

logger = logging.getLogger(__name__)

class MarketMakerStrategy(BaseStrategy):
    """
    Standard Market Making Strategy.
    Provides liquidity on both sides (Bid/Ask) around the mid-price.
    Adjusts quotes based on inventory skew to manage risk.
    """
    def __init__(self, name: str = "MarketMaker", params: Optional[dict] = None):
        super().__init__(name, params if params is not None else {})
        
        self.params: dict = params if params is not None else {}
        
        # Core MM Parameters
        self.spread_pct = float(self.params.get('spread_pct', 0.2)) / 100.0  # Total spread
        self.order_size = float(self.params.get('order_size', 10.0))         # Base order size USD
        self.reprice_threshold = float(self.params.get('reprice_threshold', 0.001)) # 0.1% move triggers reprice
        
        # Inventory Management
        self.inventory_target = float(self.params.get('inventory_target', 0.0))
        self.skew_factor = float(self.params.get('skew_factor', 1.0)) # Aggressiveness of skew
        
        # State
        self.last_bid = 0.0
        self.last_ask = 0.0

    def check_signals(self, df: pd.DataFrame) -> Tuple[bool, bool]:
        """
        Market Maker is always active unless paused.
        Returns True, True to indicate 'Active' state for both sides.
        Actual quote placement is handled by process_market_maker in Executor.
        """
        # We could add trend filters here to pause MM against strong trends
        return True, True

    def calculate_quotes(self, current_price: float, current_inventory: float) -> Tuple[float, float]:
        """
        Calculates ideal Bid and Ask prices based on current price and inventory.
        
        Logic:
        - Base Mid Price = current_price
        - Skew = (Target - Inventory) * SkewFactor
        - Skewed Mid = Mid * (1 + Skew)
        - Bid = Skewed Mid * (1 - Spread/2)
        - Ask = Skewed Mid * (1 + Spread/2)
        """
        if current_price <= 0:
            return 0.0, 0.0
            
        # Inventory Skew Logic
        # If Inventory > Target (Too Long) -> Lower quotes to encourage selling (Sell cheaper, Buy lower)
        # If Inventory < Target (Too Short) -> Raise quotes to encourage buying (Buy higher, Sell higher)
        
        # Normalize inventory deviation (simple linear model for now)
        # Assuming inventory is in USD value. 
        # A deviation of $1000 might shift price by 0.01% * skew_factor
        
        inventory_diff = self.inventory_target - current_inventory
        
        # Skew calculation (simplified)
        # Example: Target 0, Inv +1000. Diff -1000.
        # We want to lower price.
        # Shift basis points = (Diff / OrderSize) * SkewFactor * 0.0001
        
        # Protect against div by zero
        safe_order_size = self.order_size if self.order_size > 0 else 10.0
        
        skew_bps = (inventory_diff / safe_order_size) * self.skew_factor * 0.0005
        
        # Clamp skew to prevent wild pricing (max +/- 5% shift)
        skew_bps = max(-0.05, min(skew_bps, 0.05))
        
        skewed_mid = current_price * (1 + skew_bps)
        
        half_spread = self.spread_pct / 2.0
        
        ideal_bid = skewed_mid * (1 - half_spread)
        ideal_ask = skewed_mid * (1 + half_spread)
        
        # Sanity check: Ensure Bid < Ask
        if ideal_bid >= ideal_ask:
            # Fallback to unskewed
            ideal_bid = current_price * (1 - half_spread)
            ideal_ask = current_price * (1 + half_spread)
            
        self.last_bid = ideal_bid
        self.last_ask = ideal_ask
        
        return ideal_bid, ideal_ask
