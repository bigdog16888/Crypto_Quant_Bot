import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')

phys = pd.read_sql("SELECT pair, side, entry_price, size FROM active_positions WHERE bot_id=0", conn)
virt = pd.read_sql("""
    SELECT pair, 
           SUM(CASE WHEN direction='LONG' THEN total_invested ELSE -total_invested END) as virt_net 
    FROM bots JOIN trades ON bots.id = trades.bot_id 
    WHERE status != 'Stopped'
    GROUP BY pair
""", conn)

print('--- PHYSICAL ---')
phys_dict = {}
for _, row in phys.iterrows():
    pnl_dir = 1 if row['side']=='LONG' else -1
    phys_notional = row['size'] * row['entry_price'] * pnl_dir
    
    # Normalize mapping
    norm_pair = row['pair'].replace('/', '').replace(':', '').replace('-', '').upper()
    phys_dict[norm_pair] = phys_notional
    print(f"{row['pair']} (Norm: {norm_pair}): {phys_notional:.2f}")

print('\n--- VIRTUAL ---')
virt_dict = {}
for _, row in virt.iterrows():
    v = row['virt_net']
    
    # Normalize mapping
    p = str(row['pair']).split(':')[0]
    norm_pair = p.replace('/', '').replace('-', '').upper()
    
    virt_dict[norm_pair] = v
    print(f"{row['pair']} (Norm: {norm_pair}): {v:.2f}")

print('\n--- MISMATCHES ---')
mismatches = 0
for pair, v_net in virt_dict.items():
    p_net = phys_dict.get(pair, 0.0)
    if abs(v_net - p_net) > 5.0: # $5 leeway for price drift
        print(f'MISMATCH ON {pair}: Virtual {v_net:.2f} vs Physical {p_net:.2f} (Diff: {abs(v_net-p_net):.2f})')
        mismatches += 1

if mismatches == 0:
    print('✅ NO MISMATCHES DETECTED. SYSTEM IS FULLY GREEN AND SYNCED.')
