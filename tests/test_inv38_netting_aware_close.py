"""
tests/test_inv38_netting_aware_close.py

Tests for INV-38: position-aware (netting-aware) closeable_qty computation
and the universal resolve_gated_bot() recovery function.

Covers:
  A. compute_closeable_qty() — all six cases
  B. resolve_gated_bot() — DB state transitions and guard assertions
"""
import sqlite3
import uuid
import sys
import os
import pytest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import engine.database as database
from engine.recovery import compute_closeable_qty, resolve_gated_bot, RESOLVABLE_STATUSES


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def memory_db():
    """Shared-memory SQLite seeded with the full schema via init_db."""
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_inv38_{db_id}?mode=memory&cache=shared'
    persistent_conn = orig_connect(shared_uri, uri=True)

    def mock_connect(db_path, *args, **kwargs):
        kwargs['uri'] = True
        return orig_connect(shared_uri, *args, **kwargs)

    sqlite3.connect = mock_connect
    if hasattr(database._local, 'connection'):
        database._local.connection = None

    database.DB_PATH = shared_uri
    database.init_db()
    yield database.get_connection()

    sqlite3.connect = orig_connect
    database.DB_PATH = orig_db_path
    database.backup_database = orig_backup
    persistent_conn.close()


def _seed(conn, bot_id, direction, status, open_qty, cycle_id=1):
    conn.execute(
        "INSERT OR REPLACE INTO bots "
        "(id, name, pair, normalized_pair, direction, is_active, status, bot_type) "
        "VALUES (?, ?, 'ETH/USDC:USDC', 'ETHUSDC', ?, 1, ?, 'hedge_child')",
        (bot_id, f'test_bot_{bot_id}', direction, status)
    )
    existing = conn.execute("SELECT bot_id FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE trades SET open_qty=?, cycle_id=? WHERE bot_id=?",
            (open_qty, cycle_id, bot_id)
        )
    else:
        conn.execute(
            "INSERT INTO trades (bot_id, open_qty, cycle_id, total_invested, "
            "avg_entry_price, current_step, entry_confirmed) VALUES (?,?,?,0,0,1,1)",
            (bot_id, open_qty, cycle_id)
        )
    conn.commit()



def _mock_exchange(net_qty: float, order_result: dict = None):
    """Build a mock exchange reporting a fixed signed net; fills orders on create_order."""
    ex = MagicMock()
    ex.fetch_positions.return_value = [{
        'symbol': 'ETH/USDC:USDC',
        'net_qty': net_qty,
        'contracts': net_qty,
        'side': 'long' if net_qty >= 0 else 'short',
        'qty': abs(net_qty),
        'unrealizedPnl': 0.0,
        'entryPrice': 1800.0,
    }]
    if order_result is None:
        order_result = {
            'id': 'exch_001', 'filled': abs(net_qty), 'average': 1800.0, 'status': 'closed'
        }
    ex.create_order.return_value = order_result
    return ex


# ── A. compute_closeable_qty ──────────────────────────────────────────────────

class TestComputeCloseableQty:

    def test_long_bot_short_net_zero_closeable(self):
        """LONG bot, SHORT net → closeable_qty=0. No SELL reduceOnly possible."""
        assert compute_closeable_qty('LONG', 0.495, -0.256) == 0.0

    def test_long_bot_long_net_full_closeable(self):
        """LONG bot, net >= virtual → closeable_qty = virtual_qty."""
        assert compute_closeable_qty('LONG', 0.495, 0.495) == 0.495

    def test_long_bot_partial_long_net(self):
        """LONG bot, net < virtual → closeable_qty = live_net."""
        assert compute_closeable_qty('LONG', 0.833, 0.239) == 0.239

    def test_short_bot_long_net_zero_closeable(self):
        """SHORT bot, LONG net → closeable_qty=0. No BUY reduceOnly possible."""
        assert compute_closeable_qty('SHORT', 1.063, 0.239) == 0.0

    def test_short_bot_short_net_full_closeable(self):
        """SHORT bot, |net| >= virtual → closeable_qty = virtual_qty."""
        assert compute_closeable_qty('SHORT', 0.5, -1.0) == 0.5

    def test_exact_zero_net(self):
        """Net=0 → closeable_qty=0 for either direction."""
        assert compute_closeable_qty('LONG', 0.3, 0.0) == 0.0
        assert compute_closeable_qty('SHORT', 0.3, 0.0) == 0.0


# ── B. resolve_gated_bot ──────────────────────────────────────────────────────

class TestResolveGatedBot:

    def test_closeable_zero_skips_exchange_order(self, memory_db):
        """
        LONG bot + SHORT exchange net: closeable_qty=0.
        No exchange order should be placed. safe_wipe_bot should be called.
        """
        bot_id = 99001
        _seed(memory_db, bot_id, 'LONG', 'REQUIRE_MANUAL_PROOF', open_qty=0.495)
        ex = _mock_exchange(net_qty=-0.256)  # SHORT net → closeable=0

        with patch('engine.database.safe_wipe_bot', return_value=True) as mock_wipe:
            result = resolve_gated_bot(
                bot_id=bot_id,
                exchange=ex,
                action_label='AUTO_NET_CLOSE',
                reason='test: unphysical LONG, net is SHORT',
                human_approved=True,
            )

        # No exchange order must have been placed
        ex.create_order.assert_not_called()
        # safe_wipe_bot must have been called
        mock_wipe.assert_called_once()
        assert result['closeable_qty'] == 0.0
        assert result['unphysical_remainder'] == 0.495
        assert result['wiped'] is True

    def test_full_closeable_places_exchange_order(self, memory_db):
        """
        LONG bot + full LONG exchange net: closeable_qty == virtual_qty.
        Exchange order must be placed for the full amount.
        """
        bot_id = 99002
        _seed(memory_db, bot_id, 'LONG', 'REQUIRE_MANUAL_PROOF', open_qty=0.495)
        ex = _mock_exchange(net_qty=0.495, order_result={
            'id': 'ord2', 'filled': 0.495, 'average': 1800.0, 'status': 'closed'
        })

        with patch('engine.database.safe_wipe_bot', return_value=True):
            result = resolve_gated_bot(
                bot_id=bot_id,
                exchange=ex,
                action_label='AUTO_NET_CLOSE',
                reason='test: full physical',
                human_approved=True,
            )

        ex.create_order.assert_called_once()
        _pair, _type, side, qty = ex.create_order.call_args[0][:4]
        assert side == 'sell'
        assert qty == 0.495
        assert result['closeable_qty'] == 0.495
        assert result['unphysical_remainder'] == 0.0

    def test_partial_closeable_places_partial_order(self, memory_db):
        """
        LONG bot + smaller LONG exchange net: exchange order for closeable_qty only.
        """
        bot_id = 99003
        _seed(memory_db, bot_id, 'LONG', 'REQUIRE_MANUAL_PROOF', open_qty=0.833)
        ex = _mock_exchange(net_qty=0.239, order_result={
            'id': 'ord3', 'filled': 0.239, 'average': 1800.0, 'status': 'closed'
        })

        with patch('engine.database.safe_wipe_bot', return_value=True):
            result = resolve_gated_bot(
                bot_id=bot_id,
                exchange=ex,
                action_label='AUTO_NET_CLOSE',
                reason='test: partial physical',
                human_approved=True,
            )

        ex.create_order.assert_called_once()
        _, _, _, qty = ex.create_order.call_args[0][:4]
        assert qty == 0.239
        assert result['closeable_qty'] == 0.239
        assert abs(result['unphysical_remainder'] - 0.594) < 1e-6

    def test_rejects_non_resolvable_status(self, memory_db):
        """Calling resolve_gated_bot on a non-gated bot raises ValueError."""
        bot_id = 99004
        _seed(memory_db, bot_id, 'LONG', 'IN TRADE', open_qty=0.5)
        ex = _mock_exchange(net_qty=0.5)

        with pytest.raises(ValueError, match="not in resolvable set"):
            resolve_gated_bot(
                bot_id=bot_id,
                exchange=ex,
                action_label='AUTO_NET_CLOSE',
                reason='test',
                human_approved=True,
            )

    def test_rejects_missing_exchange(self, memory_db):
        """Calling without an exchange object raises ValueError."""
        bot_id = 99005
        _seed(memory_db, bot_id, 'LONG', 'REQUIRE_MANUAL_PROOF', open_qty=0.5)

        with pytest.raises(ValueError, match="exchange object required"):
            resolve_gated_bot(
                bot_id=bot_id,
                exchange=None,
                action_label='AUTO_NET_CLOSE',
                reason='test',
            )

    def test_closeable_zero_live_parity_and_gating(self, memory_db):
        """
        Integration test verifying a real closeable_qty = 0 wipe, without patching safe_wipe_bot.
        Confirms database changes occur, and validates parity_gates.py's behaviour during the drift.
        """
        from engine.parity_gates import gate_trading_allowed, gate_maintain_orders_allowed

        # 1. Seed two sibling bots on the same pair
        # Bot 1: LONG hedge-child (status REQUIRE_MANUAL_PROOF, open_qty 0.495)
        bot_long = 99101
        _seed(memory_db, bot_long, 'LONG', 'REQUIRE_MANUAL_PROOF', open_qty=0.495)

        # Bot 2: SHORT sibling bot (status IN TRADE, open_qty 0.751)
        bot_short = 99102
        memory_db.execute(
            "INSERT OR REPLACE INTO bots "
            "(id, name, pair, normalized_pair, direction, is_active, status, bot_type) "
            "VALUES (?, ?, 'ETH/USDC:USDC', 'ETHUSDC', 'SHORT', 1, 'IN TRADE', 'standard')",
            (bot_short, f'test_bot_{bot_short}')
        )
        memory_db.execute(
            "INSERT OR REPLACE INTO trades (bot_id, open_qty, cycle_id, total_invested, "
            "avg_entry_price, current_step, entry_confirmed) VALUES (?, 0.751, 1, 100.0, 1800.0, 1, 1)",
            (bot_short,)
        )
        memory_db.commit()

        # 2. Mock exchange position to show net -0.256 SHORT
        ex = _mock_exchange(net_qty=-0.256)

        # 3. Call resolve_gated_bot on the LONG bot
        with patch('engine.database.get_connection', return_value=memory_db), \
             patch('engine.parity_gates.get_exchange_signed_net', return_value=-0.256):


            result = resolve_gated_bot(
                bot_id=bot_long,
                exchange=ex,
                action_label='AUTO_NET_CLOSE',
                reason='test: real closeable=0 integration',
                human_approved=True,
            )

            # Verify the return details from resolve_gated_bot
            assert result['closeable_qty'] == 0.0
            assert result['unphysical_remainder'] == 0.495
            assert result['wiped'] is True

            # Verify bot_long trades table has open_qty=0
            long_qty = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id=?", (bot_long,)).fetchone()[0]
            assert long_qty == 0.0

            # Verify bot_long status has been reset to hedge_standby (designed for hedge-child bots)
            long_status = memory_db.execute("SELECT status FROM bots WHERE id=?", (bot_long,)).fetchone()[0]
            assert long_status == 'hedge_standby'


            # 4. Check the Parity Gates behaviour on the healthy sibling bot during the resulting drift
            # Wiping bot_long makes the virtual net go from -0.256 to -0.751.
            # Physical net on exchange remains -0.256.
            # The gap is |-0.256 - (-0.751)| = 0.495, which exceeds tolerance (0.002)

            # (A) gate_trading_allowed for sibling bot: should block entries and gate status to REQUIRE_MANUAL_PROOF
            allowed, reason = gate_trading_allowed(bot_short, 'ETH/USDC:USDC', exchange=ex)
            assert allowed is False
            assert "Pair parity gate" in reason

            # Verify the gate modified sibling status to REQUIRE_MANUAL_PROOF
            short_status = memory_db.execute("SELECT status FROM bots WHERE id=?", (bot_short,)).fetchone()[0]
            assert short_status == 'REQUIRE_MANUAL_PROOF'

            # (B) gate_maintain_orders_allowed for sibling bot:
            # Let's restore sibling status back to 'IN TRADE' for maintenance check
            memory_db.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (bot_short,))
            memory_db.commit()

            # Since virtual (-0.751) and physical (-0.256) are both negative, signs match.
            # And sibling bot is in trade (invested = 100.0 > 0.01).
            # So gate_maintain_orders_allowed should allow maintenance (True) despite the drift!
            m_allowed, m_reason = gate_maintain_orders_allowed(bot_short, 'ETH/USDC:USDC', exchange=ex, total_invested=100.0)
            assert m_allowed is True

