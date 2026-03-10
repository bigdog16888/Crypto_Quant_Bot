import sqlite3
import json

conn = sqlite3.connect('C:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/crypto_bot.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute('SELECT b.id, t.current_step, t.total_invested, b.config FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.name="short btc"')
bot = c.fetchone()
print('Bot state:', dict(bot))
