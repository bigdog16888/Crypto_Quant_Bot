import sqlite3, json, sys
path = r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
cur = sqlite3.connect(path).cursor()
info = cur.execute('PRAGMA table_info(bot_orders)').fetchall()
print(info)
