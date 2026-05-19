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
    _position_mode_hedge = None # None=Unknown, True=Hedge, False=One-Way
    _position_mode_lock = threading.Lock()
    _time_offset = None

    def __init__(self, market_type='future'):
        self.logger = logging.getLogger(f"ExchangeInterface.{market_type}")
        self.market_type = normalize_market_type(market_type)
        self._sync_time_offset()
        self.exchange = self._create_exchange_instance()
        self._ensure_markets()
        self._detect_position_mode()

    def _sync_time_offset(self):
        """Fetches the server time to calculate the offset for raw requests."""
        if ExchangeInterface._time_offset is not None:
            return
        try:
            url = 'https://demo-fapi.binance.com/fapi/v1/time' if (config.TESTNET or config.DEMO_TRADING) else 'https://fapi.binance.com/fapi/v1/time'
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                server_time = res.json()['serverTime']
                local_time = int(time.time() * 1000)
                ExchangeInterface._time_offset = server_time - local_time
                self.logger.info(f"🕒 Time offset synced: {ExchangeInterface._time_offset}ms")
        except Exception as e:
            self.logger.warning(f"Failed to sync time offset: {e}")
            ExchangeInterface._time_offset = 0

    def _get_adjusted_timestamp(self) -> int:
        if ExchangeInterface._time_offset is None:
            self._sync_time_offset()
        offset = ExchangeInterface._time_offset if ExchangeInterface._time_offset else 0
        return int(time.time() * 1000) + offset

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

    def _detect_position_mode(self):
        """
        Detects if the account is in Hedge Mode (dualSidePosition: True) or One-Way Mode (False).
        FIXED: System is strictly designed for ONE-WAY MODE to support multi-bot netting.
        """
        ExchangeInterface._position_mode_hedge = False
        self.logger.debug(f"🛡️ [MODE-DETECT] System forced to ONE-WAY MODE as required.")

    def _raw_request(self, endpoint: str, method: str = 'GET', params: dict = None) -> Any:
        """
        Executes a raw signed request to the Binance Demo FAPI.
        Correctly handles GET (query params) and POST (body params).
        """
        base_url = "https://demo-fapi.binance.com"
        query_dict = params.copy() if params else {}
        query_dict['timestamp'] = self._get_adjusted_timestamp()
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
            data = anon.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            
            # 🔍 DIAGNOSTIC: BNB/XRP Tracking
            if symbol.startswith('BNB') or symbol.startswith('XRP'):
                qty = len(data) if data else 0
                self.logger.info(f"🔍 [OHLCV-TRACE] {symbol} ({timeframe}): Fetched {qty} candles.")
            
            return data
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
                        
                        # 🚀 ONE-WAY MODE NORMALIZATION:
                        # In One-Way mode, Binance reports positionSide as 'BOTH'.
                        # The engine requires 'LONG' or 'SHORT' for proof verification.
                        raw_side = str(pos.get('positionSide', 'BOTH')).upper()
                        pos_amt = float(pos.get('positionAmt', 0))
                        
                        if raw_side == 'BOTH':
                            normalized_side = 'LONG' if pos_amt > 0 else 'SHORT'
                        else:
                            normalized_side = raw_side.upper()

                        positions.append({
                            'symbol': unified_symbol,
                            'contracts': pos_amt,
                            'qty': abs(pos_amt),      # Raw unsigned magnitude
                            'net_qty': pos_amt,       # Signed magnitude for netting math
                            'side': normalized_side.lower(), # Normalized: long, short
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
        """Rounds a value DOWN to the nearest exchange step size using Decimal for precision."""
        if not step or step <= 0: return value
        from decimal import Decimal, ROUND_FLOOR
        # Convert inputs to strings first to avoid importing existing float noise
        d_val = Decimal(str(value))
        d_step = Decimal(str(step))
        # Quantize performs the floor division in decimal space
        rounded = (d_val / d_step).quantize(Decimal('1'), rounding=ROUND_FLOOR) * d_step
        return float(rounded)

    @staticmethod
    def ceil_to_step(value: float, step: float) -> float:
        """Rounds a value UP to the nearest exchange step size using Decimal for precision."""
        if not step or step <= 0: return value
        from decimal import Decimal, ROUND_CEILING
        d_val = Decimal(str(value))
        d_step = Decimal(str(step))
        rounded = (d_val / d_step).quantize(Decimal('1'), rounding=ROUND_CEILING) * d_step
        return float(rounded)

    # ═══════════════════════════════════════════════════════════════════════════
    # LAYER 1 — CQB PREFIX ENFORCEMENT (v3.4.0)
    # ═══════════════════════════════════════════════════════════════════════════
    # All orders MUST have a CQB_ client order ID so the WebSocket handler can
    # attribute fills to their originating bot.  Non-CQB orders are rejected
    # unless the caller explicitly opts into the emergency path.
    #
    # Emergency path (reconciler healing, panic closes):
    #   Pass emergency=True AND _audit_cursor (a live DB cursor).
    #   An audit row is written to exchange_order_audit — the order is logged
    #   even though no full bot_orders receipt exists.
    #
    # Bot-context path (all normal trading):
    #   Use create_order_with_receipt() below.  It writes a pending bot_orders
    #   row before touching the exchange and updates it with the real orderId
    #   after.  Prefer this method for all new callers.
    # ═══════════════════════════════════════════════════════════════════════════
    def create_order(self, symbol, type, side, amount, price=None, params=None,
                     post_only=False, emergency=False,
                     _audit_cursor=None, _call_site: str = "unknown", human_approved: bool = False) -> dict:
        """
        Place an order on the exchange.

        Signature is backward-compatible with all existing callers.
        New keyword arguments carry the gate enforcement:

        Args:
            emergency:      Set True ONLY for reconciler/runner healing calls that
                            have no owning bot.  Requires _audit_cursor.
            _audit_cursor:  Mandatory when emergency=True.  The audit row is written
                            to exchange_order_audit inside this method.
            _call_site:     Human-readable identifier of the caller (file:function).
                            Included in the audit row for traceability.
        """
        params = params or {}

        # ── CID extraction ──────────────────────────────────────────────────
        # The existing code maps CCXT-style 'clientOrderId' → 'newClientOrderId'
        # later in the method.  Inspect both keys so the gate sees the CID
        # regardless of which convention the caller used.
        cid = (
            params.get("newClientOrderId")
            or params.get("clientOrderId")
            or ""
        )

        # ── Layer 1 gate ────────────────────────────────────────────────────
        if not emergency:
            if not cid.startswith("CQB_"):
                raise ValueError(
                    f"[EXCHANGE-GATE] Order rejected: client_order_id='{cid}' "
                    f"is missing the CQB_ prefix. "
                    f"Use create_order_with_receipt() for bot-context orders. "
                    f"For reconciler/runner healing calls pass emergency=True "
                    f"with a valid _audit_cursor. "
                    f"Call site: {_call_site}"
                )
        else:
            # Emergency path — audit cursor is not optional
            if _audit_cursor is None:
                raise ValueError(
                    f"[EXCHANGE-GATE] emergency=True requires _audit_cursor. "
                    f"All emergency orders must be logged to exchange_order_audit. "
                    f"Call site: {_call_site}"
                )

        # ── Layer 1.5 gate (Human Approval for Market/Close) ───────────────
        import os
        if config.REQUIRE_HUMAN_APPROVAL and type.upper() == 'MARKET':
            if not human_approved and not params.get('human_approved', False):
                bot_id = params.get('bot_id', 'unknown') if params else 'unknown'
                log_line = f"{int(time.time())} | 🛡️ [BLOCKED-ACTION] | Bot: {bot_id} | Symbol: {symbol} | Type: {type.upper()} | Side: {side.upper()} | Amount: {amount} | Call Site: {_call_site}\n"
                blocked_log_path = os.path.join(config.ROOT_DIR, "blocked_actions.log")
                try:
                    with open(blocked_log_path, "a", encoding="utf-8") as f:
                        f.write(log_line)
                except Exception as e:
                    self.logger.error(f"Failed to write to blocked_actions.log: {e}")
                
                self.logger.critical(
                    f"🛡️ [HUMAN-APPROVAL-REQUIRED] Blocked autonomous market order for {symbol}: "
                    f"{side} {amount} units. Call site: {_call_site}"
                )
                raise ValueError(
                    f"[HUMAN-APPROVAL-REQUIRED] Autonomous market order blocked for {symbol}. "
                    f"Requires manual/UI execution or human_approved=True."
                )

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

                # Professional Maker-Only Support
                if post_only and type.upper() == 'LIMIT':
                    # ccxt uses timeInForce: 'GTX' for 'Good Till Crossing' (Post-Only on Binance)
                    raw_params['timeInForce'] = 'GTX'

                # 🚀 ARCHITECTURAL FIX: Strictly enforce One-Way Mode.
                # Binance rejects orders with positionSide if the account is in One-Way mode.
                if 'positionSide' in raw_params:
                    del raw_params['positionSide']
                if 'position_side' in raw_params:
                    del raw_params['position_side']

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
                        if k not in ['clientOrderId', 'reduceOnly', 'positionSide', 'position_side']:
                            raw_params[k] = v
                    if 'reduceOnly' in params:
                        raw_params['reduceOnly'] = 'true' if params['reduceOnly'] else 'false'

                res = self._raw_request(endpoint, method='POST', params=raw_params)

                if res:
                    result = {
                        'id': res.get('orderId'),
                        'symbol': symbol,
                        'status': res.get('status', 'open').lower(),
                        'clientOrderId': res.get('clientOrderId')
                    }
                else:
                    raise Exception("Raw order placement returned empty response")

            else:
                # Fallback for Mainnet
                if post_only and type.lower() == 'limit':
                    params['timeInForce'] = 'GTX'
                result = self.exchange.create_order(symbol, type, side, amount, price, params)

            # ── Emergency audit write ────────────────────────────────────────
            # Written AFTER a successful placement so the order_id is available.
            # If the exchange call raised above, no audit row is written (the
            # order never landed on Binance, so there is nothing to audit).
            if emergency and _audit_cursor is not None:
                try:
                    _audit_cursor.execute("""
                        INSERT INTO exchange_order_audit (
                            order_id, client_order_id, symbol, side, qty, price,
                            call_site, context, placed_at, notes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        str(result.get('id', '')),
                        cid or result.get('clientOrderId', ''),
                        symbol, side, amount, price or 0,
                        _call_site, 'reconciler_emergency',
                        int(time.time()),
                        'Emergency healing order. No bot_orders receipt by design.'
                    ))
                    # Caller is responsible for committing their transaction.
                except Exception as _ae:
                    self.logger.warning(
                        f"[EXCHANGE-GATE] Audit write failed for emergency order "
                        f"{result.get('id')} — {_ae}"
                    )

            return result

        except APIError:
            raise
        except Exception as e:
            self.logger.error(f"Order Placement Failed: {e}")
            raise APIError(str(e))

    # ═══════════════════════════════════════════════════════════════════════════
    # LAYER 2 — RECEIPT-WRITING WRAPPER FOR ALL BOT-CONTEXT ORDERS (v3.4.0)
    # ═══════════════════════════════════════════════════════════════════════════
    # Mandatory for every order placed on behalf of a specific bot.
    # Writes a 'pending' bot_orders row BEFORE touching the exchange so that
    # even a process crash mid-flight leaves an auditable trace the reconciler
    # can find and heal.
    # ═══════════════════════════════════════════════════════════════════════════
    def create_order_with_receipt(
        self,
        cursor,               # DB cursor — mandatory, must be inside an open transaction
        bot_id: int,
        cycle_id: int,
        symbol: str,
        type: str,
        side: str,
        amount: float,
        cqb_order_type: str,  # 'entry', 'grid', 'tp', 'hedge', 'anonymous_adopt', …
        price: float = None,
        params: dict = None,
        post_only: bool = False,
        notes: str = "",
        human_approved: bool = False,
    ) -> dict:
        """
        Receipt-writing wrapper.  Preferred path for all bot-context order placement.

        Flow:
          1. Generate CQB_ CID if params does not already carry newClientOrderId.
             (Callers that pre-build their CID pass it via params["newClientOrderId"]
             and the auto-generation is skipped — no double pending rows, no collision.)
          2. INSERT a 'pending' row into bot_orders with the CID and intended qty.
          3. Call create_order() — which enforces the CQB prefix via Layer 1.
          4a. On success: UPDATE the pending row with the real exchange order_id
              and set status = 'open'.
          4b. On failure: UPDATE the pending row with status = 'failed' and the
              error message.  Re-raises so the caller can handle appropriately.

        Args:
            cursor:          Live DB cursor inside the caller's transaction.
                             The caller is responsible for committing.
            bot_id:          Owning bot's integer ID.
            cycle_id:        Current bot cycle (used in CID generation and the row).
            cqb_order_type:  Semantic order type string stored in bot_orders.order_type.
            params:          Extra exchange params.  If params["newClientOrderId"]
                             is already set, it is used as-is (no auto-generation).
        """
        params = params or {}
        ts = int(time.time())

        # Auto-generate CID only when the caller hasn't provided one
        if "newClientOrderId" not in params and "clientOrderId" not in params:
            params["newClientOrderId"] = (
                f"CQB_{bot_id}_{cqb_order_type.upper()}_{cycle_id}_{ts}"
            )

        cid = params.get("newClientOrderId") or params.get("clientOrderId")

        # ── Step 2: Write pending receipt before touching exchange ───────────
        cursor.execute("""
            INSERT INTO bot_orders (
                bot_id, order_type, client_order_id, price, amount,
                filled_amount, status, cycle_id, created_at, updated_at, notes
            ) VALUES (?, ?, ?, ?, ?, 0, 'pending', ?, ?, ?, ?)
        """, (
            bot_id, cqb_order_type, cid,
            price or 0, amount,
            cycle_id, ts, ts,
            notes or f"Pending: {cqb_order_type.upper()} {side.upper()} {amount} {symbol}"
        ))
        pending_row_id = cursor.lastrowid

        # ── Step 3: Place the order (Layer 1 gate fires inside create_order) ─
        try:
            result = self.create_order(
                symbol=symbol,
                type=type,
                side=side,
                amount=amount,
                price=price,
                params=params,
                post_only=post_only,
                # emergency=False (default) — CQB prefix enforced by Layer 1
                human_approved=human_approved
            )
        except Exception as exc:
            # ── Step 4b: Mark pending row as failed ──────────────────────────
            cursor.execute("""
                UPDATE bot_orders
                SET status = 'failed', updated_at = ?, notes = ?
                WHERE id = ?
            """, (int(time.time()), f"Exchange error: {exc}", pending_row_id))
            raise  # Caller decides how to handle

        # ── Step 4a: Update pending row with real exchange order_id ──────────
        real_order_id = str(result.get('id', ''))
        cursor.execute("""
            UPDATE bot_orders
            SET order_id = ?, status = 'open', updated_at = ?
            WHERE id = ?
        """, (real_order_id, int(time.time()), pending_row_id))

        self.logger.info(
            f"[RECEIPT] Bot {bot_id} {cqb_order_type.upper()} {side.upper()} "
            f"{amount} {symbol} → exchange order_id={real_order_id} (CID={cid})"
        )

        return result

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
                     raw_avg = float(res.get('avgPrice', 0) or 0)
                     raw_price = float(res.get('price', 0) or 0)
                     return {
                         'id': res.get('orderId', order_id),
                         'status': res.get('status', 'unknown').lower(),
                         'filled': float(res.get('executedQty', 0)),
                         'amount': float(res.get('origQty', 0)),
                         # ✅ Root cause fix: always include fill price fields.
                         # Demo FAPI returns avgPrice="0" for limit orders (the fill price
                         # equals the limit price). Fall back to 'price' if avgPrice is 0.
                         'average': raw_avg if raw_avg > 0 else raw_price,
                         'price': raw_price,
                         'clientOrderId': res.get('clientOrderId', ''),
                     }
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
