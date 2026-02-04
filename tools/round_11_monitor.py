import sqlite3
import json
import os
import sys

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from config.settings import config

def monitor_system():
    print("============================================================")
    print("ROUND 11: SYSTEM-WIDE HEALTH CHECK")
    print("============================================================")
    
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    # 1. Get All Active Bots
    cursor.execute("""
        SELECT id, name, pair, direction, is_active, config 
        FROM bots 
        WHERE is_active=1
    """)
    bots = cursor.fetchall()
    
    print(f"Checking {len(bots)} Active Bots...")
    
    bot_map = {} # pair -> bot_name
    
    for bot in bots:
        bid, name, pair, direction, active, cfg_json = bot
        normalized_pair = pair.replace('/', '').replace(':USDT', '').replace(':USDC', '').split(':')[0]
        bot_map[normalized_pair] = name
        
        # Get Trade Status
        cursor.execute("SELECT * FROM trades WHERE bot_id=?", (bid,))
        trade = cursor.fetchone()
        
        # Get Open Orders
        cursor.execute("SELECT count(*) FROM bot_orders WHERE bot_id=? AND status='open'", (bid,))
        open_orders = cursor.fetchone()[0]
        
        print(f"\n[Bot {bid}] {name} ({pair})")
        print(f" - Direction: {direction}")
        
        if trade:
            # trade table: id, bot_id, symbol, total_invested, avg_price, target_price, last_exit_price, last_exit_time, start_time, step
            invested = trade[3]
            step = trade[9]
            print(f" - Status: {'IN TRADE' if invested > 0 else 'Waiting'}")
            if invested > 0:
                print(f"   💰 Invested: ${invested:.2f} (Step {step})")
        else:
            print(f" - Status: NO TRADE RECORD (New/Idle)")
            
        print(f" - Open Orders (DB): {open_orders}")

    # 2. Exchange Check
    print("\n--- Exchange Position Verification ---")
    try:
        # Use Futures exchange as primary for now
        ex = ExchangeInterface(market_type='future') 
        positions = ex.fetch_positions()
        
        found_positions = 0
        for pos in positions:
            size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if size == 0: continue
            
            found_positions += 1
            symbol = pos['symbol']
            pnl = pos.get('unrealizedPnl', 0)
            side = 'LONG' if size > 0 else 'SHORT'
            
            clean_sym = symbol.replace('/', '').replace(':USDT', '').replace(':USDC', '').split(':')[0]
            owner = bot_map.get(clean_sym, "UNKNOWN/MANUAL")
            
            print(f" 📦 {symbol}: {side} {abs(size)} (uPnL: ${pnl})")
            print(f"    Owner: {owner}")
            
        if found_positions == 0:
            print(" ✅ Exchange is FLAT (No open positions).")
        else:
            print(f" ⚠️ Found {found_positions} active positions on exchange.")
            
    except Exception as e:
        print(f" ❌ Exchange Check Failed: {e}")

    conn.close()

if __name__ == "__main__":
    monitor_system()
