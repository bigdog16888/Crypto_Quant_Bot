import sqlite3
import sys, os, time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

def check():
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    
    # Get ALL order IDs in bot_orders for this bot (any cycle, any status)
    c.execute("SELECT DISTINCT order_id FROM bot_orders WHERE bot_id=10011 AND order_id IS NOT NULL")
    known_oids = {str(r[0]) for r in c.fetchall()}
    conn.close()
    
    ex = ExchangeInterface('future')
    since = int((time.time() - 86400 * 3) * 1000)
    trades = ex.fetch_my_trades('ETHUSDC', since=since, limit=500)
    
    # Group by OID and sum qty
    from collections import defaultdict
    oid_qty = defaultdict(float)
    oid_side = {}
    oid_ts = {}
    for t in trades:
        oid = str(t.get('order', ''))
        qty = float(t.get('amount', 0))
        oid_qty[oid] += qty
        oid_side[oid] = t.get('side', '')
        oid_ts[oid] = t.get('timestamp', 0)
    
    print("=== UNKNOWN orders on Binance (fills NOT in bot_orders) ===")
    found_unknown = False
    for oid, qty in sorted(oid_qty.items(), key=lambda x: oid_ts.get(x[0], 0)):
        if oid not in known_oids:
            dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(oid_ts.get(oid, 0)/1000))
            print(f"  [{dt}] OID={oid} SIDE={oid_side[oid].upper()} QTY={qty:.4f} -- NOT IN BOT_ORDERS")
            found_unknown = True
    
    if not found_unknown:
        print("  All exchange fills match known bot_orders -> gap is in DB calculation")
    
    # Also print known orders that shouldn't contribute to net but might
    print("\n=== Total net from Binance fills (last 3 days) ===")
    total_buy = sum(q for oid, q in oid_qty.items() if oid_side.get(oid, '').lower() == 'buy')
    total_sell = sum(q for oid, q in oid_qty.items() if oid_side.get(oid, '').lower() == 'sell')
    print(f"  Bought: {total_buy:.4f} ETH, Sold: {total_sell:.4f} ETH")
    print(f"  Net SHORT position (buy-sell for SHORT bot): {total_buy - total_sell:.6f} ETH")

if __name__ == '__main__':
    check()
