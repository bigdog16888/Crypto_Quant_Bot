"""REL-1 tests: handle_emergency_liquidation must use the WRAPPER, not ccxt-native.

Root cause (2026-09-04, docs/REL1_EMERGENCY_PATH_ROOT_CAUSE_20260904.md):
shutdown.py's emergency path called ex.exchange.fetch_positions() — ccxt-NATIVE,
which signs against production FAPI endpoints and gets -2015 AuthenticationError
on the demo key. The cancel half (wrapper) worked; the CLOSE half was dead.

These tests replay the live failure signature (native raises -2015, wrapper
returns positions) and assert the emergency path CLOSES positions through the
wrapper — for LONG, SHORT, and flat cases — plus a source-level tripwire
against re-introducing a native fetch_positions bypass into shutdown.py.
"""
import inspect
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_runner(active_bots):
    from engine.runner import BotRunner
    runner = BotRunner.__new__(BotRunner)
    runner.circuit_breaker_triggered = False

    mock_exchange = MagicMock()
    # Replay the LIVE failure signature: ccxt-native raises -2015...
    mock_exchange.exchange.fetch_positions.side_effect = Exception(
        'binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action"}'
    )
    # ...while the WRAPPER path returns real positions.
    runner._wrapper_positions = []
    mock_exchange.fetch_positions.side_effect = (
        lambda *a, **k: list(runner._wrapper_positions)
    )
    runner.exchanges = {'future': mock_exchange}
    runner.exchange = mock_exchange
    runner.get_active_bots = MagicMock(return_value=active_bots)
    return runner


# get_active_bots() row shape: (id, name, pair, direction, strategy_type,
# config_json, total_invested, current_step, rsi_limit, is_active, base_size,
# martingale_multiplier, status)
def _bot_row(bot_id, name, pair, is_active=1):
    return (bot_id, name, pair, 'long', 'martingale', '{"market_type": "future"}',
            100.0, 1, 70, is_active, 10.0, 1.5, 'active')


class TestEmergencyLiquidation:

    def test_long_position_closed_via_wrapper(self):
        """Replay of the live 09:08 incident: native fetch_positions raises -2015,
        wrapper returns a live LONG. The emergency path must CLOSE via the wrapper:
        create_order('market', 'sell', qty) + DB reset."""
        runner = _make_runner([_bot_row(10011, 'eth', 'ETH/USDC:USDC')])
        runner._wrapper_positions = [
            {'symbol': 'ETH/USDC', 'contracts': 0.901, 'size': 0.901},
        ]
        ex = runner.exchanges['future']

        with patch('engine.database.get_connection') as m_conn, \
             patch('engine.database.reset_bot_after_tp') as m_reset:
            m_conn.return_value = MagicMock()
            with patch('config.settings.config.DRY_RUN', False):
                runner.handle_emergency_liquidation()

        ex.cancel_orders_by_bot_id.assert_called_once_with(10011, 'ETH/USDC:USDC')
        # WRAPPER used (it returned the position); native never reached
        ex.fetch_positions.assert_called()
        ex.exchange.fetch_positions.assert_not_called()
        # CLOSE fired: sell the LONG
        args, kwargs = ex.create_order.call_args
        assert args[0] == 'ETH/USDC:USDC' and args[1] == 'market'
        assert args[2] == 'sell' and abs(args[3] - 0.901) < 1e-9
        assert kwargs.get('emergency') is True
        assert kwargs.get('human_approved') is True
        m_reset.assert_called_once()

    def test_short_position_closed_via_wrapper(self):
        """SHORT position (BNB -0.02): emergency must BUY to close."""
        runner = _make_runner([_bot_row(10007, 'BNB short', 'BNB/USDC:USDC')])
        runner._wrapper_positions = [
            {'symbol': 'BNB/USDC', 'contracts': -0.02, 'size': -0.02},
        ]
        ex = runner.exchanges['future']

        with patch('engine.database.get_connection') as m_conn, \
             patch('engine.database.reset_bot_after_tp') as m_reset:
            m_conn.return_value = MagicMock()
            with patch('config.settings.config.DRY_RUN', False):
                runner.handle_emergency_liquidation()

        args, _ = ex.create_order.call_args
        assert args[1] == 'market' and args[2] == 'buy' and abs(args[3] - 0.02) < 1e-9
        m_reset.assert_called_once()

    def test_flat_position_not_closed(self):
        """Zero-size position: no create_order, no DB reset."""
        runner = _make_runner([_bot_row(10001, 'sol', 'SOL/USDC:USDC')])
        runner._wrapper_positions = [
            {'symbol': 'SOL/USDC', 'contracts': 0.0, 'size': 0.0},
        ]
        ex = runner.exchanges['future']

        with patch('engine.database.get_connection') as m_conn, \
             patch('engine.database.reset_bot_after_tp') as m_reset:
            m_conn.return_value = MagicMock()
            with patch('config.settings.config.DRY_RUN', False):
                runner.handle_emergency_liquidation()

        ex.cancel_orders_by_bot_id.assert_called_once()
        ex.create_order.assert_not_called()
        m_reset.assert_not_called()

    def test_source_has_no_native_fetch_positions_bypass(self):
        """Tripwire: shutdown.py must never re-introduce ex.exchange.fetch_positions()
        (ccxt-native). The 2026-09-04 fix swapped it for the wrapper; this test
        makes a silent regression impossible."""
        import engine.runner.shutdown as shutdown_mod
        src = inspect.getsource(shutdown_mod)
        assert '.exchange.fetch_positions' not in src, (
            "shutdown.py re-introduced a ccxt-NATIVE fetch_positions call — "
            "this is the REL-1 bug (dead on -2015 on demo-fapi). "
            "Use ex.fetch_positions() (wrapper) instead. "
            "See docs/REL1_EMERGENCY_PATH_ROOT_CAUSE_20260904.md"
        )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
