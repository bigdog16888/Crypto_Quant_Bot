#!/usr/bin/env python3
"""Analyze why there are so many open orders."""

import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# Count active bots
c.execute('SELECT id, name, pair FROM bots WHERE is_active=1')
bots = c.fetchall()
print(f"Active bots: {len(bots)}")
for b in bots:
    print(f"  Bot {b[0]}: {b[1]} ({b[2]})")

# Count DB open orders by bot
print("\nDB Open Orders by Bot:")
c.execute('''
    SELECT bot_id, order_type, COUNT(*) 
    FROM bot_orders 
    WHERE status='open' 
    GROUP BY bot_id, order_type
''')
for r in c.fetchall():
    print(f"  Bot {r[0]}: {r[1]} = {r[2]}")

# Total DB open orders
c.execute("SELECT COUNT(*) FROM bot_orders WHERE status='open'")
print(f"\nTotal DB 'open' orders: {c.fetchone()[0]}")

conn.close()
