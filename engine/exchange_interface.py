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
            self.logger.warning(f"🛡️ HYBRID RAW MODE ACTIVE (Demo FAPI)")
            
        return exchange

    def _ensure_markets(self):
        """Ensures markets are loaded. Re-tries anonymously if keys cause issues."""
        if self.exchange and self.exchange.markets:
            return
            
        try:
            self.exchange.load_markets()
        except Exception as e:
            self.logger.info(f"Market load with keys failed (likely Testnet/Demo context). falling back to anonymous mode...")
            try:
                # Use shared anon instance
                anon = self._get_anon_exchange()
                self.exchange.markets = anon.load_markets()
                self.exchange.markets_by_id = anon.markets_by_id
            except Exception as e2:
                self.logger.error(f"Critical: Anonymous market load also failed: {e2}")

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
            self.logger.error(f"OHLCV Error for {symbol}: {e}")
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
                            'side': 'long' if float(pos['positionAmt']) > 0 else 'short',
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
                            'orderId': t['orderId'],
                            'symbol': t['symbol'],
                            'side': t['side'].lower(),
                            'price': float(t['price']),
                            'amount': float(t['qty']),
                            'cost': float(t['quoteQty']),
                            'commission': float(t.get('commission', 0)),
                            'timestamp': t['time']
                        })
                return trades
            
            # Mainnet fallback
            return self.exchange.fetch_my_trades(symbol, since=since, limit=limit)
        except Exception as e:
            self.logger.error(f"Trades Fetch Error for {symbol}: {e}")
            return []

    def get_symbol_precision(self, symbol: str) -> Dict[str, Any]:
        """
        Returns full precision metadata for a given symbol.
        Uses exchange metadata to ensure fundamental compliance for any pair.
        """
        try:
            self._ensure_markets()
            if not self.exchange or not self.exchange.markets:
                return {'qty_precision': 5, 'price_precision': 2, 'step_size': 0.00001, 'tick_size': 0.01}
                
            # Get the correct symbol key (BTC/USDC or BTCUSDC)
            m_key = symbol if symbol in self.exchange.markets else symbol.replace('/', '')
            market = self.exchange.markets.get(m_key)
            
            # Defaults for safety
            p_qty = 3
            p_price = 2
            step_size = 0.001
            tick_size = 0.01
            
            if market and 'info' in market and 'filters' in market['info']:
                # ROBUST FIX: Parse raw filters directly from exchange info
                for f in market['info']['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        if step_size < 1:
                            p_qty = int(-math.log10(step_size))
                        else:
                            p_qty = 0
                    
                    if f['filterType'] == 'PRICE_FILTER':
                        tick_size = float(f['tickSize'])
                        if tick_size < 1:
                            p_price = int(-math.log10(tick_size))
                        else:
                            p_price = 0
            elif market:
                 # Fallback to CCXT if raw filters missing (unlikely)
                 pass
            
            # DEMO PRECISION OVERRIDE: Demo exchange uses different tick/step sizes than mainnet.
            # CCXT loads mainnet metadata but demo rejects orders that don't match demo rules.
            if config.TESTNET or config.DEMO_TRADING:
                if 'BTC' in symbol:
                    # Demo BTC: tick_size=0.1, step_size=0.001
                    if p_price > 1:
                        self.logger.debug(f"⚙️ Demo clamp: {symbol} price precision {p_price} → 1 (tick=0.1)")
                        p_price = 1
                        tick_size = 0.1
                    if p_qty > 3:
                        self.logger.debug(f"⚙️ Demo clamp: {symbol} qty precision {p_qty} → 3 (step=0.001)")
                        p_qty = 3
                        step_size = 0.001
                elif 'ETH' in symbol:
                    # Demo ETH: tick_size=0.01, step_size=0.001
                    if p_price > 2:
                        self.logger.debug(f"⚙️ Demo clamp: {symbol} price precision {p_price} → 2 (tick=0.01)")
                        p_price = 2
                        tick_size = 0.01
                    if p_qty > 3:
                        p_qty = 3
                        step_size = 0.001
                
            return {
                'qty_precision': p_qty,
                'price_precision': p_price,
                'step_size': step_size,
                'tick_size': tick_size
            }
        except Exception as e:
            self.logger.error(f"Error fetching precision for {symbol}: {e}")
            return {
                'qty_precision': 3, # Standard usually 3 for BTC
                'price_precision': 2,
                'step_size': 0.001,
                'tick_size': 0.01
            }

    @staticmethod
    def round_to_step(value: float, step: float) -> float:
        """Rounds a value to the nearest exchange step size (e.g., 0.05)."""
        if not step or step <= 0: return value
        import math
        # Use decimal-safe rounding
        if step < 1:
            precision = int(-math.log10(step))
        else:
            precision = 0 # Integer steps like 1.0, 10.0
            
        return round(math.floor(value / step) * step, precision)

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
                 params = {'symbol': normalize_symbol(symbol), 'orderId': order_id}
                 res = self._raw_request(endpoint, params=params)
                 if res:
                     return {'id': res['orderId'], 'status': res['status'].lower()}
                 return None
            return self.exchange.fetch_order(order_id, symbol)
        except Exception as e:
            self.logger.error(f"Fetch Order Error: {e}")
            return None

    def validate_order(self, symbol: str, side: str, amount: float, price: Optional[float] = None):
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
            
            # Min Notional Check (Approximate)
            # Binance Futures usually $5 min
            if price and amount:
                 notional = price * amount
                 if notional < 5.0:
                     return False, amount, price, f"Order value ${notional:.2f} < Min Notional $5.0"

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
