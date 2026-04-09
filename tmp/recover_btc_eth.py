import sqlite3
c = sqlite3.connect('crypto_bot.db')
cursor = c.cursor()

# Relink orphans to bot IDs
cursor.execute("UPDATE active_positions SET bot_id=10016 WHERE pair='BTCUSDC' AND bot_id=0 AND side='LONG'")
cursor.execute("UPDATE active_positions SET bot_id=10011 WHERE pair='ETHUSDC' AND bot_id=0 AND side='SHORT'")

# Restore virtual ledger to pre-wipe values so the engine accepts the positions
cursor.execute("UPDATE trades SET entry_confirmed=1, total_invested=1184.08, current_step=0, avg_entry_price=(1184.08/0.017) WHERE bot_id=10016")
cursor.execute("UPDATE trades SET entry_confirmed=1, total_invested=351.79, current_step=0, avg_entry_price=(351.79/0.172) WHERE bot_id=10011")

cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id IN (10016, 10011)")

c.commit()
c.close()
print("Recovered BTC/ETH bots!")
