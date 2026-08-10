"""
tests/test_adr011_mechanism_b.py

Phase 1 regression tests for ADR-011 Mechanism B:
seal_trade_state must NOT increment cycle_id on idle bots with zero current-cycle fills.

Uses real bot data patterns from the 4 affected bots:
- 10016 (BTC LONG): +26 cycles drift — entry fills in cycle 3, TP fills marked reset_cleared, bot idle at cycle 29
- 100317 (BTC SHORT hedge): +26 cycles drift — similar pattern
- 10007 (BNB SHORT): +9 cycles drift — all TPs reset_cleared
- 100314 (BNB LONG hedge): +5 cycles drift — ADR-010 TP cascade never ran (status=filled not reset_cleared)

Also tests that legitimate cycle advancement still works (don't break existing behavior).
"""
import pytest
import sqlite3
import time
import uuid
import sys
import os

# Add repo root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine import database


@pytest.fixture
def memory_db():
    """Create an isolated in-memory database for testing (copied from test_ledger_integrity.py)."""
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
    # Apply manual migrations not present in init_db
    try:
        conn.execute("ALTER TABLE bot_orders ADD COLUMN wipe_proof_source TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE bot_orders ADD COLUMN wipe_proof_snapshot TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE bot_orders ADD COLUMN filled_at INTEGER DEFAULT 0")
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


def setup_bot_fixture(conn, bot_id, name, pair, direction, cycle_id=1, open_qty=0.0, status='Scanning'):
    """Setup a bot with trades row at given cycle."""
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO bots (id, name, pair, direction, is_active, status, normalized_pair)
        VALUES (?, ?, ?, ?, 1, ?, ?)
    """, (bot_id, name, pair, direction, status, pair.split(':')[0].replace('/', '')))
    cursor.execute("""
        INSERT INTO trades (bot_id, cycle_id, cycle_phase, open_qty, total_invested, avg_entry_price, position_side, current_step, entry_confirmed)
        VALUES (?, ?, 'IDLE', ?, 0.0, 0.0, ?, 0, 0)
    """, (bot_id, cycle_id, open_qty, direction))
    conn.commit()


def add_filled_order(conn, bot_id, order_type, cycle_id, step, filled_qty, price, client_order_id, status='filled', created_at_offset=0, direction='LONG'):
    """Add a filled bot_orders row."""
    base_time = int(time.time()) + created_at_offset
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, status, cycle_id, client_order_id, amount, filled_amount, price, step, created_at, position_side)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (bot_id, order_type, status, cycle_id, client_order_id, filled_qty, filled_qty, price, step, base_time, direction))
    conn.commit()


from engine.ledger import seal_trade_state


class TestMechanismBCycleDrift:
    """Test the Mechanism B fix: no cycle_id increment on idle bots with zero current-cycle fills."""

    def test_bot_10016_pattern_idle_bot_no_current_cycle_fills(self, memory_db):
        """
        Bot 10016 (BTC LONG): Had entry fills in cycle 3, TP fills in cycle 3,
        then bot went idle. Mechanism A put fills in old cycles.
        seal_trade_state at cycle 29 should NOT increment to 30 because
        there are ZERO fills in cycle 29.
        """
        conn = memory_db
        setup_bot_fixture(conn, 10016, 'long btc price', 'BTC/USDC:USDC', 'LONG', cycle_id=29, open_qty=0.0, status='Scanning')
        
        # Real fills from cycle 3 (legitimate cycle where trading happened)
        add_filled_order(conn, 10016, 'entry', cycle_id=3, step=1, filled_qty=0.002, price=65000, client_order_id='CID_E1', created_at_offset=-3600, direction='LONG')
        add_filled_order(conn, 10016, 'grid', cycle_id=3, step=2, filled_qty=0.002, price=64000, client_order_id='CID_G1', created_at_offset=-3500, direction='LONG')
        add_filled_order(conn, 10016, 'tp', cycle_id=3, step=3, filled_qty=0.004, price=66000, client_order_id='CID_TP1', created_at_offset=-3400, status='reset_cleared', direction='LONG')
        
        # Mechanism A drift: some fills incorrectly tagged to old cycles (e.g., cycle 5, 10, 15)
        add_filled_order(conn, 10016, 'entry', cycle_id=5, step=1, filled_qty=0.001, price=65100, client_order_id='CID_E2', created_at_offset=-2000, direction='LONG')
        add_filled_order(conn, 10016, 'tp', cycle_id=10, step=2, filled_qty=0.002, price=66100, client_order_id='CID_TP2', created_at_offset=-1500, status='reset_cleared', direction='LONG')
        add_filled_order(conn, 10016, 'entry', cycle_id=15, step=1, filled_qty=0.001, price=65200, client_order_id='CID_E3', created_at_offset=-1000, direction='LONG')
        
        # Current cycle (29) has ZERO fills - bot is idle
        # trades.total_invested may be > 0.01 due to Mechanism A drift from old cycles
        
        result = seal_trade_state(10016)
        
        # Verify cycle_id did NOT increment (stays 29)
        cursor = conn.cursor()
        cursor.execute("SELECT cycle_id, total_invested, open_qty FROM trades WHERE bot_id = 10016")
        row = cursor.fetchone()
        
        assert row[0] == 29, f"Cycle should NOT increment from 29 (idle bot, zero current-cycle fills), got {row[0]}"
        assert row[1] == 0.0, f"total_invested should be 0 (recomputed from current cycle only), got {row[1]}"
        assert row[2] == 0.0, f"open_qty should be 0, got {row[2]}"
        assert result['status'] == 'Scanning'

    def test_bot_10007_pattern_idle_bot_stale_total_invested(self, memory_db):
        """
        Bot 10007 (BNB SHORT): +9 cycles drift.
        Mechanism A put fills in wrong cycles. trades.total_invested > 0.01 from old fills.
        But current cycle has NO fills. Should NOT increment cycle_id.
        """
        conn = memory_db
        setup_bot_fixture(conn, 10007, 'bnb short', 'BNB/USDC:USDC', 'SHORT', cycle_id=13, open_qty=0.0, status='Scanning')
        
        # Legitimate fills from cycle 4 (where trading actually happened)
        add_filled_order(conn, 10007, 'entry', cycle_id=4, step=1, filled_qty=0.1, price=600, client_order_id='CID_E1', created_at_offset=-5000, direction='SHORT')
        add_filled_order(conn, 10007, 'grid', cycle_id=4, step=2, filled_qty=0.1, price=595, client_order_id='CID_G1', created_at_offset=-4900, direction='SHORT')
        add_filled_order(conn, 10007, 'tp', cycle_id=4, step=3, filled_qty=0.2, price=580, client_order_id='CID_TP1', created_at_offset=-4800, status='reset_cleared', direction='SHORT')
        
        # Mechanism A drift: fills scattered across old cycles 6, 8, 10
        add_filled_order(conn, 10007, 'entry', cycle_id=6, step=1, filled_qty=0.05, price=605, client_order_id='CID_E2', created_at_offset=-3000, direction='SHORT')
        add_filled_order(conn, 10007, 'tp', cycle_id=8, step=2, filled_qty=0.1, price=590, client_order_id='CID_TP2', created_at_offset=-2000, status='reset_cleared', direction='SHORT')
        add_filled_order(conn, 10007, 'entry', cycle_id=10, step=1, filled_qty=0.05, price=610, client_order_id='CID_E3', created_at_offset=-1000, direction='SHORT')
        
        # Current cycle 13: ZERO fills
        # But trades.total_invested may show > 0.01 from stale recomputation
        
        result = seal_trade_state(10007)
        
        cursor = conn.cursor()
        cursor.execute("SELECT cycle_id, total_invested, open_qty FROM trades WHERE bot_id = 10007")
        row = cursor.fetchone()
        
        assert row[0] == 13, f"Cycle should NOT increment from 13 (idle bot, zero current-cycle fills), got {row[0]}"
        assert row[1] == 0.0, f"total_invested should be 0 (recomputed from current cycle only), got {row[1]}"
        assert row[2] == 0.0
        assert result['status'] == 'Scanning'

    def test_bot_100314_pattern_adr010_tp_cascade_never_ran(self, memory_db):
        """
        Bot 100314 (BNB LONG hedge): +5 cycles drift.
        ADR-010 TP cascade never ran — TP has status='filled' not 'reset_cleared'.
        Mechanism B should not increment cycle_id on idle bot.
        """
        conn = memory_db
        setup_bot_fixture(conn, 100314, 'bnb short_hedge', 'BNB/USDC:USDC', 'LONG', cycle_id=8, open_qty=0.0, status='Scanning')
        
        # Legitimate fills from cycle 4
        add_filled_order(conn, 100314, 'entry', cycle_id=4, step=1, filled_qty=0.1, price=600, client_order_id='CID_E1', created_at_offset=-5000, direction='LONG')
        # TP filled but cascade never ran (ADR-010) - status='filled' not 'reset_cleared'
        add_filled_order(conn, 100314, 'tp', cycle_id=4, step=2, filled_qty=0.1, price=620, client_order_id='CID_TP1', created_at_offset=-4800, status='filled', direction='LONG')
        
        # Mechanism A drift
        add_filled_order(conn, 100314, 'entry', cycle_id=5, step=1, filled_qty=0.05, price=605, client_order_id='CID_E2', created_at_offset=-3000, direction='LONG')
        add_filled_order(conn, 100314, 'tp', cycle_id=6, step=2, filled_qty=0.05, price=615, client_order_id='CID_TP2', created_at_offset=-2000, status='filled', direction='LONG')
        
        # Current cycle 8: ZERO fills
        result = seal_trade_state(100314)
        
        cursor = conn.cursor()
        cursor.execute("SELECT cycle_id, total_invested, open_qty FROM trades WHERE bot_id = 100314")
        row = cursor.fetchone()
        
        assert row[0] == 8, f"Cycle should NOT increment from 8 (idle bot, zero current-cycle fills), got {row[0]}"
        assert row[1] == 0.0
        assert row[2] == 0.0
        assert result['status'] == 'Scanning'

    def test_bot_100317_pattern_partial_flatten_only(self, memory_db):
        """
        Bot 100317 (BTC SHORT hedge): +26 cycles drift.
        Partial flatten only — no full TP reset.
        Should not increment on idle cycle.
        """
        conn = memory_db
        setup_bot_fixture(conn, 100317, 'long btc price_hedge', 'BTC/USDC:USDC', 'SHORT', cycle_id=29, open_qty=0.0, status='Scanning')
        
        # Fills from cycle 3
        add_filled_order(conn, 100317, 'entry', cycle_id=3, step=1, filled_qty=0.002, price=65000, client_order_id='CID_E1', created_at_offset=-3600, direction='SHORT')
        add_filled_order(conn, 100317, 'close', cycle_id=3, step=2, filled_qty=0.001, price=64500, client_order_id='CID_C1', created_at_offset=-3500, direction='SHORT')  # Partial close
        
        # Mechanism A drift
        add_filled_order(conn, 100317, 'entry', cycle_id=7, step=1, filled_qty=0.001, price=65500, client_order_id='CID_E2', created_at_offset=-2000, direction='SHORT')
        add_filled_order(conn, 100317, 'close', cycle_id=12, step=2, filled_qty=0.001, price=64000, client_order_id='CID_C2', created_at_offset=-1000, direction='SHORT')
        
        # Current cycle 29: ZERO fills
        result = seal_trade_state(100317)
        
        cursor = conn.cursor()
        cursor.execute("SELECT cycle_id, total_invested, open_qty FROM trades WHERE bot_id = 100317")
        row = cursor.fetchone()
        
        assert row[0] == 29, f"Cycle should NOT increment from 29 (idle bot, zero current-cycle fills), got {row[0]}"
        assert row[1] == 0.0
        assert row[2] == 0.0
        assert result['status'] == 'Scanning'


class TestMechanismBNormalCycleAdvancement:
    """Test that legitimate cycle advancement still works (don't break existing behavior)."""

    def test_legitimate_cycle_advance_after_tp_fill(self, memory_db):
        """
        Bot has fills in CURRENT cycle, last exit is TP.
        Should NOT advance cycle (handled by reset_bot_after_tp normally).
        This test ensures we don't break the TP path.
        """
        conn = memory_db
        setup_bot_fixture(conn, 2001, 'test bot', 'BTC/USDC:USDC', 'LONG', cycle_id=5, open_qty=0.0, status='Scanning')
        
        # Current cycle (5) HAS fills - legitimate trading happened this cycle
        add_filled_order(conn, 2001, 'entry', cycle_id=5, step=1, filled_qty=0.01, price=50000, client_order_id='CID_E1', created_at_offset=-100, direction='LONG')
        add_filled_order(conn, 2001, 'tp', cycle_id=5, step=2, filled_qty=0.01, price=51000, client_order_id='CID_TP1', created_at_offset=-50, status='filled', direction='LONG')
        
        result = seal_trade_state(2001)
        
        cursor = conn.cursor()
        cursor.execute("SELECT cycle_id, total_invested, open_qty FROM trades WHERE bot_id = 2001")
        row = cursor.fetchone()
        
        # Current cycle has fills, last exit is TP
        # is_transitioning=True (was active), has_current_cycle_fills=True, but last_exit_type='tp'
        # should_increment = True and True and False = False
        # So cycle should NOT increment (TP handled by reset_bot_after_tp)
        assert row[0] == 5, f"Cycle should NOT increment when last exit is TP in current cycle (reset_bot_after_tp handles), got {row[0]}"
        assert row[1] == 0.0
        assert row[2] == 0.0

    def test_legitimate_cycle_advance_after_non_tp_exit(self, memory_db):
        """
        Bot has fills in CURRENT cycle, last exit is a close/sl (non-TP).
        Bot is transitioning from IN TRADE. Should increment cycle_id.
        This is the legitimate cycle advancement case.
        """
        conn = memory_db
        # Bot starts as IN TRADE (simulating active trading)
        # Do NOT pre-update status to Scanning - seal_trade_state will transition it
        setup_bot_fixture(conn, 2002, 'test bot', 'BTC/USDC:USDC', 'LONG', cycle_id=5, open_qty=0.01, status='IN TRADE')
        
        # Current cycle (5) HAS fills - legitimate trading this cycle
        add_filled_order(conn, 2002, 'entry', cycle_id=5, step=1, filled_qty=0.02, price=50000, client_order_id='CID_E1', created_at_offset=-100, direction='LONG')
        # Non-TP exit (e.g., force close, stop loss)
        add_filled_order(conn, 2002, 'close', cycle_id=5, step=2, filled_qty=0.02, price=49000, client_order_id='CID_C1', created_at_offset=-50, direction='LONG')
        
        # Do NOT pre-update bot status - seal_trade_state reads curr_status and transitions
        result = seal_trade_state(2002)
        
        cursor = conn.cursor()
        cursor.execute("SELECT cycle_id, total_invested, open_qty FROM trades WHERE bot_id = 2002")
        row = cursor.fetchone()
        
        # Bot WAS IN TRADE, has current-cycle fills, last exit is NOT TP
        # Should increment cycle_id: 5 -> 6
        assert row[0] == 6, f"Cycle SHOULD increment from 5 to 6 (active bot with current-cycle fills, non-TP exit), got {row[0]}"
        assert row[1] == 0.0
        assert row[2] == 0.0
        assert result['status'] == 'Scanning'

    def test_require_manual_proof_transition_to_flat(self, memory_db):
        """
        Bot in REQUIRE_MANUAL_PROOF, has current-cycle fills, transitions to flat.
        Should increment cycle_id (legitimate transition from gated active state).
        """
        conn = memory_db
        # Bot starts in REQUIRE_MANUAL_PROOF - seal_trade_state will transition it to Scanning
        # Do NOT pre-update status to Scanning
        setup_bot_fixture(conn, 2003, 'gated bot', 'BTC/USDC:USDC', 'LONG', cycle_id=10, open_qty=0.0, status='REQUIRE_MANUAL_PROOF')
        
        # Current cycle (10) HAS fills - legitimate trading this cycle
        add_filled_order(conn, 2003, 'entry', cycle_id=10, step=1, filled_qty=0.01, price=50000, client_order_id='CID_E1', created_at_offset=-100, direction='LONG')
        add_filled_order(conn, 2003, 'close', cycle_id=10, step=2, filled_qty=0.01, price=51000, client_order_id='CID_C1', created_at_offset=-50, direction='LONG')
        
        # Do NOT pre-update bot status - seal_trade_state reads curr_status and transitions
        result = seal_trade_state(2003)
        
        cursor = conn.cursor()
        cursor.execute("SELECT cycle_id, total_invested, open_qty FROM trades WHERE bot_id = 2003")
        row = cursor.fetchone()
        
        # Bot WAS REQUIRE_MANUAL_PROOF (active state), has current-cycle fills, non-TP exit
        # Should increment: 10 -> 11
        assert row[0] == 11, f"Cycle SHOULD increment from 10 to 11 (REQUIRE_MANUAL_PROOF bot with current-cycle fills), got {row[0]}"
        assert row[1] == 0.0
        assert row[2] == 0.0
        assert result['status'] == 'Scanning'

    def test_no_advance_when_not_transitioning_from_active(self, memory_db):
        """
        Bot status is 'Scanning' (not IN TRADE or REQUIRE_MANUAL_PROOF).
        Even if it has current-cycle fills (edge case), should NOT increment.
        This tests the is_transitioning = curr_status in ('IN TRADE', 'REQUIRE_MANUAL_PROOF') condition.
        """
        conn = memory_db
        # Bot status is already Scanning (not transitioning from active)
        setup_bot_fixture(conn, 2004, 'idle bot', 'BTC/USDC:USDC', 'LONG', cycle_id=7, open_qty=0.0, status='Scanning')
        
        # Current cycle (7) HAS fills (edge case: manual order placement?)
        add_filled_order(conn, 2004, 'entry', cycle_id=7, step=1, filled_qty=0.01, price=50000, client_order_id='CID_E1', created_at_offset=-100, direction='LONG')
        add_filled_order(conn, 2004, 'close', cycle_id=7, step=2, filled_qty=0.01, price=51000, client_order_id='CID_C1', created_at_offset=-50, direction='LONG')
        
        result = seal_trade_state(2004)
        
        cursor = conn.cursor()
        cursor.execute("SELECT cycle_id, total_invested, open_qty FROM trades WHERE bot_id = 2004")
        row = cursor.fetchone()
        
        # Bot NOT transitioning from active state (status='Scanning' already)
        # is_transitioning = False, so should_increment = False
        # Should NOT increment cycle_id
        assert row[0] == 7, f"Cycle should NOT increment (bot not transitioning from active state), got {row[0]}"
        assert row[1] == 0.0
        assert row[2] == 0.0
        assert result['status'] == 'Scanning'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])