import os
import tempfile
import sqlite3
import sys
sys.path.insert(0, '.')

from engine.health import compute_system_health

def test_ledger_imbalance_detected():
    # Create a temporary database
    db_fd, db_path = tempfile.mkstemp()
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        # Create schema matching what health.py/position_ledger.py expects
        cur.executescript('''CREATE TABLE bots (
                                    id INTEGER PRIMARY KEY,
                                    name TEXT,
                                    pair TEXT,
                                    direction TEXT,
                                    is_active INTEGER DEFAULT 1,
                                    normalized_pair TEXT,
                                    status TEXT,
                                    bot_type TEXT,
                                    parent_bot_id INTEGER,
                                    config TEXT,
                                    cascade_started_at INTEGER,
                                    basket_start_time INTEGER
                                );
                                CREATE TABLE trades (
                                    id INTEGER PRIMARY KEY,
                                    bot_id INTEGER,
                                    total_invested REAL,
                                    current_step INTEGER,
                                    cycle_phase TEXT,
                                    cycle_id INTEGER,
                                    open_qty REAL,
                                    avg_entry_price REAL,
                                    basket_start_time INTEGER
                                );
                                CREATE TABLE exchange_fills (
                                    id INTEGER PRIMARY KEY,
                                    exchange_order_id TEXT,
                                    client_order_id TEXT,
                                    symbol TEXT,
                                    side TEXT,
                                    qty REAL,
                                    price REAL,
                                    fee REAL,
                                    fee_asset TEXT,
                                    fill_ts INTEGER,
                                    source TEXT,
                                    bot_id INTEGER,
                                    order_type TEXT,
                                    step INTEGER,
                                    cycle_id INTEGER,
                                    raw_json TEXT
                                );
                                CREATE TABLE system_equity (
                                    key TEXT PRIMARY KEY,
                                    value REAL
                                );
                                CREATE TABLE reconciliation_logs (
                                    id INTEGER PRIMARY KEY,
                                    action TEXT,
                                    timestamp INTEGER
                                );
                                CREATE TABLE trade_history (
                                    id INTEGER PRIMARY KEY,
                                    action TEXT,
                                    symbol TEXT,
                                    price REAL
                                );
                                CREATE TABLE bot_orders (
                                    id INTEGER PRIMARY KEY,
                                    bot_id INTEGER,
                                    status TEXT,
                                    created_at INTEGER
                                );
                            ''')
        # Insert bot 10011 (ETH-SHORT)
        cur.execute("INSERT INTO bots (id, name, pair, direction, normalized_pair, status, bot_type, parent_bot_id, config, cascade_started_at, basket_start_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (10011, 'bot_eth_short', 'ETH/USDC', 'SHORT', 'ETHUSDC', 'Scanning', 'standard', None, '{}', 0, 0))
        # Insert a trade for current cycle (cycle 12) to indicate bot is active and has a cycle
        # Add open_qty and avg_entry_price for header metrics
        cur.execute("INSERT INTO trades (bot_id, total_invested, current_step, cycle_phase, cycle_id, open_qty, avg_entry_price, basket_start_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (10011, 100.0, 1, 'TRADE', 12, 0.0, 0.0, 0.0))
        # Insert exchange_fills: unmatched SELL 0.1 ETH in cycle 5 (opening SHORT not closed)
        cur.execute("""INSERT INTO exchange_fills 
                       (id, exchange_order_id, client_order_id, symbol, side, qty, price, fee, fee_asset, fill_ts, source, bot_id, order_type, step, cycle_id, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (1, 'order_1', 'client_1', 'ETH/USDC', 'SELL', 0.1, 100.0, 0.0, 'USDC', 1000, 'ws_live', 10011, 'LIMIT', 0, 5, '{}'))
        # Insert matching buy/sell pairs for cycles 7-11 that net zero each cycle
        for i, cycle in enumerate(range(7, 12), start=2):
            cur.execute("""INSERT INTO exchange_fills 
                           (id, exchange_order_id, client_order_id, symbol, side, qty, price, fee, fee_asset, fill_ts, source, bot_id, order_type, step, cycle_id, raw_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (i, f'order_{i}', f'client_{i}', 'ETH/USDC', 'BUY', 0.05, 100.0, 0.0, 'USDC', 1000+i, 'ws_live', 10011, 'LIMIT', 0, cycle, '{}'))
            cur.execute("""INSERT INTO exchange_fills 
                           (id, exchange_order_id, client_order_id, symbol, side, qty, price, fee, fee_asset, fill_ts, source, bot_id, order_type, step, cycle_id, raw_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (i+10, f'order_{i+10}', f'client_{i+10}', 'ETH/USDC', 'SELL', 0.05, 100.0, 0.0, 'USDC', 1000+i+10, 'ws_live', 10011, 'LIMIT', 0, cycle, '{}'))
        # Insert a dummy system_equity row
        cur.execute("INSERT INTO system_equity (key, value) VALUES (?, ?)", ('ENGINE_STARTED_AT', 0.0))
        # Insert a dummy reconciliation_logs row to satisfy adoptions_today query (action like %ADOPTION%)
        cur.execute("INSERT INTO reconciliation_logs (action, timestamp) VALUES (?, ?)", ('ADOPTION_SOMETHING', int(os.time()) if hasattr(os, 'time') else 0))
        # Insert a dummy trade_history row to satisfy last_act query
        cur.execute("INSERT INTO trade_history (action, symbol, price) VALUES (?, ?, ?)", ('TRADE', 'ETH/USDC', 100.0))
        conn.commit()
    finally:
        # We'll keep the connection open for now, close it after we're done with the health check
        pass
    
    # Mock exchange instance that returns position 0.0 (flat)
    class MockExchange:
        def fetch_positions(self):
            return [{'symbol': 'ETH/USDC', 'contracts': 0.0, 'entryPrice': 0.0}]
        def fetch_open_orders(self, symbol):
            return []
        def fetch_balance(self):
            return {'USDC': {'free': 0.0, 'total': 0.0}}
    
    exchange = MockExchange()
    # Use identity normalization
    norm_fn = lambda s: s
    # Tolerance function
    def qty_tolerance_fn():
        return 1e-6
    
    health = compute_system_health(
        db_path=db_path,
        exchange_instance=exchange,
        norm_fn=norm_fn,
        qty_tolerance_fn=qty_tolerance_fn,
        open_exchange_orders=[],
        bot_df_rows=[]
    )
    
    # Now we can close the connection
    try:
        conn.close()
    except:
        pass
    
    try:
        os.unlink(db_path)
    except PermissionError:
        pass  # ignore if file is locked, will be cleaned up later
    
    # Check that drift_current_cycle is False (current cycle net zero)
    eth_pair_norm = norm_fn('ETH/USDC')  # should be 'ETH/USDC'
    eth_data = health.get('netting_status_per_pair', {}).get(eth_pair_norm)
    assert eth_data is not None, f"{eth_pair_norm} should be in netting status, keys: {list(health.get('netting_status_per_pair', {}).keys())}"
    # We'll check for ledger_imbalance flag.
    ledger_imbalance_flag = eth_data.get('ledger_imbalance')
    # For the RED test (before fix), we expect ledger_imbalance_flag to be None or False.
    # But we want to assert that there IS an imbalance (so the test will fail until we add the check).
    # Let's compute the true ledger net from exchange_fills to know what we expect.
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        SELECT SUM(CASE WHEN side = 'BUY' THEN qty ELSE -qty END)
        FROM exchange_fills WHERE bot_id = ?
    """, (10011,))
    ledger_net = cur.fetchone()[0] or 0.0
    conn.close()
    # The physical net from exchange (mock) is 0.0.
    # We expect ledger_imbalance to be True if ledger_net != 0.0 (beyond tolerance).
    # We'll set a tolerance of 1e-6.
    expected_imbalance = abs(ledger_net) > 1e-6
    # Now assert that the health data reflects this.
    # We want the test to FAIL (RED) until we add the ledger_imbalance check.
    # So we assert that ledger_imbalance_flag is True (meaning we expect the imbalance to be detected).
    # This will fail on the current code because ledger_imbalance_flag is None or False.
    assert ledger_imbalance_flag == True, f"Expected ledger_imbalance=True, got {ledger_imbalance_flag}"

if __name__ == '__main__':
    test_ledger_imbalance_detected()
    print("Test passed!")