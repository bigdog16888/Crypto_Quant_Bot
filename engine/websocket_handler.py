"""
WebSocket Handler for Real-Time Updates (Phase 7)

Replaces polling with WebSocket streams for:
- Order updates (NEW, FILLED, CANCELED)
- Position updates
- Balance updates

Uses Binance User Data Stream via direct WebSocket (ccxt.pro not required).
"""

import asyncio
import json
import websockets
import logging
import threading
import time
import hmac
import hashlib
import requests
from typing import Callable, Optional, Dict, Any
from config.settings import config

logger = logging.getLogger("WebSocketHandler")


class BinanceUserDataStream:
    """
    Manages Binance User Data WebSocket stream for futures.
    Provides real-time callbacks for order and position events.
    """
    
    # Binance endpoints
    # Demo (Testnet) URLs — Updated to current official Demo FAPI endpoints
    # REST: demo-fapi.binance.com  |  WSS: fstream.binancefuture.com
    TESTNET_REST = "https://demo-fapi.binance.com"
    TESTNET_WSS  = "wss://fstream.binancefuture.com"   # User-data stream (not market stream)
    MAINNET_REST = "https://fapi.binance.com"
    MAINNET_WSS  = "wss://fstream.binance.com"
    
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        
        self.rest_url = self.TESTNET_REST if testnet else self.MAINNET_REST
        self.wss_url = self.TESTNET_WSS if testnet else self.MAINNET_WSS
        
        self.listen_key: Optional[str] = None
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Callbacks
        self._on_order_update: Optional[Callable[[Dict], None]] = None
        self._on_position_update: Optional[Callable[[Dict], None]] = None
        self._on_balance_update: Optional[Callable[[Dict], None]] = None
        
        # Last keepalive timestamp
        self._last_keepalive = 0
        
    def _sign_request(self, params: dict) -> dict:
        """Sign request with HMAC SHA256."""
        params['timestamp'] = int(time.time() * 1000)
        query = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        params['signature'] = signature
        return params
        
    def _get_listen_key(self) -> Optional[str]:
        """Get a new listen key for user data stream."""
        try:
            headers = {'X-MBX-APIKEY': self.api_key}
            resp = requests.post(
                f"{self.rest_url}/fapi/v1/listenKey",
                headers=headers,
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"📡 Obtained listenKey: {data.get('listenKey', '')[:20]}...")
            return data.get('listenKey')
        except Exception as e:
            logger.error(f"Failed to get listenKey: {e}")
            return None
            
    def _keepalive_listen_key(self) -> bool:
        """Keepalive the listen key (must be called every 30 mins)."""
        if not self.listen_key:
            return False
        try:
            headers = {'X-MBX-APIKEY': self.api_key}
            resp = requests.put(
                f"{self.rest_url}/fapi/v1/listenKey",
                headers=headers,
                timeout=10
            )
            resp.raise_for_status()
            self._last_keepalive = time.time()
            logger.debug("🔄 ListenKey keepalive sent")
            return True
        except Exception as e:
            logger.error(f"ListenKey keepalive failed: {e}")
            return False
            
    def on_order_update(self, callback: Callable[[Dict], None]):
        """Register callback for order updates."""
        self._on_order_update = callback
        return self
        
    def on_position_update(self, callback: Callable[[Dict], None]):
        """Register callback for position updates."""
        self._on_position_update = callback
        return self
        
    def on_balance_update(self, callback: Callable[[Dict], None]):
        """Register callback for balance updates."""
        self._on_balance_update = callback
        return self
        
    def _handle_message(self, message: str):
        """Parse and route WebSocket message to callbacks."""
        try:
            data = json.loads(message)
            event_type = data.get('e')
            
            logger.debug(f"📥 RAW WS MESSAGE: {message[:200]}...")

            if event_type == 'ORDER_TRADE_UPDATE':
                # Order update
                order_data = data.get('o', {})
                parsed = {
                    'event': 'order_update',
                    'symbol': order_data.get('s'),
                    'side': order_data.get('S'),
                    'order_type': order_data.get('o'),
                    'status': order_data.get('X'),  # NEW, FILLED, CANCELED, etc.
                    'order_id': order_data.get('i'),
                    'client_order_id': order_data.get('c'),
                    'price': float(order_data.get('p', 0)),
                    'qty': float(order_data.get('q', 0)),
                    'filled_qty': float(order_data.get('z', 0)),
                    'incremental_qty': float(order_data.get('l', 0)),
                    'avg_price': float(order_data.get('ap', 0)),
                    'realized_pnl': float(order_data.get('rp', 0)),
                    'timestamp': data.get('E')
                }
                logger.info(f"📬 Order Update: {parsed['symbol']} {parsed['side']} {parsed['status']} #{parsed['order_id']} | CID: {parsed['client_order_id']}")
                
                if self._on_order_update:
                    self._on_order_update(parsed)
                    
            elif event_type == 'ACCOUNT_UPDATE':
                # Position and balance update
                account_data = data.get('a', {})
                
                # Balance updates
                balances = account_data.get('B', [])
                for bal in balances:
                    parsed = {
                        'event': 'balance_update',
                        'asset': bal.get('a'),
                        'wallet_balance': float(bal.get('wb', 0)),
                        'cross_wallet_balance': float(bal.get('cw', 0)),
                        'timestamp': data.get('E')
                    }
                    if self._on_balance_update:
                        self._on_balance_update(parsed)
                        
                # Position updates
                positions = account_data.get('P', [])
                for pos in positions:
                    parsed = {
                        'event': 'position_update',
                        'symbol': pos.get('s'),
                        'side': 'LONG' if float(pos.get('pa', 0)) > 0 else 'SHORT',
                        'position_amt': float(pos.get('pa', 0)),
                        'entry_price': float(pos.get('ep', 0)),
                        'unrealized_pnl': float(pos.get('up', 0)),
                        'margin_type': pos.get('mt'),
                        'timestamp': data.get('E')
                    }
                    if abs(parsed['position_amt']) > 0:
                        logger.info(f"📊 Position Update: {parsed['symbol']} {parsed['side']} {parsed['position_amt']}")
                    if self._on_position_update:
                        self._on_position_update(parsed)
                        
            elif event_type == 'listenKeyExpired':
                logger.warning("⚠️ ListenKey expired! Reconnecting...")
                self._running = False  # Trigger reconnect
                
        except Exception as e:
            logger.error(f"Error handling WS message: {e}")
            
    async def _connect_and_listen(self):
        """Main WebSocket connection loop."""
        while self._running:
            try:
                # Get listen key
                self.listen_key = self._get_listen_key()
                if self.listen_key:
                    self.listen_key = self.listen_key.strip() # 🛡️ SAFETY: Remove any \n \r
                
                if not self.listen_key:
                    logger.error("Cannot start WS without listenKey. Retrying in 10s...")
                    await asyncio.sleep(10)
                    continue
                    
                ws_url = f"{self.wss_url}/ws/{self.listen_key}"
                logger.info(f"🔌 Connecting to WebSocket: {ws_url[:50]}...")
                
                async with websockets.connect(
                    ws_url,
                    open_timeout=15,
                    ping_interval=20,
                    ping_timeout=10
                ) as ws:
                    self.ws = ws
                    logger.info("✅ WebSocket connected!")
                    self._last_keepalive = time.time()
                    
                    while self._running:
                        try:
                            # Keepalive every 20 mins
                            if time.time() - self._last_keepalive > 1200:
                                self._keepalive_listen_key()
                                
                            # Wait for message with timeout
                            message = await asyncio.wait_for(ws.recv(), timeout=30)
                            self._handle_message(message)
                            
                        except asyncio.TimeoutError:
                            # No message, just continue (ping/pong handled by websockets lib)
                            continue
                        except websockets.exceptions.ConnectionClosed:
                            logger.warning("WebSocket connection closed. Reconnecting...")
                            break
                            
            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
                
    def start(self):
        """Start the WebSocket handler in a background thread."""
        if self._running:
            logger.warning("WebSocket already running")
            return
            
        self._running = True
        
        def run_loop():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._connect_and_listen())
            finally:
                self._loop.close()
                
        self._thread = threading.Thread(target=run_loop, daemon=True, name="WSHandler")
        self._thread.start()
        logger.info("🚀 WebSocket handler started in background thread")
        
    @property
    def is_alive(self) -> bool:
        """Check if the stream thread is running and WebSocket is connected."""
        return self._running and self.ws is not None and self._thread is not None and self._thread.is_alive()
        
    def stop(self):
        """Stop the WebSocket handler."""
        logger.info("Stopping WebSocket handler...")
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("WebSocket handler stopped")


# Global instance
_ws_handler: Optional[BinanceUserDataStream] = None


def get_websocket_handler() -> Optional[BinanceUserDataStream]:
    """Get or create the global WebSocket handler."""
    global _ws_handler
    if _ws_handler is None:
        api_key = config.API_KEY
        api_secret = config.API_SECRET
        testnet = getattr(config, 'DEMO_TRADING', True)
        
        if not api_key or not api_secret:
            logger.error("Cannot start WebSocket: Missing API credentials")
            return None
            
        _ws_handler = BinanceUserDataStream(api_key, api_secret, testnet=testnet)
        
    return _ws_handler


def start_websocket_stream(
    on_order_update: Optional[Callable] = None,
    on_position_update: Optional[Callable] = None,
    on_balance_update: Optional[Callable] = None
):
    """
    Start the WebSocket stream with optional callbacks.
    Call this once during bot startup.
    """
    handler = get_websocket_handler()
    if not handler:
        return False
        
    if on_order_update:
        handler.on_order_update(on_order_update)
    if on_position_update:
        handler.on_position_update(on_position_update)
    if on_balance_update:
        handler.on_balance_update(on_balance_update)
        
    handler.start()
    return True
