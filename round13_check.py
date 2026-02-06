"""Round 13 Check - Post-Fix Verification"""
import sqlite3
from engine.exchange_interface import ExchangeInterface

print("=" * 70)
print("ROUND 13 - POST-FIX VERIFICATION")
print("=" * 70)

# 1. Exchange State
print("\n[1] EXCHANGE STATE")
print("-" * 50)
ex = ExchangeInterface(market_type='future')
positions = ex.fetch_positions()
active_pos = [p for p in positions if abs(float(p.get('contracts', 0) or 0)) > 0]
print(f"Active positions: {len(active_pos)}")
for p in active_pos:
    print(f"  {p.get('symbol')}: {p.get('side')} {p.get('contracts')} @ ${p.get('entryPrice')}")

orders = ex.fetch_open_orders()
print(f"\nOpen orders: {len(orders) if orders else 0}")
by_tag = {}
for o in orders or []:
    cid = o.get('clientOrderId', '')
    if cid.startswith('CQB_'):
        parts = cid.split('_')
        bot_id = parts[1] if len(parts) > 1 else 'unknown'
        by_tag[bot_id] = by_tag.get(bot_id, 0) + 1
for bot_id, count in by_tag.items():
    print(f"  Bot {bot_id}: {count} orders")

# 2. Database State
print("\n[2] DATABASE STATE")
print("-" * 50)
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

cur.execute('''
    SELECT t.bot_id, b.name, b.pair, t.current_step, t.total_invested, t.entry_order_id
    FROM trades t JOIN bots b ON t.bot_id = b.id
    WHERE t.total_invested > 0
''')
in_trade = cur.fetchall()
print(f"Bots in trade: {len(in_trade)}")
for r in in_trade:
    print(f"  Bot {r[0]} ({r[1]}): Step {r[3]}, ${r[4]:.2f}, entry_id={r[5]}")

cur.execute('''
    SELECT bot_id, COUNT(*) as cnt FROM bot_orders WHERE status = 'open' GROUP BY bot_id
''')
order_counts = cur.fetchall()
print(f"\nBots with open orders in DB:")
for r in order_counts:
    print(f"  Bot {r[0]}: {r[1]} open orders")

# 3. Sync Check
print("\n[3] SYNC CHECK")
print("-" * 50)
db_bots_in_trade = len(in_trade)
ex_positions = len(active_pos)

# Match: bots in trade should have positions
if db_bots_in_trade <= ex_positions:
    print(f"✅ SYNC OK: {db_bots_in_trade} bots in trade, {ex_positions} positions")
else:
    print(f"❌ DESYNC: {db_bots_in_trade} bots in trade but only {ex_positions} positions!")

# Check order ownership matches
for bot_id, order_count in order_counts:
    ex_count = by_tag.get(str(bot_id), 0)
    if ex_count == order_count:
        print(f"✅ Bot {bot_id}: {order_count} DB orders = {ex_count} exchange orders")
    else:
        print(f"⚠️ Bot {bot_id}: {order_count} DB orders vs {ex_count} exchange orders")

# 4. Over-adoption Check
print("\n[4] OVER-ADOPTION CHECK")
print("-" * 50)
cur.execute('''
    SELECT b.pair, COUNT(*) as cnt 
    FROM trades t JOIN bots b ON t.bot_id = b.id 
    WHERE t.total_invested > 0 
    GROUP BY b.pair HAVING cnt > 1
''')
multi = cur.fetchall()
if multi:
    print("❌ OVER-ADOPTION DETECTED:")
    for r in multi:
        print(f"   Pair {r[0]}: {r[1]} bots claim position!")
else:
    print("✅ No over-adoption: max 1 bot per pair in trade")

conn.close()
print("\n" + "=" * 70)
