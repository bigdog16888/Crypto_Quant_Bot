"""
tests/test_stale_whitelist_cleanup.py — Test suite for ADR-006 Independent Accounting and whitelist auto-cleanup.
"""

import pytest
import uuid
import sqlite3
from unittest.mock import MagicMock, patch
from engine import database
from engine.database import get_pair_virtual_net, recompute_invested_from_orders
from engine.ledger import seal_trade_state
from engine.reconciler import StateReconciler


@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_ow_net_{db_id}?mode=memory&cache=shared'
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


def _seed_bot(conn, bot_id, pair, direction, open_qty=0.0, cycle=1):
    conn.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status, bot_type) "
        "VALUES (?, ?, ?, ?, ?, 1, 'IN TRADE', 'standard')",
        (bot_id, f'bot_{bot_id}', pair, 'BTCUSDC', direction),
    )
    conn.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, wipe_wall_ts, position_side) "
        "VALUES (?, ?, ?, 0, ?)",
        (bot_id, cycle, open_qty, direction),
    )
    conn.commit()


def test_independent_accounting_long_short_same_pair(memory_db):
    """
    Simulate long btc price filling 0.10 BTC and short btc filling 0.05 BTC independently.
    Assert NEITHER bot's open_qty references or depends on the other's fills.
    Assert get_pair_virtual_net correctly sums to +0.05.
    """
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.0, cycle=1)
    _seed_bot(memory_db, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.0, cycle=1)

    # Insert entry fill for LONG bot (0.10 BTC)
    memory_db.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (10016, 'entry', 'e_long_1', 'CQB_10016_ENTRY_1', 60000.0, 0.10, 0.10, 'filled', 1, 1, 'LONG')
    """)
    # Insert entry fill for SHORT bot (0.05 BTC)
    memory_db.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (10022, 'entry', 'e_short_1', 'CQB_10022_ENTRY_1', 60000.0, 0.05, 0.05, 'filled', 1, 1, 'SHORT')
    """)
    memory_db.commit()

    # Seal both bots
    seal_trade_state(10016, force_recompute=True)
    seal_trade_state(10022, force_recompute=True)

    # Check database open_qty values
    long_oq = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id=10016").fetchone()[0]
    short_oq = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id=10022").fetchone()[0]

    assert long_oq == pytest.approx(0.10)
    assert short_oq == pytest.approx(0.05)

    # Virtual net should sum to +0.05 (LONG is +, SHORT is -)
    assert get_pair_virtual_net('BTC/USDC:USDC') == pytest.approx(0.05)


def test_no_virtual_netting_rows_written(memory_db):
    """
    Run a full fill-credit cycle for two opposite-direction bots on the same pair.
    Assert ZERO rows with order_type='virtual_netting' are created.
    """
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.0, cycle=1)
    _seed_bot(memory_db, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.0, cycle=1)

    # Insert open order rows first
    memory_db.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (10016, 'entry', 'e_long_fill_1', 'CQB_10016_ENTRY_1', 60000.0, 0.10, 0.0, 'open', 1, 1, 'LONG')
    """)
    memory_db.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (10022, 'entry', 'e_short_fill_1', 'CQB_10022_ENTRY_1', 60000.0, 0.05, 0.0, 'open', 1, 1, 'SHORT')
    """)
    memory_db.commit()

    from engine.ledger import credit_fill

    mock_exchange = MagicMock()
    # Credit entry fill on LONG bot
    credit_fill(
        bot_id=10016,
        order_id='e_long_fill_1',
        avg_price=60000.0,
        cumulative_qty=0.10,
        order_type='entry',
        exchange=mock_exchange,
    )
    # Credit entry fill on SHORT bot
    credit_fill(
        bot_id=10022,
        order_id='e_short_fill_1',
        avg_price=60000.0,
        cumulative_qty=0.05,
        order_type='entry',
        exchange=mock_exchange,
    )

    # Assert no virtual netting rows exist in the DB
    vn_count = memory_db.execute(
        "SELECT COUNT(*) FROM bot_orders WHERE order_type='virtual_netting'"
    ).fetchone()[0]
    assert vn_count == 0


def test_archived_legacy_rows_ignored_in_recompute(memory_db):
    """
    A bot with historical archived_legacy virtual_netting rows recomputes
    open_qty correctly from only its real entry/exit fills.
    """
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.0, cycle=1)

    # Insert a real entry fill (0.10 BTC)
    memory_db.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (10016, 'entry', 'e_long_1', 'CQB_10016_ENTRY_1', 60000.0, 0.10, 0.10, 'filled', 1, 1, 'LONG')
    """)
    # Insert an archived legacy virtual netting row (which historically exits 0.04 BTC)
    memory_db.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (10016, 'virtual_netting', 'VN_long_1', 'CQB_10016_VNET_1', 60000.0, 0.04, 0.04, 'archived_legacy', 1, 0, 'LONG')
    """)
    memory_db.commit()

    # Recompute invested/open_qty
    cost, avg, qty, step = recompute_invested_from_orders(10016)

    # Quantities of 'archived_legacy' rows should be ignored, so qty should remain 0.10, not 0.06
    assert qty == pytest.approx(0.10)


def test_migration_archives_existing_virtual_netting_rows(memory_db):
    """
    Run the migration on a DB containing virtual_netting rows, confirm they
    become archived_legacy, confirm seal_trade_state then produces
    correct open_qty for affected bots.
    """
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.0, cycle=1)

    # Insert real entry fill
    memory_db.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (10016, 'entry', 'e_long_1', 'CQB_10016_ENTRY_1', 60000.0, 0.10, 0.10, 'filled', 1, 1, 'LONG')
    """)
    # Insert virtual netting row with status='filled'
    memory_db.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (10016, 'virtual_netting', 'VN_long_1', 'CQB_10016_VNET_1', 60000.0, 0.04, 0.04, 'filled', 1, 0, 'LONG')
    """)
    memory_db.commit()

    # Run migration 008
    from engine.migrations.migration_008_archive_legacy_netting import run as run_migration_8
    memory_db.execute("DELETE FROM schema_migrations WHERE version='migration_008_archive_legacy_netting'")
    memory_db.commit()
    run_migration_8(database.DB_PATH)

    # Verify status changed to 'archived_legacy'
    status = memory_db.execute(
        "SELECT status FROM bot_orders WHERE order_id='VN_long_1'"
    ).fetchone()[0]
    assert status == 'archived_legacy'

    # Verify the migration force-resealed the bot, updating open_qty to 0.10
    oq_after = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id=10016").fetchone()[0]
    assert oq_after == pytest.approx(0.10)


def test_apply_oneway_entry_cross_reduction_removed():
    """
    Confirm the function no longer exists / is not importable.
    """
    with pytest.raises(ImportError):
        from engine.oneway_netting import apply_oneway_entry_cross_reduction


def test_reconciler_auto_clears_stale_whitelists(memory_db):
    """
    Verify that if a manual whitelist is present in the database but the physical and virtual nets
    are already aligned, the reconciler pass automatically deletes the whitelist row and logs the cleanup.
    """
    # Insert a manual whitelist for BTCUSDC (LONG, 0.004)
    memory_db.execute("""
        INSERT INTO manual_whitelists (pair, side, qty, created_at)
        VALUES ('BTCUSDC', 'LONG', 0.004, 1782363852)
    """)
    memory_db.commit()

    # Instantiate reconciler
    StateReconciler._last_global_offline_scan = 0.0
    recon = StateReconciler()

    # Mock exchanges to return aligned physical and virtual positions.
    # Physical position = +0.05 BTC (LONG 0.05)
    # Virtual position = +0.05 BTC (LONG 0.05)
    # They match! But there is a whitelist of 0.004 LONG.
    # Since raw physical matches virtual (0.05 == 0.05), the whitelist is stale.
    mock_exchange = MagicMock()
    mock_exchange.fetch_positions.return_value = [
        {'symbol': 'BTC/USDC:USDC', 'contracts': 0.05, 'side': 'long'}
    ]
    recon.exchanges = {'future': mock_exchange}
    recon.prime_startup_snapshot()

    # Seed the active bots so virtual net is computed as 0.05
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.05)
    memory_db.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (10016, 'entry', 'e_long_1', 'CQB_10016_ENTRY_1', 60000.0, 0.05, 0.05, 'filled', 1, 1, 'LONG')
    """)
    memory_db.commit()

    with patch.object(recon, 'validate_individual_bots', return_value=[]):
        recon.reconcile_all()

    # Assert whitelist row has been deleted
    wl_count = memory_db.execute("SELECT COUNT(*) FROM manual_whitelists WHERE pair='BTCUSDC'").fetchone()[0]
    assert wl_count == 0
