import ccxt
import time
import math
import logging
import threading
import requests
import hmac
import hashlib
from urllib.parse import urlencode
from typing import Dict, List, Optional, Any, Tuple
from engine.exceptions import APIError, NetworkError
from config.settings import config

logger = logging.getLogger("ExchangeInterface")

class ExchangeInterface:
    """
    Standardized interface for USDC/USDT Futures trading.
    FUNDAMENTAL FIX: Uses proven raw API signing for Demo environment to bypass CCXT bugs.
    """
    _anon_exchange = None
    _anon_lock = threading.Lock()
    # Class-level cache: normalized symbol -> {step_size, tick_size, qty_precision, price_precision}
    _exchange_info_cache: Dict[str, Any] = {}
    _exchange_info_loaded = False
    _exchange_info_lock = threading.Lock()
    _hybrid_mode_logged = False


    def __init__(self, market_type='future'):
        self.logger = logging.getLogger(f"ExchangeInterface.{market_type}")
        self.market_type = normalize_market_type(market_type)  # Canonical gate: 'futures'→'future'
        self.exchange = self._create_exchange_instance()
        self._ensure_markets()

    @classmethod
    def _get_anon_exchange(cls):
        """Returns a shared anonymous exchange instance to prevent memory leaks."""
        if cls._anon_exchange is None:
            with cls._anon_lock:
                if cls._anon_exchange is None:
                    cls._anon_exchange = ccxt.binance({
                        'enableRateLimit': True,
                        'options': {'defaultType': 'future', 'adjustForTimeDifference': True}
                    })
                    if config.TESTNET or config.DEMO_TRADING:
                        cls._anon_exchange.urls['api']['fapiPublic'] = 'https://demo-fapi.binance.com/fapi/v1'
        return cls._anon_exchange

    def _create_exchange_instance(self):
        # We use CCXT for public data and order placement
        exchange = ccxt.binance({
            'apiKey': config.API_KEY,
            'secret': config.API_SECRET,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True,
                'recvWindow': 10000,
            }
        })
        
        if config.TESTNET or config.DEMO_TRADING:
            # Point CCXT to the demo server for public calls
            base_url = 'https://demo-fapi.binance.com'
            if not hasattr(exchange, 'urls'): exchange.urls = {'api': {}}
            exchange.urls['api']['fapiPublic'] = f"{base_url}/fapi/v1"
            exchange.urls['api']['fapi'] = base_url
            if not ExchangeInterface._hybrid_mode_logged:
                self.logger.warning(f"🛡️ HYBRID RAW MODE ACTIVE (Demo FAPI)")
                ExchangeInterface._hybrid_mode_logged = True
            
        return exchange

    def _ensure_markets(self):
        """Ensures markets are loaded. Re-tries anonymously if keys cause issues."""
        if self.exchange and self.exchange.markets:
            return
            
        try:
            self.exchange.load_markets()
        except Exception as e:
            self.logger.debug(f"Market load with keys failed (likely Testnet/Demo context). falling back to anonymous mode...")
            try:
                # Use shared anon instance
                anon = self._get_anon_exchange()
                self.exchange.markets = anon.load_markets()
                self.exchange.markets_by_id = anon.markets_by_id
            except Exception as e2:
                self.logger.error(f"Critical: Anonymous market load also failed: {e2}")

    @classmethod
    def _fetch_exchange_info(cls):
        """
        Fetches LOT_SIZE and PRICE_FILTER for EVERY symbol from the real exchange endpoint.
        Queries Demo FAPI on demo mode, Mainnet FAPI otherwise.
        Cached at the class level—only runs once per process lifetime.
        """
        if cls._exchange_info_loaded:
            return
        with cls._exchange_info_lock:
            if cls._exchange_info_loaded:  # double-check inside lock
                return
            try:
                if config.TESTNET or config.DEMO_TRADING:
                    url = 'https://demo-fapi.binance.com/fapi/v1/exchangeInfo'
                else:
                    url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                for sym_info in data.get('symbols', []):
                    raw_sym = sym_info.get('symbol', '')  # e.g. BNBUSDC
                    step_size = 0.001
                    tick_size = 0.01
                    min_notional = 5.0  # Binance FAPI default fallback
                    for f in sym_info.get('filters', []):
                        if f['filterType'] == 'LOT_SIZE':
                            step_size = float(f['stepSize'])
                        elif f['filterType'] == 'PRICE_FILTER':
                            tick_size = float(f['tickSize'])
                        elif f['filterType'] == 'MIN_NOTIONAL':
                            min_notional = float(f.get('notional', f.get('minNotional', 5.0)))
                    qty_precision = (int(-math.log10(step_size)) if 0 < step_size < 1 else 0)
                    price_precision = (int(-math.log10(tick_size)) if 0 < tick_size < 1 else 0)
                    cls._exchange_info_cache[raw_sym] = {
                        'step_size': step_size,
                        'tick_size': tick_size,
                        'qty_precision': qty_precision,
                        'price_precision': price_precision,
                        'min_notional': min_notional,
                    }
                cls._exchange_info_loaded = True
                logger.info(f"📐 Exchange precision cache loaded for {len(cls._exchange_info_cache)} symbols from {'Demo' if config.DEMO_TRADING or config.TESTNET else 'Mainnet'} FAPI.")
            except Exception as e:
                logger.error(f"⚠️ Could not load exchangeInfo precision cache: {e}")

    def _raw_request(self, endpoint: str, method: str = 'GET', params: dict = None) -> Any:
        """
        Executes a raw signed request to the Binance Demo FAPI.
        Correctly handles GET (query params) and POST (body params).
        """
        base_url = "https://demo-fapi.binance.com"
        query_dict = params.copy() if params else {}
        query_dict['timestamp'] = int(time.time() * 1000)
        query_dict['recvWindow'] = 60000 # Max allowed by Binance to tolerate time drift
        
        # --- FUNDAMENTAL FIX: DETERMINISTIC SIGNING ---
        # 1. Sort keys lexicographically
        # 2. Format floats consistently to avoid signature mismatches
        sorted_keys = sorted(query_dict.keys())
        query_parts = []
        for key in sorted_keys:
            val = query_dict[key]
            if isinstance(val, float):
                # Format to 8 decimal places and remove trailing zeros
                val = format(val, '.8f').rstrip('0').rstrip('.')
            query_parts.append(f"{key}={val}")
        
        query_string = "&".join(query_parts)
        
        signature = hmac.new(
            config.API_SECRET.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        headers = {
            "X-MBX-APIKEY": config.API_KEY,
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        try:
            if method.upper() == 'GET' or method.upper() == 'DELETE':
                url = f"{base_url}{endpoint}?{query_string}&signature={signature}"
                response = requests.request(method, url, headers=headers, timeout=10)
            else:
                # For POST/PUT, parameters and signature must be in the body
                body = f"{query_string}&signature={signature}"
                url = f"{base_url}{endpoint}"
                self.logger.debug(f"🚀 RAW POST: {url} | Body: {body}")
                response = requests.request(method, url, headers=headers, data=body, timeout=10)

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 400 and 'Unknown order' in response.text:
                # Expected: order was already filled/cancelled on exchange
                self.logger.debug(f"Cancel-order 400 (order already gone): {response.text}")
                return None
            else:
                self.logger.error(f"Raw API Error {response.status_code}: {response.text}")
                # 🚀 FUNDAMENTAL FIX: Bubble up the actual Binance Error so we don't mask it
                try:
                    err_json = response.json()
                    error_msg = err_json.get('msg', response.text)
                except:
                    error_msg = response.text
                raise APIError(f"Binance API {response.status_code}: {error_msg}")
        except APIError:
            raise
        except Exception as e:
            self.logger.error(f"Raw Request Failed ({endpoint}): {e}")
            raise APIError(f"Request Error: {str(e)}")

    def get_last_price(self, symbol: str) -> Optional[float]:
        try:
            # FIXED: Use shared anon instance to prevent memory leaks
            anon = self._get_anon_exchange()
            ticker = anon.fetch_ticker(symbol)
            return float(ticker['last'])
        except Exception as e:
            self.logger.error(f"Price Error for {symbol}: {e}")
            return None

    def fetch_ohlcv(self, symbol: str, timeframe: str = '1m', limit: int = 50) -> List:
        try:
            # FIXED: Use shared anon instance
            anon = self._get_anon_exchange()
            return anon.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as e:
            err_msg = str(e)
            # If it's a JSON decoding error (truncated response), log it as a warning instead of error to reduce alarm
            if "Expecting" in err_msg or "JSON" in err_msg:
                self.logger.warning(f"⚠️ OHLCV JSON Error for {symbol} (intermittent): {err_msg[:60]}...")
            else:
                self.logger.error(f"OHLCV Error for {symbol}: {e}")
            return []
    def get_available_symbols(self, quote_asset: str = 'USDT') -> List[str]:
        """Returns a list of available symbols for the given quote asset."""
        try:
            self._ensure_markets()
            if not self.exchange or not self.exchange.markets:
                return []
            
            symbols = []
            is_futures = self.market_type == 'future'
            
            for symbol in self.exchange.markets:
                market = self.exchange.markets[symbol]
                if not market.get('active', True):
                    continue
                
                # Futures symbols use the format BASE/QUOTE:QUOTE (e.g. BNB/USDC:USDC)
                # Spot symbols use BASE/QUOTE (e.g. BNB/USDC)
                has_settle_suffix = ':' in symbol
                
                if is_futures:
                    # Only include futures contracts (BASE/QUOTE:SETTLE)
                    if has_settle_suffix and f"/{quote_asset}:" in symbol:
                        symbols.append(symbol)
                else:
                    # Only include spot pairs (BASE/QUOTE, no colon)
                    if not has_settle_suffix and symbol.endswith(f"/{quote_asset}"):
                        symbols.append(symbol)
                        
            return sorted(symbols)
        except Exception as e:
            self.logger.error(f"Error fetching symbols: {e}")
            return None # Return None to signal error, distinct from empty list []

    def fetch_positions(self) -> List[dict]:
        try:
            # Use PROVEN raw logic for private account data
            res = self._raw_request('/fapi/v2/account')
            
            if res is None: return None # 🚀 FIXED: Request failed, return None
            
            positions = []
            if 'positions' in res:
                # Ensure markets are loaded for symbol unification
                self._ensure_markets()
                
                for pos in res['positions']:
                    if float(pos.get('positionAmt', 0)) != 0:
                        raw_symbol = pos['symbol']
                        unified_symbol = raw_symbol
                        
                        # Try to map raw symbol (BTCUSDT) to Unified (BTC/USDT)
                        if self.exchange.markets_by_id and raw_symbol in self.exchange.markets_by_id:
                            entry = self.exchange.markets_by_id[raw_symbol]
                            if isinstance(entry, list):
                                unified_symbol = entry[0]['symbol']
                            else:
                                unified_symbol = entry['symbol']
                        
                        positions.append({
                            'symbol': unified_symbol,
                            'contracts': float(pos['positionAmt']),
                            'side': str(pos.get('positionSide', 'BOTH')).lower(), # Normalized: long, short, both
                            'unrealizedPnl': float(pos['unrealizedProfit']),
                            'entryPrice': float(pos['entryPrice'])
                        })
            return positions
        except Exception as e:
            self.logger.error(f"Positions Fetch Error: {e}")
            return None # 🚀 FIXED: Return None to indicate failure, not "empty"

    def fetch_balance(self) -> Dict[str, Any]:
        try:
            # Use PROVEN raw logic for private balance data
            res = self._raw_request('/fapi/v2/balance')
            balance = {'total': {}}
            if res:
                for item in res:
                    asset = item['asset']
                    balance['total'][asset] = float(item['balance'])
            return balance
        except Exception as e:
            self.logger.error(f"Balance Fetch Error: {e}")
            return {}

    def fetch_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        try:
            # Fallback to raw for Demo because CCXT might hit -2008
            endpoint = '/fapi/v1/openOrders'
            params = {'symbol': normalize_symbol(symbol)} if symbol else {}
            res = self._raw_request(endpoint, params=params)
            orders = []
            if res:
                for o in res:
                    orders.append({
                        'id': o['orderId'],
                        'symbol': o['symbol'],
                        'side': o['side'].lower(),
                        'price': float(o['price']),
                        'amount': float(o['origQty']),
                        'clientOrderId': o['clientOrderId'],
                        'status': o['status'].lower(),
                        'type': o['type'].lower(),
                        'timestamp': o['time']
                    })
            return orders
        except Exception as e:
            self.logger.error(f"Orders Fetch Error: {e}")
            return []

    def fetch_closed_orders(self, symbol: str, since: Optional[int] = None, limit: int = 50) -> List[dict]:
        """
        Fetches closed/filled orders. 
        Crucial for State Reconstruction (Offline Fills).
        """
        try:
            if config.TESTNET or config.DEMO_TRADING:
                endpoint = '/fapi/v1/allOrders'
                params = {
                    'symbol': normalize_symbol(symbol),
                    'limit': limit
                }
                if since: params['startTime'] = since
                
                res = self._raw_request(endpoint, params=params)
                orders = []
                if res:
                    for o in res:
                        status = o['status'].lower()
                        # We only care about terminal states for "closed" orders
                        if status in ['filled', 'canceled', 'expired', 'rejected']:
                            orders.append({
                                'id': o['orderId'],
                                'symbol': o['symbol'],
                                'side': o['side'].lower(),
                                'price': float(o['avgPrice'] if float(o.get('avgPrice', 0)) > 0 else o['price']),
                                'amount': float(o['executedQty']), # Use executedQty for fills
                                'clientOrderId': o['clientOrderId'],
                                'status': status,
                                'type': o['type'].lower(),
                                'timestamp': o['time'],
                                'average': float(o.get('avgPrice', 0))
                            })
                return orders
            
            # Mainnet fallback
            return self.exchange.fetch_closed_orders(symbol, since=since, limit=limit)
        except Exception as e:
            self.logger.error(f"Closed Orders Fetch Error for {symbol}: {e}")
            return []

    def fetch_my_trades(self, symbol: str, since: Optional[int] = None, limit: int = 50) -> List[dict]:
        """
        Fetches specific fill details (trades).
        Crucial for forensic proof of position ownership.
        """
        try:
            if config.TESTNET or config.DEMO_TRADING:
                endpoint = '/fapi/v1/userTrades'
                params = {
                    'symbol': normalize_symbol(symbol),
                    'limit': limit
                }
                if since: params['startTime'] = since
                
                res = self._raw_request(endpoint, params=params)
                trades = []
                if res:
                    for t in res:
                        trades.append({
                            'id': t['id'],
                            'order': str(t['orderId']),   # CCXT-normalised key used by reconciler
                            'orderId': t['orderId'],       # raw key, kept for backward compat
                            'symbol': t['symbol'],
                            'side': t['side'].lower(),
                            'price': float(t['price']),
                            'amount': float(t['qty']),
                            'cost': float(t['quoteQty']),
                            'commission': float(t.get('commission', 0)),
                            # clientOrderId is NOT available on /fapi/v1/userTrades responses.
                            # Reconciler PASS 2 must look up CID from bot_orders by order_id.
                            'clientOrderId': '',
                            'timestamp': t['time']
                        })
                return trades
            
            # Mainnet fallback
            return self.exchange.fetch_my_trades(symbol, since=since, limit=limit)
        except Exception as e:
            if "2015" in str(e):
                self.logger.error(f"🛡️ GEOFENCED/RESTRICTED: API permission denied for {symbol} trades. Forensic proof will be skipped.")
                raise PermissionError(f"API Permission denied for {symbol}")
            self.logger.error(f"Trades Fetch Error for {symbol}: {e}")
            return []

    def get_symbol_precision(self, symbol: str) -> Dict[str, Any]:
        """
        Returns precision metadata for an exchange symbol.
        FUNDAMENTAL: Fetches real step sizes from the actual Demo or Mainnet exchangeInfo
        endpoint (cached). Works universally for ALL pairs without hardcoded overrides.
        """
        # Ensure the cache is populated from the real exchange endpoint
        self._fetch_exchange_info()

        # Normalize to the raw Binance symbol format (e.g., BTC/USDC:USDC -> BTCUSDC)
        norm = normalize_symbol(symbol)

        if norm in self._exchange_info_cache:
            return self._exchange_info_cache[norm]

        # Fallback: parse from CCXT markets if cache somehow missed this symbol
        self.logger.warning(f"⚠️ [{norm}] not in precision cache — falling back to CCXT markets.")
        try:
            self._ensure_markets()
            m_key = symbol if symbol in self.exchange.markets else symbol.replace('/', '')
            market = self.exchange.markets.get(m_key, {})
            step_size = 0.001
            tick_size = 0.01
            for f in market.get('info', {}).get('filters', []):
                if f['filterType'] == 'LOT_SIZE':
                    step_size = float(f['stepSize'])
                elif f['filterType'] == 'PRICE_FILTER':
                    tick_size = float(f['tickSize'])
            qty_precision = int(-math.log10(step_size)) if step_size < 1 else 0
            price_precision = int(-math.log10(tick_size)) if tick_size < 1 else 0
            return {'qty_precision': qty_precision, 'price_precision': price_precision,
                    'step_size': step_size, 'tick_size': tick_size}
        except Exception as e:
            self.logger.error(f"Error fetching precision for {symbol}: {e}")
            return {'qty_precision': 3, 'price_precision': 2, 'step_size': 0.001, 'tick_size': 0.01}

    @staticmethod
    def round_to_step(value: float, step: float) -> float:
        """Rounds a value DOWN to the nearest exchange step size (e.g., 0.05). Use for normal quantities."""
        if not step or step <= 0: return value
        import math
        if step < 1:
            precision = int(-math.log10(step))
        else:
            precision = 0
        return round(math.floor(value / step) * step, precision)

    @staticmethod
    def ceil_to_step(value: float, step: float) -> float:
        """Rounds a value UP to the nearest exchange step size. Use when scaling UP to meet minimum notional."""
        if not step or step <= 0: return value
        import math
        if step < 1:
            precision = int(-math.log10(step))
        else:
            precision = 0
        return round(math.ceil(value / step) * step, precision)

    def create_order(self, symbol, type, side, amount, price=None, params=None) -> dict:
        try:
            # Use raw signature logic for order placement on Demo
            if config.TESTNET or config.DEMO_TRADING:
                endpoint = '/fapi/v1/order'
                
                # 🚀 FIXED: DYNAMIC PRECISION CALCULATION
                prec = self.get_symbol_precision(symbol)
                
                # 🚀 FUNDAMENTAL FIX: Precision Rounding
                # Binance rejects if we don't round to the exact precision
                amount_rounded = self.round_to_step(amount, prec['step_size'])
                qty_str = "{:.{}f}".format(amount_rounded, prec['qty_precision'])
                
                raw_params = {
                    'symbol': normalize_symbol(symbol),
                    'side': side.upper(),
                    'type': type.upper(),
                    'quantity': qty_str,
                }
                
                if price:
                    price_rounded = self.round_to_step(price, prec['tick_size'])
                    price_str = "{:.{}f}".format(price_rounded, prec['price_precision'])
                    raw_params['price'] = price_str
                    raw_params['timeInForce'] = 'GTC'
                
                # Merge extra params (like clientOrderId)
                if params:
                    # Rename CCXT-style keys to Binance-style keys
                    if 'clientOrderId' in params:
                        raw_params['newClientOrderId'] = params['clientOrderId']
                    for k, v in params.items():
                        if k not in ['clientOrderId', 'reduceOnly']:
                            raw_params[k] = v
                    if 'reduceOnly' in params:
                        raw_params['reduceOnly'] = 'true' if params['reduceOnly'] else 'false'

                res = self._raw_request(endpoint, method='POST', params=raw_params)
                if res:
                    # Return CCXT-like structure
                    return {
                        'id': res.get('orderId'),
                        'symbol': symbol,
                        'status': res.get('status', 'open').lower(),
                        'clientOrderId': res.get('clientOrderId')
                    }
                else:
                    raise Exception("Raw order placement returned empty response")
            
            # Fallback for Mainnet
            return self.exchange.create_order(symbol, type, side, amount, price, params or {})
        except Exception as e:
            self.logger.error(f"Order Placement Failed: {e}")
            raise APIError(str(e))

    def fetch_order(self, order_id: str, symbol: str):
        try:
            if config.TESTNET or config.DEMO_TRADING:
                 endpoint = '/fapi/v1/order'
                 params = {'symbol': normalize_symbol(symbol)}
                 if str(order_id).isdigit():
                     params['orderId'] = order_id
                 else:
                     params['origClientOrderId'] = order_id
                     
                 res = self._raw_request(endpoint, params=params)
                 if res:
                     return {'id': res.get('orderId', order_id), 'status': res.get('status', 'unknown').lower(), 'filled': float(res.get('executedQty', 0)), 'amount': float(res.get('origQty', 0))}
                 return None
                 
            if str(order_id).isdigit():
                return self.exchange.fetch_order(order_id, symbol)
            else:
                return self.exchange.fetch_order(order_id, symbol, params={'origClientOrderId': order_id})
        except Exception as e:
            self.logger.error(f"Fetch Order Error for {order_id} (Symbol: {symbol}): {e}")
            raise e

    def validate_order(self, symbol: str, side: str, amount: float, price: Optional[float] = None, is_closing: bool = False):
        """Standardizes order validation before sending to exchange."""
        try:
            # Ensure markets are loaded before validation
            self._ensure_markets()
            
            if not self.exchange or not self.exchange.markets:
                return False, amount, price, "Exchange markets not loaded"
                
            # Get correct symbol
            m_key = symbol if symbol in self.exchange.markets else symbol.replace('/', '')
            if m_key not in self.exchange.markets:
                # Try normalized comparison
                norm_symbol = normalize_symbol(symbol)
                found = False
                for s in self.exchange.markets:
                    if normalize_symbol(s) == norm_symbol:
                        symbol = s # Update to exact exchange symbol
                        found = True
                        break
                if not found:
                    return False, amount, price, f"Symbol {symbol} not found in markets"
            
            market = self.exchange.markets[m_key]
            
            # Min Notional Auto-Adjustment
            # 🚀 FUNDAMENTAL FIX: Use the real per-symbol value from the exchange info cache
            # (already populated from Binance fapi/v1/exchangeInfo at startup).
            # This correctly handles all pairs on both Demo and Mainnet without any hardcoding.
            _sym_info = self.get_symbol_precision(symbol)
            min_notional = _sym_info.get('min_notional', 5.0)
            
            if price and amount:
                 notional = price * amount
                 if notional < min_notional:
                     if is_closing:
                         self.logger.info(f"🛡️ [MIN-NOTIONAL] Order value ${notional:.2f} < ${min_notional}. Skipping auto-scale because this is a closing/TP order.")
                     else:
                         # 🚀 AUTO-SCALE: Dynamically increase the amount to meet the exchange's minimum notional limit
                         # Add a $2.00 buffer to ensure slight price drops don't cause instant rejection during network transit
                         needed_notional = min_notional + 2.0
                         adjusted_amount = needed_notional / price
                         
                         # Ceil to exchange step size so we never round back below the notional threshold
                         try:
                             prec = self.get_symbol_precision(symbol)
                             amount = self.ceil_to_step(adjusted_amount, prec['step_size'])
                         except:
                             amount = adjusted_amount
                         
                         self.logger.warning(f"⚠️ [AUTO-SCALE] Order value ${notional:.2f} < Min Notional ${min_notional}. Auto-scaled amount to target ${needed_notional:.2f} ({amount} units).")

            # Basic Validation Passed
            return True, amount, price, ""
        except Exception as e:
            self.logger.error(f"Validation exception: {e}")
            return False, amount, price, str(e)

    def cancel_order(self, order_id, symbol):
        try:
            if config.TESTNET or config.DEMO_TRADING:
                endpoint = '/fapi/v1/order'
                params = {
                    'symbol': normalize_symbol(symbol),
                    'orderId': order_id
                }
                return self._raw_request(endpoint, method='DELETE', params=params)
            return self.exchange.cancel_order(order_id, symbol)
        except Exception as e:
            self.logger.error(f"Cancel Order Failed: {e}")
            return None

    def cancel_orders_by_bot_id(self, bot_id: int, symbol: str):
        cancelled_count = 0
        try:
            prefix = f"CQB_{bot_id}_"
            for order in self.fetch_open_orders(symbol):
                if order.get('clientOrderId', '').startswith(prefix):
                    self.cancel_order(order['id'], symbol)
                    cancelled_count += 1
        except: pass
        return cancelled_count

    def cancel_all_orders(self, symbol: str):
        """Cancels all open orders for a symbol."""
        try:
            if config.TESTNET or config.DEMO_TRADING:
                endpoint = '/fapi/v1/allOpenOrders'
                params = {'symbol': normalize_symbol(symbol)}
                return self._raw_request(endpoint, method='DELETE', params=params)
            return self.exchange.cancel_all_orders(symbol)
        except Exception as e:
            self.logger.error(f"Cancel All Orders Failed for {symbol}: {e}")
            return None

    def get_best_bid_ask(self, symbol: str) -> tuple:
        """
        Fetches the current best bid and ask price for a symbol.
        Returns (bid, ask) tuple. Used to retry Post-Only orders at the correct maker price.
        - LONG entry / SHORT TP → price must be <= best BID (place below market to be maker)
        - SHORT entry / LONG TP → price must be >= best ASK (place above market to be maker)
        """
        try:
            norm_sym = normalize_symbol(symbol)
            if config.TESTNET or config.DEMO_TRADING:
                res = self._raw_request('/fapi/v1/ticker/bookTicker', params={'symbol': norm_sym})
                if res:
                    return float(res['bidPrice']), float(res['askPrice'])
            # Mainnet fallback via CCXT
            ticker = self.exchange.fetch_ticker(symbol)
            return float(ticker['bid']), float(ticker['ask'])
        except Exception as e:
            self.logger.error(f"get_best_bid_ask failed for {symbol}: {e}")
            return None, None

def cleanup_caches():
    pass

def normalize_symbol(symbol: str) -> str:
    if not symbol: return ""
    return symbol.replace('/', '').replace('-', '').split(':')[0].upper()

def normalize_market_type(mt: str) -> str:
    """Canonicalize market type strings: 'futures'/'swap' → 'future', etc."""
    if not mt: return 'future'
    mt = mt.lower().strip()
    if mt in ('futures', 'swap', 'linear'):
        return 'future'
    return mt
