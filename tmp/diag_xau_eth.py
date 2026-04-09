import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# XAUUSDT bots
print("=== XAU BOTS ===")
c.execute("SELECT id, name, pair, direction, is_active FROM bots WHERE pair LIKE '%XAU%'")
for r in c.fetchall():
    print(r)

# ETH bots    
print("\n=== ETH BOTS ===")
c.execute("SELECT id, name, pair, direction, total_invested, avg_entry_price, current_step FROM bots b LEFT JOIN trades t ON b.id=t.bot_id WHERE b.pair LIKE '%ETH%USDC%'")
for r in c.fetchall():
    print(r)

# Active positions for both
print("\n=== ACTIVE POSITIONS XAU/ETH ===")
c.execute("SELECT bot_id, pair, side, size FROM active_positions WHERE pair LIKE '%XAU%' OR pair LIKE '%ETH%'")
for r in c.fetchall():
    print(r)

conn.close()
