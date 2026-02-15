"""Check Bot 37 state fully"""
import sqlite3

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

# Open orders in DB
cur.execute("SELECT order_id, order_type, status, client_order_id FROM bot_orders WHERE bot_id = 37 AND status = 'open'")
open_orders = cur.fetchall()
print("BOT 37 OPEN ORDERS IN DB:")
for o in open_orders:
    print(f"  ID: {o[0]}, Type: {o[1]}, Status: {o[2]}, ClientID: {o[3]}")
print(f"Total: {len(open_orders)}")

# Trade state
cur.execute("SELECT entry_order_id, tp_order_id, total_invested, current_step FROM trades WHERE bot_id = 37")
trade = cur.fetchone()
print("\nBOT 37 TRADE STATE:")
print(f"  entry_order_id: {trade[0]}")
print(f"  tp_order_id: {trade[1]}")
print(f"  total_invested: {trade[2]}")
print(f"  current_step: {trade[3]}")

# The key question: is the trades.tp_order_id matching open orders?
if trade[1]:
    cur.execute("SELECT * FROM bot_orders WHERE order_id = ?", (trade[1],))
    matched = cur.fetchone()
    print(f"\nMatched TP order in bot_orders: {bool(matched)}")
