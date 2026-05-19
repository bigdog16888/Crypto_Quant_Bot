import sqlite3, pandas as pd

conn = sqlite3.connect('crypto_bot.db')

print("=== Step 4: Ledger Reset for bot 10020 ===")

conn.execute("""
UPDATE trades SET 
    open_qty = 0,
    total_invested = 0,
    current_step = 0,
    tp_order_id = NULL,
    cycle_phase = 'SCANNING',
    avg_entry_price = 0
WHERE bot_id = 10020
""")

rows_trades = conn.total_changes
print(f"  trades updated: {rows_trades} row(s)")

conn.execute("""
UPDATE bot_orders SET status = 'reset_cleared'
WHERE bot_id = 10020 
AND status NOT IN ('reset_cleared','cancelled','canceled','failed')
""")

rows_orders = conn.total_changes - rows_trades
print(f"  bot_orders cleared: {rows_orders} row(s)")

conn.commit()

print("\n=== Step 5: Verify Ledger ===")
df = pd.read_sql_query('''
SELECT open_qty, total_invested, current_step, tp_order_id, cycle_phase
FROM trades WHERE bot_id = 10020;
''', conn)
print(df.to_string(index=False))
