"""
tests/test_position_ledger.py — Unit tests for canonical position_ledger module.
"""
import pytest
import sqlite3
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.position_ledger import (
    compute_bot_position,
    compute_pair_position,
    BotPosition,
    PairPosition,
)
import engine.database as db


def setup_test_db():
    """Set up a fresh test database."""
    test_db = "crypto_bot_test_position.db"
    if os.path.exists(test_db):
        os.remove(test_db)
    
    # Temporarily override DB_PATH
    import engine.database as db_module
    original_path = db_module.DB_PATH
    db_module.DB_PATH = os.path.join(os.path.dirname(__file__), '..', test_db)
    
    db.init_db()
    conn = db.get_connection()
    
    return conn, original_path, test_db


def teardown_test_db(conn, original_path, test_db):
    """Clean up test database."""
    import engine.database as db_module
    conn.close()
    db_module.DB_PATH = original_path
    if os.path.exists(test_db):
        os.remove(test_db)


def insert_test_fills(conn):
    """Insert standard test fills."""
    fills = [
        # Bot 1000: LONG, cycle 1
        ('EXCH_1000_ENTRY', 'CQB_1000_ENTRY_1_1', 'BTC/USDT:USDT', 'BUY', 0.1, 50000.0, 0.0, None, 1000000, 'backfill', 1000, 'entry', 1, 1, None),
        ('EXCH_1000_GRID1', 'CQB_1000_GRID_1_2', 'BTC/USDT:USDT', 'BUY', 0.05, 49000.0, 0.0, None, 1000001, 'backfill', 1000, 'grid', 2, 1, None),
        ('EXCH_1000_TP', 'CQB_1000_TP_1_3', 'BTC/USDT:USDT', 'SELL', 0.15, 51000.0, 0.0, None, 1000002, 'backfill', 1000, 'tp', 3, 1, None),
        
        # Bot 1001: SHORT, cycle 1
        ('EXCH_1001_ENTRY', 'CQB_1001_ENTRY_1_1', 'BTC/USDT:USDT', 'SELL', 0.1, 50000.0, 0.0, None, 1000010, 'backfill', 1001, 'entry', 1, 1, None),
        ('EXCH_1001_GRID1', 'CQB_1001_GRID_1_2', 'BTC/USDT:USDT', 'SELL', 0.05, 51000.0, 0.0, None, 1000011, 'backfill', 1001, 'grid', 2, 1, None),
        ('EXCH_1001_TP', 'CQB_1001_TP_1_3', 'BTC/USDT:USDT', 'BUY', 0.15, 49000.0, 0.0, None, 1000012, 'backfill', 1001, 'tp', 3, 1, None),
        
        # Bot 1002: LONG, cycle 2 (new cycle, no carry)
        ('EXCH_1002_ENTRY', 'CQB_1002_ENTRY_2_1', 'ETH/USDT:USDT', 'BUY', 1.0, 3000.0, 0.0, None, 2000000, 'backfill', 1002, 'entry', 1, 2, None),
    ]
    
    for f in fills:
        conn.execute("""
            INSERT INTO exchange_fills 
            (exchange_order_id, client_order_id, symbol, side, qty, price, 
             fee, fee_asset, fill_ts, source, bot_id, order_type, step, cycle_id, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, f)
    conn.commit()
    
    # Add bot entries
    conn.execute("INSERT INTO bots (id, name, pair, direction, status) VALUES (1000, 'test_long', 'BTC/USDT:USDT', 'LONG', 'IN_TRADE')")
    conn.execute("INSERT INTO bots (id, name, pair, direction, status) VALUES (1001, 'test_short', 'BTC/USDT:USDT', 'SHORT', 'IN_TRADE')")
    conn.execute("INSERT INTO bots (id, name, pair, direction, status) VALUES (1002, 'test_long_eth', 'ETH/USDT:USDT', 'LONG', 'IN_TRADE')")
    conn.commit()


class TestPositionLedger:
    
    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Set up and tear down for each test."""
        conn, original_path, test_db = setup_test_db()
        insert_test_fills(conn)
        self.conn = conn
        self.original_path = original_path
        self.test_db = test_db
        yield
        teardown_test_db(conn, original_path, test_db)
    
    def test_compute_bot_position_long(self):
        """Test LONG bot: entry BUY + grid BUY, TP SELL → net positive."""
        pos = compute_bot_position(1000, self.conn)
        
        # Net qty = 0.1 + 0.05 - 0.15 = 0.0 (fully closed)
        assert pos.net_qty == pytest.approx(0.0, abs=1e-10)
        assert pos.fills_count == 3
        assert pos.pair == 'BTC/USDT:USDT'
        assert pos.direction == 'LONG'
        
        # Realized PnL = (51000 * 0.15) - (50000 * 0.1 + 49000 * 0.05)
        # = 7650 - (5000 + 2450) = 7650 - 7450 = 200
        assert pos.realized_pnl == pytest.approx(200.0, abs=0.01)
    
    def test_compute_bot_position_short(self):
        """Test SHORT bot: entry SELL + grid SELL, TP BUY → net negative (or 0 if closed)."""
        pos = compute_bot_position(1001, self.conn)
        
        # Net qty = -(0.1 + 0.05) + 0.15 = 0.0 (fully closed)
        assert pos.net_qty == pytest.approx(0.0, abs=1e-10)
        assert pos.fills_count == 3
        assert pos.pair == 'BTC/USDT:USDT'
        assert pos.direction == 'SHORT'
        
        # Realized PnL = (50000 * 0.1 + 51000 * 0.05) - (49000 * 0.15)
        # = (5000 + 2550) - 7350 = 7550 - 7350 = 200
        assert pos.realized_pnl == pytest.approx(200.0, abs=0.01)
    
    def test_compute_bot_position_open(self):
        """Test bot with open position (no TP yet)."""
        pos = compute_bot_position(1002, self.conn)
        
        assert pos.net_qty == 1.0  # 1.0 BTC long
        assert pos.fills_count == 1
        assert pos.avg_entry_price == pytest.approx(3000.0)
        assert pos.realized_pnl == 0.0
    
    def test_compute_bot_position_cycle_floor(self):
        """Test cycle_floor filters to current cycle only."""
        # Bot 1000 has fills in cycle 1
        pos_all = compute_bot_position(1000, self.conn)
        pos_cycle1 = compute_bot_position(1000, self.conn, cycle_floor=1)
        pos_cycle2 = compute_bot_position(1000, self.conn, cycle_floor=2)
        
        assert pos_all.fills_count == 3
        assert pos_cycle1.fills_count == 3
        assert pos_cycle2.fills_count == 0  # No fills in cycle 2
        assert pos_cycle2.net_qty == 0.0
    
    def test_compute_pair_position(self):
        """Test pair aggregation across multiple bots."""
        pair = compute_pair_position('BTC/USDT:USDT', self.conn)
        
        assert pair.pair == 'BTC/USDT:USDT'
        assert len(pair.bots) == 2  # bot 1000 and 1001
        
        # Net = 0.0 (1000) + 0.0 (1001) = 0.0
        assert pair.net_qty == pytest.approx(0.0, abs=1e-10)
        
        bot_positions = {bp.bot_id: bp.net_qty for bp in pair.bots}
        assert bot_positions[1000] == pytest.approx(0.0, abs=1e-10)
        assert bot_positions[1001] == pytest.approx(0.0, abs=1e-10)
    
    def test_compute_pair_position_multi_pair(self):
        """Test pair aggregation for ETH."""
        pair = compute_pair_position('ETH/USDT:USDT', self.conn)
        
        assert pair.pair == 'ETH/USDT:USDT'
        assert len(pair.bots) == 1  # only bot 1002
        assert pair.net_qty == 1.0
        assert pair.bots[0].bot_id == 1002
        assert pair.bots[0].net_qty == 1.0
    
    def test_side_signing_convention(self):
        """Verify sign convention: LONG → positive, SHORT → negative."""
        # Add an open LONG position to bot 1000 (cycle 2)
        self.conn.execute("""
            INSERT INTO exchange_fills 
            (exchange_order_id, client_order_id, symbol, side, qty, price, 
             fill_ts, source, bot_id, order_type, step, cycle_id)
            VALUES ('EXCH_TEST_LONG', 'CID_LONG', 'BTC/USDT:USDT', 'BUY', 0.5, 50000.0,
                    3000000, 'backfill', 1000, 'entry', 1, 2)
        """)
        self.conn.commit()
        
        pos_long = compute_bot_position(1000, self.conn, cycle_floor=2)
        assert pos_long.net_qty > 0  # LONG = positive
        
        # Add an open SHORT position (new bot)
        self.conn.execute("""
            INSERT INTO bots (id, name, pair, direction, status) VALUES (1003, 'test_short2', 'BTC/USDT:USDT', 'SHORT', 'IN_TRADE')
        """)
        self.conn.execute("""
            INSERT INTO exchange_fills 
            (exchange_order_id, client_order_id, symbol, side, qty, price, 
             fill_ts, source, bot_id, order_type, step, cycle_id)
            VALUES ('EXCH_TEST_SHORT', 'CID_SHORT', 'BTC/USDT:USDT', 'SELL', 0.5, 50000.0,
                    3000001, 'backfill', 1003, 'entry', 1, 2)
        """)
        self.conn.commit()
        
        pos_short = compute_bot_position(1003, self.conn, cycle_floor=2)
        assert pos_short.net_qty < 0  # SHORT = negative
        
        # Pair position should be 0 (offsetting)
        pair = compute_pair_position('BTC/USDT:USDT', self.conn)
        assert pair.net_qty == pytest.approx(0.0, abs=0.001)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])