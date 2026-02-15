"""Check Bot 44 complete history to understand how SHORT was placed"""
import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

# Bot 44 current config
cur.execute("SELECT id, name, pair, direction, is_active FROM bots WHERE id = 44")
bot = cur.fetchone()
print(f"BOT 44 CURRENT CONFIG:")
print(f"  Name: {bot[1]}")
print(f"  Pair: {bot[2]}")
print(f"  Direction: {bot[3]}")
print(f"  Active: {bot[4]}")

# Check trade history for SELL entries (would indicate SHORT position)
cur.execute("""
    SELECT id, action, symbol, price, amount, notes, timestamp
    FROM trade_history 
    WHERE bot_id = 44 AND action IN ('BUY', 'SELL')
    ORDER BY id DESC LIMIT 20
""")
trades = cur.fetchall()
print(f"\nBOT 44 TRADE HISTORY (BUY/SELL):")
for t in trades:
    print(f"  {t[6]}: {t[1]} {t[4]} @ ${t[3]} | {t[5][:40] if t[5] else ''}")

# Count buys vs sells
cur.execute("SELECT action, COUNT(*) FROM trade_history WHERE bot_id = 44 AND action IN ('BUY', 'SELL') GROUP BY action")
counts = cur.fetchall()
print(f"\nACTION COUNTS:")
for c in counts:
    print(f"  {c[0]}: {c[1]}")

# Check if there was ever a direction change
cur.execute("""
    SELECT notes, timestamp FROM trade_history 
    WHERE bot_id = 44 AND (notes LIKE '%direction%' OR notes LIKE '%config%' OR action = 'CONFIG_CHANGE')
    ORDER BY id DESC LIMIT 10
""")
config_changes = cur.fetchall()
if config_changes:
    print(f"\nCONFIG CHANGES:")
    for c in config_changes:
        print(f"  {c}")
else:
    print(f"\nNo config changes recorded")

# Check bot_orders for SELL entries (entry orders that created SHORT)
cur.execute("""
    SELECT order_id, order_type, status, price, amount 
    FROM bot_orders 
    WHERE bot_id = 44 AND order_type = 'entry'
    ORDER BY id DESC LIMIT 10
""")
entries = cur.fetchall()
print(f"\nBOT 44 ENTRY ORDERS:")
for e in entries:
    print(f"  {e[0]}: {e[1]} | {e[2]} | ${e[3]} x {e[4]}")

conn.close()
