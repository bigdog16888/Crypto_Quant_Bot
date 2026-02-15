"""COMPLETE SYSTEM RESET AND FIX
1. Close all open positions on exchange
2. Cancel all open orders
3. Reset all bots to idle
4. Clean the database
"""
import sqlite3
from engine.exchange_interface import ExchangeInterface

print("=" * 80)
print("COMPLETE SYSTEM RESET")
print("=" * 80)

ex = ExchangeInterface(market_type='future')

# 1. Close all positions
print("\n[1] CLOSING ALL POSITIONS...")
positions = ex.fetch_positions()
for p in positions:
    contracts = float(p.get('contracts', 0) or 0)
    if abs(contracts) > 0:
        symbol = p.get('symbol')
        side = p.get('side')
        close_side = 'buy' if side == 'short' else 'sell'
        print(f"  Closing {symbol}: {side} {contracts}")
        try:
            order = ex.exchange.create_order(
                symbol=symbol,
                type='market',
                side=close_side,
                amount=abs(contracts),
                params={'reduceOnly': True}
            )
            print(f"    ✅ Closed: {order.get('id')}")
        except Exception as e:
            print(f"    ❌ Error: {e}")

# 2. Cancel all open orders
print("\n[2] CANCELLING ALL OPEN ORDERS...")
orders = ex.fetch_open_orders()
for o in orders or []:
    try:
        ex.exchange.cancel_order(o.get('id'), o.get('symbol'))
        print(f"  ✅ Cancelled: {o.get('id')}")
    except Exception as e:
        print(f"  ❌ Error cancelling {o.get('id')}: {e}")

# 3. Reset all bots in database
print("\n[3] RESETTING ALL BOTS TO IDLE...")
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

# Reset trades table
cur.execute('''
    UPDATE trades SET
        current_step = 0,
        total_invested = 0,
        avg_entry_price = 0,
        target_tp_price = 0,
        last_exit_price = 0,
        last_exit_time = 0,
        entry_confirmed = 0,
        basket_start_time = 0,
        entry_order_id = NULL,
        tp_order_id = NULL,
        bot_position_id = NULL,
        close_type = NULL
''')
print(f"  ✅ Reset {cur.rowcount} trade records")

# Clear bot_orders
cur.execute("DELETE FROM bot_orders")
print(f"  ✅ Cleared bot_orders table")

# Clear bot_ownership if it exists
try:
    cur.execute("DELETE FROM bot_ownership")
    print(f"  ✅ Cleared bot_ownership table")
except:
    pass

conn.commit()
conn.close()

# 4. Verify
print("\n[4] VERIFICATION...")
positions = ex.fetch_positions()
active = [p for p in positions if abs(float(p.get('contracts', 0) or 0)) > 0]
orders = ex.fetch_open_orders()
print(f"  Open positions: {len(active)}")
print(f"  Open orders: {len(orders) if orders else 0}")

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM trades WHERE total_invested > 0")
in_trade = cur.fetchone()[0]
print(f"  Bots in trade: {in_trade}")
conn.close()

print("\n" + "=" * 80)
print("RESET COMPLETE!")
print("=" * 80)
