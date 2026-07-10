import pytest
import time
import uuid
import sqlite3
from engine import database
from engine.parity_gates import flag_orphan_fill_manual_proof, gate_trading_allowed, gate_maintain_orders_allowed

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


@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_grace_{db_id}?mode=memory&cache=shared'
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


def test_flag_orphan_fill_ignores_gating_on_recent_fill(memory_db, caplog):
    import logging
    logging.getLogger("engine.parity_gates").setLevel(logging.INFO)
    caplog.set_level(logging.INFO, logger="engine.parity_gates")
    # Insert a bot
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair, status) "
        "VALUES (2000, 'test_bot', 'BTC/USDC:USDC', 'LONG', 1, 'BTCUSDC', 'IN TRADE')"
    )
    # Insert a very recent filled order (updated_at = current time)
    now_ts = int(time.time())
    memory_db.execute(
        """INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, 
           status, cycle_id, step, position_side, created_at, updated_at) 
           VALUES (2000, 'grid', 'o123', 60000.0, 1.0, 1.0, 'filled', 1, 1, 'LONG', ?, ?)""",
        (now_ts - 5, now_ts)
    )
    memory_db.commit()

    # Call flag_orphan_fill_manual_proof
    flag_orphan_fill_manual_proof(2000, 'orphan_order', 'BTC/USDC:USDC', 1.0, 'test_source')

    # Verify that the bot is NOT gated (remains 'IN TRADE')
    row = memory_db.execute("SELECT status FROM bots WHERE id=2000").fetchone()
    assert row[0] == 'IN TRADE'
    
    # Confirm the grace log message is in logs
    assert any("[PROOF-GRACE]" in record.message for record in caplog.records)


def test_flag_orphan_fill_gates_when_no_recent_fill(memory_db):
    # Insert a bot
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair, status) "
        "VALUES (2001, 'test_bot2', 'BTC/USDC:USDC', 'LONG', 1, 'BTCUSDC', 'IN TRADE')"
    )
    # Insert a stale filled order (updated_at = current time - 120s)
    now_ts = int(time.time())
    memory_db.execute(
        """INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, 
           status, cycle_id, step, position_side, created_at, updated_at) 
           VALUES (2001, 'grid', 'o124', 60000.0, 1.0, 1.0, 'filled', 1, 1, 'LONG', ?, ?)""",
        (now_ts - 130, now_ts - 120)
    )
    memory_db.commit()

    from unittest.mock import patch
    with patch("engine.parity_gates.get_exchange_signed_net", return_value=99.0):
        # Call flag_orphan_fill_manual_proof
        flag_orphan_fill_manual_proof(2001, 'orphan_order2', 'BTC/USDC:USDC', 1.0, 'test_source')

    # Verify that the bot IS gated (status -> 'REQUIRE_MANUAL_PROOF')
    row = memory_db.execute("SELECT status FROM bots WHERE id=2001").fetchone()
    assert row[0] == 'REQUIRE_MANUAL_PROOF'



def test_gate_maintain_orders_allowed_skips_gate_on_recent_fill(memory_db, caplog):
    import logging
    logging.getLogger("engine.parity_gates").setLevel(logging.INFO)
    caplog.set_level(logging.INFO, logger="engine.parity_gates")

    # Insert a bot
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair, status) "
        "VALUES (2002, 'test_bot3', 'BTC/USDC:USDC', 'LONG', 1, 'BTCUSDC', 'IN TRADE')"
    )
    # Insert trades row to set virtual net to 1.5
    memory_db.execute(
        """INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, 
           avg_entry_price, cycle_phase, wipe_wall_ts, position_side) 
           VALUES (2002, 1, 1, 1.5, 100, 10.0, 'SCANNING', 0, 'LONG')"""
    )
    # Insert a very recent filled order (updated_at = current time) to trigger the grace period
    now_ts = int(time.time())
    memory_db.execute(
        """INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, 
           status, cycle_id, step, position_side, created_at, updated_at) 
           VALUES (2002, 'grid', 'o125', 60000.0, 1.0, 1.0, 'filled', 1, 1, 'LONG', ?, ?)""",
        (now_ts - 5, now_ts)
    )
    memory_db.commit()

    # Mock exchange has physical position = 0.5 (mismatch from virtual 1.5)
    ex = MockExchange({'BTC/USDC:USDC': 0.5})

    # Call gate_maintain_orders_allowed with total_invested = 0.0 to fall through to trading gate
    allowed, reason = gate_maintain_orders_allowed(2002, 'BTC/USDC:USDC', exchange=ex, total_invested=0.0)

    # Verify that it is NOT allowed to maintain/enter because parity is off
    assert allowed is False
    # Verify that the bot is NOT gated (remains 'IN TRADE')
    row = memory_db.execute("SELECT status FROM bots WHERE id=2002").fetchone()
    assert row[0] == 'IN TRADE'
    # Confirm the grace log message is in logs
    assert any("[PROOF-GRACE]" in record.message for record in caplog.records)


def test_gate_trading_allowed_skips_gate_on_recent_fill(memory_db, caplog):
    import logging
    logging.getLogger("engine.parity_gates").setLevel(logging.INFO)
    caplog.set_level(logging.INFO, logger="engine.parity_gates")

    # Insert a bot
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair, status) "
        "VALUES (2003, 'test_bot4', 'BTC/USDC:USDC', 'LONG', 1, 'BTCUSDC', 'IN TRADE')"
    )
    # Insert trades row to set virtual net to 1.5
    memory_db.execute(
        """INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, 
           avg_entry_price, cycle_phase, wipe_wall_ts, position_side) 
           VALUES (2003, 1, 1, 1.5, 100, 10.0, 'SCANNING', 0, 'LONG')"""
    )
    # Insert a very recent filled order (updated_at = current time) to trigger the grace period
    now_ts = int(time.time())
    memory_db.execute(
        """INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, 
           status, cycle_id, step, position_side, created_at, updated_at) 
           VALUES (2003, 'grid', 'o126', 60000.0, 1.0, 1.0, 'filled', 1, 1, 'LONG', ?, ?)""",
        (now_ts - 5, now_ts)
    )
    memory_db.commit()

    # Mock exchange has physical position = 0.5 (mismatch from virtual 1.5)
    ex = MockExchange({'BTC/USDC:USDC': 0.5})

    # Call gate_trading_allowed
    allowed, reason = gate_trading_allowed(2003, 'BTC/USDC:USDC', exchange=ex)

    # Verify that it is NOT allowed to enter because parity is off
    assert allowed is False
    # Verify that the bot is NOT gated (remains 'IN TRADE')
    row = memory_db.execute("SELECT status FROM bots WHERE id=2003").fetchone()
    assert row[0] == 'IN TRADE'
    # Confirm the grace log message is in logs
    assert any("[PROOF-GRACE]" in record.message for record in caplog.records)


# ─── v5.3.4: size-cap regression tests ───────────────────────────────────────


def test_pair_has_recent_fill_size_cap_blocks_grace_for_large_gap(memory_db):
    """gap > 20 units + recent fill → pair_has_recent_fill returns False (grace bypassed)."""
    from engine.parity_gates import pair_has_recent_fill

    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair, status) "
        "VALUES (2010, 'cap_bot_large', 'BTC/USDC:USDC', 'LONG', 1, 'BTCUSDC', 'IN TRADE')"
    )
    now_ts = int(time.time())
    memory_db.execute(
        """INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount,
           status, cycle_id, step, position_side, created_at, updated_at)
           VALUES (2010, 'grid', 'cap_o1', 60000.0, 25.0, 25.0, 'filled', 1, 1, 'LONG', ?, ?)""",
        (now_ts - 5, now_ts)   # very recent fill — within 60s window
    )
    memory_db.commit()

    # Gap of 25 units > max_gap_units=20 → grace must be bypassed
    result = pair_has_recent_fill(
        memory_db,
        bot_ids=[2010],
        window_seconds=60,
        max_gap_units=20.0,
        gap_abs=25.0,       # > 20 → should return False regardless of recent fill
    )
    assert result is False, (
        "pair_has_recent_fill must return False when gap_abs (25.0) > max_gap_units (20.0), "
        "even though a recent fill exists within the window."
    )


def test_pair_has_recent_fill_size_cap_allows_grace_for_small_gap(memory_db):
    """gap ≤ 20 units + recent fill → pair_has_recent_fill returns True (grace holds)."""
    from engine.parity_gates import pair_has_recent_fill

    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair, status) "
        "VALUES (2011, 'cap_bot_small', 'BTC/USDC:USDC', 'LONG', 1, 'BTCUSDC', 'IN TRADE')"
    )
    now_ts = int(time.time())
    memory_db.execute(
        """INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount,
           status, cycle_id, step, position_side, created_at, updated_at)
           VALUES (2011, 'grid', 'cap_o2', 60000.0, 0.5, 0.5, 'filled', 1, 1, 'LONG', ?, ?)""",
        (now_ts - 5, now_ts)   # very recent fill — within 60s window
    )
    memory_db.commit()

    # Gap of 0.5 units < max_gap_units=20 → grace should hold
    result = pair_has_recent_fill(
        memory_db,
        bot_ids=[2011],
        window_seconds=60,
        max_gap_units=20.0,
        gap_abs=0.5,        # ≤ 20 → should return True (fill is recent)
    )
    assert result is True, (
        "pair_has_recent_fill must return True when gap_abs (0.5) ≤ max_gap_units (20.0) "
        "and a recent fill exists within the window."
    )


def test_flag_orphan_fill_gates_for_large_gap_even_with_recent_fill(memory_db):
    """orphan fill with qty > 20 units + recent timestamp → must still gate (REQUIRE_MANUAL_PROOF), not grace."""
    from unittest.mock import patch

    # Insert a bot
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair, status) "
        "VALUES (2012, 'cap_bot_large_orphan', 'BTC/USDC:USDC', 'LONG', 1, 'BTCUSDC', 'IN TRADE')"
    )
    # Insert a very recent filled order (updated_at = current time)
    now_ts = int(time.time())
    memory_db.execute(
        """INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount,
           status, cycle_id, step, position_side, created_at, updated_at)
           VALUES (2012, 'grid', 'cap_o3', 60000.0, 1.0, 1.0, 'filled', 1, 1, 'LONG', ?, ?)""",
        (now_ts - 5, now_ts)
    )
    memory_db.commit()

    # Call flag_orphan_fill_manual_proof with qty = 25.0 (> 20.0 cap)
    # Patch get_exchange_signed_net to make sure bypass-gate doesn't fire
    with patch("engine.parity_gates.get_exchange_signed_net", return_value=99.0):
        flag_orphan_fill_manual_proof(2012, 'orphan_order_large', 'BTC/USDC:USDC', 25.0, 'test_source')

    # Verify that the bot IS gated (status -> 'REQUIRE_MANUAL_PROOF') because qty > 20 units cap bypassed grace
    row = memory_db.execute("SELECT status FROM bots WHERE id=2012").fetchone()
    assert row[0] == 'REQUIRE_MANUAL_PROOF'



