"""Investigate orphan position - check trade log history"""
from engine.database import get_connection

print('=== TRADE LOG (Recent BTC entries) ===')
conn = get_connection()
cur = conn.cursor()
for r in cur.execute('''
    SELECT bot_id, action, symbol, price, amount, cost_usdc, step, notes, timestamp
    FROM trade_log
    WHERE symbol LIKE '%BTC%'
    ORDER BY timestamp DESC
    LIMIT 30
''').fetchall():
    notes = r[7][:40] if r[7] else ""
    print(f'Bot {r[0]}: {r[1]} | {r[4]:.6f} @ {r[3]:.2f} = ${r[5]:.2f} | Step {r[6]} | {notes}')

print('\n=== GRID FILLS (Should update position) ===')
for r in cur.execute('''
    SELECT bot_id, action, price, amount, cost_usdc, step
    FROM trade_log
    WHERE action LIKE '%GRID%'
    ORDER BY timestamp DESC
    LIMIT 20
''').fetchall():
    print(f'Bot {r[0]}: {r[1]} | {r[3]:.6f} @ {r[2]:.2f} = ${r[4]:.2f} | Step {r[5]}')

print('\n=== BOT CURRENT STEP vs GRID FILLS ===')
for r in cur.execute('''
    SELECT t.bot_id, 
           (SELECT current_step FROM trades WHERE bot_id = t.bot_id) as db_step,
           MAX(t.step) as max_grid_step,
           COUNT(*) as grid_count
    FROM trade_log t
    WHERE t.action LIKE '%GRID%'
    GROUP BY t.bot_id
''').fetchall():
    print(f'Bot {r[0]}: DB Step = {r[1]}, Max Grid Step = {r[2]}, Grid Fills = {r[3]}')
