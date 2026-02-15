"""Emergency fix: Reset bots that incorrectly adopted the same position"""
import sqlite3

# Bot 37 is the real owner (orders are tagged CQB_37_*)
# Bots 32, 33, 34, 35, 36, 38, 39, 41 incorrectly adopted

REAL_OWNER = 37
INCORRECT_BOTS = [32, 33, 34, 35, 36, 38, 39, 41]

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

print("Resetting incorrectly adopted bots...")
for bot_id in INCORRECT_BOTS:
    cur.execute('''
        UPDATE trades 
        SET current_step = 0, 
            total_invested = 0, 
            avg_entry_price = 0, 
            target_tp_price = 0,
            entry_order_id = NULL,
            tp_order_id = NULL
        WHERE bot_id = ?
    ''', (bot_id,))
    print(f"  Reset Bot {bot_id}")

conn.commit()
conn.close()
print("\n✅ Done! Only Bot 37 should now show as in trade.")
