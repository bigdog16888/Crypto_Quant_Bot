"""
WebSocket lifecycle mixin for BotRunner.

Contains the per-cycle WebSocket health-check logic that was previously
inline in BotRunner.run_cycle. Extracted byte-for-byte (no logic changes)
as part of the engine/runner/ package split (Module 2 of 4).

WS-STOP logic (graceful shutdown, DB worker flush) already lives in
ShutdownMixin._graceful_shutdown and ShutdownMixin._run_shutdown_sequence
respectively — it is NOT duplicated here. This mixin only owns the
per-cycle "is the stream alive, restart if not" check.
"""

import logging

logger = logging.getLogger("BotRunner")


class WebSocketLifecycleMixin:
    """Per-cycle WebSocket stream health-check."""

    def _ws_health_check(self):
        """
        [WS-HEALTH-CHECK] Ensure real-time stream is active.
        Extracted verbatim from BotRunner.run_cycle (original lines 1130-1143).
        No logic changes.
        """
        # 🚀 [WS-HEALTH-CHECK] Ensure real-time stream is active
        try:
            from engine.websocket_handler import get_websocket_handler, start_websocket_stream
            from engine.ws_event_handlers import handle_order_update, handle_position_update

            ws_h = get_websocket_handler()
            if not ws_h or not ws_h.is_alive:
                logger.warning("⚠️ WebSocket handler is DOWN or not initialized. Restarting...")
                start_websocket_stream(
                    on_order_update=handle_order_update,
                    on_position_update=handle_position_update
                )
        except Exception as ws_err:
            logger.error(f"Failed WS health check: {ws_err}")