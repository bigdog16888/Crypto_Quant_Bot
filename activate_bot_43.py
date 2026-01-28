"""
Manually activate Bot #43 to test if the trigger works
"""
import sqlite3

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

print("Before:")
cur.execute('SELECT id, name, is_active FROM bots WHERE id=43')
row = cur.fetchone()
print(f"  Bot #{row[0]}: {row[1]} - Active: {row[2]}")

# Activate the bot
cur.execute('UPDATE bots SET is_active = 1 WHERE id=43')
conn.commit()

print("\nAfter:")
cur.execute('SELECT id, name, is_active FROM bots WHERE id=43')
row = cur.fetchone()
print(f"  Bot #{row[0]}: {row[1]} - Active: {row[2]}")

print("\n✅ Bot #43 manually activated in database!")
print("   The engine should pick this up on the next scan cycle.")

conn.close()
