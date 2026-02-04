import ccxt
import time
import logging
import math
import threading
from engine.exceptions import InsufficientFundsError, OrderNotFoundError, APIError, NetworkError
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

# Generic Cache: {method_key: (timestamp, data)}
# Used for balances, positions, and tickers to prevent double-calls in the same cycle.
_generic_cache = {}
_GENERIC_CACHE_TTL = 3  # 3 seconds

# API Call Metrics
_api_call_count = 0
_api_call_lock = threading.Lock()

# Per-Symbol Order Lock: Prevents race conditions when multiple bots trade same symbol
# In One-Way mode, Binance provisionally locks margin during order processing
# Multiple simultaneous orders on same symbol can fail with "insufficient balance"
_symbol_order_locks = {}
_symbol_locks_lock = threading.Lock()  # Meta-lock for creating per-symbol locks

def get_symbol_lock(symbol: str) -> threading.Lock:
    """Get or create a lock for a specific symbol."""
    global _symbol_order_locks
    normalized = normalize_symbol(symbol)
    with _symbol_locks_lock:
        if normalized not in _symbol_order_locks:
            _symbol_order_locks[normalized] = threading.Lock()
        return _symbol_order_locks[normalized]

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

def normalize_symbol(symbol: str) -> str:
    """Standardized symbol normalization for comparison across exchange/DB."""
    if not symbol: return ""
    return symbol.replace('/', '').replace(':USDT', '').replace(':USDC', '').split(':')[0].upper()

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
            'options': {
                'defaultType': market_type,
                'adjustForTimeDifference': True,
                'recvWindow': 60000,
                'warnOnFetchOpenOrdersWithoutSymbol': False,
            },
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

    def _make_hashable(self, val):
        """Recursively converts unhashable objects (lists, dicts) to hashable ones (tuples)."""
        if isinstance(val, list):
            return tuple(self._make_hashable(item) for item in val)
        if isinstance(val, dict):
            return tuple(sorted((k, self._make_hashable(v)) for k, v in val.items()))
        return val

    def _coalesced_request(self, method, *args, **kwargs):
        """
        Make API request with coalescing and short-term caching.
        1. Checks generic cache for recent results.
        2. Prevents duplicate simultaneous requests.
        """
        global _pending_requests, _request_lock, _generic_cache
        
        # Idempotent methods suitable for coalescing and short-term caching
        coalesce_methods = {'fetch_ohlcv', 'fetch_balance', 'fetch_positions', 'fetch_open_orders', 'fetch_ticker'}
        
        if method in coalesce_methods:
            # Create a hashable key for the request (Recursive)
            request_key = (
                method, 
                tuple(self._make_hashable(a) for a in args) if args else None, 
                tuple(sorted((k, self._make_hashable(v)) for k, v in kwargs.items())) if kwargs else None
            )
            current_time = time.time()

            # 1. Check Generic Cache (Global skip if fresh enough)
            if request_key in _generic_cache:
                ts, data = _generic_cache[request_key]
                if current_time - ts < _GENERIC_CACHE_TTL:
                    return data
            
            # --- GLOBAL-TO-LOCAL CACHE RESOLUTION (v1.1) ---
            # If we're asking for a specific symbol but have a global result cached, use it.
            if method == 'fetch_open_orders' and args and args[0] is not None:
                global_key = (method, None, tuple(sorted(kwargs.items())) if kwargs else None)
                if global_key in _generic_cache:
                    ts, all_orders = _generic_cache[global_key]
                    if current_time - ts < _GENERIC_CACHE_TTL:
                        # Filter for this symbol
                        symbol = args[0]
                        return [o for o in all_orders if o.get('symbol') == symbol]

            if method == 'fetch_positions' and args and args[0] is not None:
                global_key = (method, None, tuple(sorted(kwargs.items())) if kwargs else None)
                if global_key in _generic_cache:
                    ts, all_positions = _generic_cache[global_key]
                    if current_time - ts < _GENERIC_CACHE_TTL:
                        # Filter for this symbol
                        target_symbol = args[0]
                        return [p for p in all_positions if p.get('symbol') == target_symbol]
            
            with _request_lock:
                # 2. Check if request is already in progress
                if request_key in _pending_requests:
                    pending_time, _ = _pending_requests[request_key]
                    # If request was started very recently (< 2 seconds), and we're okay waiting
                    if current_time - pending_time < 2:
                        self.logger.debug(f"⏳ Coalescing {method}")
                        # In a multi-threaded env, we should wait for the first one.
                        # For simplicity here, we'll just allow the first one to finish 
                        # and subsequent ones will hit the cache in the next cycle or after a short delay.
                        # But actually, let's just proceed for the first one to fill the cache.
                
                # Register that we are starting this request
                _pending_requests[request_key] = (current_time, None)
            
            try:
                # Make the actual request
                result = self._execute_request(method, *args, **kwargs)
                
                # 3. Update Generic Cache on success
                _generic_cache[request_key] = (time.time(), result)
                return result
            finally:
                # Always clear from pending
                with _request_lock:
                    _pending_requests.pop(request_key, None)
        
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
                
                # ULTIMATE FIX FOR BINANCE -1104 / Hedge Mode
                if method == 'create_order':
                    # Extract core params from kwargs (keyword-friendly)
                    symbol = kwargs.get('symbol')
                    order_type = kwargs.get('type')
                    side = kwargs.get('side')
                    amount = kwargs.get('amount')
                    price = kwargs.get('price')
                    
                    # Clean params while preserving important ones like clientOrderId
                    raw_p = kwargs.get('params', {})
                    clean_p = raw_p.copy()
                    
                    # Fix BINANCE -1104 / Position Side Conflict
                    # In Hedge mode, positionSide is used. In One-Way, reduceOnly is used.
                    # Providing both can cause -1104.
                    if 'positionSide' in clean_p and 'reduceOnly' in clean_p:
                        self.logger.debug(f"⚠️ Conflict: Both positionSide and reduceOnly provided. Removing reduceOnly.")
                        del clean_p['reduceOnly']
                    
                    self.logger.debug(f"📤 API create_order: {symbol} {side} {amount} @ {price} | Params: {list(clean_p.keys())}")

                    
                    # Call CCXT with keyword args to prevent positional misalignment
                    try:
                        return func(
                            symbol=symbol,
                            type=order_type,
                            side=side,
                            amount=amount,
                            price=price,
                            params=clean_p
                        )
                    except Exception as e:
                        # Log raw response if available
                        raw_error = getattr(e, 'response', 'No response')
                        self.logger.error(f"❌ API Order Error: {e} | Raw: {raw_error}")
                        
                        # Map to Standard Exceptions
                        err_msg = str(e).lower()
                        if 'insufficient balance' in err_msg or 'account has insufficient' in err_msg:
                            raise InsufficientFundsError(f"Insufficient funds for {symbol} {side}: {e}")
                        elif 'order not found' in err_msg or 'unknown order' in err_msg:
                            raise OrderNotFoundError(f"Order not found: {e}")
                        elif 'network' in err_msg or 'timeout' in err_msg:
                            raise NetworkError(f"Network error: {e}")
                        else:
                            raise APIError(f"Exchange API Error: {e}")
                
                return func(*args, **kwargs)
            
            except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                if attempt < max_retries:
                    self.logger.warning(f"⚠️ Network error on {method} (Attempt {attempt+1}/{max_retries}): {e}")
                    time.sleep(delay * (attempt + 1))
                    continue
                raise NetworkError(f"Persistent Network Error: {e}")

            except (InsufficientFundsError, OrderNotFoundError, APIError) as e:
                # Re-raise standard exceptions immediately (no retry for logic errors)
                raise e
            except Exception as e:
                if "code\":-1104" in str(e) and method == 'create_order':
                    self.logger.error(f"🔥 FINAL ATTEMPT at order: params caused -1104. Retrying with EMPTY params.")
                    return getattr(self.exchange, method)(kwargs.get('symbol'), kwargs.get('type'), kwargs.get('side'), kwargs.get('amount'), kwargs.get('price'), {})
                
                # Map generic CCXT errors to APIError
                raise APIError(f"Generic Exchange Error: {e}")

    # _safe_request was a duplicate of _execute_request using the same logic. 
    # Removed to enforce Single Responsibility Principle.
    # All methods (create_order, etc) should use the specific _coalesced_request or underlying _execute_request.

    # --- ORDER CREATION & VALIDATION (v0.6.4) ---
    def validate_order(self, symbol, side, amount, price):
        """
        Robust validation against exchange limits.
        Ensures types are correct for comparison.
        """
        try:
            self._ensure_markets()
            market = self.exchange.market(symbol)
            limits = market.get('limits', {})
            
            # 1. Cast all inputs to float immediately
            f_amt = float(amount or 0.0)
            f_price = float(price or 0.0)
            
            # 2. Validate Amount
            min_amt = limits.get('amount', {}).get('min')
            if min_amt is not None and f_amt < float(min_amt):
                return False, amount, price, f"Amount {f_amt} < Min {min_amt}"
            
            # 3. Sanitize Precision
            safe_amt = self.exchange.amount_to_precision(symbol, f_amt)
            safe_price = self.exchange.price_to_precision(symbol, f_price) if f_price > 0 else None
            
            # 4. Post-Only Safety
            if side in ['buy', 'sell'] and safe_price:
                tick_size = float(market.get('precision', {}).get('price', 0))
                if tick_size > 0:
                    s_price_f = float(safe_price)
                    if side == 'buy':
                        safe_price = self.exchange.price_to_precision(symbol, s_price_f - tick_size)
                    else:
                        safe_price = self.exchange.price_to_precision(symbol, s_price_f + tick_size)
            
            # 5. Min Notional Check
            if safe_price:
                cost = float(safe_amt) * float(safe_price)
                min_cost = limits.get('cost', {}).get('min')
                if min_cost is not None and cost < float(min_cost):
                    return False, safe_amt, safe_price, f"Cost {cost:.2f} < Min Notional {min_cost}"

            return True, safe_amt, safe_price, ""
        except Exception as e:
            self.logger.error(f"Validation error: {e}")
            return False, amount, price, str(e)

    def create_order(self, symbol, type, side, amount, price=None, params={}, bot_id=None, order_type=None):
        """
        Create an order with optional bot tagging for reconciliation.
        Uses per-symbol locking to prevent race conditions in One-Way mode.
        
        Args:
            bot_id: If provided, tags order with clientOrderId for tracking.
            order_type: Type of order (e.g., 'ENTRY', 'TP', 'GRID') for the tag.
        """
        if params is None: params = {}
        
        # === BOT ORDER TAGGING (Phase 2: clientOrderId) ===
        # Tag format: CQB_{bot_id}_{type}_{short_uuid}
        # This allows us to identify bot orders during reconciliation.
        if bot_id is not None:
            import uuid
            short_uuid = str(uuid.uuid4())[:8]
            o_type_tag = order_type or 'ORDER'
            client_order_id = f"CQB_{bot_id}_{o_type_tag}_{short_uuid}"
            params['clientOrderId'] = client_order_id
            self.logger.debug(f"🏷️ Tagged order with clientOrderId: {client_order_id}")
        
        # Pre-validation
        is_valid, s_amt, s_price, err = self.validate_order(symbol, side, amount, price)
        if not is_valid:
             self.logger.error(f"❌ Validation failed: {err}")
             # Minimal fallback...
             return self._execute_request('create_order', symbol=symbol, type=type, side=side, amount=amount, price=price, params=params)
        
        # === PER-SYMBOL LOCK (Prevents "insufficient balance" race condition) ===
        # In One-Way mode, Binance provisionally locks margin during order processing.
        # Multiple bots on same symbol racing to place orders causes false rejections.
        symbol_lock = get_symbol_lock(symbol)
        
        try:
            with symbol_lock:
                return self._execute_request('create_order', symbol=symbol, type=type, side=side, amount=s_amt, price=s_price, params=params)
        except Exception as e:
            # FIX: Only mock network errors, NOT logic/balance errors
            str_e = str(e).lower()
            critical_errors = ['insufficient balance', 'margin is insufficient', 'insufficient funds', 'account has insufficient']
            if any(crit in str_e for crit in critical_errors):
                 self.logger.error(f"CRITICAL ORDER FAILURE: {e}")
                 raise e
            raise e

    def fetch_balance(self):
        return self._coalesced_request('fetch_balance')

    def fetch_open_orders(self, symbol=None):
        """Fetch open orders. If symbol is None, fetches for ALL symbols."""
        return self._coalesced_request('fetch_open_orders', symbol=symbol)

    def fetch_positions(self, symbols=None):
        """Fetch positions. If symbols is None, fetches for ALL symbols."""
        if self.market_type in ['future', 'swap']:
            return self._coalesced_request('fetch_positions', symbols=symbols)
        return []

    # Standardized aliases
    def get_open_orders(self, symbol=None):
        return self.fetch_open_orders(symbol)

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
        result = self._coalesced_request('fetch_ohlcv', symbol=symbol, timeframe=timeframe, limit=limit)

        # Update cache
        _ohlcv_cache[cache_key] = (current_time, result)
        self.logger.debug(f"✅ OHLCV fetched for {symbol} ({timeframe})")

        return result

    def cancel_all_orders(self, symbol):
        return self._execute_request('cancel_all_orders', symbol=symbol)
    
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

            return self._execute_request('set_leverage', int(leverage), futures_symbol)
        except Exception as e:
            self.logger.error(f"Failed to set leverage {leverage}x for {symbol}: {e}")
            return False

    def calculate_real_leverage(self, symbol: str, position_data: dict = None) -> int:
        """
        Robustly determines the actual effective leverage of a position.
        Handles cases where 'leverage' field is missing (Testnet) by calculating from margin.
        
        Args:
            symbol (str): The trading pair (e.g. BTC/USDT)
            position_data (dict, optional): Existing position data if available.
            
        Returns:
            int: The leverage value (e.g. 20) or None if undetermined.
        """
        try:
            # 1. Get Position Data
            if not position_data:
                positions = self.fetch_positions()
                norm_target = normalize_symbol(symbol)
                for p in positions:
                    if normalize_symbol(p.get('symbol')) == norm_target:
                        position_data = p
                        break
            
            if not position_data:
                return None
                
            # 2. explicit Check
            explicit_lev = position_data.get('leverage')
            if explicit_lev is not None:
                try:
                    return int(float(explicit_lev))
                except: pass
                
            # 3. Calculation Fallback (Notional / Initial Margin)
            # info field contains raw exchange response
            info = position_data.get('info', {})
            notional = float(info.get('notional', 0))
            init_margin = float(info.get('positionInitialMargin', 0))
            
            if init_margin > 0:
                calc_lev = round(abs(notional) / init_margin)
                return int(calc_lev)
                
            return None
            
        except Exception as e:
            self.logger.error(f"Failed to calculate real leverage for {symbol}: {e}")
            return None

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

    # DELETED DUPLICATE validate_order

    def cancel_order(self, order_id, symbol):
        """
        Robust, Synchronous Cancellation.
        Waits for confirmation that order is gone.
        """
        try:
            self._execute_request('cancel_order', order_id, symbol)
            
            # Synchronous Verify (Wait up to 2s)
            for _ in range(4):
                try:
                    check = self._execute_request('fetch_order', order_id, symbol)
                    if check['status'] in ['closed', 'canceled', 'cancelled', 'expired', 'rejected']:
                        return True
                    time.sleep(0.5)
                except Exception as e:
                    if "Order not found" in str(e) or "-2013" in str(e):
                        return True
            return True
        except Exception as e:
            if "Order not found" in str(e) or "-2013" in str(e) or "Unknown order" in str(e):
                return True
            self.logger.error(f"Cancel failed for {order_id}: {e}")
            raise e

    def fetch_order(self, order_id, symbol=None):
        """Fetch a single order by ID."""
        return self._execute_request('fetch_order', order_id, symbol)
        
    def cancel_all_orders(self, symbol):
        """Cancels all open orders for a symbol."""
        return self._execute_request('cancel_all_orders', symbol)

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
                clean_symbol = normalize_symbol(pos_symbol)

                size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                if size != 0:  # Only include open positions
                    positions_map[clean_symbol] = pos

            # Return position for each requested symbol (or None if not found)
            result = {}
            for sym in symbols:
                clean_sym = normalize_symbol(sym)
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
            all_tickers = self._execute_request('fetch_tickers', symbols=symbols)

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

def cleanup_caches():
    """
    Prunes expired entries from module-level caches to prevent memory leaks.
    Should be called periodically (e.g., every 60s) by the runner.
    """
    global _ohlcv_cache, _pending_requests, _generic_cache
    
    now = time.time()
    
    # 1. Cleanup OHLCV Cache
    # Structure: {key: (timestamp, data)}
    count_ohlcv = 0
    keys_to_del = [k for k, v in _ohlcv_cache.items() if now - v[0] > _OHLCV_CACHE_TTL]
    for k in keys_to_del:
        del _ohlcv_cache[k]
        count_ohlcv += 1
        
    # 2. Cleanup Generic Cache
    count_generic = 0
    keys_to_del = [k for k, v in _generic_cache.items() if now - v[0] > _GENERIC_CACHE_TTL]
    for k in keys_to_del:
        del _generic_cache[k]
        count_generic += 1

    # 3. Cleanup Pending Requests (Stuck futures)
    count_pending = 0
    with _request_lock:
        # 60s timeout for any API call is huge, but safe
        keys_to_del = [k for k, v in _pending_requests.items() if now - v[0] > 60] 
        for k in keys_to_del:
            del _pending_requests[k]
            count_pending += 1

    if count_ohlcv > 0 or count_generic > 0 or count_pending > 0:
        pass
        # logging.getLogger("ExchangeInterface").debug(f"🧹 Cache Cleanup: OHLCV={count_ohlcv}, Generic={count_generic}, Pending={count_pending}")
