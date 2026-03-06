import sqlite3
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# Check all SOL bots
c.execute("SELECT id, name, status, direction, pair FROM bots WHERE id IN (10008, 10011, 10021)")
bots = c.fetchall()
for b in bots:
    print("Bot:", b)
    c.execute("SELECT total_invested, avg_entry_price, current_step, basket_start_time FROM trades WHERE bot_id=?", (b[0],))
    trade = c.fetchone()
    print("Trade:", trade)

# Check active_positions table for SOL
print("\n--- Active Positions (SOL) ---")
try:
    c.execute("SELECT * FROM active_positions WHERE pair LIKE '%SOL%'")
    [print(r) for r in c.fetchall()]
except Exception as e:
    print("active_positions error:", e)

conn.close()
