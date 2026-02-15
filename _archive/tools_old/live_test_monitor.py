import sys
import os
import time
import sqlite3
import pandas as pd

# Add parent directory to path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)

from engine.database import get_connection, get_all_bots, get_bot_status
from engine.exchange_interface import ExchangeInterface

def monitor_live_test():
    print("\n🔍 LIVE TEST MONITORING REPORT")
    print("=============================")
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Active Bots
    cursor.execute("SELECT COUNT(*) FROM bots WHERE is_active = 1")
    active_bots = cursor.fetchone()[0]
    print(f"✅ Active Bots Configured: {active_bots}")
    
    # 2. Bots IN TRADE
    cursor.execute("SELECT b.id, b.name, b.pair, b.direction, t.total_invested, t.current_step FROM bots b JOIN trades t ON b.id = t.bot_id WHERE t.total_invested > 0")
    in_trade_bots = cursor.fetchall()
    print(f"📈 Bots Currently IN TRADE: {len(in_trade_bots)}")
    
    virtual_net_position = 0.0
    
    for bot in in_trade_bots:
        bid, name, pair, direction, invested, step = bot
        print(f"   - Bot {bid} ({name}): {direction} | Invested: ${invested:.2f} | Step: {step}")
        
        # Calculate virtual contribution
        # Need entry price to get size in coins
        cursor.execute("SELECT avg_entry_price FROM trades WHERE bot_id = ?", (bid,))
        entry_price = cursor.fetchone()[0]
        
        if entry_price > 0:
            size_coins = invested / entry_price
            if direction == 'SHORT':
                size_coins = -size_coins
            virtual_net_position += size_coins
            print(f"     -> Virtual Position: {size_coins:.4f} {pair.split('/')[0]}")
    
    print(f"\n📊 Net Virtual Position: {virtual_net_position:.4f} BTC")
    
    # 3. Open Orders
    cursor.execute("SELECT COUNT(*) FROM bot_orders WHERE status = 'open'")
    open_orders = cursor.fetchone()[0]
    print(f"📝 Open Orders in DB: {open_orders}")
    
    # Detail orders per bot
    cursor.execute("SELECT bot_id, count(*) FROM bot_orders WHERE status = 'open' GROUP BY bot_id")
    for row in cursor.fetchall():
        print(f"   - Bot {row[0]}: {row[1]} open orders")

    # 4. Exchange Comparison (Simulated Fetch)
    print("\n🔄 Exchange Position Check (Real-Time)...")
    try:
        exchange = ExchangeInterface(market_type='future')
        positions = exchange.fetch_positions()
        
        # Filter for BTC/USDC
        btc_pos = [p for p in positions if 'BTC' in p['symbol'] and 'USDC' in p['symbol']]
        
        total_exchange_net = 0.0
        if btc_pos:
            print(f"   Exchange Reports {len(btc_pos)} Positions:")
            for p in btc_pos:
                size = float(p.get('contracts', 0) or p.get('size', 0))
                side = p.get('side', 'long') # usually netted in future, so size is signed or side is distinct
                # In hedge mode, we might have long and short
                if side == 'short': size = -abs(size)
                else: size = abs(size)
                
                print(f"   - {p['symbol']}: {size:.4f} ({p.get('side', 'net')})")
                total_exchange_net += size
        else:
            print("   - No BTC/USDC positions found on exchange.")
            
        print(f"\n⚖️  COMPARISON:")
        print(f"   Virtual Net: {virtual_net_position:.4f}")
        print(f"   Exchange Net: {total_exchange_net:.4f}")
        
        diff = abs(virtual_net_position - total_exchange_net)
        if diff < 0.001:
            print("   ✅ MATCHED (within tolerance)")
        else:
            print(f"   ❌ MISMATCH (Diff: {diff:.4f})")
            print("      Note: This is expected if 'Ghost Trade' logic is working correctly to ignore aggregate mismatch!")
            
    except Exception as e:
        print(f"   ❌ Exchange Check Failed: {e}")

if __name__ == "__main__":
    monitor_live_test()
