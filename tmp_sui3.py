"""Precise SUI mismatch diagnosis using correct tables."""
import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# 1. Bot IDs
c.execute("SELECT id, name, direction FROM bots WHERE pair='SUI/USDC:USDC'")
bots = c.fetchall()
bot_ids = ','.join(str(b[0]) for b in bots)
print("=== SUI Bots ===")
for b in bots:
    print(f"  {b[0]}: {b[1]} ({b[2]})")

# 2. Trade STATE table (where per-bot current qty is stored)
c.execute(f"SELECT bot_id, current_step, total_invested, avg_entry_price FROM trades WHERE bot_id IN ({bot_ids})")
print("\n=== TRADES (live state) ===")
for r in c.fetchall():
    qty = r[2] / r[3] if r[3] else 0
    print(f"  bot_id={r[0]}, step={r[1]}, invested={r[2]:.2f}, avg={r[3]:.5f}, qty={qty:.2f}")

# 3. Raw filled amounts from bot_orders
c.execute(f"""
    SELECT b.direction, bo.order_type, SUM(bo.filled_amount)
    FROM bot_orders bo JOIN bots b ON bo.bot_id=b.id
    WHERE bo.bot_id IN ({bot_ids}) AND bo.filled_amount > 0
    GROUP BY b.direction, bo.order_type
""")
rows = c.fetchall()
long_buys  = sum(r[2] for r in rows if r[0]=='LONG'  and r[1].lower() in ('grid','entry'))
long_sells = sum(r[2] for r in rows if r[0]=='LONG'  and r[1].lower() == 'tp')
short_sells= sum(r[2] for r in rows if r[0]=='SHORT' and r[1].lower() in ('grid','entry'))
short_buys = sum(r[2] for r in rows if r[0]=='SHORT' and r[1].lower() == 'tp')

print("\n=== ORDER-BASED NET MATH ===")
print(f"  LONG  buys={long_buys:.2f}, TP_sells={long_sells:.2f}  → net_long={long_buys-long_sells:.2f}")
print(f"  SHORT sells={short_sells:.2f}, TP_buys={short_buys:.2f} → net_short={short_sells-short_buys:.2f}")
db_net = (long_buys - long_sells) - (short_sells - short_buys)
exch = -28541.10
diff = db_net - exch
print(f"\n  DB net (long minus short): {db_net:.2f}")
print(f"  Exchange SHORT:             {exch:.2f}")
print(f"  Gap (DB - Exch):            {diff:.4f} SUI  ≈ ${diff*1.033:.2f}")

print("\n=== INDIVIDUAL ROWS ===")
for r in rows:
    print(f"  dir={r[0]}, type={r[1]}, filled={r[2]:.2f}")

# 4. TP filled amounts at each step
print("\n=== ALL TP FILLS ===")
c.execute(f"""
    SELECT bo.bot_id, bo.step, bo.status, bo.amount, bo.filled_amount, bo.price
    FROM bot_orders bo WHERE bo.bot_id IN ({bot_ids}) AND bo.order_type='tp' AND bo.filled_amount > 0
    ORDER BY bo.bot_id, bo.step
""")
for r in c.fetchall():
    print(f"  bot={r[0]}, step={r[1]}, status={r[2]}, amt={r[3]:.2f}, filled={r[4]:.2f}, price={r[5]:.4f}")

conn.close()
