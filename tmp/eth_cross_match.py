import sqlite3
import sys, os, time
from collections import defaultdict
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

def check():
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    
    # 1. Get all filled bot_orders for ALL ETH bots
    print("Fetching local DB bot_orders for ETH bots...")
    c.execute("""
        SELECT order_id, order_type, amount, filled_amount, bot_id
        FROM bot_orders 
        WHERE bot_id IN (10011, 10021, 100002) AND order_id IS NOT NULL AND status != 'canceled'
    """)
    db_orders = c.fetchall()
    
    db_fills = defaultdict(float)
    db_side = {}
    for oid, otype, amt, filled, bot_id in db_orders:
        if filled is None: filled = 0.0
        db_fills[str(oid)] += float(filled)
        
        # Infer side based on bot direction and order type
        c.execute("SELECT direction FROM bots WHERE id=?", (bot_id,))
        bdir = c.fetchone()[0]
        if bdir == 'SHORT':
            db_side[str(oid)] = 'sell' if otype in ('entry','grid','adoption') else 'buy'
        else:
            db_side[str(oid)] = 'buy' if otype in ('entry','grid','adoption') else 'sell'
                
    conn.close()
    print(f"Total OIDs in DB with fills: {len(db_fills)}")
    
    # 2. Get all Binance trades
    print("\nFetching Binance trades...")
    ex = ExchangeInterface('future')
    since = int((time.time() - 86400 * 3) * 1000) # last 3 days
    trades = ex.fetch_my_trades('ETHUSDC', since=since, limit=1000)
    print(f"Fetched {len(trades)} trades from Binance")
    
    ex_fills = defaultdict(float)
    ex_side = {}
    for t in trades:
        oid = str(t.get('order', ''))
        qty = float(t.get('amount', 0))
        ex_fills[oid] += qty
        ex_side[oid] = t.get('side', '').lower()
        
    print("\n=== MISMATCHES ===")
    mismatches = 0
    # Check what's in Exchange but diff in DB
    for oid, ex_qty in ex_fills.items():
        db_qty = db_fills.get(oid, 0.0)
        
        # Tolerance for float comparison
        if abs(ex_qty - db_qty) > 0.00001:
            print(f"OID {oid} -> Exchange QTY: {ex_qty:.4f} ({ex_side.get(oid)}), DB QTY: {db_qty:.4f} (Side: {db_side.get(oid, 'UNKNOWN')}) | Diff: {ex_qty - db_qty:.4f}")
            if db_qty == 0.0:
                print(f"  -> MISSING ENTIRELY FROM DB")
            mismatches += 1
            
    if mismatches == 0:
        print("All exchange fills match DB exactly.")

if __name__ == '__main__':
    check()
