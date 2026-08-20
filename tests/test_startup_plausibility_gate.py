"""Unit tests for the O-9 matched-pair plausibility gate (Step 2.5)."""
import types
import unittest
from unittest import mock
import sys, os
sys.path.append(os.getcwd())

from engine.runner.startup import StartupMixin


def _run_gate(active_bot_ids, status_map, exchange_nets, gate_enabled=True):
    """Invoke _startup_pair_plausibility_gate on a bare StartupMixin namespace,
    mocking get_bot_status and get_exchange_signed_net so NO DB/exchange is touched.
    Mirror of the Pattern E test's isolated unbound-method approach.
    """
    stub = types.SimpleNamespace()

    def fake_get_bot_status(bid):
        return status_map.get(bid)

    def fake_get_exchange_signed_net(exchange, pair):
        return exchange_nets.get(pair)

    with mock.patch("engine.runner.startup.config") as mconf:
        mconf.STARTUP_PLAUSIBILITY_GATE = gate_enabled
        with mock.patch("engine.database.get_bot_status", side_effect=fake_get_bot_status), \
             mock.patch("engine.parity_gates.get_exchange_signed_net", side_effect=fake_get_exchange_signed_net), \
             mock.patch("engine.parity_gates.qty_tolerance", return_value=0.002):
            return StartupMixin._startup_pair_plausibility_gate(stub, None, None, active_bot_ids)


class TestPlausibilityGate(unittest.TestCase):
    def test_db_real_exchange_flat_blocks(self):
        """DB holds a real position but exchange is flat -> block."""
        status = {1: {'pair': 'BNBUSDC', 'direction': 'LONG', 'open_qty': 1.5, 'is_active': True}}
        nets = {'BNBUSDC': 0.0}
        blocked = _run_gate([1], status, nets)
        self.assertEqual(blocked, {1})

    def test_db_flat_exchange_real_blocks(self):
        """Exchange holds a real position but DB is flat -> block."""
        status = {2: {'pair': 'SOLUSDC', 'direction': 'LONG', 'open_qty': 0.0, 'is_active': True}}
        nets = {'SOLUSDC': 3.7}
        blocked = _run_gate([2], status, nets)
        self.assertEqual(blocked, {2})

    def test_both_real_same_sign_ok(self):
        """Both real, same sign, within tolerance -> NOT blocked (no-op)."""
        status = {3: {'pair': 'ETHUSDC', 'direction': 'LONG', 'open_qty': 2.0, 'is_active': True}}
        nets = {'ETHUSDC': 2.0005}
        blocked = _run_gate([3], status, nets)
        self.assertEqual(blocked, set())

    def test_sign_mismatch_blocks(self):
        """DB long vs exchange short (one-way mode impossible) -> block."""
        status = {4: {'pair': 'BTCUSDC', 'direction': 'LONG', 'open_qty': 1.0, 'is_active': True}}
        nets = {'BTCUSDC': -1.0}
        blocked = _run_gate([4], status, nets)
        self.assertEqual(blocked, {4})

    def test_flat_both_ok(self):
        """Both flat -> not blocked (normal scanning bot, no position)."""
        status = {5: {'pair': 'SUIUSDC', 'direction': 'LONG', 'open_qty': 0.0, 'is_active': True}}
        nets = {'SUIUSDC': 0.0}
        blocked = _run_gate([5], status, nets)
        self.assertEqual(blocked, set())

    def test_gate_disabled_returns_empty(self):
        """STARTUP_PLAUSIBILITY_GATE=False -> no bots blocked."""
        status = {1: {'pair': 'BNBUSDC', 'direction': 'LONG', 'open_qty': 1.5, 'is_active': True}}
        nets = {'BNBUSDC': 0.0}
        blocked = _run_gate([1], status, nets, gate_enabled=False)
        self.assertEqual(blocked, set())

    def test_exchange_unavailable_defers(self):
        """physical None -> not gated (deferred to Step 8 strict audit)."""
        status = {6: {'pair': 'LINKUSDC', 'direction': 'LONG', 'open_qty': 1.0, 'is_active': True}}
        nets = {'LINKUSDC': None}
        blocked = _run_gate([6], status, nets)
        self.assertEqual(blocked, set())

    def test_inactive_or_missing_bot_skipped(self):
        """Inactive/missing bot status -> skipped (not gated, no crash)."""
        status = {7: {'pair': 'XRPUSDC', 'direction': 'LONG', 'open_qty': 1.0, 'is_active': False}}
        blocked = _run_gate([7], status, {})
        self.assertEqual(blocked, set())

    def test_short_db_position(self):
        """Short direction: signed net is negative; DB short vs flat exchange -> block."""
        status = {8: {'pair': 'ETHUSDC', 'direction': 'SHORT', 'open_qty': 2.0, 'is_active': True}}
        nets = {'ETHUSDC': 0.0}
        blocked = _run_gate([8], status, nets)
        self.assertEqual(blocked, {8})

    def test_mixed_bots_pair_level_isolation(self):
        """Only implausible bots blocked; plausible ones returned as not-blocked."""
        status = {
            1: {'pair': 'BNBUSDC', 'direction': 'LONG', 'open_qty': 1.5, 'is_active': True},
            3: {'pair': 'ETHUSDC', 'direction': 'LONG', 'open_qty': 2.0, 'is_active': True},
        }
        nets = {'BNBUSDC': 0.0, 'ETHUSDC': 2.0005}
        blocked = _run_gate([1, 3], status, nets)
        self.assertEqual(blocked, {1})


if __name__ == '__main__':
    unittest.main()