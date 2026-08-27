"""
Regression test for the reconcile_oneway_pair_open_qty sign bug (2026-08-26).

The bug: the function used `diff = virtual - physical` and trimmed when `diff > 0`.
For a net-SHORT pair where the DB UNDER-reports (exchange has more short than DB),
this sign convention incorrectly triggered a TRIM, destroying the ledger further
on every restart.

Real SOL numbers from the incident:
  - virtual (DB) = -9.12   (bot 100001 SHORT 14.3 + bot 100324 LONG 5.18)
  - physical (exchange) = -15.2
  - The exchange has MORE short (15.2 > 9.12) → this is an UNDER-report.
  - Buggy code: diff = -9.12 - (-15.2) = +6.08 > 0 → TRIMMED 6.08 (WRONG)
  - Fixed code: |virtual|=9.12 < |physical|=15.2 → under-report → NO trim, drift note
"""
import pytest
from unittest.mock import MagicMock, patch


class TestOneWayRepairSignBug:
    """Verify reconcile_oneway_pair_open_qty never trims on an under-report."""

    @pytest.fixture
    def mock_exchange(self):
        ex = MagicMock()
        ex.fetch_ticker.return_value = {'last': 96.5}
        return ex

    def _make_conn(self, bot_rows=None, cycle_id=26):
        """Build a mock DB connection with controllable query results."""
        conn = MagicMock()
        call_count = [0]

        def execute_side_effect(*args, **kwargs):
            call_count[0] += 1
            m = MagicMock()
            if call_count[0] == 1:
                # First query: bot list for trimming
                m.fetchall.return_value = bot_rows or []
            else:
                # Subsequent queries: cycle_id lookup etc.
                m.fetchone.return_value = (cycle_id,)
            return m

        conn.execute.side_effect = execute_side_effect
        return conn

    def test_sol_underreport_does_not_trim(self, mock_exchange):
        """
        SOL incident: virtual=-9.12, physical=-15.2 (both SHORT, exchange more short).
        This is an UNDER-report. Must NOT trim; must write drift note.
        The buggy code trimmed 6.08 here, corrupting bot 100001 on every restart.
        """
        from engine.oneway_netting import reconcile_oneway_pair_open_qty

        mock_conn = self._make_conn(bot_rows=[])

        with patch('engine.oneway_netting.get_pair_open_qty_net', return_value=-9.12), \
             patch('engine.parity_gates.get_exchange_signed_net', return_value=-15.2), \
             patch('engine.database.get_connection', return_value=mock_conn):

            result = reconcile_oneway_pair_open_qty(mock_exchange, 'SOL/USDC:USDC')

            # Must be flagged as under-report, NOT trimmed
            assert result is not None
            assert 'under-report' in result.lower() or 'drift' in result.lower()
            assert 'trimmed' not in result.lower()

    def test_short_overreport_trims_correctly(self, mock_exchange):
        """
        SHORT over-report: virtual=-20.38, physical=-15.2 (DB has MORE short than exchange).
        |virtual|=20.38 > |physical|=15.2 → over-report → trim the excess 5.18.
        """
        from engine.oneway_netting import reconcile_oneway_pair_open_qty

        # One SHORT bot with 20.38 open_qty
        mock_conn = self._make_conn(
            bot_rows=[(100001, 'SHORT', 'SOL/USDC:USDC', 'SOLUSDC', 20.38)]
        )

        with patch('engine.oneway_netting.get_pair_open_qty_net', return_value=-20.38), \
             patch('engine.parity_gates.get_exchange_signed_net', return_value=-15.2), \
             patch('engine.database.get_connection', return_value=mock_conn), \
             patch('engine.database.save_bot_order') as mock_save, \
             patch('engine.ledger.seal_trade_state'):

            result = reconcile_oneway_pair_open_qty(mock_exchange, 'SOL/USDC:USDC')

            # Should trim the SHORT bot's excess
            assert result is not None
            assert 'trimmed' in result.lower()
            assert mock_save.called

    def test_long_overreport_trims_correctly(self, mock_exchange):
        """
        LONG over-report: virtual=+20.38, physical=+15.2 (DB has MORE long than exchange).
        |virtual|=20.38 > |physical|=15.2 → over-report → trim excess 5.18.
        """
        from engine.oneway_netting import reconcile_oneway_pair_open_qty

        mock_conn = self._make_conn(
            bot_rows=[(100001, 'LONG', 'SOL/USDC:USDC', 'SOLUSDC', 20.38)]
        )

        with patch('engine.oneway_netting.get_pair_open_qty_net', return_value=20.38), \
             patch('engine.parity_gates.get_exchange_signed_net', return_value=15.2), \
             patch('engine.database.get_connection', return_value=mock_conn), \
             patch('engine.database.save_bot_order') as mock_save, \
             patch('engine.ledger.seal_trade_state'):

            result = reconcile_oneway_pair_open_qty(mock_exchange, 'SOL/USDC:USDC')

            assert result is not None
            assert 'trimmed' in result.lower()
            assert mock_save.called

    def test_parity_no_action(self, mock_exchange):
        """virtual == physical → no repair, returns None."""
        from engine.oneway_netting import reconcile_oneway_pair_open_qty

        with patch('engine.oneway_netting.get_pair_open_qty_net', return_value=-15.2), \
             patch('engine.parity_gates.get_exchange_signed_net', return_value=-15.2):
            result = reconcile_oneway_pair_open_qty(mock_exchange, 'SOL/USDC:USDC')
            assert result is None

    def test_sign_conflict_manual_review(self, mock_exchange):
        """
        Opposite signs: virtual=+5.0 (LONG), physical=-15.2 (SHORT).
        Cannot auto-repair safely → manual review.
        """
        from engine.oneway_netting import reconcile_oneway_pair_open_qty

        with patch('engine.oneway_netting.get_pair_open_qty_net', return_value=5.0), \
             patch('engine.parity_gates.get_exchange_signed_net', return_value=-15.2):
            result = reconcile_oneway_pair_open_qty(mock_exchange, 'SOL/USDC:USDC')
            assert result is not None
            assert 'sign conflict' in result.lower() or 'manual review' in result.lower()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
