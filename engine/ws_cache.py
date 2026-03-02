"""
WebSocket Memory Cache (Phase 11.2)

A fast, thread-safe memory store for exchange data updated in real-time
by the WebSocket stream. Prevents the bot from needing to poll the REST API 
frequently when managing 20+ pairs.
"""

from typing import Dict, List
import threading
import time

class WSCache:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(WSCache, cls).__new__(cls)
                cls._instance._init_cache()
            return cls._instance

    def _init_cache(self):
        self.positions: Dict[str, Dict] = {}  # { 'BTCUSDC': { 'contracts': 1.5, 'entryPrice': 90000, 'unrealizedPnl': 150.0, ... } }
        self.open_orders: Dict[str, Dict] = {} # { '12345678': { 'id': '12345678', 'symbol': 'BTCUSDC', ... } }
        self.last_update_time = 0.0
        self.data_lock = threading.RLock()

    def update_position(self, symbol: str, position_data: Dict):
        """Update or insert a position from WS event."""
        with self.data_lock:
            if position_data.get('contracts', 0) == 0:
                # Position closed, remove from cache
                self.positions.pop(symbol, None)
            else:
                self.positions[symbol] = position_data
            self.last_update_time = time.time()

    def populate_from_rest(self, positions: List[Dict], orders: List[Dict]):
        """Seed the cache with a full REST snapshot (e.g. on startup or after staleness)."""
        with self.data_lock:
            self.positions.clear()
            self.open_orders.clear()
            
            for p in (positions or []):
                sym = p.get('symbol')
                if sym: self.positions[sym] = p
                
            for o in (orders or []):
                oid = o.get('id')
                if oid: self.open_orders[str(oid)] = o
                
            self.last_update_time = time.time()

    def update_order(self, order_id: str, order_data: Dict):
        """Update or insert an open order from WS event."""
        with self.data_lock:
            status = order_data.get('status', '').upper()
            if status in ['FILLED', 'CANCELED', 'EXPIRED', 'REJECTED']:
                # Order no longer open, remove from cache
                self.open_orders.pop(str(order_id), None)
            else:
                self.open_orders[str(order_id)] = order_data
            self.last_update_time = time.time()

    def get_all_positions(self) -> List[Dict]:
        """Return a snapshot of all active physical positions."""
        with self.data_lock:
            return list(self.positions.values())

    def get_all_open_orders(self) -> List[Dict]:
        """Return a snapshot of all active open orders."""
        with self.data_lock:
            return list(self.open_orders.values())

    def is_fresh(self, max_age_seconds: float = 300) -> bool:
        """Check if the cache has received *any* updates recently."""
        with self.data_lock:
            if self.last_update_time == 0.0:
                return False
            return (time.time() - self.last_update_time) < max_age_seconds

def get_ws_cache() -> WSCache:
    """Singleton getter for the WS Cache."""
    return WSCache()
