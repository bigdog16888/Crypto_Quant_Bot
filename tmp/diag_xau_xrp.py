import sqlite3
import sys, os, time
from collections import defaultdict
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

def fix_xau():
    ex = ExchangeInterface('future')
    positions = ex.fetch_positions()
    for p in (positions or []):
        if 'XAU' in str(p.get('symbol', '')) and str(p.get('side', '')).lower() == 'long':
            contracts = float(p.get('contracts', 0))
            if contracts > 0:
                print(f"Closing {contracts} of {p['symbol']}")
                try:
                    res = ex.exchange.create_order(
                        symbol=p['symbol'],
                        type='market',
                        side='sell',
                        amount=contracts,
                        params={'reduceOnly': True}
                    )
                    print(f"XAU closed successfully: {res.get('id')}")
                except Exception as e:
                    print(f"Error closing XAU: {e}")

def check_xrp():
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    
    print("\nFetching local DB bot_orders for XRP bots...")
    c.execute("""
        SELECT order_id, order_type, amount, filled_amount, bot_id
        FROM bot_orders 
        WHERE bot_id IN (SELECT id FROM bots WHERE pair LIKE '%XRP%') 
        AND order_id IS NOT NULL AND status != 'canceled' AND filled_amount > 0
    """)
    db_orders = c.fetchall()
    
    db_fills = defaultdict(float)
    db_side = {}
    for oid, otype, amt, filled, bot_id in db_orders:
        filled_qty = float(filled) if filled else 0.0
        db_fills[str(oid)] += filled_qty
        
        c.execute("SELECT direction FROM bots WHERE id=?", (bot_id,))
        row = c.fetchone()
        bdir = row[0] if row else 'LONG'
        
        if bdir == 'SHORT':
            db_side[str(oid)] = 'sell' if otype in ('entry','grid','adoption') else 'buy'
        else:
            db_side[str(oid)] = 'buy' if otype in ('entry','grid','adoption') else 'sell'
                
    conn.close()
    
    print("\nFetching Binance trades...")
    ex = ExchangeInterface('future')
    since = int((time.time() - 86400 * 7) * 1000)
    trades = ex.fetch_my_trades('XRPUSDC', since=since, limit=1000)
    
    ex_fills = defaultdict(float)
    ex_side = {}
    for t in trades:
        oid = str(t.get('order', ''))
        qty = float(t.get('amount', 0))
        ex_fills[oid] += qty
        ex_side[oid] = t.get('side', '').lower()
        
    print("\n=== XRP MISMATCHES ===")
    mismatches = 0
    for oid, ex_qty in ex_fills.items():
        db_qty = db_fills.get(oid, 0.0)
        if abs(ex_qty - db_qty) > 0.00001:
            print(f"OID {oid} -> Exchange QTY: {ex_qty:.4f} ({ex_side.get(oid)}), DB QTY: {db_qty:.4f} (Side: {db_side.get(oid, 'UNKNOWN')}) | Diff: {ex_qty - db_qty:.4f}")
            if db_qty == 0.0:
                print("  -> MISSING ENTIRELY FROM DB")
            mismatches += 1
            
    if mismatches == 0:
        print("All XRP exchange fills match DB exactly.")

if __name__ == '__main__':
    fix_xau()
    check_xrp()
