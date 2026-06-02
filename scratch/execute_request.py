import sys
import os
import sqlite3
import json

sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface
from config.settings import config

def run_sql():
    print("=== RUNNING SQL QUERY FOR ALL SUI BOTS ===")
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    query = """
    SELECT b.id, b.name, b.normalized_pair, b.direction, b.bot_type,
           t.open_qty, t.avg_entry_price, t.cycle_id, t.entry_confirmed, t.cycle_phase
    FROM trades t JOIN bots b ON b.id = t.bot_id
    WHERE b.normalized_pair LIKE '%SUI%'
    ORDER BY b.id;
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    cols = [description[0] for description in cursor.description]
    print(f"Columns: {cols}")
    for row in rows:
        print(row)
        
    print("\n=== ALL ACTIVE POSITIONS IN DB ===")
    cursor.execute("SELECT * FROM active_positions")
    ap_rows = cursor.fetchall()
    for row in ap_rows:
        print(row)
        
    print("\n=== ALL TRADES WITH NON-ZERO OPEN QTY ===")
    cursor.execute("SELECT bot_id, open_qty, avg_entry_price, cycle_id, cycle_phase FROM trades WHERE open_qty != 0")
    t_rows = cursor.fetchall()
    for row in t_rows:
        print(row)
    conn.close()
    print("=========================\n")

def run_python_exchange():
    print("=== RUNNING PYTHON EXCHANGE POSITION FETCH ===")
    ex = ExchangeInterface()
    exchange = ex.exchange
    try:
        # Check all SUI positions on the exchange using fetch_positions
        positions = ex.fetch_positions()
        if positions:
            for p in positions:
                if 'SUI' in p.get('symbol', ''):
                    print(f"Unified Symbol: {p['symbol']}, side: {p['side']}, contracts: {p['contracts']}, entryPrice: {p['entryPrice']}")
        else:
            print("No positions found.")
    except Exception as e:
        print(f"Error running fetch_positions: {e}")
    print("==============================================\n")

def search_log_mismatch():
    print("=== SEARCHING LOGS FOR MISMATCH INFO ===")
    log_files = ['engine.log'] + [f'engine.log.{i}' for i in range(1, 6)]
    found = False
    for lf in log_files:
        if os.path.exists(lf):
            try:
                with open(lf, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        if '64.18' in line or 'System -64' in line or 'Exchange' in line and '-64' in line:
                            print(f"{lf}: {line.strip()}")
                            found = True
            except Exception as e:
                print(f"Error reading {lf}: {e}")
    if not found:
        print("No match containing 64.18 found in engine logs.")
    print("========================================\n")

if __name__ == '__main__':
    run_sql()
    run_python_exchange()
    search_log_mismatch()
