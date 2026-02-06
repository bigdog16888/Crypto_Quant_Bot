"""Deep diagnostic to find state sync gaps"""
import sqlite3
from engine.exchange_interface import ExchangeInterface

def main():
    print("=" * 70)
    print("FUNDAMENTAL STATE SYNC DIAGNOSTIC")
    print("=" * 70)
    
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    
    # 1. All trades with any data
    print("\n[1] TRADES TABLE (all rows with any data)")
    print("-" * 60)
    c.execute('''
        SELECT t.bot_id, b.name, b.pair, t.current_step, t.total_invested, 
               t.avg_entry_price, t.target_tp_price, t.entry_order_id, t.tp_order_id
        FROM trades t
        JOIN bots b ON t.bot_id = b.id
        WHERE b.is_active = 1
    ''')
    for row in c.fetchall():
        bot_id, name, pair, step, invested, entry, tp, entry_oid, tp_oid = row
        in_trade = invested > 0
        has_orders = entry_oid or tp_oid
        status = "IN TRADE" if in_trade else ("HAS ORDERS" if has_orders else "IDLE")
        print(f"  Bot {bot_id} | {name:20} | invested=${invested or 0:.2f} | entry_order={entry_oid} | tp_order={tp_oid} | {status}")
        if has_orders and not in_trade:
            print(f"     ⚠️ BUG: Orders exist but total_invested=0!")
    
    # 2. Exchange state
    print("\n[2] EXCHANGE STATE")
    print("-" * 60)
    ex = ExchangeInterface(market_type='future')
    
    # Orders
    orders = ex.fetch_open_orders()
    print(f"  Open Orders: {len(orders) if orders else 0}")
    for o in (orders or []):
        oid = o.get('id', '')
        coid = o.get('clientOrderId', '')
        print(f"    {o.get('symbol')} | {o.get('side')} | {o.get('amount')} @ {o.get('price')}")
        print(f"      ID: {oid} | ClientID: {coid}")
        
        # Check if this order belongs to a bot
        if coid and coid.startswith('CQB_'):
            parts = coid.split('_')
            if len(parts) >= 3:
                bot_id = parts[1]
                order_type = parts[2]
                print(f"      -> Belongs to Bot {bot_id} ({order_type})")
    
    # Positions
    positions = ex.fetch_positions()
    active_pos = [p for p in positions if abs(float(p.get('contracts', 0))) > 0]
    print(f"\n  Active Positions: {len(active_pos)}")
    for p in active_pos:
        print(f"    {p.get('symbol')} | {p.get('side')} | {p.get('contracts')} @ {p.get('entryPrice')}")
    
    # 3. bot_orders table
    print("\n[3] BOT_ORDERS TABLE (open)")
    print("-" * 60)
    c.execute('''
        SELECT bo.bot_id, b.name, bo.order_type, bo.order_id, bo.status, bo.client_order_id
        FROM bot_orders bo
        JOIN bots b ON bo.bot_id = b.id
        WHERE bo.status = 'open'
    ''')
    rows = c.fetchall()
    print(f"  Count: {len(rows)}")
    for row in rows:
        print(f"    Bot {row[0]} ({row[1]}): {row[2]} | status={row[4]} | id={row[3]}")
    
    # 4. Identify the gap
    print("\n[4] STATE SYNC GAP ANALYSIS")
    print("-" * 60)
    
    # Find bots with orders on exchange but no total_invested
    c.execute('''
        SELECT t.bot_id, b.name, b.pair, t.entry_order_id, t.tp_order_id
        FROM trades t
        JOIN bots b ON t.bot_id = b.id
        WHERE (t.entry_order_id IS NOT NULL OR t.tp_order_id IS NOT NULL)
          AND t.total_invested = 0
          AND b.is_active = 1
    ''')
    orphan_orders = c.fetchall()
    
    if orphan_orders:
        print(f"  ⚠️ FOUND {len(orphan_orders)} BOTS WITH ORDERS BUT NO INVESTMENT:")
        for row in orphan_orders:
            print(f"    Bot {row[0]} ({row[1]}): entry={row[3]}, tp={row[4]}")
        print("\n  ROOT CAUSE: Order placed successfully but update_martingale_step() never called")
    else:
        print("  No orphan orders found")
    
    # Find exchange orders not tracked in DB
    if orders:
        tracked_ids = set()
        c.execute('SELECT order_id FROM bot_orders WHERE status = "open"')
        tracked_ids = {r[0] for r in c.fetchall()}
        c.execute('SELECT entry_order_id FROM trades WHERE entry_order_id IS NOT NULL')
        tracked_ids.update({r[0] for r in c.fetchall()})
        c.execute('SELECT tp_order_id FROM trades WHERE tp_order_id IS NOT NULL')
        tracked_ids.update({r[0] for r in c.fetchall()})
        
        untracked = [o for o in orders if o.get('id') not in tracked_ids]
        if untracked:
            print(f"\n  ⚠️ FOUND {len(untracked)} EXCHANGE ORDERS NOT IN DB:")
            for o in untracked:
                print(f"    {o.get('id')} | {o.get('symbol')} | {o.get('clientOrderId')}")
    
    conn.close()
    print("\n" + "=" * 70)

if __name__ == "__main__":
    main()
