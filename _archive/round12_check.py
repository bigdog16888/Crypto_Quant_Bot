"""Round 12 Comprehensive Health Check"""
import sqlite3
from engine.exchange_interface import ExchangeInterface

def main():
    print("=" * 70)
    print("ROUND 12 COMPREHENSIVE HEALTH CHECK")
    print("=" * 70)
    
    conn = sqlite3.connect('crypto_bot.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # 1. Active bots and their trade status
    print("\n[1] ACTIVE BOTS STATUS")
    print("-" * 60)
    c.execute("""
        SELECT b.id, b.name, b.pair, b.is_active, b.status, b.direction,
               COALESCE(t.total_invested, 0) as invested,
               COALESCE(t.avg_entry_price, 0) as entry,
               COALESCE(t.target_tp_price, 0) as tp,
               COALESCE(t.current_step, 0) as step
        FROM bots b
        LEFT JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active = 1
        ORDER BY b.id
    """)
    bots = c.fetchall()
    in_trade_count = 0
    for b in bots:
        is_in_trade = b['invested'] > 0
        if is_in_trade:
            in_trade_count += 1
            status_str = f"IN TRADE (Step {b['step']})"
        else:
            status_str = b['status']
        print(f"Bot {b['id']:2d} | {b['name']:<20} | {b['pair']:<15} | {status_str}")
        if is_in_trade:
            print(f"       Entry: ${b['entry']:.4f} | TP: ${b['tp']:.4f}")
    print(f"\n>>> Total Active: {len(bots)} | In Trade: {in_trade_count}")
    
    # 2. Open Orders in DB
    print("\n[2] OPEN ORDERS IN DATABASE")
    print("-" * 60)
    c.execute("""
        SELECT o.bot_id, b.name, o.order_type, o.price, o.amount, o.order_id, o.status
        FROM bot_orders o
        JOIN bots b ON o.bot_id = b.id
        WHERE o.status = 'open'
        ORDER BY o.bot_id
    """)
    db_orders = c.fetchall()
    if db_orders:
        for o in db_orders:
            print(f"  Bot {o['bot_id']:2d} ({o['name'][:12]:<12}) | {o['order_type']:<10} | {o['amount']:.6f} @ ${o['price']:.4f}")
    else:
        print("  (No open orders in database)")
    print(f"\n>>> Total open orders in DB: {len(db_orders)}")
    
    # 3. Exchange open orders
    print("\n[3] EXCHANGE OPEN ORDERS")
    print("-" * 60)
    ex = ExchangeInterface(market_type='future')
    all_orders = []
    try:
        all_orders = ex.fetch_open_orders()
        if all_orders:
            for o in all_orders:
                tag = o.get('clientOrderId', 'N/A')
                print(f"  {o['symbol']:<15} | {o['type']:<6} | {o['side']:<5} | {o['amount']:.6f} @ ${o['price']:.4f} | Tag: {tag[:25]}")
        else:
            print("  (No open orders on exchange)")
        print(f"\n>>> Total orders on exchange: {len(all_orders)}")
    except Exception as e:
        print(f"Error fetching orders: {e}")
    
    # 4. Exchange positions
    print("\n[4] EXCHANGE POSITIONS")
    print("-" * 60)
    active_pos = []
    try:
        positions = ex.fetch_positions()
        active_pos = [p for p in positions if float(p.get('contracts', 0)) != 0]
        if active_pos:
            for p in active_pos:
                side = p.get('side', 'unknown')
                contracts = p.get('contracts', 0)
                entry = float(p.get('entryPrice', 0))
                unrealized = float(p.get('unrealizedPnl', 0))
                print(f"  {p['symbol']:<18} | {side:>5} | Size: {contracts:>12} | Entry: ${entry:>10.4f} | uPnL: ${unrealized:>10.2f}")
        else:
            print("  (No open positions on exchange)")
        print(f"\n>>> Active positions on exchange: {len(active_pos)}")
    except Exception as e:
        print(f"Error fetching positions: {e}")
    
    # 5. Validation summary
    print("\n" + "=" * 70)
    print("VALIDATION SUMMARY")
    print("=" * 70)
    
    expected_orders = in_trade_count * 2
    
    print(f"  Bots in trade:             {in_trade_count}")
    print(f"  Expected orders (2/bot):   {expected_orders}")
    print(f"  Actual orders in DB:       {len(db_orders)}")
    print(f"  Orders on Exchange:        {len(all_orders)}")
    print(f"  Positions on Exchange:     {len(active_pos)}")
    
    # Sync checks
    print("\n--- SYNC CHECKS ---")
    if in_trade_count == len(active_pos):
        print(f"  ✅ Bots in trade ({in_trade_count}) matches exchange positions ({len(active_pos)})")
    else:
        print(f"  ⚠️ MISMATCH: {in_trade_count} bots in trade vs {len(active_pos)} exchange positions")
    
    if len(db_orders) == len(all_orders):
        print(f"  ✅ DB orders ({len(db_orders)}) matches exchange orders ({len(all_orders)})")
    else:
        print(f"  ⚠️ MISMATCH: {len(db_orders)} orders in DB vs {len(all_orders)} orders on exchange")
    
    if in_trade_count == 0:
        print(f"\n  ℹ️  All bots are currently idle (not in trade)")
    elif len(db_orders) == expected_orders:
        print(f"  ✅ Order count ({len(db_orders)}) matches expected ({expected_orders})")
    else:
        print(f"  ⚠️ MISMATCH: {len(db_orders)} orders vs expected {expected_orders} (2 per bot in trade)")
    
    conn.close()
    print("\n" + "=" * 70)

if __name__ == "__main__":
    main()
