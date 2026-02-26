import sys
import os
import logging
import pandas as pd

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_bot_params
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.exchange_interface import ExchangeInterface

logging.basicConfig(level=logging.INFO)

def test_signal(bot_id):
    print(f"🔬 Testing Signal for Bot {bot_id}...")
    
    # 1. Load Bot Params
    params = get_bot_params(bot_id)
    if not params:
        print(f"❌ Bot {bot_id} not found in DB.")
        return

    name, pair, direction, base_order, safety_order, total_volume, strategy_name, config_json = params
    print(f"   Bot: {name} | Pair: {pair} | Dir: {direction}")
    print(f"   Config: {config_json}")
    
    # Parse Config
    import json
    config = json.loads(config_json)
    
    # 2. Init Strategy
    strategy = MartingaleStrategy(config)
    
    # 3. Get Price
    exchange = ExchangeInterface(market_type='future' if config.get('market_type') == 'future' else 'spot')
    current_price = exchange.get_last_price(pair)
    print(f"   Current Price: {current_price}")
    
    if not current_price:
        print("❌ Failed to get price.")
        return

    # 4. Check Signal
    # Mock Market Data (Empty DF as typical for price-only trigger)
    market_data = pd.DataFrame({'close': [current_price]})
    
    buy, sell = strategy.check_signals(market_data, current_price)
    print(f"   Signals -> Buy: {buy} | Sell: {sell}")
    
    # 5. Manual Check
    mode = int(config.get('mode_price', 0))
    thresh = float(config.get('price_threshold', 0))
    
    print(f"   Manual Logic Check:")
    print(f"     Mode {mode} (1=>, 2=<)")
    print(f"     Threshold: {thresh}")
    
    if mode == 2:
        res = current_price < thresh
        print(f"     {current_price} < {thresh} = {res}")
    elif mode == 1:
        res = current_price > thresh
        print(f"     {current_price} > {thresh} = {res}")

if __name__ == "__main__":
    test_signal(10000)
