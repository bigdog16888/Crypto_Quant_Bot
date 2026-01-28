import ccxt
import time
import logging
import os
import sys
import sys
import math
from config.settings import config

class ExchangeInterface:
    def __init__(self, exchange_id='binance', market_type='spot', validate=False):
        self.exchange_id = exchange_id
        self.market_type = market_type
        self.logger = logging.getLogger(__name__)
        
        # 1. Initialize CCXT
        options = {
            'defaultType': market_type,
            'adjustForTimeDifference': True,
        }
        
        self.exchange = getattr(ccxt, exchange_id)({
            'apiKey': config.API_KEY,
            'secret': config.API_SECRET,
            'enableRateLimit': True,
            'options': options,
            'timeout': 30000,
        })
        
        # 2. Enable Demo Trading (Unified environment)
        if config.TESTNET:
            self.logger.warning("🚀 ENABLING BINANCE DEMO TRADING")
            self.exchange.enable_demo_trading(True)
            
        # 3. Load Markets (CRITICAL for validOrderTypes)
        self.markets_loaded = False
        self._ensure_markets()

        if validate:
            self._validate_api_keys()

    def _ensure_markets(self):
        if not self.markets_loaded:
            try:
                self.exchange.load_markets()
                self.markets_loaded = True
                self.logger.info("✅ Markets loaded successfully")
            except Exception as e:
                self.logger.error(f"❌ Failed to load markets: {e}")

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
                 self.logger.error(f"❌ CRITICAL ORDER FAILURE: {e}")
                 raise e # Do not mock this!
            
            # if config.TESTNET:
            #     self.logger.warning(f"⚠️ TESTNET MOCK: Real order failed ({e}). Returning FAKE success.")
            #     # ... MOCK REMOVED ...
            raise e
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
        return self._safe_request('fetch_balance')

    def get_open_orders(self, symbol):
        """Standardized alias for fetch_open_orders"""
        return self.fetch_open_orders(symbol)

    def fetch_open_orders(self, symbol):
        return self._safe_request('fetch_open_orders', symbol=symbol)

    def fetch_positions(self, symbols=None):
        if self.market_type in ['future', 'swap']:
            return self._safe_request('fetch_positions', symbols=symbols)
        return []

    def fetch_ticker(self, symbol):
        return self._safe_request('fetch_ticker', symbol=symbol)

    def fetch_ohlcv(self, symbol, timeframe='1h', limit=100):
        return self._safe_request('fetch_ohlcv', symbol=symbol, timeframe=timeframe, limit=limit)

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
        try: return self._safe_request('set_leverage', int(leverage), symbol)
        except: return False

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
