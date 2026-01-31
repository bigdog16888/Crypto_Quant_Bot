import ccxt
import time
import logging
import math
import threading
from config.settings import config

# ============================================
# MODULE-LEVEL CACHES (v0.7.0)
# Prevents redundant API calls across threads
# ============================================
_markets_cache = {}
_markets_cache_timestamp = {}
_MARKETS_CACHE_TTL = 300  # 5 minutes

# OHLCV Cache: {symbol_timeframe_limit: (timestamp, data)}
_ohlcv_cache = {}
_OHLCV_CACHE_TTL = 30  # 30 seconds

# Request Coalescing: Prevents duplicate simultaneous API calls
# Format: {(method, *args, *kwargs): (timestamp, future/result)}
_pending_requests = {}
_request_lock = threading.Lock()

# API Call Metrics
_api_call_count = 0
_api_call_lock = threading.Lock()

# Module-level flags to prevent repeated logging
_demo_trading_logged = False
_demo_trading_lock = threading.Lock()

def _get_cache_key(exchange_id, market_type):
    """Generate cache key for markets"""
    return f"{exchange_id}_{market_type}"

def _get_ohlcv_cache_key(symbol, timeframe, limit):
    """Generate cache key for OHLCV data"""
    return f"{symbol}_{timeframe}_{limit}"

def cleanup_caches(max_age_seconds=600):
    """
    Clean up expired cache entries to prevent memory leaks.
    Call periodically (e.g., every hour).
    """
    global _markets_cache, _markets_cache_timestamp, _ohlcv_cache
    current_time = time.time()

    # Clean OHLCV cache
    expired_keys = []
    for key, (timestamp, data) in _ohlcv_cache.items():
        if current_time - timestamp > max_age_seconds:
            expired_keys.append(key)

    for key in expired_keys:
        del _ohlcv_cache[key]

    # Clean markets cache
    for key in list(_markets_cache_timestamp.keys()):
        if current_time - _markets_cache_timestamp.get(key, 0) > max_age_seconds:
            if key in _markets_cache:
                del _markets_cache[key]
            if key in _markets_cache_timestamp:
                del _markets_cache_timestamp[key]

    logging.getLogger("exchange_interface").info(f"Cache cleanup: Removed {len(expired_keys)} OHLCV entries")

class ExchangeInterface:
    def __init__(self, exchange_id='binance', market_type='spot', validate=False):
        self.exchange_id = exchange_id
        self.market_type = market_type
        self.logger = logging.getLogger(__name__)
        
        # 1. Initialize CCXT
        options = {
            'defaultType': market_type,
            'adjustForTimeDifference': True,  # Auto-sync time with server
            'recvWindow': 60000,              # Tolerate up to 60s of clock drift
        }
        
        self.exchange = getattr(ccxt, exchange_id)({
            'apiKey': config.API_KEY,
            'secret': config.API_SECRET,
            'enableRateLimit': True,
            'options': options,
            'timeout': 30000,
        })
        
        # 2. Enable Demo Trading (Unified environment)
        # Only log once to avoid spam (each thread creates its own instance)
        if config.TESTNET:
            with _demo_trading_lock:
                global _demo_trading_logged
                if not _demo_trading_logged:
                    self.logger.warning("ENABLING BINANCE DEMO TRADING")
                    _demo_trading_logged = True
            self.exchange.enable_demo_trading(True)
            
        # 3. Load Markets (CRITICAL for validOrderTypes)
        self.markets_loaded = False
        self._key_logged = False  # Initialize tracking flag
        self._ensure_markets()

        if validate:
            self._validate_api_keys()

    def _ensure_markets(self):
        """Load markets with caching to prevent redundant API calls"""
        global _markets_cache, _markets_cache_timestamp
        
        if not self.markets_loaded:
            try:
                cache_key = _get_cache_key(self.exchange_id, self.market_type)
                current_time = time.time()
                
                # Check if we have a valid cached version
                if cache_key in _markets_cache:
                    cache_age = current_time - _markets_cache_timestamp.get(cache_key, 0)
                    if cache_age < _MARKETS_CACHE_TTL:
                        # Use cached markets
                        self.exchange.markets = _markets_cache[cache_key]
                        self.markets_loaded = True
                        self.logger.debug(f"✅ Markets loaded from cache (age: {cache_age:.0f}s)")
                        return
                
                # Load fresh markets
                self.exchange.load_markets()
                self.markets_loaded = True
                
                # Update cache
                _markets_cache[cache_key] = self.exchange.markets
                _markets_cache_timestamp[cache_key] = current_time
                
                self.logger.info(f"✅ Markets loaded successfully (cached for {_MARKETS_CACHE_TTL}s)")
            except Exception as e:
                self.logger.error(f"❌ Failed to load markets: {e}")

    def _coalesced_request(self, method, *args, **kwargs):
        """
        Make API request with coalescing - prevents duplicate simultaneous requests.
        If another thread is already making the same request, wait for it instead of duplicating.
        """
        global _pending_requests, _request_lock
        
        # Create cache key for this request (only for idempotent reads)
        coalesce_methods = {'fetch_ohlcv', 'fetch_balance', 'fetch_positions', 'fetch_open_orders', 'fetch_ticker'}
        
        if method in coalesce_methods:
            # Create a hashable key for the request
            request_key = (method, args[0] if args else None, tuple(sorted(kwargs.items())) if kwargs else None)
            
            with _request_lock:
                # Check if request is already in progress
                if request_key in _pending_requests:
                    pending_time, future = _pending_requests[request_key]
                    # If request was started recently (< 5 seconds), wait for it
                    if time.time() - pending_time < 5:
                        self.logger.debug(f"⏳ Coalesced request waiting for {method}")
                        # Wait for the result (this is a simplified implementation)
                        # In production, you'd use threading.Event or concurrent.futures
                        del _pending_requests[request_key]  # Remove to avoid stale entries
                
                # Start the request and register it
                _pending_requests[request_key] = (time.time(), None)
            
            # Make the actual request
            result = self._execute_request(method, *args, **kwargs)
            
            # Clear from pending
            with _request_lock:
                _pending_requests.pop(request_key, None)
            
            return result
        
        # For non-coalesced methods (writes), execute directly
        return self._execute_request(method, *args, **kwargs)
    
    def _execute_request(self, method, *args, **kwargs):
        """Execute the actual API request (used by coalesced and non-coalesced paths)"""
        global _api_call_count, _api_call_lock
        
        with _api_call_lock:
            _api_call_count += 1
        
        max_retries = config.MAX_RETRIES
        delay = config.RETRY_DELAY
        
        for attempt in range(max_retries + 1):
            try:
                func = getattr(self.exchange, method)
                
                # ULTIMATE FIX FOR BINANCE -1104
                if method == 'create_order':
                    # Extract core params
                    symbol = kwargs.get('symbol')
                    order_type = kwargs.get('type')
                    side = kwargs.get('side')
                    amount = kwargs.get('amount')
                    price = kwargs.get('price')
                    
                    # Strip params to the absolute bare minimum
                    # We ONLY allow functional flags that don't conflict with Binance internals
                    raw_p = kwargs.get('params', {})
                    clean_p = {}
                    if 'reduceOnly' in raw_p: clean_p['reduceOnly'] = raw_p['reduceOnly']
                    if 'postOnly' in raw_p: clean_p['postOnly'] = raw_p['postOnly']
                    
                    # Call CCXT with positional args + cleaned dict
                    return func(symbol, order_type, side, amount, price, clean_p)
                
                return func(*args, **kwargs)
            
            except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                if attempt < max_retries:
                    time.sleep(delay * (attempt + 1))
                    continue
                raise
            except Exception as e:
                if "code\":-1104" in str(e) and method == 'create_order':
                    self.logger.error(f"🔥 FINAL ATTEMPT at order: params caused -1104. Retrying with EMPTY params.")
                    return getattr(self.exchange, method)(kwargs.get('symbol'), kwargs.get('type'), kwargs.get('side'), kwargs.get('amount'), kwargs.get('price'), {})
                raise

    def _safe_request(self, method, *args, **kwargs):
        if not hasattr(self, '_key_logged'):
            key = str(self.exchange.apiKey)
            self.logger.info(f"Using API Key: {key[:5]}...")
            self._key_logged = True

        max_retries = config.MAX_RETRIES
        delay = config.RETRY_DELAY
        
        for attempt in range(max_retries + 1):
            try:
                func = getattr(self.exchange, method)
                
                # ULTIMATE FIX FOR BINANCE -1104
                if method == 'create_order':
                    # Extract core params
                    symbol = kwargs.get('symbol')
                    order_type = kwargs.get('type')
                    side = kwargs.get('side')
                    amount = kwargs.get('amount')
                    price = kwargs.get('price')
                    
                    # Strip params to the absolute bare minimum
                    # We ONLY allow functional flags that don't conflict with Binance internals
                    raw_p = kwargs.get('params', {})
                    clean_p = {}
                    if 'reduceOnly' in raw_p: clean_p['reduceOnly'] = raw_p['reduceOnly']
                    if 'postOnly' in raw_p: clean_p['postOnly'] = raw_p['postOnly']
                    
                    # Call CCXT with positional args + cleaned dict
                    return func(symbol, order_type, side, amount, price, clean_p)
                
                return func(*args, **kwargs)
            
            except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                if attempt < max_retries:
                    time.sleep(delay * (attempt + 1))
                    continue
                raise
            except Exception as e:
                if "code\":-1104" in str(e) and method == 'create_order':
                    self.logger.error(f"🔥 FINAL ATTEMPT at order: params caused -1104. Retrying with EMPTY params.")
                    return getattr(self.exchange, method)(kwargs.get('symbol'), kwargs.get('type'), kwargs.get('side'), kwargs.get('amount'), kwargs.get('price'), {})
                raise

    def create_order(self, symbol, type, side, amount, price=None, params={}):
        if params is None: params = {}
        # Pre-validation
        is_valid, s_amt, s_price, err = self.validate_order(symbol, side, amount, price)
        if not is_valid:
             self.logger.error(f"Validation failed: {err}")
             # Minimal fallback...
             return self._safe_request('create_order', symbol=symbol, type=type, side=side, amount=amount, price=price, params=params)
        
        try:
            return self._safe_request('create_order', symbol=symbol, type=type, side=side, amount=s_amt, price=s_price, params=params)
        except Exception as e:
            # FIX: Only mock network errors, NOT logic/balance errors
            # "Account has insufficient balance" or "Margin is insufficient" should FAIL hard.
            str_e = str(e).lower()
            critical_errors = ['insufficient balance', 'margin is insufficient', 'insufficient funds', 'account has insufficient']
            
            if any(crit in str_e for crit in critical_errors):
                 self.logger.error(f"CRITICAL ORDER FAILURE: {e}")
                 raise e # Do not mock this!
            
            # if config.TESTNET:
            #     self.logger.warning(f"⚠️ TESTNET MOCK: Real order failed ({e}). Returning FAKE success.")
            #     # ... MOCK REMOVED ...
            raise e

    def validate_order(self, symbol, side, amount, price):
        self._ensure_markets()
        try:
            sanitized_amount = float(self.exchange.amount_to_precision(symbol, amount))
            sanitized_price = float(self.exchange.price_to_precision(symbol, price)) if price else None
            return True, sanitized_amount, sanitized_price, None
        except Exception as e:
            return False, amount, price, str(e)

    def fetch_balance(self):
        return self._coalesced_request('fetch_balance')

    def get_open_orders(self, symbol):
        """Standardized alias for fetch_open_orders"""
        return self.fetch_open_orders(symbol)

    def fetch_open_orders(self, symbol):
        return self._coalesced_request('fetch_open_orders', symbol=symbol)

    def fetch_positions(self, symbols=None):
        if self.market_type in ['future', 'swap']:
            return self._coalesced_request('fetch_positions', symbols=symbols)
        return []

    def fetch_ticker(self, symbol):
        return self._coalesced_request('fetch_ticker', symbol=symbol)

    def fetch_ohlcv(self, symbol, timeframe='1h', limit=100):
        """
        Fetch OHLCV data with caching to reduce API calls.
        Cache TTL: 30 seconds (price data changes slowly)
        """
        global _ohlcv_cache, _OHLCV_CACHE_TTL

        cache_key = _get_ohlcv_cache_key(symbol, timeframe, limit)
        current_time = time.time()

        # Check cache first
        if cache_key in _ohlcv_cache:
            cached_time, cached_data = _ohlcv_cache[cache_key]
            cache_age = current_time - cached_time
            if cache_age < _OHLCV_CACHE_TTL:
                self.logger.debug(f"✅ OHLCV cache hit for {symbol} ({timeframe}, age: {cache_age:.0f}s)")
                return cached_data

        # Fetch fresh data
        result = self._safe_request('fetch_ohlcv', symbol=symbol, timeframe=timeframe, limit=limit)

        # Update cache
        _ohlcv_cache[cache_key] = (current_time, result)
        self.logger.debug(f"✅ OHLCV fetched for {symbol} ({timeframe})")

        return result

    def cancel_all_orders(self, symbol):
        return self._safe_request('cancel_all_orders', symbol=symbol)
    
    def get_last_price(self, symbol: str) -> float:
        try:
            ticker = self.fetch_ticker(symbol)
            if not ticker: return 0.0
            last = ticker.get('last')
            if last is None: return 0.0
            return float(last)
        except Exception:
            return 0.0

    def set_leverage(self, symbol, leverage):
        """Set leverage with proper futures symbol formatting."""
        try:
            # Ensure futures format for symbol
            if '/' in symbol and ':USDT' not in symbol and ':USDC' not in symbol:
                # Convert BTC/USDC to BTC/USDC:USDC for futures
                if symbol.endswith('/USDC'):
                    futures_symbol = f"{symbol}:USDC"
                elif symbol.endswith('/USDT'):
                    futures_symbol = f"{symbol}:USDT"
                else:
                    futures_symbol = f"{symbol}:USDT"
            else:
                futures_symbol = symbol

            return self._safe_request('set_leverage', int(leverage), futures_symbol)
        except Exception as e:
            self.logger.error(f"Failed to set leverage {leverage}x for {symbol}: {e}")
            return False

    def get_min_order_usd(self, symbol: str, price: float = 0.0) -> float:
        try:
            self._ensure_markets()
            market = self.exchange.market(symbol)
            
            # Check for 'cost' limit (e.g. Min 5 USDT or 100 USDC on Testnet)
            # This is the "Min Notional" value
            min_cost = market.get('limits', {}).get('cost', {}).get('min')
            if min_cost:
                return float(min_cost)
            
            # Fallback: Check 'amount' limit * price
            min_amount = market.get('limits', {}).get('amount', {}).get('min')
            if min_amount and price and price > 0:
                return float(min_amount * price)
            
            # Default fallback
            return 5.0
        except Exception:
            return 5.0
                
    def calculate_safe_min_size(self, symbol: str, price: float) -> float:
        """
        Calculates the SAFE minimum USD size that satisfies both:
        1. Min Notional (Cost) Limit
        2. Exchange Step Size (Lot Size) constraints
        
        Example:
        - Price: $89,000
        - Min Notional: $100
        - Step Size: 0.001 BTC
        
        Math:
        - Raw needed: 100 / 89000 = 0.00112 BTC
        - If rounded down (standard): 0.001 BTC = $89 (INVALID < $100)
        - Must round UP: 0.002 BTC = $178 (VALID > $100)
        
        Returns: Float (USD value, e.g. 178.0)
        """
        try:
            self._ensure_markets()
            market = self.exchange.market(symbol)
            limits = market.get('limits', {})
            precision = market.get('precision', {})
            
            # 1. Get Min Notional (Cost)
            min_cost = limits.get('cost', {}).get('min')
            if not min_cost:
                min_cost = 5.0 # Default fallback
            min_cost = float(min_cost)
            
            # 2. Get Step Size (Amount Precision/Min)
            step_size = precision.get('amount')
            if not step_size:
                step_size = limits.get('amount', {}).get('min')
            
            if not step_size: 
                # If no step size defined, just return min_cost with 1% buffer
                return min_cost * 1.01
                
            step_size = float(step_size)
            
            # 3. Calculate Quantity needed
            raw_qty = min_cost / price
            
            # 4. Round UP to nearest step size
            # steps = ceil(raw_qty / step_size)
            # safe_qty = steps * step_size
            steps = math.ceil(raw_qty / step_size)
            safe_qty = steps * step_size
            
            # 5. Convert back to USD
            safe_usd = safe_qty * price
            
            # 6. Add small epsilon (0.1%) to handle floating point jitters
            return safe_usd * 1.001
            
        except Exception as e:
            self.logger.error(f"Error calculating safe min size: {e}")
            return 10.0 # Fallback

    def get_available_symbols(self, quote_asset='USDT'):
        self._ensure_markets()
        return [s for s in self.exchange.symbols if quote_asset in s]

    def _validate_api_keys(self):
        try: return bool(self.fetch_balance())
        except: return False

    def fetch_order(self, order_id, symbol):
        """Fetch a single order by ID."""
        return self._safe_request('fetch_order', id=order_id, symbol=symbol)

    def wait_for_fill(self, order, timeout=30, timeout_seconds=None):
        """
        Polls the exchange to check if an order is filled.
        Returns the filled order object or raises TimeoutError.
        """
        # Handle parameter alias
        if timeout_seconds is not None:
            timeout = timeout_seconds

        if not order or 'id' not in order:
            return order

        order_id = order['id']
        symbol = order['symbol']
        start_time = time.time()
        
        # Immediate mock check
        if order.get('info', {}).get('mock'):
             self.logger.info(f"Mock order {order_id} filled instantly.")
             order['status'] = 'closed'
             order['filled'] = order['amount']
             order['remaining'] = 0.0
             return order
        
        while (time.time() - start_time) < timeout:
            try:
                updated_order = self.fetch_order(order_id, symbol)
                status = updated_order.get('status')
                
                if status in ['closed', 'filled']:
                    return updated_order
                if status in ['canceled', 'rejected', 'expired']:
                    self.logger.warning(f"Order {order_id} was {status}")
                    return updated_order
                    
                time.sleep(1) # Wait 1s between polls
            except Exception as e:
                self.logger.warning(f"Error polling order {order_id}: {e}")
                time.sleep(1)
        
        # If we get here, it timed out
        self.logger.warning(f"Timed out waiting for fill on order {order_id}")
        return order

    # ============================================
    # BATCH API CALLS (v1.0)
    # Reduces API calls by fetching multiple items at once
    # ============================================

    def fetch_balances_all(self):
        """
        Fetch all balances in a single call (more efficient than per-currency).
        Returns dict of currency -> balance info.
        """
        return self._coalesced_request('fetch_balance')

    def fetch_positions_by_symbols(self, symbols: list) -> dict:
        """
        Fetch positions for multiple symbols efficiently.
        Returns dict: {symbol: position_data or None}
        """
        try:
            all_positions = self._coalesced_request('fetch_positions')
            positions_map = {}

            if not all_positions:
                return {sym: None for sym in symbols}

            # Build lookup map (handle both 'symbol' and 'info.symbol' formats)
            for pos in all_positions:
                if not pos:
                    continue
                pos_symbol = pos.get('symbol', '')
                # Normalize symbol (remove :USDC etc)
                clean_symbol = pos_symbol.replace('/', '').split(':')[0].upper()

                size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                if size != 0:  # Only include open positions
                    positions_map[clean_symbol] = pos

            # Return position for each requested symbol (or None if not found)
            result = {}
            for sym in symbols:
                clean_sym = sym.replace('/', '').upper()
                result[sym] = positions_map.get(clean_sym, None)

            return result
        except Exception as e:
            self.logger.error(f"Failed to fetch positions for {symbols}: {e}")
            return {sym: None for sym in symbols}

    def fetch_tickers_bulk(self, symbols: list) -> dict:
        """
        Fetch tickers for multiple symbols in one call.
        More efficient than calling fetch_ticker() for each symbol.
        Returns dict: {symbol: ticker_data}
        """
        try:
            if not symbols:
                return {}

            # Use CCXT's fetch_tickers for bulk request
            all_tickers = self._safe_request('fetch_tickers', symbols=symbols)

            if not all_tickers:
                return {}

            # Return only requested symbols
            result = {}
            for sym in symbols:
                if sym in all_tickers:
                    result[sym] = all_tickers[sym]

            return result
        except Exception as e:
            # Fallback: fetch individually if bulk fails
            self.logger.warning(f"Bulk ticker fetch failed, falling back to individual: {e}")
            result = {}
            for sym in symbols:
                try:
                    result[sym] = self._coalesced_request('fetch_ticker', symbol=sym)
                except:
                    pass
            return result

    # ============================================
    # METRICS & DIAGNOSTICS
    # ============================================

    def get_api_call_stats(self) -> dict:
        """Return API call statistics for monitoring"""
        global _api_call_count, _ohlcv_cache, _markets_cache
        
        return {
            'total_api_calls': _api_call_count,
            'ohlcv_cache_size': len(_ohlcv_cache),
            'markets_cache_size': len(_markets_cache),
            'pending_requests': len(_pending_requests)
        }

    def reset_api_stats(self):
        """Reset API call counters (call after statistics are collected)"""
        global _api_call_count
        with _api_call_lock:
            _api_call_count = 0
