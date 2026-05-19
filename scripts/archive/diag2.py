import sqlite3, pandas as pd
conn = sqlite3.connect('crypto_bot.db')

print("=== Fix Issue 2: Clear stale tp_order_id and set SCANNING for BNB bot 10007 ===")
c = conn.execute("""
    UPDATE trades SET 
        tp_order_id = NULL,
        cycle_phase = 'SCANNING'
    WHERE bot_id = 10007
""")
print(f"  Rows updated: {c.rowcount}")
conn.commit()

print("\n=== Verify BNB bot 10007 ===")
df = pd.read_sql_query('''
SELECT bot_id, open_qty, total_invested, current_step, cycle_id, tp_order_id, cycle_phase
FROM trades WHERE bot_id = 10007;
''', conn)
print(df.to_string(index=False))

# Also check virtual net for BNB
from engine.database import get_pair_virtual_net
vnet = get_pair_virtual_net('BNB/USDC:USDC')
print(f"\n  get_pair_virtual_net('BNB/USDC:USDC') = {vnet}  (expected: 0.0)")
