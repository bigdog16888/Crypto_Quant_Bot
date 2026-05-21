"""Tests for pair parity gates and cycle-reset blocking."""
import pytest
import uuid
import sqlite3
from engine import database
from engine.parity_gates import (
    assert_cycle_reset_allowed,
    CycleResetBlockedError,
    gate_trading_allowed,
    gate_maintain_orders_allowed,
    projected_pair_virtual_after_bot_flat,
    forensic_adopt_allowed,
    get_bot_signed_contribution,
    gate_heal_fill_qty,
    gate_heal_exit_without_entry,
    deflate_pair_ledger_overcount,
    pair_parity_ok,
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
        "VALUES (?, 1, 1, 0.54, 100, 10.0, 'SCANNING', 0, 'SHORT')",
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


def test_gate_maintain_allows_in_trade_despite_mismatch(memory_db):
    _setup_short_bot(memory_db, 106)
    ex = MockExchange({'LINK/USDC:USDC': -1.08})
    allowed, _ = gate_maintain_orders_allowed(
        106, 'LINK/USDC:USDC', ex, total_invested=100.0,
    )
    assert allowed is True


def test_gate_trading_blocks_mismatch(memory_db):
    _setup_short_bot(memory_db, 102)
    ex = MockExchange({'LINK/USDC:USDC': -1.08})
    allowed, reason = gate_trading_allowed(102, 'LINK/USDC:USDC', ex)
    assert allowed is False
    assert 'parity' in reason.lower() or 'virtual' in reason.lower()

    row = memory_db.execute("SELECT status FROM bots WHERE id=102").fetchone()
    assert row[0] == 'REQUIRE_MANUAL_PROOF'


def test_gate_heal_blocks_when_ledger_at_exchange(memory_db):
    _setup_short_bot(memory_db, 104)
    ex = MockExchange({'LINK/USDC:USDC': -0.54})
    assert gate_heal_fill_qty('LINK/USDC:USDC', 0.5, exchange=ex) == 0.0


def test_gate_heal_exit_without_entry(memory_db):
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair) "
        "VALUES (200, 'short sol', 'SOL/USDC:USDC', 'SHORT', 1, 'SOLUSDC')",
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, "
        "avg_entry_price, cycle_phase, wipe_wall_ts, position_side) "
        "VALUES (200, 31, 0, 0, 0, 0, 'SCANNING', 0, 'SHORT')",
    )
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, "
        "status, cycle_id, step, position_side, client_order_id) "
        "VALUES (200, 'tp', 'tp1', 85.0, 0.11, 0, 'filled', 31, 1, 'SHORT', 'CQB_200_TP_31_1')",
    )
    memory_db.commit()
    assert gate_heal_exit_without_entry(200, 'tp', 0.11) is False


def test_parity_pass_at_tolerance_boundary(memory_db):
    """0.008 vs 0.006 with tol 0.002 is IN parity — must not flag mismatch."""
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair) "
        "VALUES (108, 'long btc', 'BTC/USDC:USDC', 'LONG', 1, 'BTCUSDC')"
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, "
        "avg_entry_price, cycle_phase, wipe_wall_ts, position_side) "
        "VALUES (108, 1, 2, 0.008, 600, 75000, 'ACTIVE', 0, 'LONG')"
    )
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, "
        "status, cycle_id, step, position_side) "
        "VALUES (108, 'entry', 'btc_e1', 76000.0, 0.004, 0.004, 'filled', 1, 1, 'LONG'), "
        "       (108, 'grid', 'btc_g1', 76000.0, 0.004, 0.004, 'filled', 1, 2, 'LONG')"
    )
    memory_db.commit()
    ex = MockExchange({'BTC/USDC:USDC': 0.006})
    ok, v, p, d = pair_parity_ok('BTC/USDC:USDC', exchange=ex)
    assert ok is True
    assert abs(d) <= 0.002 + 1e-9
    assert deflate_pair_ledger_overcount(ex, 'BTC/USDC:USDC') is None


def test_deflate_when_excess_exceeds_tolerance(memory_db):
    """When excess > PAIR_PARITY_QTY_TOLERANCE, deflate must trim ledger to exchange."""
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair) "
        "VALUES (109, 'long btc', 'BTC/USDC:USDC', 'LONG', 1, 'BTCUSDC')"
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, "
        "avg_entry_price, cycle_phase, wipe_wall_ts, position_side) "
        "VALUES (109, 1, 2, 0.009, 600, 75000, 'ACTIVE', 0, 'LONG')"
    )
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, "
        "status, cycle_id, step, position_side) "
        "VALUES (109, 'entry', 'btc_e2', 76000.0, 0.005, 0.005, 'filled', 1, 1, 'LONG'), "
        "       (109, 'grid', 'btc_g2', 76000.0, 0.004, 0.004, 'filled', 1, 2, 'LONG')"
    )
    memory_db.commit()
    ex = MockExchange({'BTC/USDC:USDC': 0.006})
    assert database.get_pair_virtual_net('BTC/USDC:USDC') == pytest.approx(0.009, abs=1e-6)
    msg = deflate_pair_ledger_overcount(ex, 'BTC/USDC:USDC')
    assert msg is not None
    assert database.get_pair_virtual_net('BTC/USDC:USDC') == pytest.approx(0.006, abs=1e-6)


def test_deflate_pair_overcount(memory_db):
    _setup_short_bot(memory_db, 105)
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, "
        "status, cycle_id, step, position_side) "
        "VALUES (105, 'grid', 'g1', 10.0, 0.54, 0.54, 'filled', 1, 2, 'SHORT')",
    )
    memory_db.execute(
        "UPDATE trades SET open_qty = 1.08 WHERE bot_id = 105"
    )
    memory_db.commit()
    ex = MockExchange({'LINK/USDC:USDC': -0.54})
    assert database.get_pair_virtual_net('LINK/USDC:USDC') == pytest.approx(-1.08, abs=0.01)
    msg = deflate_pair_ledger_overcount(ex, 'LINK/USDC:USDC')
    assert msg is not None
    assert database.get_pair_virtual_net('LINK/USDC:USDC') == pytest.approx(-0.54, abs=0.01)


def test_bot_signed_contribution_matches_virtual(memory_db):
    _setup_short_bot(memory_db, 103)
    v = database.get_pair_virtual_net('LINK/USDC:USDC')
    c = get_bot_signed_contribution(103)
    assert v == pytest.approx(c, abs=1e-6)
