import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')
query_virtual = """
    SELECT b.pair, b.direction, b.name,
           SUM(CASE WHEN bo.order_type IN ('entry', 'grid', 'adoption_add') THEN bo.filled_amount ELSE 0 END) as pos_adds,
           SUM(CASE WHEN bo.order_type IN ('tp', 'close', 'adoption_reduce') THEN bo.filled_amount ELSE 0 END) as pos_subs,
           t.avg_entry_price
    FROM bots b
    LEFT JOIN bot_orders bo ON b.id = bo.bot_id AND bo.filled_amount > 0
    LEFT JOIN trades t ON b.id = t.bot_id
    WHERE b.is_active = 1
    GROUP BY b.id
"""
df_virt = pd.read_sql(query_virtual, conn)
print(df_virt)

virtual_net_usd = 0.0
for _, row in df_virt.iterrows():
    qty_abs = float(row['pos_adds'] or 0) - float(row['pos_subs'] or 0)
    if qty_abs > 0.000001 and pd.notna(row['avg_entry_price']) and float(row['avg_entry_price']) > 0:
        avg_price = float(row['avg_entry_price'])
        amt_usd = qty_abs * avg_price
        
        signed_qty = qty_abs if row['direction'] == 'LONG' else -qty_abs
        signed_usd = amt_usd if row['direction'] == 'LONG' else -amt_usd
        print(f"BOT {row['name']} ({row['direction']}): {qty_abs:.4f} units | Net: {signed_qty:.4f} | USD: ${signed_usd:.2f}")

conn.close()
