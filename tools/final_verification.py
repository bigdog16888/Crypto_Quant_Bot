import sqlite3
import os
import sys
import re

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface

def check_status():
    print("============================================================")
    print("ROUND 13: SYSTEM HEALTH CHECK")
    print("============================================================")
    
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    # 1. Bots in Trade
    cursor.execute("SELECT b.id, b.name, b.pair, t.total_invested FROM bots b JOIN trades t ON b.id = t.bot_id WHERE t.total_invested > 0")
    active_trades = cursor.fetchall()
    print(f"1. Bots in Trade: {len(active_trades)}")
    for bot in active_trades:
        # Get open orders for this bot
        cursor.execute("SELECT count(*) FROM bot_orders WHERE bot_id=? AND status='open'", (bot[0],))
        order_count = cursor.fetchone()[0]
        print(f"   - [Bot {bot[0]}] {bot[1]} ({bot[2]}): Used ${bot[3]:.2f} | Open Orders: {order_count}")

    # 2. Total Open Orders
    cursor.execute("SELECT count(*) FROM bot_orders WHERE status='open'")
    total_orders = cursor.fetchone()[0]
    print(f"\n2. Total System Open Orders: {total_orders}")

    # 3. Open Positions (Exchange)
    print("\n3. Exchange Positions:")
    try:
        ex = ExchangeInterface(market_type='future')
        positions = ex.fetch_positions()
        valid_pos = [p for p in positions if float(p.get('contracts', 0) or p.get('size', 0)) != 0]
        
        if not valid_pos:
            print("   ✅ NONE (Exchange is Flat)")
        else:
            for p in valid_pos:
                amt = float(p.get('contracts', 0) or p.get('size', 0))
                print(f"   - {p['symbol']}: {amt} (uPnL: ${p.get('unrealizedPnl', 0)})")
    except Exception as e:
        print(f"   ❌ Error fetching positions: {e}")

    # 4. Log Health Check
    print("\n4. Backend Log Health (Last 500 lines):")
    log_file = "engine.log"
    if os.path.exists(log_file):
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()[-500:]
            
        errors = [l.strip() for l in lines if "ERROR" in l]
        warnings = [l.strip() for l in lines if "WARNING" in l and "ENABLING BINANCE DEMO" not in l and "Emergency Market Close" not in l]
        
        if not errors and not warnings:
             print("   ✅ Clean Logs (No Errors/Warnings)")
        else:
            if errors:
                print(f"   ❌ Found {len(errors)} ERRORS:")
                for e in errors[-3:]: print(f"      {e}")
            if warnings:
                print(f"   ⚠️ Found {len(warnings)} WARNINGS:")
                for w in warnings[-3:]: print(f"      {w}")
    else:
        print("   ⚠️ engine.log not found")

    conn.close()

if __name__ == "__main__":
    check_status()
