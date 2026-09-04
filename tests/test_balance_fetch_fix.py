"""
Test for the balance-fetch bug fix in BotRunner.check_circuit_breaker.

Before fix:
- _calculate_stablecoin_balance looked for top-level keys (balance['USDT'])
- Real CCXT balance dict is nested: balance['total']['USDT']
- This caused balance_fetch_success=False, skipping BOTH O-1 and O-3

After fix:
- _calculate_stablecoin_balance reads nested dict correctly
- O-1 (_check_position_size) runs unconditionally (DB-only, needs no balance)
- O-3 (_check_rolling_drawdown) runs only if balance_fetch_success=True
"""
import pytest
from unittest.mock import MagicMock, patch


class TestBalanceFetchFix:
    """Tests that the balance-fetch gate is fixed and breakers run correctly."""

    def test_calculate_stablecoin_balance_reads_nested_dict(self):
        """_calculate_stablecoin_balance should read balance['total']['USDT'] and balance['total']['USDC']."""
        from engine.runner import BotRunner
        runner = BotRunner.__new__(BotRunner)

        # CCXT-style nested balance dict (what fetch_balance actually returns)
        ccxt_balance = {
            'total': {
                'USDT': 5000.0,
                'USDC': 3000.0,
                'BTC': 0.1
            },
            'free': {'USDT': 4500.0},
            'used': {'USDT': 500.0}
        }

        result = runner._calculate_stablecoin_balance(ccxt_balance)
        assert result == 8000.0, f"Expected 8000.0, got {result}"

    def test_calculate_stablecoin_balance_fallback_to_flat(self):
        """Should fall back to flat dict keys if nested 'total' is missing."""
        from engine.runner import BotRunner
        runner = BotRunner.__new__(BotRunner)

        # Legacy flat dict style
        flat_balance = {
            'USDT': 5000.0,
            'USDC': 3000.0,
        }

        result = runner._calculate_stablecoin_balance(flat_balance)
        assert result == 8000.0, f"Expected 8000.0, got {result}"

    def test_calculate_stablecoin_balance_empty_returns_zero(self):
        """Empty or missing keys should return 0.0."""
        from engine.runner import BotRunner
        runner = BotRunner.__new__(BotRunner)

        assert runner._calculate_stablecoin_balance({}) == 0.0
        assert runner._calculate_stablecoin_balance({'total': {}}) == 0.0
        assert runner._calculate_stablecoin_balance({'total': {'BTC': 0.1}}) == 0.0

    def _make_runner(self):
        from engine.runner import BotRunner
        runner = BotRunner.__new__(BotRunner)
        runner.circuit_breaker_triggered = False
        runner.exchanges = {}
        runner.initial_equity = 0
        return runner

    def test_o1_runs_unconditionally_even_when_balance_fetch_fails(self):
        """
        O-1 (_check_position_size) should run even when balance fetch fails.
        O-3 (_check_rolling_drawdown) should be skipped when balance fetch fails.
        """
        runner = self._make_runner()

        active_bots = [
            (1, 'bot1', 'BTC/USDC', 'long', 'martingale', '{}', 100.0, 1, 70, 1, 10.0, 1.5, 'active'),
            (2, 'bot2', 'ETH/USDC', 'short', 'martingale', '{}', 200.0, 2, 65, 1, 10.0, 1.5, 'active'),
        ]

        with patch.object(runner, 'get_active_bots', return_value=active_bots), \
             patch.object(runner, '_check_position_size') as mock_o1, \
             patch.object(runner, '_check_rolling_drawdown') as mock_o3, \
             patch.object(runner, '_calculate_stablecoin_balance', return_value=0.0):
            runner.check_circuit_breaker(exchange_snapshot=None)

        # O-1 must run even though balance fetch failed
        mock_o1.assert_called_once()
        # O-3 must NOT run (no balance -> no equity)
        mock_o3.assert_not_called()

    def test_o1_and_o3_both_run_when_balance_fetch_succeeds(self):
        """When balance fetch succeeds, both O-1 and O-3 should run."""
        runner = self._make_runner()

        active_bots = [
            (1, 'bot1', 'BTC/USDC', 'long', 'martingale', '{}', 100.0, 1, 70, 1, 10.0, 1.5, 'active'),
        ]

        # Provide a snapshot so balance_fetch_success=True without needing exchanges
        exchange_snapshot = {
            'future': {
                'balance': {'total': {'USDT': 10000.0, 'USDC': 0.0}},
                'positions': []
            }
        }

        with patch.object(runner, 'get_active_bots', return_value=active_bots), \
             patch.object(runner, '_check_position_size') as mock_o1, \
             patch.object(runner, '_check_rolling_drawdown') as mock_o3, \
             patch('engine.runner.get_bot_status', return_value={'total_invested': 100.0}):
            runner.check_circuit_breaker(exchange_snapshot=exchange_snapshot)

        mock_o1.assert_called_once()
        mock_o3.assert_called_once()
        # A1 (2026-09-04): equity = wallet + uPnL. DB Cost EXCLUDED (futures
        # wallet already carries position cost in margin — adding DB Cost
        # double-counted open positions and caused the live 37.14% false fire).
        # equity = 10000 (balance) + 0 (uPnL) = 10000
        current_equity = mock_o3.call_args[0][0]
        assert current_equity == 10000.0, f"Expected equity 10000, got {current_equity}"

    def test_o1_and_o3_run_with_exchange_snapshot_and_upnl(self):
        """When exchange_snapshot has balance + positions, both run and equity includes uPnL."""
        runner = self._make_runner()

        active_bots = [
            (1, 'bot1', 'BTC/USDC', 'long', 'martingale', '{}', 100.0, 1, 70, 1, 10.0, 1.5, 'active'),
        ]

        exchange_snapshot = {
            'future': {
                'balance': {'total': {'USDT': 5000.0, 'USDC': 0.0}},
                'positions': [
                    {'symbol': 'BTC/USDC', 'unrealizedPnl': 50.0},
                    {'symbol': 'ETH/USDC', 'unrealizedPnl': -20.0},
                ]
            }
        }

        with patch.object(runner, 'get_active_bots', return_value=active_bots), \
             patch.object(runner, '_check_position_size') as mock_o1, \
             patch.object(runner, '_check_rolling_drawdown') as mock_o3, \
             patch('engine.runner.get_bot_status', return_value={'total_invested': 100.0}):
            runner.check_circuit_breaker(exchange_snapshot=exchange_snapshot)

        mock_o1.assert_called_once()
        mock_o3.assert_called_once()
        # A1 (2026-09-04): equity = wallet + uPnL (Cost excluded).
        # equity = 5000 (balance) + 30 (uPnL) = 5030
        current_equity = mock_o3.call_args[0][0]
        assert current_equity == 5030.0, f"Expected equity 5030, got {current_equity}"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])