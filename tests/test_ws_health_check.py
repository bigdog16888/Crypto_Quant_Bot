"""
tests/test_ws_health_check.py
=============================
Unit test for WebSocketLifecycleMixin._ws_health_check().

This closes the coverage gap flagged in the Module 2 self-review:
_ws_health_check was previously only exercised via run_cycle integration
tests. Here we test it in isolation with a mocked WebSocket state.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from engine.runner.websocket_lifecycle import WebSocketLifecycleMixin


class _TestRunner(WebSocketLifecycleMixin):
    """Minimal object that mixes in only the WS lifecycle method."""
    pass


@pytest.fixture()
def runner():
    return _TestRunner()


def test_ws_health_check_restarts_when_handler_down(runner):
    """When the WS handler is None or not alive, start_websocket_stream is called."""
    fake_handler = MagicMock()
    fake_handler.is_alive = False  # handler exists but is dead

    with patch(
        "engine.websocket_handler.get_websocket_handler",
        return_value=fake_handler,
    ) as mock_get, patch(
        "engine.websocket_handler.start_websocket_stream"
    ) as mock_start, patch(
        "engine.ws_event_handlers.handle_order_update", MagicMock()
    ), patch(
        "engine.ws_event_handlers.handle_position_update", MagicMock()
    ):
        runner._ws_health_check()

    mock_get.assert_called_once()
    mock_start.assert_called_once()


def test_ws_health_check_no_restart_when_handler_alive(runner):
    """When the WS handler is alive, start_websocket_stream is NOT called."""
    fake_handler = MagicMock()
    fake_handler.is_alive = True  # handler alive

    with patch(
        "engine.websocket_handler.get_websocket_handler",
        return_value=fake_handler,
    ) as mock_get, patch(
        "engine.websocket_handler.start_websocket_stream"
    ) as mock_start:
        runner._ws_health_check()

    mock_get.assert_called_once()
    mock_start.assert_not_called()


def test_ws_health_check_no_raise_on_import_failure(runner):
    """If the WS modules fail to import/raise, the method swallows the error."""
    with patch(
        "engine.websocket_handler.get_websocket_handler",
        side_effect=RuntimeError("simulated WS failure"),
    ):
        # Should not raise
        runner._ws_health_check()
