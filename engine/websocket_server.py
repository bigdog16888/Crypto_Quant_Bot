import asyncio
import websockets
import json
import threading
import logging
from config.settings import config

logger = logging.getLogger("WSServer")

class WebSocketServer(threading.Thread):
    def __init__(self, host="0.0.0.0", port=8765):
        super().__init__()
        self.host = host
        self.port = port
        self.server = None
        self.clients = set()
        self.loop = None
        self.running = True
        self.daemon = True # Dies with main thread
        self.name = "WSServer"

    def run(self):
        """Run the asyncio loop in this thread."""
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            logger.debug(f"[WSS_LOOP] Event loop created and set in thread {self.name}")
            
            start_server = websockets.serve(self.handler, self.host, self.port)
            logging.info(f"WebSocket Server starting on ws://{self.host}:{self.port}")
            
            self.loop.run_until_complete(start_server)
            logging.info("WebSocket Server LISTENING")
            logger.debug(f"[WSS_LOOP] Event loop running forever in thread {self.name}")
            self.loop.run_forever()
        except Exception as e:
            logging.error(f"WebSocket Server crashed: {e}")
            logger.error(f"[WSS_LOOP] Loop stopped due to error in thread {self.name}")
        finally:
            if self.loop:
                if not self.loop.is_closed():
                    logger.debug(f"[WSS_LOOP] Closing loop in thread {self.name}")
                    self.loop.close()
                logger.debug(f"[WSS_LOOP] Loop status: {self.loop.is_running()}, {self.loop.is_closed()}")

    async def handler(self, websocket):
        """Handle new connections."""
        self.clients.add(websocket)
        try:
            # Keep connection open and handle incoming messages (if any)
            async for message in websocket:
                # We can handle control messages from UI here (e.g. "subscribe")
                pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.remove(websocket)

    def broadcast(self, message: dict):
        """Thread-safe broadcast method."""
        if not self.loop or not self.running or not self.loop.is_running():
            logger.debug(f"[WSS_BROADCAST] Skipping broadcast: loop running={self.loop.is_running() if self.loop else 'N/A'}, running flag={self.running}")
            return
        
        if self.clients:
            json_msg = json.dumps(message)
            # Schedule the broadcast in the event loop
            asyncio.run_coroutine_threadsafe(self._broadcast_async(json_msg), self.loop)

    async def _broadcast_async(self, message):
        if not self.clients: return
        # websockets.broadcast is available in newer versions, else iterate
        # But websockets 10+ has broadcast helper. 
        # For safety/compat, let's just iterate.
        to_remove = set()
        for client in self.clients:
            try:
                await client.send(message)
            except Exception:
                to_remove.add(client)
        
        for client in to_remove:
            self.clients.discard(client)

    def stop(self):
        self.running = False
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
