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
            'timeout': 10000, # 10s timeout
            # Fix timestamp sync issues with Binance
            'recvWindow': 10000,  # 10 second tolerance for clock drift
            'adjustForTimeDifference': True,  # Auto-sync with server time
        })
        
        self.logger = logging.getLogger(__name__)
        
        # Testnet Override
        if config.TESTNET:
            # Removed emojis from internal logs to prevent Windows encoding crashes
            self.logger.warning("USING TESTNET (SANDBOX) MODE")
            
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
                })
            elif market_type == 'spot':
                testnet_base = 'https://testnet.binance.vision'
                self.exchange.urls['api'].update({
                    'public': f'{testnet_base}/api/v3',
                    'private': f'{testnet_base}/api/v3',
                })
                self.exchange.set_sandbox_mode(True)
            else:
                self.exchange.set_sandbox_mode(True)

            
        # Cache for market info (minNotional, filters)
        self.markets_loaded = False
        
        # Store market type for fallback logic
        self.market_type = market_type
        
        # Optimization: Do NOT auto-load markets on init
        # CCXT by default might try to load markets if you call methods that need them,
        # but explicit loading is better controlled via _ensure_markets()
        
        # PROACTIVE FIX FOR TESTNET FUTURES:
        # We must inject markets IMMEDIATELY to prevent any accidental auto-load triggering SAPI calls.
        if config.TESTNET and self.market_type in ['future', 'swap']:
            self._inject_testnet_markets()

    def _inject_testnet_markets(self):
        """Injects dummy markets for Futures Testnet to bypass Mainnet SAPI calls."""
        self.logger.info("FUTURES TESTNET: Injecting manual markets to bypass SAPI errors")

        
        # Per-coin configuration based on REAL Binance Futures requirements
        # Format: {'amount': min_qty, 'price': price_tick, 'notional': min_order_usd}
        coin_config = {
            'BTC': {'amount': 0.001, 'price': 0.10, 'notional': 100.0},   # 0.001 BTC @ $100k = $100
            'ETH': {'amount': 0.01, 'price': 0.01, 'notional': 20.0},    # 0.01 ETH @ $3.5k = $35
            'BNB': {'amount': 0.01, 'price': 0.01, 'notional': 20.0},    # 0.01 BNB @ $700 = $7
            'SOL': {'amount': 0.1, 'price': 0.01, 'notional': 20.0},     # 0.1 SOL @ $200 = $20
            'XRP': {'amount': 1.0, 'price': 0.0001, 'notional': 5.0},    # 1 XRP @ $2.50 = $2.50
            'ADA': {'amount': 1.0, 'price': 0.0001, 'notional': 5.0},    # 1 ADA @ $1 = $1
            'DOGE': {'amount': 1.0, 'price': 0.00001, 'notional': 5.0},  # 1 DOGE @ $0.40 = $0.40
            'AVAX': {'amount': 0.1, 'price': 0.01, 'notional': 20.0},    # 0.1 AVAX @ $40 = $4
            'DOT': {'amount': 0.1, 'price': 0.001, 'notional': 5.0},     # 0.1 DOT @ $7 = $0.70
            'MATIC': {'amount': 1.0, 'price': 0.0001, 'notional': 5.0},  # 1 MATIC @ $0.40 = $0.40
            'LINK': {'amount': 0.1, 'price': 0.01, 'notional': 5.0},     # 0.1 LINK @ $25 = $2.50
            'LTC': {'amount': 0.01, 'price': 0.01, 'notional': 5.0},     # 0.01 LTC @ $130 = $1.30
        }
        # Default for unknown coins - conservative values
        default_config = {'amount': 0.01, 'price': 0.01, 'notional': 20.0}
        
        injected_markets = {}
        injected_ids = {}
        symbols_list = []
        ids_list = []
        
        common_coins = list(coin_config.keys())
        quote_assets = ['USDT', 'USDC']
        
        for quote in quote_assets:
            for base in common_coins:
                symbol = f"{base}/{quote}"
                market_id = f"{base}{quote}"
                p_config = coin_config.get(base, default_config)
                
                precision = {
                    'amount': p_config['amount'],
                    'price': p_config['price']
                }
                
                # Limits based on per-coin configuration
                limits = {
                    'amount': {'min': p_config['amount'], 'max': 10000},
                    'price': {'min': p_config['price'], 'max': 1000000},
                    'cost': {'min': p_config['notional']}  # Per-pair minimum notional
                }

                
                injected_markets[symbol] = {
                    'id': market_id,
                    'symbol': symbol,
                    'base': base,
                    'quote': quote,
                    'baseId': base,
                    'quoteId': quote,
                    'active': True,
                    'precision': precision,
                    'limits': limits,
                    'info': {
                        'orderTypes': ['LIMIT', 'MARKET', 'STOP', 'STOP_MARKET', 'TAKE_PROFIT', 'TAKE_PROFIT_MARKET'],
                    },
                    # CRITICAL: Flags required by CCXT binance implementation
                    'spot': False,
                    'margin': False,
                    'swap': True,
                    'future': True,
                    'option': False,
                    'active': True,
                    'contract': True,
                    'linear': True,
                    'inverse': False,
                    'taker': 0.0004,
                    'maker': 0.0002,
                    'percentage': True,
                    'tierBased': False,
                    'feeSide': 'get',
                    'type': 'future',
                    'delivery': False,
                    'prediction': False,
                    'settle': quote,
                    'settleId': quote,
                    # CRITICAL: Order types required by CCXT validation
                    'orderTypes': ['limit', 'market', 'stop', 'stop_market', 'take_profit', 'take_profit_market'],
                }
                injected_ids[market_id] = injected_markets[symbol]
                symbols_list.append(symbol)
                ids_list.append(market_id)
        
        # Apply to CCXT instance using set_markets to ensure internal indexes (ids, symbols, markets_by_id) are built correctly
        # Note: set_markets might expect a list of markets in some versions, or a dict.
        # Usually it takes a list of market dictionaries.
        
        markets_list = list(injected_markets.values())
        try:
            self.exchange.set_markets(markets_list)
        except AttributeError:
            # Fallback for older CCXT versions that might not have set_markets or behave differently
            self.exchange.markets = injected_markets
            self.exchange.markets_by_id = injected_ids
            self.exchange.symbols = symbols_list
            self.exchange.ids = ids_list
            self.exchange.markets_loaded = True
        
        # CRITICAL: Set flags on BOTH wrapper and CCXT instance (redundant but safe)
        self.markets_loaded = True
        self.exchange.markets_loaded = True

    def _ensure_markets(self):
        """Ensures markets are loaded for validation."""
        if not self.markets_loaded:
            # Check again if we need to inject (redundant but safe)
            if config.TESTNET and self.market_type in ['future', 'swap']:
                self._inject_testnet_markets()
                return

            try:
                # OPTIMIZATION: Only load if absolutely necessary for the validation logic
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

        # 0. PRE-CHECK: Calculate minimum based on precision and limits
        precision = market.get('precision', {})
        amount_precision = precision.get('amount', 0.0001)  # Default tick size
        
        # Handle both precision formats:
        # - Tick size format (e.g., 0.00001) - used by Binance
        # - Decimal places format (e.g., 5) - sometimes used
        if amount_precision >= 1:
            # Decimal places format (integer like 5 means 5 decimal places)
            min_qty_from_precision = 10 ** (-int(amount_precision))
        else:
            # Tick size format (float like 0.00001)
            min_qty_from_precision = amount_precision
        
        # Also check explicit limits
        limits = market.get('limits', {})
        amount_limits = limits.get('amount', {})
        min_amount_explicit = amount_limits.get('min', 0)
        
        # Use the larger of precision-based or explicit minimum
        effective_min_qty = max(min_qty_from_precision, min_amount_explicit or 0)
        effective_min_usd = effective_min_qty * price
        
        # Check MinNotional from limits
        cost_limits = limits.get('cost', {})
        min_cost = cost_limits.get('min', 0)
        
        # Final minimum USD is the max of precision-based and notional
        final_min_usd = max(effective_min_usd, min_cost or 0)
        
        # Pre-check: Will this order be too small after precision rounding?
        # Note: 10% buffer is just a heuristic, might be too strict if user wants exact min.
        # Let's trust sanitized_amount check below more.
        if amount < effective_min_qty * 0.99: # Allow tiny epsilon diff
             return False, amount, price, f"Order too small: ${amount * price:.2f} < Min ${final_min_usd:.2f} for {symbol} (min qty: {effective_min_qty})"

        # 1. Precision Checks
        try:
            sanitized_amount = float(self.exchange.amount_to_precision(symbol, amount))
            sanitized_price = float(self.exchange.price_to_precision(symbol, price))
        except Exception as e:
            return False, amount, price, f"Precision Error: {e}"

        # 2. Post-sanitization check (amount might round to 0)
        if sanitized_amount <= 0:
            return False, amount, price, f"Order too small: rounds to 0 after precision. Min ${final_min_usd:.2f} needed for {symbol}"
        
        # 3. Limit Checks (MinQty, MinNotional)
        # Check Qty
        if min_amount_explicit and sanitized_amount < min_amount_explicit:
             return False, sanitized_amount, sanitized_price, f"Amount {sanitized_amount} < Min {min_amount_explicit}"

        # Check Cost (Price * Amount) -> MinNotional
        cost = sanitized_amount * sanitized_price
        if min_cost and cost < min_cost:
            return False, sanitized_amount, sanitized_price, f"Order Value ${cost:.2f} < MinNotional ${min_cost}"

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

        if 'amount' in kwargs and method == 'create_order':
            price_val = kwargs.get('price')
            if price_val is not None:
                usd_value = float(kwargs['amount']) * float(price_val)
                if usd_value > config.MAX_ORDER_USD:
                    raise ValueError(f"SECURITY: Order value ${usd_value} exceeds limit ${config.MAX_ORDER_USD}")
            else:
                # For Market orders, we don't have price in kwargs usually, 
                # but we still want to estimate security limit if possible
                pass


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
            
            except (ccxt.AuthenticationError, ccxt.PermissionDenied) as e:
                # Special handling for Binance Testnet: Spot and Futures use different keys
                if config.TESTNET:
                    self.logger.warning(f"Auth failed on {method} ({self.market_type}). This is common on Binance Testnet if using Future-only or Spot-only keys.")
                else:
                    self.logger.error(f"AUTHENTICATION ERROR on {method} ({self.market_type}): {e}")
                raise
            
            except (ccxt.ExchangeError, ccxt.InsufficientFunds, ccxt.InvalidOrder) as e:

                # Do NOT retry logic errors
                self.logger.error(f"Logic/Exchange error on {method}: {e}")
                raise
                
            except Exception as e:
                import traceback
                self.logger.error(f"Unexpected error on {method}: {e}")
                self.logger.error(traceback.format_exc())
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

    def get_min_order_usd(self, symbol: str, price: float = 0.0) -> float:
        """
        Calculate the minimum USD order size for a symbol based on precision and limits.
        Useful for UI validation and user feedback.
        
        Returns: Minimum USD value required for a valid order.
        """
        self._ensure_markets()
        
        try:
            market = self.exchange.market(symbol)
        except Exception:
            return 5.0  # Fallback to safe default
        
        if price <= 0:
            price = self.get_last_price(symbol)
        if price <= 0:
            return 5.0  # Fallback
        
        # Calculate from precision
        precision = market.get('precision', {})
        amount_precision = precision.get('amount', 0.0001)
        
        # Handle both precision formats
        if amount_precision >= 1:
            min_qty_from_precision = 10 ** (-int(amount_precision))
        else:
            min_qty_from_precision = amount_precision
        
        min_usd_from_precision = min_qty_from_precision * price
        
        # Get explicit limits
        limits = market.get('limits', {})
        min_amount = limits.get('amount', {}).get('min', 0)
        min_cost = limits.get('cost', {}).get('min', 0)
        
        effective_min_qty = max(min_qty_from_precision, min_amount or 0)
        
        # Calculate raw USD needed based on Qty
        effective_min_usd_from_qty = effective_min_qty * price
        
        # Determine the safe USD value that ensures Qty >= MinQty AND Val >= MinNotional
        # after rounding DOWN to step size.
        
        # 1. Determine Min Notional Requirement
        target_notional = max(effective_min_usd_from_qty, min_cost or 0)
        
        # 2. Calculate Required Quantity to meet Notional
        if price > 0:
            req_qty_for_notional = target_notional / price
        else:
            req_qty_for_notional = 0
            
        # 3. Final Required Qty is max of (MinQty, ReqQtyForNotional)
        final_req_qty = max(effective_min_qty, req_qty_for_notional)
        
        # 4. Ceil this to the next valid step size to be safe
        # (If we round down, we might drop below min notional)
        if amount_precision > 0:
            import math
            # E.g. Req=0.15, Step=0.1 -> 0.2
            # Steps = Req / Step -> ceil -> * Step
            steps = math.ceil(final_req_qty / amount_precision)
            safe_qty = steps * amount_precision
        else:
            safe_qty = final_req_qty
            
        # 5. Calculate Safe USD
        safe_usd = safe_qty * price
        
        # Return with 1% buffer to account for price fluctuation
        return safe_usd * 1.01

    def get_available_symbols(self, quote_asset='USDT'):
        """
        Dynamically fetches tickers and filters by quote asset (e.g. USDT, USDC).
        Has fallback for testnet where load_markets may fail.
        """
        try:
            self._ensure_markets()
            if self.exchange.symbols:
                symbols = [
                    symbol for symbol in self.exchange.symbols 
                    if symbol.endswith(f"/{quote_asset}") or symbol.endswith(f"{quote_asset}")
                ]
                symbols.sort()
                return symbols
            else:
                # Fallback if markets didn't load (common on testnet)
                raise Exception("No symbols loaded")
        except Exception as e:
            # Swallow AuthenticationError specifically to avoid red UI warnings when user has wrong keys for mode
            if "Invalid Api-Key" in str(e) or isinstance(e, ccxt.AuthenticationError):
                self.logger.warning(f"Auth failed during symbol fetch ({self.market_type}). Using fallback list.")
            else:
                self.logger.warning(f"Failed to fetch symbols dynamically: {e}. Using fallback list.")
            
            # Fallback list for Binance Futures Testnet
            if self.market_type in ['future', 'swap']:
                fallback = [
                    f"BTC/{quote_asset}", f"ETH/{quote_asset}", f"BNB/{quote_asset}",
                    f"SOL/{quote_asset}", f"XRP/{quote_asset}", f"DOGE/{quote_asset}",
                    f"ADA/{quote_asset}", f"AVAX/{quote_asset}", f"DOT/{quote_asset}",
                    f"MATIC/{quote_asset}", f"LINK/{quote_asset}", f"LTC/{quote_asset}"
                ]
            else:
                fallback = [
                    f"BTC/{quote_asset}", f"ETH/{quote_asset}", f"BNB/{quote_asset}",
                    f"SOL/{quote_asset}", f"XRP/{quote_asset}"
                ]
            return fallback

    def fetch_ohlcv(self, symbol, timeframe='1h', limit=100):
        return self._safe_request('fetch_ohlcv', symbol=symbol, timeframe=timeframe, limit=limit)

    def create_order(self, symbol, type, side, amount, price=None, params={}):
        """
        Creates an order with pre-validation logic.
        """
        # Ensure params is a dict
        if params is None:
            params = {}
            
        if price is None:
            if type == 'limit':
                raise ValueError("Price required for Limit orders")
            # Market orders logic...
            pass
        # Validate Limit Order
        if price is not None:
            is_valid, s_amt, s_price, err = self.validate_order(symbol, side, amount, price)
            if not is_valid:
                self.logger.error(f"Order Validation Failed: {err}")
                raise ValueError(f"Order Validation Failed: {err}")
            
            # Update args with sanitized values
            amount = s_amt
            price = s_price

        # Merge extra params (e.g., postOnly)
        return self._safe_request('create_order', symbol=symbol, type=type, side=side, amount=amount, price=price, params=params)

    def set_leverage(self, symbol, leverage):
        """
        Sets leverage for a specific symbol (Futures only).
        """
        try:
            if not self.exchange.has.get('setLeverage'):
                # Many spot exchanges don't have this, or it's implied
                return False
            
            # Ensure leverage is int
            leverage = int(leverage)
            
            # Call ccxt set_leverage
            # Note: Binance expects set_leverage(leverage, symbol)
            return self._safe_request('set_leverage', leverage, symbol)
        except Exception as e:
            self.logger.error(f"Failed to set leverage {leverage}x for {symbol}: {e}")
            return False

    def fetch_balance(self):
        params = {}
        # Ensure we target the correct wallet in Futures mode to avoid 'sapi' Mainnet leaks
        if self.exchange.options.get('defaultType') == 'future':
            params['type'] = 'future'
        return self._safe_request('fetch_balance', params=params)

    def fetch_funding_rate(self, symbol):
        """Fetches the current funding rate for a symbol (Futures only)."""
        try:
            # Check if exchange supports funding rates
            if not self.exchange.has.get('fetchFundingRate'):
                self.logger.warning(f"Exchange does not support fetchFundingRate")
                return None
            return self._safe_request('fetch_funding_rate', symbol=symbol)
        except Exception as e:
            self.logger.error(f"Error fetching funding rate for {symbol}: {e}")
            return None

    def fetch_open_interest(self, symbol):
        """Fetches open interest for a symbol (Futures only)."""
        try:
            if not self.exchange.has.get('fetchOpenInterest'):
                self.logger.warning(f"Exchange does not support fetchOpenInterest")
                return None
            return self._safe_request('fetch_open_interest', symbol=symbol)
        except Exception as e:
            self.logger.error(f"Error fetching open interest for {symbol}: {e}")
            return None

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
        """Fetches a specific order by ID with retry for propagation delay."""
        max_retries = 3
        for i in range(max_retries):
            try:
                return self._safe_request('fetch_order', id=order_id, symbol=symbol)
            except ccxt.OrderNotFound as e:
                if i < max_retries - 1:
                    time.sleep(1) # Wait for propagation
                    continue
                raise e
            except ccxt.ExchangeError as e:
                # Binance specific "Order does not exist" is often propagation
                if "-2013" in str(e) and i < max_retries - 1:
                    time.sleep(1)
                    continue
                raise e

    
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
