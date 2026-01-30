
from engine.exchange_interface import ExchangeInterface
import sqlite3

print("Loading DB bots...")
conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()
cursor.execute("SELECT id, name, pair, is_active FROM bots WHERE is_active = 1")
bots = cursor.fetchall()
bot_map = {}
for b in bots:
    print(f"DB Bot: {b}")
    if b[2]:
        bot_map[b[2]] = b[1]

print("\nFetching Exchange Positions (Testnet)...")
try:
    ex = ExchangeInterface(market_type='future')
    positions = ex.exchange.fetch_positions()
    print(f"Found {len(positions)} positions.")
    for p in positions:
        size = float(p.get('contracts', 0) or 0)
        formatted_sym = p.get('symbol')
        print(f"Exchange Pos: {formatted_sym} | Size: {size} | Raw Symbol: {p.get('info', {}).get('symbol')}")
        
        if size != 0:
            match = bot_map.get(formatted_sym)
            print(f"  -> Match in DB? {'✅ ' + match if match else '❌ UNKNOWN'}")
            
except Exception as e:
    print(f"Error: {e}")
