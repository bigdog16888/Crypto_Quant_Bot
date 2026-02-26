
import sqlite3
import os
import sys
import pandas as pd

sys.path.append(os.getcwd())
try:
    from config.settings import config
    from engine.exchange_interface import ExchangeInterface
except ImportError:
    print("Import failed.")
    sys.exit(1)

def audit_system_state():
    print("=== FULL SYSTEM AUDIT ===")
    
    # 1. Database State
    db_path = config.PATHS["DB_FILE"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    print("\n--- [DB] Active Bots ---")
    cursor.execute("SELECT id, name, pair, direction, status, is_active FROM bots WHERE is_active=1")
    bots = cursor.fetchall()
    active_bot_ids = []
    for b in bots:
        print(f"Bot {b['id']} ({b['name']}) | {b['pair']} | {b['direction']} | Status: {b['status']}")
        active_bot_ids.append(b['id'])
        
    print("\n--- [DB] Trade States ---")
    cursor.execute(f"SELECT bot_id, total_invested, entry_order_id, current_step FROM trades WHERE bot_id IN ({','.join(map(str, active_bot_ids))})")
    trades = cursor.fetchall()
    for t in trades:
        print(f"Bot {t['bot_id']} | Invested: {t['total_invested']} | Step: {t['current_step']}")

    conn.close()

    # 2. Exchange State
    print("\n--- [EXCHANGE] Open Orders ---")
    interface = ExchangeInterface()
    try:
        # Check BTC and XAU
        for symbol in ['BTC/USDC', 'XAU/USDT']:
            print(f"Checking {symbol}...")
            orders = interface.fetch_open_orders(symbol)
            if orders:
                for o in orders:
                    print(f"  [{symbol}] ID: {o['id']} | Type: {o['type']} | Side: {o['side']} | Amt: {o['amount']} | Price: {o['price']} | ClientID: {o['clientOrderId']}")
            else:
                print(f"  No open orders for {symbol}")
                
            print(f"Checking Positions for {symbol}...")
            # We need to scan all positions to be sure
            # But fetch_positions returns ALL non-zero positions usually
            pass
            
        print("\n--- [EXCHANGE] All Positions ---")
        positions = interface.fetch_positions()
        if positions:
            for p in positions:
                print(f"  Symbol: {p['symbol']} | Side: {p['side']} | Size: {p['contracts']} | uPnL: {p['unrealizedPnl']}")
        else:
            print("  No positions found.")

    except Exception as e:
        print(f"Exchange Error: {e}")

if __name__ == "__main__":
    audit_system_state()
