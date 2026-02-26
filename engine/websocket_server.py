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
            logger.debug(f"[WSS_LOOP] Event loop created in thread {self.name}")
            self.loop.run_until_complete(self._serve())
        except Exception as e:
            logger.warning(f"WebSocket Server unavailable: {e}")
        finally:
            if self.loop and not self.loop.is_closed():
                self.loop.close()

    async def _serve(self):
        """Start the server and run forever."""
        try:
            async with websockets.serve(self.handler, self.host, self.port):
                logger.info(f"WebSocket Server LISTENING on ws://{self.host}:{self.port}")
                await asyncio.Future()  # run forever
        except OSError as e:
            # Port already in use or other socket error — non-fatal
            logger.warning(f"WebSocket Server could not bind (port in use?): {e}")
        except Exception as e:
            logger.warning(f"WebSocket Server stopped: {e}")

    async def handler(self, websocket):
        """Handle new connections."""
        self.clients.add(websocket)
        try:
            async for message in websocket:
                pass  # Handle control messages from UI if needed
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)

    def broadcast(self, message: dict):
        """Thread-safe broadcast method."""
        if not self.loop or not self.running or not self.loop.is_running():
            return
        
        if self.clients:
            json_msg = json.dumps(message)
            try:
                asyncio.run_coroutine_threadsafe(self._broadcast_async(json_msg), self.loop)
            except Exception as e:
                logger.debug(f"[WSS_BROADCAST] Failed to schedule async broadcast: {e}")

    async def _broadcast_async(self, message):
        if not self.clients:
            return
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
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
