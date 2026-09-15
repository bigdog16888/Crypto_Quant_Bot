#!/usr/bin/env python3
"""
Regression test for SUI cycle-sweep bug fix.
Built from REAL order data for bot 10018 (sui long).

IMPORTANT (2026-09-15): this test was formerly coupled to the LIVE
production database (it read bot_orders for 10018 straight out of
crypto_bot.db at fixture-build time). That made the test non-deterministic:
any production change to bot 10018's orders silently shifted the hardcoded
assertion expectations (e.g. a reopened/re-sealed order flipped cycle 25 from
7 -> 8 reset_cleared rows). The fixture is now FROZEN: the real order rows
captured on 2026-09-15 are embedded below and the test asserts against that
fixed baseline, so live trading on bot 10018 can never mutate expectations.
If the underlying logic genuinely changes, update FROZEN_10018_ORDERS and the
assertions together, deliberately -- not as a side effect of a production write.
"""

import sqlite3
import tempfile
import os

# Frozen snapshot of bot_orders for bot 10018 (captured 2026-09-15).
# Each tuple: (id, bot_id, order_type, status, filled_amount, price,
#              filled_at, created_at, cycle_id, client_order_id, position_side)
FROZEN_10018_ORDERS = [
    (358, 10018, 'entry', 'reset_cleared', 7.3, 0.7165, 1785114081, 1785114078, 0, 'CQB_10018_ENTRY_0_1_1785114078', 'LONG'),
    (460, 10018, 'tp', 'reset_cleared', 7.3, 0.719, 0, 1785123986, 0, 'CQB_10018_TP_0_1_R1785123985', 'LONG'),
    (473, 10018, 'entry', 'reset_cleared', 7.3, 0.7177, 0, 1785127436, 0, 'CQB_10018_ENTRY_0_1_1785127435', 'LONG'),
    (546, 10018, 'tp', 'reset_cleared', 7.3, 0.7209, 1785137123, 1785136477, 0, 'CQB_10018_TP_0_1_R1785136476', 'LONG'),
    (554, 10018, 'entry', 'reset_cleared', 7.3, 0.717, 1785137210, 1785137207, 1, 'CQB_10018_ENTRY_1_1_1785137207', 'LONG'),
    (556, 10018, 'grid', 'reset_cleared', 16.8, 0.7137, 1785142823, 1785137219, 1, 'CQB_10018_GRID_1_2', 'LONG'),
    (714, 10018, 'tp', 'reset_cleared', 24.1, 0.7164, 1785153683, 1785153633, 1, 'CQB_10018_TP_1_2_R1785153633', 'LONG'),
    (716, 10018, 'entry', 'reset_cleared', 7.3, 0.7167, 1785153995, 1785153995, 2, 'CQB_10018_ENTRY_2_1_1785153995', 'LONG'),
    (720, 10018, 'grid', 'reset_cleared', 16.8, 0.7138, 1785154819, 1785154010, 2, 'CQB_10018_GRID_2_2', 'LONG'),
    (735, 10018, 'grid', 'reset_cleared', 38.9, 0.7115, 1785155509, 1785154829, 2, 'CQB_10018_GRID_2_3', 'LONG'),
    (749, 10018, 'grid', 'reset_cleared', 89.8, 0.7088, 1785155652, 1785155522, 2, 'CQB_10018_GRID_2_4', 'LONG'),
    (793, 10018, 'tp', 'reset_cleared', 105.3, 0.718, 1785159863, 1785159258, 2, 'CQB_10018_TP_2_4_R1785159257', 'LONG'),
    (800, 10018, 'tp', 'reset_cleared', 39.7, 0.7177, 1785159872, 1785159872, 2, 'CQB_10018_TP_2_4_R1785159871', 'LONG'),
    (801, 10018, 'close', 'reset_cleared', 7.8, 0.7169, 1785159879, 1785159879, 2, 'CQB_10018_CLOSE_2_1785159879', 'LONG'),
    (812, 10018, 'entry', 'reset_cleared', 7.3, 0.7174, 1785160083, 1785160081, 3, 'CQB_10018_ENTRY_3_1_1785160081', 'LONG'),
    (814, 10018, 'grid', 'reset_cleared', 16.8, 0.7131, 1785161200, 1785160091, 3, 'CQB_10018_GRID_3_2', 'LONG'),
    (842, 10018, 'grid', 'reset_cleared', 38.9, 0.7095, 1785162068, 1785161205, 3, 'CQB_10018_GRID_3_3', 'LONG'),
    (848, 10018, 'grid', 'reset_cleared', 90.0, 0.7057, 1785162473, 1785162074, 3, 'CQB_10018_GRID_3_4', 'LONG'),
    (856, 10018, 'grid', 'reset_cleared', 208.2, 0.701, 1785162640, 1785162488, 3, 'CQB_10018_GRID_3_5', 'LONG'),
    (861, 10018, 'grid', 'reset_cleared', 481.3, 0.6963, 1785163051, 1785162643, 3, 'CQB_10018_GRID_3_6', 'LONG'),
    (942, 10018, 'tp', 'reset_cleared', 68.8, 0.7034, 1785171505, 1785171152, 3, 'CQB_10018_TP_3_6_R1785171152', 'LONG'),
    (948, 10018, 'tp', 'reset_cleared', 44.1, 0.7036, 1785171570, 1785171554, 3, 'CQB_10018_TP_3_6_R1785171554', 'LONG'),
    (952, 10018, 'tp', 'reset_cleared', 729.6, 0.7036, 1785171647, 1785171638, 3, 'CQB_10018_TP_3_6_R1785171638', 'LONG'),
    (954, 10018, 'entry', 'reset_cleared', 7.4, 0.7036, 1785171706, 1785171659, 4, 'CQB_10018_ENTRY_4_1', 'LONG'),
    (1072, 10018, 'tp', 'reset_cleared', 7.4, 0.7036, 1785186116, 1785185214, 4, 'CQB_10018_TP_4_1_R1785185214', 'LONG'),
    (1083, 10018, 'entry', 'reset_cleared', 7.4, 0.7011, 1785186800, 1785186740, 5, 'CQB_10018_ENTRY_5_1_1785186740', 'LONG'),
    (1085, 10018, 'grid', 'reset_cleared', 17.2, 0.6978, 1785189729, 1785186806, 5, 'CQB_10018_GRID_5_2', 'LONG'),
    (1109, 10018, 'grid', 'reset_cleared', 39.8, 0.6953, 1785192051, 1785189737, 5, 'CQB_10018_GRID_5_3', 'LONG'),
    (1130, 10018, 'grid', 'reset_cleared', 91.8, 0.6925, 1785192059, 1785192056, 5, 'CQB_10018_GRID_5_4', 'LONG'),
    (1132, 10018, 'grid', 'reset_cleared', 211.3, 0.6897, 1785192065, 1785192064, 5, 'CQB_10018_GRID_5_5', 'LONG'),
    (1133, 10018, 'grid', 'reset_cleared', 491.0, 0.6866, 1785192367, 1785192071, 5, 'CQB_10018_GRID_5_6', 'LONG'),
    (1142, 10018, 'tp', 'reset_cleared', 858.5, 0.6983, 1786331407, 1785193277, 5, 'CQB_10018_TP_5_6_R1785193277', 'LONG'),
    (1184, 10018, 'entry', 'reset_cleared', 7.6, 0.6851, 1786000031, 1786000030, 6, 'CQB_10018_ENTRY_6_1_1786000029', 'LONG'),
    (1338, 10018, 'entry', 'reset_cleared', 7.5, 0.6915, 1786332081, 1786332078, 8, 'CQB_10018_ENTRY_8_1', 'LONG'),
    (1340, 10018, 'grid', 'reset_cleared', 17.4, 0.6875, 0, 1786332088, 8, 'CQB_10018_GRID_8_2', 'LONG'),
    (1408, 10018, 'tp', 'reset_cleared', 24.9, 0.6954, 1786342860, 1786342261, 8, 'CQB_10018_TP_8_2_R1786342261', 'LONG'),
    (1415, 10018, 'entry', 'reset_cleared', 7.5, 0.694, 1786342933, 1786342906, 10, 'CQB_10018_ENTRY_10_1', 'LONG'),
    (1417, 10018, 'grid', 'reset_cleared', 17.3, 0.6912, 0, 1786342937, 10, 'CQB_10018_GRID_10_2', 'LONG'),
    (1531, 10018, 'tp', 'reset_cleared', 7.5, 0.6978, 0, 1786351041, 10, 'CQB_10018_TP_10_1_R1786351041', 'LONG'),
    (1546, 10018, 'grid', 'reset_cleared', 41.0, 0.6746, 1786954855, 1786940395, 10, 'CQB_10018_GRID_10_3', 'LONG'),
    (1703, 10018, 'entry', 'reset_cleared', 7.4, 0.7083, 0, 1787191629, 11, 'CQB_10018_ENTRY_11_1', 'LONG'),
    (1717, 10018, 'grid', 'reset_cleared', 17.1, 0.7041, 0, 1787192599, 11, 'CQB_10018_GRID_11_2', 'LONG'),
    (1731, 10018, 'grid', 'reset_cleared', 39.4, 0.7004, 1787194940, 1787194365, 11, 'CQB_10018_GRID_11_3', 'LONG'),
    (1750, 10018, 'grid', 'reset_cleared', 91.1, 0.6968, 1787195427, 1787194943, 11, 'CQB_10018_GRID_11_4', 'LONG'),
    (1925, 10018, 'tp', 'reset_cleared', 83.6, 0.7007, 1787207070, 1787207068, 11, 'CQB_10018_TP_11_4_R1787207067', 'LONG'),
    (1927, 10018, 'tp', 'reset_cleared', 64.2, 0.7003, 1787207078, 1787207076, 11, 'CQB_10018_TP_11_4_R1787207076', 'LONG'),
    (1928, 10018, 'close', 'reset_cleared', 7.2, 0.6991, 1787207085, 1787207084, 11, 'CQB_10018_CLOSE_11_1787207084', 'LONG'),
    (1935, 10018, 'entry', 'reset_cleared', 7.4, 0.7026, 1787207229, 1787207222, 12, 'CQB_10018_ENTRY_12_1_1787207221', 'LONG'),
    (1937, 10018, 'grid', 'reset_cleared', 17.1, 0.6993, 0, 1787207231, 12, 'CQB_10018_GRID_12_2', 'LONG'),
    (1960, 10018, 'tp', 'reset_cleared', 7.4, 0.7102, 0, 1787210833, 12, 'CQB_10018_TP_12_1_R1787210833', 'LONG'),
    (1985, 10018, 'tp', 'reset_cleared', 17.1, 0.7133, 1787214134, 1787214130, 12, 'CQB_10018_TP_12_2_R1787214129', 'LONG'),
    (1990, 10018, 'entry', 'reset_cleared', 7.3, 0.7132, 1787214211, 1787214168, 14, 'CQB_10018_ENTRY_14_1', 'LONG'),
    (2001, 10018, 'tp', 'reset_cleared', 7.3, 0.7231, 0, 1787215114, 14, 'CQB_10018_TP_14_1_R1787215113', 'LONG'),
    (2552, 10018, 'entry', 'reset_cleared', 6.5, 0.8027, 1788765317, 1788765306, 15, 'CQB_10018_ENTRY_15_1_1788765306', 'LONG'),
    (2554, 10018, 'grid', 'reset_cleared', 15.0, 0.7965, 1788766736, 1788765325, 15, 'CQB_10018_GRID_15_2', 'LONG'),
    (2570, 10018, 'grid', 'reset_cleared', 34.8, 0.7904, 1788766844, 1788766743, 15, 'CQB_10018_GRID_15_3', 'LONG'),
    (2587, 10018, 'tp', 'reset_cleared', 56.3, 0.8045, 1788768595, 1788767752, 15, 'CQB_10018_TP_15_3_R1788767752', 'LONG'),
    (2601, 10018, 'entry', 'reset_cleared', 6.4, 0.8078, 1788768712, 1788768708, 17, 'CQB_10018_ENTRY_17_1_1788768708', 'LONG'),
    (2623, 10018, 'tp', 'reset_cleared', 6.4, 0.8182, 0, 1788770523, 17, 'CQB_10018_TP_17_1_R1788770522', 'LONG'),
    (2634, 10018, 'entry', 'reset_cleared', 6.4, 0.8179, 0, 1788825858, 17, 'CQB_10018_ENTRY_17_1_1788825858', 'LONG'),
    (2647, 10018, 'tp', 'reset_cleared', 6.4, 0.8292, 1788830310, 1788826777, 17, 'CQB_10018_TP_17_1_R1788826777', 'LONG'),
    (2656, 10018, 'entry', 'reset_cleared', 6.3, 0.8297, 1788830350, 1788830347, 18, 'CQB_10018_ENTRY_18_1', 'LONG'),
    (2660, 10018, 'grid', 'reset_cleared', 14.5, 0.8233, 1788833545, 1788830377, 18, 'CQB_10018_GRID_18_2_R1788830377', 'LONG'),
    (2700, 10018, 'grid', 'reset_cleared', 33.7, 0.8175, 1788836306, 1788835935, 18, 'CQB_10018_GRID_18_3', 'LONG'),
    (2707, 10018, 'grid', 'reset_cleared', 78.1, 0.8115, 1788848503, 1788836307, 18, 'CQB_10018_GRID_18_4', 'LONG'),
    (2812, 10018, 'grid', 'reset_cleared', 180.9, 0.8062, 0, 1788848505, 18, 'CQB_10018_GRID_18_5', 'LONG'),
    (2853, 10018, 'tp', 'reset_cleared', 132.6, 0.8206, 0, 1788855711, 18, 'CQB_10018_TP_18_4_R1788855711', 'LONG'),
    (2864, 10018, 'tp', 'reset_cleared', 180.9, 0.8182, 1788912573, 1788912541, 18, 'CQB_10018_TP_18_5', 'LONG'),
    (2874, 10018, 'entry', 'reset_cleared', 6.3, 0.8223, 1788912913, 1788912908, 20, 'CQB_10018_ENTRY_20_1_1788912908', 'LONG'),
    (2876, 10018, 'grid', 'reset_cleared', 14.6, 0.8173, 1788913599, 1788912917, 20, 'CQB_10018_GRID_20_2', 'LONG'),
    (2881, 10018, 'grid', 'reset_cleared', 33.9, 0.8128, 1788916603, 1788913625, 20, 'CQB_10018_GRID_20_3', 'LONG'),
    (2907, 10018, 'grid', 'reset_cleared', 78.5, 0.8078, 1788923454, 1788916609, 20, 'CQB_10018_GRID_20_4', 'LONG'),
    (2960, 10018, 'grid', 'reset_cleared', 181.8, 0.8065, 1788923818, 1788923468, 20, 'CQB_10018_GRID_20_5', 'LONG'),
    (2961, 10018, 'tp', 'reset_cleared', 315.1, 0.8204, 1788929098, 1788923827, 20, 'CQB_10018_TP_20_5', 'LONG'),
    (2943, 10018, 'tp', 'reset_cleared', 54.8, 0.823, 1788941111, 1788941111, 22, 'CQB_10018_TP_20_3_R1788921114', 'LONG'),
    (2976, 10018, 'entry', 'reset_cleared', 6.4, 0.8196, 1788929148, 1788929144, 22, 'CQB_10018_ENTRY_22_1', 'LONG'),
    (2977, 10018, 'grid', 'reset_cleared', 14.7, 0.814, 1788932529, 1788929153, 22, 'CQB_10018_GRID_22_2', 'LONG'),
    (2986, 10018, 'grid', 'reset_cleared', 34.1, 0.8098, 0, 1788932533, 22, 'CQB_10018_GRID_22_3', 'LONG'),
    (2991, 10018, 'dust_close', 'reset_cleared', 0.4, 0.8098, 0, 1789004130, 22, 'CQB_10018_DUSTWIPE', 'LONG'),
    (3028, 10018, 'entry', 'reset_cleared', 6.8, 0.7613, 1789005555, 1789005554, 23, 'CQB_10018_ENTRY_23_1_1789005553', 'LONG'),
    (3060, 10018, 'tp', 'reset_cleared', 6.8, 0.7695, 1789009381, 1789009157, 23, 'CQB_10018_TP_23_1_R1789009157', 'LONG'),
    (3061, 10018, 'entry', 'reset_cleared', 6.8, 0.7698, 1789009430, 1789009421, 25, 'CQB_10018_ENTRY_25_1', 'LONG'),
    (3063, 10018, 'grid', 'reset_cleared', 15.6, 0.7649, 1789021647, 1789009441, 25, 'CQB_10018_GRID_25_2', 'LONG'),
    (3151, 10018, 'grid', 'reset_cleared', 36.3, 0.7627, 1789025630, 1789021654, 25, 'CQB_10018_GRID_25_3', 'LONG'),
    (3187, 10018, 'grid', 'reset_cleared', 83.7, 0.7595, 1789083589, 1789025639, 25, 'CQB_10018_GRID_25_4', 'LONG'),
    (3233, 10018, 'grid', 'reset_cleared', 202.0, 0.7208, 0, 1789083596, 25, 'CQB_10018_GRID_25_5', 'LONG'),
    (3334, 10018, 'tp', 'reset_cleared', 142.4, 0.7614, 0, 1789097092, 25, 'CQB_10018_TP_25_4_R1789097092', 'LONG'),
    (3414, 10018, 'tp', 'reset_cleared', 56.2, 0.7278, 1789185692, 1789185307, 25, 'CQB_10018_TP_25_5_R1789185307', 'LONG'),
    (3419, 10018, 'tp', 'reset_cleared', 34.3, 0.7278, 0, 1789185716, 25, 'CQB_10018_TP_25_5_R1789185716', 'LONG'),
    (3479, 10018, 'grid', 'reset_cleared', 7.2, 0.7182, 0, 1789211248, 26, 'CQB_10018_GRID_26_1', 'LONG'),
    (3661, 10018, 'grid', 'reset_cleared', 17.2, 0.697, 0, 1789340108, 26, 'CQB_10018_GRID_26_2', 'LONG'),
    (3792, 10018, 'tp', 'reset_cleared', 135.9, 0.7209, 1789365890, 1789354637, 26, 'CQB_10018_TP_26_2_R1789354637', 'LONG'),
    (3863, 10018, 'entry', 'reset_cleared', 7.4, 0.7079, 1789456630, 1789456595, 27, 'CQB_10018_ENTRY_27_1_1789456595', 'LONG'),
    (3868, 10018, 'grid', 'reset_cleared', 17.0, 0.7046, 0, 1789456633, 27, 'CQB_10018_GRID_27_2', 'LONG'),
    (3951, 10018, 'tp', 'reset_cleared', 21.8, 0.711, 1789465467, 1789465408, 27, 'CQB_10018_TP_27_2_R1789465408', 'LONG'),
    (3969, 10018, 'dust_close', 'reset_cleared', 7.1, 0.7123, 1789465804, 1789465804, 27, 'CQB_10018_DUST_27_2', 'BOTH'),
    (3986, 10018, 'manual_close', 'filled', 7.1, 0.7119, 0, 1789470708, 29, 'CQB_10018_MANUAL_CLOSE_29_1789470708', 'BOTH'),
    (3987, 10018, 'entry', 'filled', 7.3, 0.7102, 1789470884, 1789470884, 29, 'CQB_10018_ENTRY_29_1', 'LONG'),
]

def create_test_fixture():
    """Create a test database with the FROZEN SUI order history (decoupled from live DB)."""
    fd, tmp_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

    conn = sqlite3.connect(tmp_path)
    cur = conn.cursor()

    cur.execute('''
        CREATE TABLE bot_orders (
            id INTEGER PRIMARY KEY,
            bot_id INTEGER,
            order_type TEXT,
            status TEXT,
            filled_amount REAL,
            price REAL,
            filled_at INTEGER,
            created_at INTEGER,
            cycle_id INTEGER,
            client_order_id TEXT,
            position_side TEXT
        )
    ''')

    cur.execute('''
        CREATE TABLE bots (
            id INTEGER PRIMARY KEY,
            name TEXT,
            direction TEXT,
            bot_type TEXT,
            pair TEXT,
            is_active INTEGER DEFAULT 1
        )
    ''')

    # Insert frozen bot
    cur.execute("INSERT INTO bots VALUES (10018, 'sui long', 'LONG', 'standard', 'SUI/USDC', 1)")

    # Insert frozen orders -- NEVER read from a live database.
    cur.executemany('''
        INSERT INTO bot_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)
    ''', FROZEN_10018_ORDERS)

    conn.commit()
    conn.close()

    return tmp_path


def run_cycle_sweep_buggy(db_path, bot_id, current_cycle):
    """Current buggy sweep: only looks at status='filled' orders."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    swept = []
    for cycle in range(current_cycle):
        cur.execute('''
            SELECT 
                SUM(CASE WHEN order_type IN ('entry','grid','adoption','adoption_add','carry') THEN filled_amount ELSE 0.0 END),
                SUM(CASE WHEN order_type IN ('tp','close','dust_close','sl','adoption_reduce','flatten_close') THEN filled_amount ELSE 0.0 END)
            FROM bot_orders
            WHERE bot_id = ? AND cycle_id = ? AND status = 'filled'
        ''', (bot_id, cycle))
        entry, exit = cur.fetchone()
        entry = entry or 0
        exit = exit or 0
        
        if abs(entry - exit) < 1e-6 and (entry > 0 or exit > 0):
            swept.append(cycle)
    
    conn.close()
    return swept

def run_cycle_sweep_fixed(db_path, bot_id, current_cycle):
    """Fixed sweep: looks at ALL fills regardless of status."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    swept = []
    for cycle in range(current_cycle):
        cur.execute('''
            SELECT 
                SUM(CASE WHEN order_type IN ('entry','grid','adoption','adoption_add','carry') THEN filled_amount ELSE 0.0 END),
                SUM(CASE WHEN order_type IN ('tp','close','dust_close','sl','adoption_reduce','flatten_close') THEN filled_amount ELSE 0.0 END)
            FROM bot_orders
            WHERE bot_id = ? AND cycle_id = ? AND filled_amount > 0
        ''', (bot_id, cycle))
        entry, exit = cur.fetchone()
        entry = entry or 0
        exit = exit or 0
        
        if abs(entry - exit) < 1e-6 and (entry > 0 or exit > 0):
            swept.append(cycle)
    
    conn.close()
    return swept

def test_sweep_reproduces_bug():
    """Test that buggy sweep finds NOTHING (all orders are reset_cleared)."""
    db_path = create_test_fixture()
    try:
        swept = run_cycle_sweep_buggy(db_path, 10018, 26)
        print(f"Buggy sweep result: {swept}")
        assert swept == [], f"Expected [], got {swept}"
        print("✅ Buggy sweep correctly finds NOTHING (all orders are reset_cleared)")
        return True
    finally:
        os.unlink(db_path)

def test_sweep_fixed_identifies_correct_cycles():
    """Test that fixed sweep correctly identifies which cycles to sweep."""
    db_path = create_test_fixture()
    try:
        swept = run_cycle_sweep_fixed(db_path, 10018, 26)
        print(f"Fixed sweep result: {swept}")
        
        expected_swept = [0, 1, 2, 3, 4, 5, 8, 11, 12, 14, 15, 17, 18, 20, 22, 23]
        expected_not_swept = [6, 10, 25]
        
        assert swept == expected_swept, f"Expected {expected_swept}, got {swept}"
        print("✅ Fixed sweep correctly identifies balanced cycles")
        
        for cycle in expected_not_swept:
            assert cycle not in swept, f"Cycle {cycle} should NOT be swept"
        print("✅ Fixed sweep correctly does NOT sweep unbalanced cycles (6, 10, 25)")
        
        return True
    finally:
        os.unlink(db_path)

def test_classify_reset_cleared():
    """Test the classification logic on the FROZEN SUI order history.

    Decoupled from crypto_bot.db: assertions are pinned to FROZEN_10018_ORDERS,
    so live trading on bot 10018 can never mutate them.
    """
    db_path = create_test_fixture()
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        
        # Get cycles with reset_cleared orders
        cur.execute('''
            SELECT DISTINCT cycle_id FROM bot_orders 
            WHERE bot_id = 10018 AND status = 'reset_cleared' AND filled_amount > 0
            ORDER BY cycle_id
        ''')
        cycles = [r[0] for r in cur.fetchall()]
        
        classifications = {}
        for cycle in cycles:
            # Count reset_cleared orders
            cur.execute('''
                SELECT COUNT(*), SUM(filled_amount)
                FROM bot_orders
                WHERE bot_id = 10018 AND cycle_id = ? AND status = 'reset_cleared' AND filled_amount > 0
            ''', (cycle,))
            reset_count, reset_units = cur.fetchone()
            reset_count = reset_count or 0
            reset_units = reset_units or 0
            
            # True balance
            cur.execute('''
                SELECT 
                    SUM(CASE WHEN order_type IN ('entry','grid','adoption','adoption_add','carry') THEN filled_amount ELSE 0.0 END),
                    SUM(CASE WHEN order_type IN ('tp','close','dust_close','sl','adoption_reduce','flatten_close') THEN filled_amount ELSE 0.0 END)
                FROM bot_orders
                WHERE bot_id = 10018 AND cycle_id = ? AND filled_amount > 0
            ''', (cycle,))
            entry, exit = cur.fetchone()
            entry = entry or 0
            exit = exit or 0
            net = entry - exit
            
            balanced = abs(net) < 1e-6
            classification = 'LEGITIMATE' if balanced else 'INCORRECT'
    
            classifications[cycle] = {
                'reset_orders': reset_count,
                'reset_units': reset_units,
                'entry': entry,
                'exit': exit,
                'net': net,
                'balanced': balanced,
                'classification': classification
            }
        
        conn.close()
        
        # Verify exact expected results (pinned to frozen fixture)
        assert classifications[6]['classification'] == 'INCORRECT'
        assert classifications[6]['reset_orders'] == 1
        assert classifications[6]['reset_units'] == 7.6
        assert classifications[6]['net'] == 7.6
        
        assert classifications[10]['classification'] == 'INCORRECT'
        assert classifications[10]['reset_orders'] == 4
        assert classifications[10]['reset_units'] == 73.3
        assert classifications[10]['net'] == 58.3
        
        # Cycle 25 currently has 8 reset_cleared orders totaling 577.3 units
        # (incl. id 3419 tp 34.3 re-sealed during the 2026-09-15 session).
        assert classifications[25]['classification'] == 'INCORRECT'
        assert classifications[25]['reset_orders'] == 8
        assert classifications[25]['reset_units'] == 577.3
        assert abs(classifications[25]['net'] - 111.5) < 0.01
        
        # Verify all other cycles are LEGITIMATE
        for cycle in [0,1,2,3,4,5,8,11,12,14,15,17,18,20,22,23]:
            assert classifications[cycle]['classification'] == 'LEGITIMATE', f"Cycle {cycle} should be LEGITIMATE"
        
        print("✅ Classification matches expected results exactly (frozen fixture)")
        print(f"   Cycle 6: {classifications[6]['reset_orders']} orders, {classifications[6]['reset_units']} units, net={classifications[6]['net']} → INCORRECT")
        print(f"   Cycle 10: {classifications[10]['reset_orders']} orders, {classifications[10]['reset_units']} units, net={classifications[10]['net']} → INCORRECT")
        print(f"   Cycle 25: {classifications[25]['reset_orders']} orders, {classifications[25]['reset_units']} units, net={classifications[25]['net']} → INCORRECT")
        
        return True
    finally:
        os.unlink(db_path)

if __name__ == '__main__':
    print("=" * 60)
    print("SUI CYCLE-SWEEP REGRESSION TEST (Frozen Fixture)")
    print("=" * 60)
    
    test_sweep_reproduces_bug()
    print()
    test_sweep_fixed_identifies_correct_cycles()
    print()
    test_classify_reset_cleared()
    print()
    print("=" * 60)
    print("ALL TESTS PASSED ✅")
    print("=" * 60)
