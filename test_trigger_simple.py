import sqlite3
import json
import pandas as pd
from engine.exchange_interface import ExchangeInterface
from engine.strategies.martingale_strategy import MartingaleStrategy

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()
cur.execute('SELECT id, name, pair, direction, config, is_active FROM bots WHERE id=43')
row = cur.fetchone()
conn.close()

bot_id, name, pair, direction, config_str, is_active = row
config = json.loads(config_str)

print(f"Bot #{bot_id}: {name}")
print(f"Active: {'YES' if is_active else 'NO'}")
print()

# Get current price
ex = ExchangeInterface(market_type='future', validate=False)
ticker = ex.fetch_ticker(pair)
current_price = float(ticker['last'])
print(f"Current BTC Price: ${current_price:,.2f}")

# Check trigger
threshold = config.get('price_threshold', 0.0)
mode_price = config.get('mode_price', 0)
print(f"Trigger: Price {'Above' if mode_price==1 else 'Below'} ${threshold:,.2f}")
print()

# Test with strategy
ohlcv = ex.fetch_ohlcv(symbol=pair, timeframe='1m', limit=100)
df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

strategy = MartingaleStrategy(name=name, params=config)
buy_signal, sell_signal = strategy.check_signals(df)

print(f"Trigger Test Results:")
print(f"  buy_signal: {buy_signal}")
print(f"  sell_signal: {sell_signal}")
print(f"  Bot Direction: {direction}")
print()

should_trigger = (direction == 'LONG' and buy_signal) or (direction == 'SHORT' and sell_signal)

if should_trigger:
    print("RESULT: Bot WOULD trigger if active!")
    if not is_active:
        print("ACTION NEEDED: Activate bot in UI")
else:
    print("RESULT: Trigger conditions NOT met")
