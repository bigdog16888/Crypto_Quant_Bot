
import sqlite3
import time
import os
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

def check_system_round28():
    print("=== ROUND 28 SYSTEM DIAGNOSTIC ===")
    
    # 1. BOTS IN TRADE
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT b.id, b.name, b.pair, b.status, t.current_step, t.total_invested, t.target_tp_price, t.avg_entry_price
        FROM bots b
        LEFT JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active=1
    """)
    bots = c.fetchall()
    
    in_trade_count = 0
    print("\n[ACTIVE BOTS STATUS]")
    for b in bots:
        bid, name, pair, status, step, inv, tp, avg = b
        if status == 'IN TRADE':
            in_trade_count += 1
            inv = inv or 0
            # Calculate Next Grid Price (approx for display)
            # This logic assumes standard grid spacing, we just want a rough number or retrieve if stored
            # For now, we report what is in DB
            print(f"✅ Bot {bid} ({name}) | Status: IN TRADE | Step: {step} | Invested: ${inv:.2f}")
            print(f"   > TP Price: {tp if tp else 'N/A'}")
            print(f"   > Avg Entry: {avg if avg else 'N/A'}")
        else:
             print(f"⏺️ Bot {bid} ({name}) | Status: {status}")
             
    print(f"\nTotal Bots in Trade: {in_trade_count}")

    # 2. OPEN ORDERS
    print("\n[OPEN ORDERS]")
    c.execute("SELECT bot_id, order_type, price, amount, order_id FROM bot_orders WHERE status='open'")
    orders = c.fetchall()
    print(f"Total Open Orders: {len(orders)}")
    for o in orders:
        print(f"   Bot {o[0]} [{o[1]}] | Price: {o[2]} | Amount: {o[3]} | ID: {o[4]}")

    # 3. EXCHANGE POSITIONS
    print("\n[EXCHANGE POSITIONS]")
    try:
        ex = ExchangeInterface(market_type='future')
        # Try fetch_positions
        if hasattr(ex.exchange, 'fetch_positions'):
            positions = ex.exchange.fetch_positions()
            valid_pos = [p for p in positions if float(p.get('info', {}).get('positionAmt', 0)) != 0]
            print(f"Total Live Positions: {len(valid_pos)}")
            for p in valid_pos:
                amt = float(p.get('info', {}).get('positionAmt', 0))
                print(f"   {p['symbol']} | Size: {amt} | Entry: {p['entryPrice']}")
        else:
            print("   (Fallback fetch)")
            # Fallback logic if needed
            print("   Unable to fetch positions directly via standard method.")
    except Exception as e:
        print(f"   Error fetching positions: {e}")

    # 4. LOG WARNINGS/ERRORS (Last 50 lines)
    print("\n[RECENT LOG WARNINGS/ERRORS]")
    log_file = 'engine.log'
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                recent = lines[-200:] # Last 200 lines
                found = 0
                for line in recent:
                    if 'WARNING' in line or 'ERROR' in line or 'CRITICAL' in line:
                         # Filter out some noise if needed
                         print(f"   {line.strip()}")
                         found += 1
                if found == 0:
                    print("   No recent warnings/errors found in last 200 lines.")
        except Exception as e:
            print(f"   Could not read log file: {e}")
    else:
        print("   Log file not found.")

    conn.close()
    print("\n=== DIAGNOSTIC COMPLETE ===")

if __name__ == "__main__":
    check_system_round28()
