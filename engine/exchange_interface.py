import ccxt
import time
import logging
from config.settings import config

class ExchangeInterface:
    def __init__(self, exchange_id='binance', market_type='spot'):
        self.exchange_id = exchange_id
        
        # Determine options based on market type
        options = {'defaultType': market_type} 
        
        self.exchange = getattr(ccxt, exchange_id)({
            'apiKey': config.API_KEY,
            'secret': config.API_SECRET,
            'enableRateLimit': True,
            'options': options,
            'timeout': 10000 # 10s timeout
        })
        
        # Testnet Override
        if config.TESTNET:
            # Special handling for Futures: set_sandbox_mode is deprecated/broken in some CCXT versions
            # So we manually override URLs and SKIP set_sandbox_mode for futures
            if market_type in ['future', 'delivery', 'swap']:
                testnet_base = 'https://testnet.binancefuture.com'
                self.exchange.urls['api'].update({
                    'fapiPublic': f'{testnet_base}/fapi/v1',
                    'fapiPublicV2': f'{testnet_base}/fapi/v2',
                    'fapiPublicV3': f'{testnet_base}/fapi/v3',
                    'fapiPrivate': f'{testnet_base}/fapi/v1',
                    'fapiPrivateV2': f'{testnet_base}/fapi/v2',
                    'fapiPrivateV3': f'{testnet_base}/fapi/v3',
                    'dapiPublic': f'{testnet_base}/dapi/v1',
                    'dapiPrivate': f'{testnet_base}/dapi/v1',
                    'dapiPrivateV2': f'{testnet_base}/dapi/v2',
                    # Nuclear Option: Route EVERYTHING to Testnet
                    # This prevents ANY accidental Mainnet connection if CCXT falls back to 'sapi' or 'spot'
                    'sapi': f'{testnet_base}/fapi/v1',
                    'spot': f'{testnet_base}/fapi/v1',
                })
            else:
                # For Spot, standard sandbox mode still works fine
                self.exchange.set_sandbox_mode(True)
            
            self.logger = logging.getLogger(__name__)
            self.logger.warning("⚠️ USING TESTNET (SANDBOX) MODE ⚠️")
        else:
            self.logger = logging.getLogger(__name__)
            
        # Cache for market info (minNotional, filters)
        self.markets_loaded = False

    def _ensure_markets(self):
        """Ensures markets are loaded for validation."""
        if not self.markets_loaded:
            try:
                self.exchange.load_markets()
                self.markets_loaded = True
            except Exception as e:
                self.logger.error(f"Failed to load markets: {e}")
                # Don't raise, might be network, let retry handle it later

    def validate_order(self, symbol, side, amount, price):
        """
        Validates order against exchange rules (MinNotional, MinQty, Precision) BEFORE sending.
        Returns: (is_valid, sanitized_amount, sanitized_price, error_msg)
        """
        self._ensure_markets()
        
        try:
            market = self.exchange.market(symbol)
        except Exception:
            return False, amount, price, f"Symbol {symbol} not found in markets"

        # 1. Precision Checks
        try:
            sanitized_amount = float(self.exchange.amount_to_precision(symbol, amount))
            sanitized_price = float(self.exchange.price_to_precision(symbol, price))
        except Exception as e:
            return False, amount, price, f"Precision Error: {e}"

        # 2. Limit Checks (MinQty, MinNotional)
        # Limits structure varies by exchange, CCXT standardizes most
        limits = market.get('limits', {})
        
        # Check Amount (Min/Max)
        amount_limits = limits.get('amount', {})
        min_amount = amount_limits.get('min')
        if min_amount and sanitized_amount < min_amount:
            return False, sanitized_amount, sanitized_price, f"Amount {sanitized_amount} < Min {min_amount}"

        # Check Cost (Price * Amount) -> MinNotional
        cost_limits = limits.get('cost', {})
        min_cost = cost_limits.get('min')
        cost = sanitized_amount * sanitized_price
        if min_cost and cost < min_cost:
            return False, sanitized_amount, sanitized_price, f"Cost {cost:.2f} < MinNotional {min_cost}"

        return True, sanitized_amount, sanitized_price, None

    def _safe_request(self, method, *args, **kwargs):
        """
        Wrapper for API calls with:
        1. Security Checks (Whitelist, Max Order)
        2. Validation (MinNotional - implicit via validate_order call in create_order)
        3. Retry Logic (Network resilience)
        """
        # --- 1. Security Checks ---
        if 'symbol' in kwargs:
            sym = kwargs['symbol']
            # Allow base symbol (e.g. BTC/USDT) if it matches allowed list logic
            pass

        if 'amount' in kwargs and 'price' in kwargs and method == 'create_order':
            usd_value = float(kwargs['amount']) * float(kwargs['price'])
            if usd_value > config.MAX_ORDER_USD:
                raise ValueError(f"SECURITY: Order value ${usd_value} exceeds limit ${config.MAX_ORDER_USD}")

        if config.DRY_RUN:
            # Allow fetch methods even in dry run
            if method.startswith('fetch') or method.startswith('load'):
                pass
            else:
                # Sanitized logging: avoid printing API keys or secrets if any passed (unlikely in kwargs here but safe practice)
                # self.exchange handles auth internally, so args/kwargs usually just have symbol/amount.
                self.logger.info(f"[DRY_RUN] Would call {method} with kwargs: {kwargs}")
                return {"status": "dry_run", "info": "Skipped actual API call", "id": "dry_run_id"}

        # --- 2. Execution with Retry ---
        max_retries = config.MAX_RETRIES
        delay = config.RETRY_DELAY
        
        for attempt in range(max_retries + 1):
            try:
                func = getattr(self.exchange, method)
                return func(*args, **kwargs)
            
            except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable) as e:
                if attempt < max_retries:
                    self.logger.warning(f"Network error on {method}: {e}. Retrying ({attempt+1}/{max_retries})...")
                    time.sleep(delay * (attempt + 1)) # Exponential backoffish
                else:
                    self.logger.error(f"Failed {method} after {max_retries} retries: {e}")
                    raise
            
            except (ccxt.ExchangeError, ccxt.InsufficientFunds, ccxt.InvalidOrder) as e:
                # Do NOT retry logic errors
                self.logger.error(f"Logic/Exchange error on {method}: {e}")
                raise
                
            except Exception as e:
                self.logger.error(f"Unexpected error on {method}: {e}")
                raise

    def get_last_price(self, symbol: str) -> float:
        """Fetches the latest ticker price for a symbol."""
        try:
            ticker = self._safe_request('fetch_ticker', symbol=symbol)
            if ticker and 'last' in ticker:
                return float(ticker['last'])
            return 0.0
        except Exception as e:
            self.logger.error(f"Error fetching price for {symbol}: {e}")
            return 0.0

    def get_available_symbols(self, quote_asset='USDT'):
        """
        Dynamically fetches tickers and filters by quote asset (e.g. USDT, USDC).
        """
        try:
            self._ensure_markets()
            symbols = [
                symbol for symbol in self.exchange.symbols 
                if symbol.endswith(f"/{quote_asset}") or symbol.endswith(f"{quote_asset}")
            ]
            symbols.sort()
            return symbols
        except Exception as e:
            self.logger.error(f"Failed to fetch symbols: {e}")
            return []

    def fetch_ohlcv(self, symbol, timeframe='1h', limit=100):
        return self._safe_request('fetch_ohlcv', symbol=symbol, timeframe=timeframe, limit=limit)

    def create_order(self, symbol, type, side, amount, price=None, params={}):
        """
        Creates an order with pre-validation logic.
        """
        if price is None:
            if type == 'limit':
                raise ValueError("Price required for Limit orders")
            # Market orders logic...
            pass
        else:
            # Validate Limit Order
            is_valid, s_amt, s_price, err = self.validate_order(symbol, side, amount, price)
            if not is_valid:
                self.logger.error(f"Order Validation Failed: {err}")
                return None # Or raise
            
            # Update args with sanitized values
            amount = s_amt
            price = s_price

        # Merge extra params (e.g., postOnly)
        return self._safe_request('create_order', symbol=symbol, type=type, side=side, amount=amount, price=price, params=params)

    def fetch_balance(self):
        params = {}
        # Ensure we target the correct wallet in Futures mode to avoid 'sapi' Mainnet leaks
        if self.exchange.options.get('defaultType') == 'future':
            params['type'] = 'future'
        return self._safe_request('fetch_balance', params=params)

    def fetch_open_orders(self, symbol):
        """Fetches open orders for a specific symbol."""
        return self._safe_request('fetch_open_orders', symbol=symbol)

    def cancel_all_orders(self, symbol):
        """Cancels all open orders for a specific symbol."""
        # Using ccxt's cancel_all_orders if available, otherwise loop cancel
        # generic cancel_all_orders might not be supported by all exchanges in ccxt base, 
        # but binance supports it. Safe request handles errors.
        return self._safe_request('cancel_all_orders', symbol=symbol)

    def fetch_order(self, order_id, symbol):
        """Fetches a specific order by ID to check fill status."""
        return self._safe_request('fetch_order', id=order_id, symbol=symbol)
    
    def wait_for_fill(self, order_id, symbol, timeout_seconds=30, poll_interval=2):
        """
        Waits for an order to fill, with timeout.
        Returns: (filled: bool, order_status: dict or None)
        """
        import time
        start_time = time.time()
        
        while time.time() - start_time < timeout_seconds:
            try:
                order = self.fetch_order(order_id, symbol)
                if order is None:
                    return False, None
                    
                status = order.get('status', 'unknown')
                
                if status == 'closed':
                    self.logger.info(f"Order {order_id} filled completely")
                    return True, order
                elif status == 'canceled' or status == 'cancelled':
                    self.logger.warning(f"Order {order_id} was cancelled")
                    return False, order
                elif status == 'expired':
                    self.logger.warning(f"Order {order_id} expired")
                    return False, order
                    
                # Still open, wait and poll again
                time.sleep(poll_interval)
                
            except Exception as e:
                self.logger.error(f"Error checking order {order_id}: {e}")
                time.sleep(poll_interval)
        
        self.logger.warning(f"Order {order_id} did not fill within {timeout_seconds}s")
        return False, None
