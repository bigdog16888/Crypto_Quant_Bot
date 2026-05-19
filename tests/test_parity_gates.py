"""Tests for pair parity gates and cycle-reset blocking."""
import pytest
import uuid
import sqlite3
from engine import database
from engine.parity_gates import (
    assert_cycle_reset_allowed,
    CycleResetBlockedError,
    gate_trading_allowed,
    projected_pair_virtual_after_bot_flat,
    forensic_adopt_allowed,
    get_bot_signed_contribution,
)


@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_parity_{db_id}?mode=memory&cache=shared'
    persistent_conn = orig_connect(shared_uri, uri=True)

    def mock_connect(db_path, *args, **kwargs):
        kwargs['uri'] = True
        return orig_connect(shared_uri, *args, **kwargs)

    sqlite3.connect = mock_connect
    if hasattr(database._local, 'connection'):
        database._local.connection = None

    database.DB_PATH = shared_uri
    database.init_db()
    conn = database.get_connection()
    conn.commit()
    yield conn

    persistent_conn.close()
    sqlite3.connect = orig_connect
    database.backup_database = orig_backup
    database.DB_PATH = orig_db_path
    if hasattr(database._local, 'connection'):
        database._local.connection = None


def _setup_short_bot(conn, bot_id, pair='LINK/USDC:USDC'):
    conn.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair) "
        "VALUES (?, 'short link', ?, 'SHORT', 1, 'LINKUSDC')",
        (bot_id, pair),
    )
    conn.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, "
        "avg_entry_price, cycle_phase, wipe_wall_ts, position_side) "
        "VALUES (?, 1, 1, 0, 100, 10.0, 'SCANNING', 0, 'SHORT')",
        (bot_id,),
    )
    conn.execute(
        "INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, "
        "status, cycle_id, step, position_side) "
        "VALUES (?, 'entry', 'e1', 10.0, 0.54, 0.54, 'filled', 1, 1, 'SHORT')",
        (bot_id,),
    )
    conn.commit()


class MockExchange:
    def __init__(self, nets):
        self._nets = nets

    def fetch_positions(self):
        out = []
        for sym, net in self._nets.items():
            if abs(net) < 1e-12:
                continue
            out.append({'symbol': sym, 'net_qty': net, 'contracts': net})
        return out


def test_forensic_adopt_disabled_by_default():
    assert forensic_adopt_allowed() is False


def test_cycle_reset_blocked_when_exchange_gap_remains(memory_db):
    _setup_short_bot(memory_db, 100)
    ex = MockExchange({'LINK/USDC:USDC': -1.08})
    assert database.get_pair_virtual_net('LINK/USDC:USDC') == pytest.approx(-0.54, abs=0.01)
    projected = projected_pair_virtual_after_bot_flat(100, 'LINK/USDC:USDC')
    assert projected == pytest.approx(0.0, abs=1e-6)

    with pytest.raises(CycleResetBlockedError):
        assert_cycle_reset_allowed(100, 'LINK/USDC:USDC', 'TP_HIT', exchange=ex)


def test_cycle_reset_allowed_after_manual_with_human(memory_db):
    _setup_short_bot(memory_db, 101)
    ex = MockExchange({'LINK/USDC:USDC': -1.08})
    assert_cycle_reset_allowed(
        101, 'LINK/USDC:USDC', 'MANUAL_CLOSE', human_approved=True, exchange=ex,
    )


def test_gate_trading_blocks_mismatch(memory_db):
    _setup_short_bot(memory_db, 102)
    ex = MockExchange({'LINK/USDC:USDC': -1.08})
    allowed, reason = gate_trading_allowed(102, 'LINK/USDC:USDC', ex)
    assert allowed is False
    assert 'parity' in reason.lower() or 'virtual' in reason.lower()

    row = memory_db.execute("SELECT status FROM bots WHERE id=102").fetchone()
    assert row[0] == 'REQUIRE_MANUAL_PROOF'


def test_bot_signed_contribution_matches_virtual(memory_db):
    _setup_short_bot(memory_db, 103)
    v = database.get_pair_virtual_net('LINK/USDC:USDC')
    c = get_bot_signed_contribution(103)
    assert v == pytest.approx(c, abs=1e-6)
