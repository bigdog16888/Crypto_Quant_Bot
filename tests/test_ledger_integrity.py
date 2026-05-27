import sqlite3
import pytest
import time
import uuid
from engine import database

@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH
    
    # Disable backup during testing
    database.backup_database = lambda: None
    
    # Generate unique URI for each test to ensure isolation
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_db_{db_id}?mode=memory&cache=shared'
    
    # Keep one persistent connection open so the shared memory db isn't destroyed
    persistent_conn = orig_connect(shared_uri, uri=True)
    
    def mock_connect(db_path, *args, **kwargs):
        kwargs['uri'] = True
        return orig_connect(shared_uri, *args, **kwargs)
        
    sqlite3.connect = mock_connect
    
    # Clear thread local to force new connection
    if hasattr(database._local, 'connection'):
        database._local.connection = None
        
    database.DB_PATH = shared_uri
    database.init_db()
    
    conn = database.get_connection()
    # Apply manual migrations not present in init_db (now baseline, but try/except for safety)
    try:
        conn.execute("ALTER TABLE bot_orders ADD COLUMN wipe_proof_source TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE bot_orders ADD COLUMN wipe_proof_snapshot TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    
    yield conn
    
    # Teardown
    persistent_conn.close()
    sqlite3.connect = orig_connect
    database.backup_database = orig_backup
    database.DB_PATH = orig_db_path
    if hasattr(database._local, 'connection'):
        database._local.connection = None

def setup_bot_fixture(conn, bot_id, name, pair, direction):
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair)
        VALUES (?, ?, ?, ?, 1, ?)
    """, (bot_id, name, pair, direction, pair.split(':')[0].replace('/', '')))
    cursor.execute("""
        INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price, cycle_phase, wipe_wall_ts, position_side)
        VALUES (?, 1, 0, 0.0, 0.0, 0.0, 'SCANNING', 0, ?)
    """, (bot_id, direction))
    conn.commit()

def test_recompute_invested(memory_db):
    setup_bot_fixture(memory_db, 1, 'Test Bot', 'BTC/USDC:USDC', 'LONG')
    
    cursor = memory_db.cursor()
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES 
        (1, 'entry', 'order_1', 50000.0, 1.0, 1.0, 'filled', 1, 1),
        (1, 'grid', 'order_2', 40000.0, 1.0, 1.0, 'filled', 1, 2)
    """)
    memory_db.commit()
    
    invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(1, 1)
    
    assert invested == 90000.0
    assert open_qty == 2.0
    assert avg_price == 45000.0
    assert max_step == 2

def test_get_pair_virtual_net(memory_db):
    setup_bot_fixture(memory_db, 1, 'Test Bot', 'BTC/USDC:USDC', 'LONG')
    
    cursor = memory_db.cursor()
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_1', 50000.0, 2.0, 2.0, 'filled', 1, 1)
    """)
    memory_db.commit()
    
    database.recompute_invested_from_orders(1, 1)
    database.sync_trades_from_orders(1)
    
    net = database.get_pair_virtual_net('BTC/USDC:USDC')
    assert net == 2.0 

def test_partial_tp_fill(memory_db):
    setup_bot_fixture(memory_db, 1, 'Test Bot', 'BTC/USDC:USDC', 'LONG')
    
    cursor = memory_db.cursor()
    # Entry for 2.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_1', 50000.0, 2.0, 2.0, 'filled', 1, 1)
    """)
    # Partial TP for 1.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'tp', 'order_tp_1', 60000.0, 2.0, 1.0, 'filled', 1, 2)
    """)
    memory_db.commit()
    
    invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(1, 1)
    assert open_qty == 1.0
    assert invested == 50000.0

def test_full_tp_cascade(memory_db):
    setup_bot_fixture(memory_db, 1, 'Test Bot', 'BTC/USDC:USDC', 'LONG')
    
    cursor = memory_db.cursor()
    # Entry for 2.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_1', 50000.0, 2.0, 2.0, 'filled', 1, 1)
    """)
    # Full TP
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'tp', 'order_tp_2', 60000.0, 2.0, 2.0, 'filled', 1, 2)
    """)
    memory_db.commit()
    
    invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(1, 1)
    assert open_qty == 0.0
    assert invested == 0.0

def test_credit_fill_caps_to_order_amount(memory_db):
    """filled_amount must never exceed order amount (prevents 0.002 order / 1.0 fill bug)."""
    setup_bot_fixture(memory_db, 1, 'BTC Bot', 'BTC/USDC:USDC', 'LONG')
    cursor = memory_db.cursor()
    cursor.execute(
        """
        INSERT INTO bot_orders
        (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, client_order_id)
        VALUES (1, 'entry', 'ex1', 77000.0, 0.002, 0.0, 'open', 1, 1, 'CQB_1_ENTRY_1_1')
        """
    )
    memory_db.commit()
    from engine.ledger import credit_fill, seal_trade_state
    ok = credit_fill(1, 'ex1', cumulative_qty=1.0, avg_price=77000.0, order_type='entry', is_cumulative=True)
    assert ok
    row = cursor.execute("SELECT filled_amount FROM bot_orders WHERE order_id='ex1'").fetchone()
    assert row[0] == pytest.approx(0.0021, rel=1e-4)  # amount * 1.05 cap
    seal_trade_state(1)
    _c, _a, net_qty, _s = database.recompute_invested_from_orders(1, 1)
    assert net_qty == pytest.approx(0.0021, rel=1e-4)


def test_canonical_dedup_uses_max_fill_row(memory_db):
    """Duplicate CID rows must not undercount — highest filled_amount row wins."""
    setup_bot_fixture(memory_db, 1, 'Short Bot', 'LINK/USDC:USDC', 'SHORT')
    cursor = memory_db.cursor()
    cursor.execute(
        """
        INSERT INTO bot_orders
        (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, client_order_id)
        VALUES
        (1, 'entry', 'old', 9.0, 1.08, 0.54, 'filled', 1, 1, 'CQB_1_ENTRY_1_1'),
        (1, 'entry', 'new', 9.0, 1.08, 1.08, 'filled', 1, 1, 'CQB_1_ENTRY_1_1')
        """
    )
    memory_db.commit()
    _cost, _avg, basket_net, _step = database.recompute_invested_from_orders(1, 1)
    database.sync_trades_from_orders(1)
    assert basket_net == pytest.approx(1.08)
    assert database.get_pair_virtual_net('LINK/USDC:USDC') == pytest.approx(-1.08)


def test_hedge_child_open_qty_calculation(memory_db):
    """Hedge child bot (SHORT) has standard positive open_qty and is calculated correctly."""
    setup_bot_fixture(memory_db, 1, 'Long Bot', 'SOL/USDC:USDC', 'LONG')
    setup_bot_fixture(memory_db, 2, 'Short Bot', 'SOL/USDC:USDC', 'SHORT')
    cursor = memory_db.cursor()
    cursor.execute("UPDATE bots SET hedge_child_bot_id = 2 WHERE id = 1")
    cursor.execute("UPDATE bots SET parent_bot_id = 1, bot_type = 'hedge_child' WHERE id = 2")
    
    # Insert entry order for child (SHORT)
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (2, 'entry', 'child_entry', 80.0, 2.32, 2.32, 'filled', 1, 1, 'SHORT')
    """)
    memory_db.commit()
    
    invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(2, 1)
    assert open_qty == pytest.approx(2.32)


def test_hedge_child_order_virtual_net(memory_db):
    """Pair virtual net includes both parent and child bots."""
    setup_bot_fixture(memory_db, 1, 'Long Bot', 'BTC/USDC:USDC', 'LONG')
    setup_bot_fixture(memory_db, 2, 'Short Bot', 'BTC/USDC:USDC', 'SHORT')
    cursor = memory_db.cursor()
    cursor.execute("UPDATE bots SET hedge_child_bot_id = 2 WHERE id = 1")
    cursor.execute("UPDATE bots SET parent_bot_id = 1, bot_type = 'hedge_child' WHERE id = 2")
    
    # Add an entry order for bot 1 (+2.0 net)
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (1, 'entry', 'order_1', 50000.0, 2.0, 2.0, 'filled', 1, 1, 'LONG')
    """)
    # Add an entry order for child bot (-1.0 net)
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (2, 'entry', 'child_entry', 50000.0, 1.0, 1.0, 'filled', 1, 1, 'SHORT')
    """)
    memory_db.commit()
    
    database.recompute_invested_from_orders(1, 1)
    database.sync_trades_from_orders(1)
    database.recompute_invested_from_orders(2, 1)
    database.sync_trades_from_orders(2)
    
    net = database.get_pair_virtual_net('BTC/USDC:USDC')
    # Net should be +2.0 (parent) - 1.0 (child) = +1.0
    assert net == 1.0

def test_cancelled_partial_fill_not_phantom(memory_db):
    setup_bot_fixture(memory_db, 1, 'LINK Bot', 'LINK/USDT:USDT', 'LONG')
    
    cursor = memory_db.cursor()
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_link_1', 15.0, 10.0, 5.28, 'cancelled', 1, 1)
    """)
    memory_db.commit()
    database.sync_trades_from_orders(1)
    
    # Assert virtual net before reset is 5.28 (since it's a partially filled cancelled order)
    assert database.get_pair_virtual_net('LINK/USDT:USDT') == 5.28
    
    class _FlatEx:
        def fetch_positions(self):
            return []

    # Run the reset cascade (exchange flat — parity gate allows reset)
    database.reset_bot_after_tp(1, 20.0, exchange=_FlatEx())
    
    # Assert virtual net after reset is 0.0 (no longer phantom 5.28)
    assert database.get_pair_virtual_net('LINK/USDT:USDT') == 0.0

def test_partial_tp_no_cascade_block(memory_db):
    setup_bot_fixture(memory_db, 1, 'SUI Bot', 'SUI/USDC:USDC', 'SHORT')
    cursor = memory_db.cursor()
    
    # Bot has 15.7 open_qty in trades
    cursor.execute("UPDATE trades SET open_qty = 15.7, tp_order_id = 'tp_sui_123' WHERE bot_id = 1")
    # Insert entry order of 15.7
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_sui_1', 1.2, 15.7, 15.7, 'filled', 1, 1)
    """)
    # Insert open TP order of 15.7
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'tp', 'tp_sui_123', 1.3, 15.7, 0.0, 'open', 1, 2)
    """)
    memory_db.commit()
    
    # credit partial fill of 8.3
    from engine.ledger import credit_fill
    success = credit_fill(1, 'tp_sui_123', 8.3, 1.3, 'tp')
    assert success is True
    
    # Assert open_qty becomes 7.4
    row = cursor.execute("SELECT open_qty, tp_order_id, cycle_id FROM trades WHERE bot_id = 1").fetchone()
    # 15.7 - 8.3 = 7.4
    assert abs(row[0] - 7.4) < 1e-6
    
    # Simulate the deadlock fix where tp_order_id gets cleared to allow new TP placement
    cursor.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = 1")
    memory_db.commit()
    
    row2 = cursor.execute("SELECT open_qty, tp_order_id, cycle_id FROM trades WHERE bot_id = 1").fetchone()
    assert row2[1] is None
    assert row2[2] == 1  # Cycle is NOT reset



def test_tp_cascade_idempotent(memory_db):
    from engine.ledger import handle_tp_completion
    
    setup_bot_fixture(memory_db, 1, 'LINK Bot', 'LINK/USDT:USDT', 'LONG')
    
    cursor = memory_db.cursor()
    # Bot has 5.0 open_qty
    cursor.execute("UPDATE trades SET open_qty = 5.0, tp_order_id = 'tp_link_1' WHERE bot_id = 1")
    # Insert entry order of 5.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_link_1', 15.0, 5.0, 5.0, 'filled', 1, 1)
    """)
    # Insert TP order of 5.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'tp', 'tp_link_1', 18.0, 5.0, 5.0, 'filled', 1, 2)
    """)
    memory_db.commit()
    
    class MockExchange:
        def fetch_positions(self):
            return []
        def fetch_open_orders(self, symbol=None):
            return []
            
    mock_ex = MockExchange()
    
    # First call
    res1 = handle_tp_completion(bot_id=1, exit_price=18.0, pair='LINK/USDT:USDT', exchange=mock_ex, cycle_id=1)
    assert res1 is True
    
    # Assert cycle incremented and open_qty is 0
    row1 = cursor.execute("SELECT cycle_id, open_qty FROM trades WHERE bot_id = 1").fetchone()
    assert row1[0] == 2
    assert row1[1] == 0.0
    
    # Second call (idempotency test)
    res2 = handle_tp_completion(bot_id=1, exit_price=18.0, pair='LINK/USDT:USDT', exchange=mock_ex, cycle_id=1)
    
    row2 = cursor.execute("SELECT cycle_id, open_qty FROM trades WHERE bot_id = 1").fetchone()
    assert row2[1] == 0.0

def test_stalemate_evictor_partial_clears_tp_order_id(memory_db):
    setup_bot_fixture(memory_db, 1, 'Stalemate Bot', 'BTC/USDC:USDC', 'LONG')
    cursor = memory_db.cursor()
    
    # Setup bot state
    cursor.execute("UPDATE trades SET open_qty = 10.0, tp_order_id = 'tp_stalemate_1' WHERE bot_id = 1")
    # Insert entry order of 10.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_entry_1', 50000.0, 10.0, 10.0, 'filled', 1, 1)
    """)
    # Insert TP order of 10.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'tp', 'tp_stalemate_1', 55000.0, 10.0, 0.0, 'open', 1, 2)
    """)
    memory_db.commit()
    
    # 1. Simulate TP partial fill credit
    from engine.ledger import credit_fill
    success = credit_fill(1, 'tp_stalemate_1', 4.0, 55000.0, 'tp')
    assert success is True
    
    # 2. Simulate Stalemate Evictor detecting partial fill on the order
    row = cursor.execute("SELECT open_qty, tp_order_id, cycle_id FROM trades WHERE bot_id = 1").fetchone()
    current_open_qty = row[0]
    
    # Assert open_qty is decremented to 6.0
    assert abs(current_open_qty - 6.0) < 1e-6
    
    if current_open_qty > 0.001:
        # Clear tp_order_id
        cursor.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = 1")
        memory_db.commit()
        
    # 3. Assertions
    final_row = cursor.execute("SELECT open_qty, tp_order_id, cycle_id FROM trades WHERE bot_id = 1").fetchone()
    assert abs(final_row[0] - 6.0) < 1e-6  # NOT zeroed
    assert final_row[1] is None  # tp_order_id is NULL
    assert final_row[2] == 1  # Cycle NOT reset

def test_anonymous_fill_adoption(memory_db):
    setup_bot_fixture(memory_db, 1, 'SOL Long Bot', 'SOL/USDC:USDC', 'LONG')
    cursor = memory_db.cursor()
    
    # 1. Bot has an entry order of 0.21 at $150.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (1, 'entry', 'order_entry_1', 150.0, 0.21, 0.21, 'filled', 1, 1, 'LONG')
    """)
    # Update trades table to reflect this entry
    cursor.execute("""
        UPDATE trades 
        SET open_qty = 0.21, total_invested = 31.5, avg_entry_price = 150.0, cycle_phase = 'ACTIVE'
        WHERE bot_id = 1
    """)
    memory_db.commit()
    
    # 2. Simulate an exchange position of 0.43 by inserting an adoption row for 0.22
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (1, 'adoption', 'adopt_1', 150.0, 0.22, 0.22, 'filled', 1, 0, 'LONG')
    """)
    memory_db.commit()
    
    # Now run recompute_invested_from_orders to update trades/check results
    total_invested, avg_price, open_qty, current_step = database.recompute_invested_from_orders(1, 1)
    
    # Update the trades table cache with the recomputed values (as in production)
    cursor.execute("""
        UPDATE trades 
        SET total_invested = ?, avg_entry_price = ?, open_qty = ?, current_step = ?
        WHERE bot_id = 1
    """, (total_invested, avg_price, open_qty, current_step))
    memory_db.commit()
    
    # Assert get_pair_virtual_net returns 0.43 (0.21 + 0.22)
    net = database.get_pair_virtual_net('SOL/USDC:USDC')
    assert abs(net - 0.43) < 1e-6
    
    # Assert recompute_invested_from_orders returns correct total_invested
    # bought_cost = (0.21 * 150) + (0.22 * 150) = 31.5 + 33.0 = 64.5
    assert abs(total_invested - 64.5) < 1e-6
    # open_qty should be 0.43
    assert abs(open_qty - 0.43) < 1e-6

def test_cancelled_partial_fill_not_counted_as_active(memory_db):
    setup_bot_fixture(memory_db, 1, 'BTC Bot', 'BTC/USDC:USDC', 'LONG')
    
    cursor = memory_db.cursor()
    # Insert a cancelled order that has been marked as 'reset_cleared'
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_btc_1', 76836.7, 0.002, 0.002, 'reset_cleared', 1, 1)
    """)
    memory_db.commit()
    
    # Assert recompute_invested_from_orders does NOT count it in bought_qty / open_qty
    total_invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(1, 1)
    assert open_qty == 0.0
    assert total_invested == 0.0

def test_duplicate_entry_same_cid_not_double_counted(memory_db):
    setup_bot_fixture(memory_db, 1, 'Duplicate Bot', 'BTC/USDC:USDC', 'LONG')
    
    cursor = memory_db.cursor()
    # Insert three bot_orders rows with identical client_order_id and status='filled'
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, client_order_id, position_side)
        VALUES 
        (1, 'entry', 'order_d1', 50000.0, 1.0, 1.0, 'filled', 1, 1, 'DUPLICATE_CID_123', 'LONG'),
        (1, 'entry', 'order_d2', 50000.0, 1.0, 1.0, 'filled', 1, 1, 'DUPLICATE_CID_123', 'LONG'),
        (1, 'entry', 'order_d3', 50000.0, 1.0, 1.0, 'filled', 1, 1, 'DUPLICATE_CID_123', 'LONG')
    """)
    memory_db.commit()
    
    # Assert recompute_invested_from_orders only counts unique fills, not duplicates
    total_invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(1, 1)
    database.sync_trades_from_orders(1)
    assert open_qty == 1.0
    assert total_invested == 50000.0
    
    # Also assert get_pair_virtual_net only counts unique fills
    net = database.get_pair_virtual_net('BTC/USDC:USDC')
    assert net == 1.0



