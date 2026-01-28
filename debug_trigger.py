
import sqlite3
import json
import logging
from engine.exchange_interface import ExchangeInterface
from config.settings import config as global_config

logging.basicConfig(level=logging.INFO)

def check_trigger():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    cur.execute('SELECT id, name, pair, config, direction FROM bots ORDER BY id DESC LIMIT 1')
    row = cur.fetchone()
    conn.close()

    if not row:
        print("No bots found.")
        return

    bot_id, name, pair, config_str, direction = row
    config = json.loads(config_str)
    
    print(f"--- Bot {bot_id}: {name} ({pair}) [{direction}] ---")
    
    # Trigger Config
    mode_price = config.get('mode_price', 0)
    threshold = float(config.get('price_threshold', 0.0))
    print(f"Configured Trigger: Mode {mode_price} (0=OFF, 1=Above, 2=Below), Threshold: {threshold}")
    
    # Market Data
    ex = ExchangeInterface(market_type='future', validate=False)
    ticker = ex.fetch_ticker(pair)
    current_price = float(ticker['last'])
    print(f"Current Price: {current_price}")
    
    # Evaluation
    triggered = False
    if mode_price == 1: # Above
        if current_price > threshold: triggered = True
        print(f"Check: {current_price} > {threshold}? {triggered}")
    elif mode_price == 2: # Below
        if current_price < threshold: triggered = True
        print(f"Check: {current_price} < {threshold}? {triggered}")
    else:
        print("Price Trigger is OFF (Mode 0)")

if __name__ == "__main__":
    check_trigger()
