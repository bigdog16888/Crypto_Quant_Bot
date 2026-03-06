import sqlite3, json, time, datetime
import sys, os
sys.path.append(os.getcwd())
from engine.manager import calculate_early_exit_decay
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
c.execute('SELECT config, target_tp_price, avg_entry_price, current_step, t.basket_start_time FROM bots b JOIN trades t ON b.id = t.bot_id WHERE b.id = 10016')
row = c.fetchone()
config = json.loads(row[0])
basket_start = datetime.datetime.fromtimestamp(row[4])
now = datetime.datetime.now()
initial_tp = 76000 # dummy original tp
print(f'Start: {basket_start}, Now: {now}, Diff: {(now-basket_start).total_seconds()/3600}h')
total_orders = row[3] + 1
break_even = row[2]
decayed_tp = calculate_early_exit_decay(basket_start, now, total_orders, initial_tp, break_even, config)
print(f'Original TP: {initial_tp}, Decayed TP: {decayed_tp}, DB TP: {row[1]}')
