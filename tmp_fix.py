import sqlite3
import json

conn = sqlite3.connect('C:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/crypto_bot.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

# Get bot state
c.execute('SELECT b.id, t.total_invested, b.config FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.name="short btc"')
bot = c.fetchone()
if bot:
    bot_id = bot['id']
    total_invested = bot['total_invested']
    config = json.loads(bot['config'] or '{}')
    base = config.get('base_order_size', 10.0)
    mult = config.get('martingale_multiplier', 1.05)
    
    print(f"Bot {bot_id} | Invested=${total_invested} | Base=${base} | Mult={mult}")
    
    simulated_total = base
    simulated_step = 1
    current_order_size = base
    
    while simulated_total < (total_invested * 0.95):
        simulated_step += 1
        current_order_size *= mult
        simulated_total += current_order_size
        if simulated_step >= 50:
            break
            
    print(f"Calculated true step: {simulated_step}")
    
    # Update state
    c.execute('UPDATE trades SET current_step = ? WHERE bot_id = ?', (simulated_step, bot_id))
    conn.commit()
    print("Database updated.")
