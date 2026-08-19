"""Unit tests for the startup-barrier foreign-symbol diagnostic (AC#2, t_1e3d4f64).

The 2026-08-18 incident: the demo/testnet account was reset overnight and seeded
with USDS-M synthetic positions (BNBUSD, BTCUSD, ...) that do NOT match the bot's
trading symbols (BNB/USDC:USDC, ...). normalize_symbol() strips separators, so
BNB/USDC:USDC -> BNBUSDC while the seed row BNBUSD -> BNBUSD: different keys.
The pair audit (audit_pair_ledger_vs_exchange) only iterates bot pairs, so those
foreign symbols were invisible in the barrier log.

_classify_foreign_positions() splits live exchange positions into symbols that
match an active bot pair vs foreign symbols, so the barrier can surface them.
It is strictly read-only: it never modifies the DB or the exchange.

Design note: the helper is invoked as an unbound StartupMixin method on a bare
namespace object. This deliberately avoids instantiating BotRunner, whose
__init__ runs schema migrations against the production DB — the helper itself
needs no runner state, so this keeps the test fully isolated (no DB, no network).
"""
import types
import unittest
import os
import sys

sys.path.append(os.getcwd())

from engine.runner.startup import StartupMixin


class _FakeExchange:
    """Minimal stand-in: only fetch_positions is exercised by the helper."""
    def __init__(self, positions):
        self._positions = positions
        self.fetch_calls = 0

    def fetch_positions(self, symbols=None):
        self.fetch_calls += 1
        return self._positions


def _classify(exchange, pairs):
    """Invoke the mixin helper without constructing a BotRunner (no DB touch)."""
    stub = types.SimpleNamespace()
    return StartupMixin._classify_foreign_positions(stub, exchange, pairs)


class TestForeignSymbolClassification(unittest.TestCase):
    def test_foreign_seed_rows_are_flagged_not_matched(self):
        """USDS-M synthetic seed rows (BNBUSD/BTCUSD) must land in 'foreign'."""
        ex = _FakeExchange([
            # Foreign synthetic seed rows (USDS-M, no matching bot pair)
            {'symbol': 'BNBUSD', 'contracts': 227.0, 'net_qty': 227.0, 'side': 'long'},
            {'symbol': 'BTCUSD', 'contracts': 778.0, 'net_qty': 778.0, 'side': 'long'},
            {'symbol': 'NEARUSD', 'contracts': -4.0, 'net_qty': -4.0, 'side': 'short'},
            # A real bot-pair position
            {'symbol': 'BNB/USDC:USDC', 'contracts': 0.01, 'net_qty': 0.01, 'side': 'long'},
        ])
        pairs = ['BNB/USDC:USDC', 'BTC/USDC:USDC']

        foreign, matched = _classify(ex, pairs)

        foreign_norms = {n for _, n, _ in foreign}
        matched_norms = {n for _, n, _ in matched}
        self.assertEqual(foreign_norms, {'BNBUSD', 'BTCUSD', 'NEARUSD'})
        self.assertEqual(matched_norms, {'BNBUSDC'})
        # Signed nets preserved
        fdict = {n: net for _, n, net in foreign}
        self.assertAlmostEqual(fdict['NEARUSD'], -4.0)
        self.assertAlmostEqual(fdict['BNBUSD'], 227.0)

    def test_zero_qty_positions_are_ignored(self):
        """positionAmt=0 rows (the post-reset state) must not appear at all."""
        ex = _FakeExchange([
            {'symbol': 'BNBUSD', 'contracts': 0.0, 'net_qty': 0.0, 'side': 'long'},
            {'symbol': 'BTCUSD', 'contracts': 0, 'net_qty': 0, 'side': 'short'},
        ])
        foreign, matched = _classify(ex, ['BNB/USDC:USDC'])
        self.assertEqual(foreign, [])
        self.assertEqual(matched, [])

    def test_empty_and_none_positions(self):
        self.assertEqual(_classify(_FakeExchange([]), ['X/USDC:USDC']), ([], []))
        self.assertEqual(_classify(_FakeExchange(None), ['X/USDC:USDC']), ([], []))

    def test_fetch_failure_returns_empty_not_raise(self):
        """A fetch_positions exception must degrade gracefully (read-only diagnostic)."""
        class _Boom(_FakeExchange):
            def fetch_positions(self, symbols=None):
                raise RuntimeError("network down")

        foreign, matched = _classify(_Boom([]), ['BNB/USDC:USDC'])
        self.assertEqual(foreign, [])
        self.assertEqual(matched, [])

    def test_side_fallback_when_net_qty_missing(self):
        """If net_qty/contracts absent, derive signed net from qty+side."""
        ex = _FakeExchange([
            {'symbol': 'ETHUSD', 'qty': 195.0, 'side': 'long'},
            {'symbol': 'ETCUSD', 'qty': 37.0, 'side': 'short'},
        ])
        foreign, matched = _classify(ex, ['BNB/USDC:USDC'])
        fdict = {n: net for _, n, net in foreign}
        self.assertAlmostEqual(fdict['ETHUSD'], 195.0)
        self.assertAlmostEqual(fdict['ETCUSD'], -37.0)

    def test_helper_is_pure_readonly(self):
        """The helper must not call any mutating exchange/DB method."""
        ex = _FakeExchange([{'symbol': 'BNBUSD', 'net_qty': 1.0, 'side': 'long'}])
        # Only fetch_positions should be touched.
        _classify(ex, ['BNB/USDC:USDC'])
        self.assertEqual(ex.fetch_calls, 1)


if __name__ == '__main__':
    unittest.main()
