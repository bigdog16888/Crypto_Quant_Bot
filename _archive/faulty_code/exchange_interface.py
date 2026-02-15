import ccxt
import time
import logging
import json
import os
import math
import threading
from typing import Dict, List, Optional, Any, Union

from engine.exceptions import InsufficientFundsError, OrderNotFoundError, APIError, NetworkError
from config.settings import get_config

config = get_config()

logger = logging.getLogger("ExchangeInterface")

# --- Caching Globals ---
_generic_cache = {}
_ohlcv_cache = {}
_GENERIC_CACHE_TTL = 3 # seconds
_OHLCV_CACHE_TTL = 30 # seconds
_request_lock = threading.Lock()
_pending_requests = {}
_api_call_count = 0
_api_call_lock = threading.Lock()

def _get_generic_cache_key(method, symbol=None, params=None):
    key = f"{method}"
    if symbol: key += f"_{symbol}"
    if params: key += f"_{json.dumps(params, sort_keys=True)}"
    return key

def _get_ohlcv_cache_key(symbol, timeframe, limit):
    return f"ohlcv_{symbol}_{timeframe}_{limit}"

def cleanup_caches():
    global _generic_cache, _ohlcv_cache
    now = time.time()
    
    # Clean generic cache
    to_delete = [k for k, (ts, _) in _generic_cache.items() if now - ts > _GENERIC_CACHE_TTL]
    for k in to_delete:
        _generic_cache.pop(k)
    if to_delete: logger.info(f"Cache cleanup: Removed {len(to_delete)} generic entries")
    
    # Clean OHLCV cache
    to_delete = [k for k, (ts, _) in _ohlcv_cache.items() if now - ts > _OHLCV_CACHE_TTL]
    for k in to_delete:
        _ohlcv_cache.pop(k)
    if to_delete: logger.info(f"Cache cleanup: Removed {len(to_delete)} OHLCV entries")

class ExchangeInterface:
    def __init__(self, market_type='future'):
        self.market_type = market_type
        self.exchange = self._create_exchange_instance()
        self.logger = logging.getLogger(f"ExchangeInterface.{market_type}")
        self._ensure_markets()

    def _create_exchange_instance(self):
        exchange_class = getattr(ccxt, config.EXCHANGE_ID)
        exchange = exchange_class({
            'apiKey': config.API_KEY,
            'secret': config.API_SECRET,
            'enableRateLimit': True,
            'options': {
                'defaultType': self.market_type,
            },
            'timeout': 30000, # 30 seconds
        })
        if config.DEMO_TRADING: 
            exchange.set_sandbox_mode(True)
            self.logger.warning("ENABLING BINANCE DEMO TRADING")
        
        # Binance specific config (for futures)
        if config.EXCHANGE_ID == 'binance' and self.market_type == 'future':
            exchange.options['defaultType'] = 'future'
            exchange.options['warnOnFetchOpenOrdersWithoutSymbol'] = False
            exchange.options['adjustForTimeDifference'] = True
            exchange.options['recvWindow'] = 10000

        return exchange

    def _invalidate_open_orders_cache(self, symbol: Optional[str] = None):
        """Invalidates the cached open orders for a given symbol or all symbols."""
        cache_key = _get_generic_cache_key('fetch_open_orders', symbol=symbol)
        if cache_key in _generic_cache:
            del _generic_cache[cache_key]
            self.logger.debug(f"🧹 Invalidated open_orders cache for {symbol}")

    def _ensure_markets(self):
        if not self.exchange.markets:
            self.logger.info("Loading markets...")
            self.exchange.load_markets()
            self.logger.info("Markets loaded.")

    def _coalesced_request(self, method: str, symbol: Optional[str] = None, params: Optional[dict] = None, force_refresh: bool = False, cache_ttl: Optional[float] = None) -> Any:
        """
        Coalesces identical API requests and caches results to reduce API spam.
        Uses _request_lock to ensure only one thread makes the live API call.
        """
        request_key = _get_generic_cache_key(method, symbol, params)
        current_time = time.time()
        
        # Use method-specific TTL if provided, else generic
        effective_ttl = cache_ttl if cache_ttl is not None else _GENERIC_CACHE_TTL

        # Check cache first if not forcing refresh
        if not force_refresh and request_key in _generic_cache:
            cached_time, cached_data = _generic_cache[request_key]
            if (current_time - cached_time) < effective_ttl:
                self.logger.debug(f"✅ Cache hit for {method} {symbol} (age: {current_time - cached_time:.1f}s)")
                return cached_data

        # If a request is already pending, wait for it
        while request_key in _pending_requests:
            self.logger.debug(f"⏳ Request {request_key} is pending, waiting...")
            time.sleep(0.1)
            # After waiting, check cache again in case the pending request finished
            if not force_refresh and request_key in _generic_cache:
                cached_time, cached_data = _generic_cache[request_key]
                if (current_time - cached_time) < effective_ttl:
                    self.logger.debug(f"✅ Cache hit after wait for {method} {symbol} (age: {current_time - cached_time:.1f}s)")
                    return cached_data
        
        # This thread is the first to make the request, mark as pending
        with _request_lock:
            _pending_requests[request_key] = threading.current_thread().name

        try:
            result = self._execute_request(method, symbol=symbol, params=params)
            if result is not None: # Cache only non-None results
                _generic_cache[request_key] = (time.time(), result)
            return result
        finally:
            # Always clear from pending
            with _request_lock:
                _pending_requests.pop(request_key, None)

    def _execute_request(self, method: str, **kwargs) -> Any:
        """Execute the actual API request (used by coalesced and non-coalesced paths)"""
        global _api_call_count, _api_call_lock
        
        with _api_call_lock:
            _api_call_count += 1
        
        max_retries = config.MAX_RETRIES
        delay = config.RETRY_DELAY
        
        for attempt in range(max_retries + 1):
            try:
                func = getattr(self.exchange, method)
                
                # ULTIMATE FIX FOR BINANCE -1104 / Hedge Mode
                if method == 'create_order':
                    symbol = kwargs.get('symbol')
                    order_type = kwargs.get('type')
                    side = kwargs.get('side')
                    amount = kwargs.get('amount')
                    price = kwargs.get('price')
                    
                    raw_p = kwargs.get('params')
                    clean_p = raw_p.copy() if raw_p is not None else {}
                    
                    if 'positionSide' in clean_p and 'reduceOnly' in clean_p:
                        self.logger.debug(f"⚠️ Conflict: Both positionSide and reduceOnly provided. Removing reduceOnly.")
                        del clean_p['reduceOnly']
                    
                    self.logger.debug(f"📤 API create_order: {symbol} {side} {amount} @ {price} | Params: {list(clean_p.keys())}")
                    return func(symbol=symbol, type=order_type, side=side, amount=amount, price=price, params=clean_p)
                
                # For all other methods, pass kwargs directly
                return func(**kwargs)
            
            except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                if attempt < max_retries:
                    self.logger.warning(f"⚠️ Network error on {method} (Attempt {attempt+1}/{max_retries}): {e}")
                    time.sleep(delay * (attempt + 1))
                    continue
                raise NetworkError(f"Persistent Network Error: {e}")

            except (InsufficientFundsError, OrderNotFoundError, APIError) as e:
                raise e
            except Exception as e:
                if "code\":-1104" in str(e) and method == 'create_order':
                    self.logger.error(f"🔥 FINAL ATTEMPT at order: params caused -1104. Retrying with EMPTY params.")
                    return func(symbol=kwargs.get('symbol'), type=kwargs.get('type'), side=kwargs.get('side'), amount=kwargs.get('amount'), price=kwargs.get('price'), params={})
                
                raise APIError(f"Generic Exchange Error: {e}")

    def fetch_positions(self, symbol: Optional[str] = None, force_refresh: bool = False, params: Optional[dict] = None) -> List[dict]:
        raw_positions = self._coalesced_request('fetch_positions', symbol=symbol, params=params, force_refresh=force_refresh)
        self.logger.critical(f"🔍 [API-RESULT] fetch_positions API returned (force_refresh={force_refresh}): {len(raw_positions) if raw_positions else 0} positions")
        if raw_positions:
            for idx, p in enumerate(raw_positions):
                self.logger.critical(f"🔍 [API-RESULT]   Position {idx}: {p.get('symbol', 'UNKNOWN')} - contracts={p.get('contracts', 0)}, size={p.get('size', 0)}")
        return raw_positions

    def fetch_balance(self, params: Optional[dict] = None):
        return self._coalesced_request('fetch_balance', params=params)

    def fetch_open_orders(self, symbol: Optional[str] = None, force_refresh: bool = False, params: Optional[dict] = None) -> List[dict]:
        return self._coalesced_request('fetch_open_orders', symbol=symbol, params=params, force_refresh=force_refresh)
    
    def cancel_orders_by_bot_id(self, bot_id: int, symbol: str):
        cancelled_count = 0
        open_orders = self.fetch_open_orders(symbol, force_refresh=True) 
        bot_prefix = f"CQB_{bot_id}_"
        
        for order in open_orders:
            client_oid = order.get('clientOrderId', '')
            if client_oid.startswith(bot_prefix):
                try:
                    self.logger.info(f"Cancelling order {order['id']} ({client_oid}) for bot {bot_id}")
                    self.exchange.cancel_order(order['id'], symbol)
                    cancelled_count += 1
                except Exception as e:
                    self.logger.error(f"Failed to cancel order {order['id']} for bot {bot_id}: {e}")
        
        if cancelled_count > 0:
            self._invalidate_open_orders_cache(symbol)
        else:
            self.logger.info(f"  ℹ️  No orders to cancel for Bot {bot_id}")
        
        return cancelled_count

    def validate_order(self, symbol: str, side: str, amount: float, price: Optional[float] = None):
        if symbol not in self.exchange.markets:
            raise ValueError(f"Invalid symbol: {symbol}")

        market = self.exchange.markets[symbol]

        if price is not None:
            price_precision = market['precision']['price']
            if price_precision and price % price_precision != 0:
                price = self.exchange.price_to_precision(symbol, price)
                self.logger.warning(f"⚠️ Price {price} for {symbol} adjusted to precision: {price_precision}")

        amount_precision = market['precision']['amount']
        if amount_precision and amount % amount_precision != 0:
            amount = self.exchange.amount_to_precision(symbol, amount)
            self.logger.warning(f"⚠️ Amount {amount} for {symbol} adjusted to precision: {amount_precision}")

        min_notional = market.get('limits', {}).get('cost', {}).get('min', 0.0)
        if min_notional > 0 and price is not None and (amount * price) < min_notional:
            if price > 0:
                new_amount = math.ceil(min_notional / price / amount_precision) * amount_precision
                self.logger.warning(f"⚠️ Amount {amount} for {symbol} adjusted to meet min notional ${min_notional}: {new_amount}")
                amount = new_amount
            else:
                self.logger.warning(f"Cannot check min_notional for {symbol} with 0 price.")
        
        max_notional = market.get('limits', {}).get('cost', {}).get('max', 0.0)
        if max_notional > 0 and price is not None and (amount * price) > max_notional:
            self.logger.error(f"❌ Order notional ${amount * price:.2f} exceeds max notional ${max_notional:.2f} for {symbol}")
            raise ValueError(f"Order notional exceeds max for {symbol}")

        min_amount = market.get('limits', {}).get('amount', {}).get('min', 0.0)
        if amount < min_amount:
            self.logger.error(f"❌ Order amount {amount} is less than min amount {min_amount} for {symbol}")
            raise ValueError(f"Order amount too small for {symbol}")

        max_amount = market.get('limits', {}).get('amount', {}).get('max', 0.0)
        if max_amount > 0 and amount > max_amount:
            self.logger.error(f"❌ Order amount {amount} exceeds max amount {max_amount} for {symbol}")
            raise ValueError(f"Order amount too large for {symbol}")
            
        return True, amount, price, ""

    def get_min_order_usd(self, symbol: str, current_price: float) -> float:
        if symbol not in self.exchange.markets: return 0.0
        market = self.exchange.markets[symbol]
        min_notional = market.get('limits', {}).get('cost', {}).get('min', 0.0)
        return min_notional

    def calculate_safe_min_size(self, symbol: str, price: float) -> float:
        if symbol not in self.exchange.markets: return 0.0
        market = self.exchange.markets[symbol]

        min_notional = market.get('limits', {}).get('cost', {}).get('min', 0.0)
        if min_notional <= 0: return 0.0

        amount_precision = market['precision']['amount']

        base_amount_raw = (min_notional / price) * 1.00001
        
        if amount_precision > 0: 
            min_amount_precision_adjusted = math.ceil(base_amount_raw / amount_precision) * amount_precision
        else:
            min_amount_precision_adjusted = base_amount_raw
        
        return min_amount_precision_adjusted * price

    def get_market_precision(self, symbol: str):
        if symbol not in self.exchange.markets: return None
        market = self.exchange.markets[symbol]
        return market['precision']

    def get_market_limits(self, symbol: str):
        if symbol not in self.exchange.markets: return None
        market = self.exchange.markets[symbol]
        return market['limits']

    def fetch_order(self, order_id: str, symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self._coalesced_request('fetch_order', symbol=symbol, params={'id': order_id}, cache_ttl=0) 

    def wait_for_fill(self, order: Dict[str, Any], timeout_seconds: int = 60) -> Optional[Dict[str, Any]]:
        order_id = order['id']
        symbol = order['symbol']
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            try:
                status = self.fetch_order(order_id, symbol)
                if status and status.get('status') in ['closed', 'filled']:
                    return status
            except OrderNotFoundError:
                self.logger.warning(f"Order {order_id} not found during wait_for_fill. Assuming cancelled or failed.")
                return None
            except Exception as e:
                self.logger.error(f"Error fetching order {order_id} during wait_for_fill: {e}")
            time.sleep(1) 
        self.logger.warning(f"Timed out waiting for fill on order {order_id}")
        return None

    def _get_api_call_count(self) -> int:
        with _api_call_lock:
            return _api_call_count

    def _reset_api_call_count(self):
        with _api_call_lock:
            _api_call_count = 0

def normalize_symbol(symbol: str) -> str:
    if not symbol: return ""
    
    if ':' in symbol:
        symbol = symbol.split(':')[0]
    return symbol.replace('/', '').replace('-', '').upper()
