import sys, time
sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()
cursor = conn.cursor()

# 1. SUI Bot 10018 -> Adopt 11103.4 SUI @ 0.85357
sui_inv = 11103.4 * 0.853573881494
cursor.execute('''
    UPDATE trades 
    SET total_invested = ?, avg_entry_price = ?, current_step = MAX(current_step, 1), 
        entry_confirmed = 1, cycle_phase = 'ACTIVE'
    WHERE bot_id = 10018
''', (sui_inv, 0.853573881494))
cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=10018")
print(f'✅ SUI bot 10018 synced to ${sui_inv:.2f} (11103.4 SUI)')

# 2. ETH Bot 10011 -> Shrink to 0.035 ETH @ 2076.74
eth_inv = 0.035 * 2076.74
cursor.execute('''
    UPDATE trades 
    SET total_invested = ?, avg_entry_price = ?, current_step = MAX(current_step, 1),
        entry_confirmed = 1, cycle_phase = 'ACTIVE'
    WHERE bot_id = 10011
''', (eth_inv, 2076.74))
cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=10011")
print(f'✅ ETH bot 10011 synced to ${eth_inv:.2f} (0.035 ETH)')

# 3. SOL -> Rescue Orphan SHORT (bot 0 -> bot 10001 short sol)
sol_inv = 0.06 * 79.78
cursor.execute("UPDATE trades SET total_invested=0, current_step=0, cycle_phase='IDLE' WHERE bot_id=10008")
cursor.execute("UPDATE bots SET status='Scanning' WHERE id=10008")
cursor.execute('''
    UPDATE trades 
    SET total_invested = ?, avg_entry_price = ?, current_step = 1, 
        entry_confirmed = 1, cycle_phase = 'ACTIVE',
        basket_start_time = ?
    WHERE bot_id = 10001
''', (sol_inv, 79.78, int(time.time())))
cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=10001")
cursor.execute("UPDATE active_positions SET bot_id=10001 WHERE pair LIKE '%SOL%' AND side='SHORT'")
print(f'✅ SOL orphan SHORT given to bot 10001 (${sol_inv:.2f}) and LONG bot 10008 wiped.')

# 4. BNB Bot 10007 -> Adopt to 0.03 BNB @ 593.03
bnb_inv = 0.03 * 593.0366666667
cursor.execute('''
    UPDATE trades 
    SET total_invested = ?, avg_entry_price = ?, current_step = MAX(current_step, 1),
        entry_confirmed = 1, cycle_phase = 'ACTIVE'
    WHERE bot_id = 10007
''', (bnb_inv, 593.0366666667))
cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=10007")
print(f'✅ BNB bot 10007 synced to ${bnb_inv:.2f} (0.03 BNB)')

conn.commit()
conn.close()
