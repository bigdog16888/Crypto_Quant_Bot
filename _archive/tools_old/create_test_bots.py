import sys
import os

# Add parent directory to path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)

from engine.database import add_bot, get_connection
import json

def create_bots():
    bots = [
        {"name": "Test_Bot_A", "pair": "BTC/USDC", "direction": "LONG", "size": 0.1},
        {"name": "Test_Bot_B", "pair": "BTC/USDC", "direction": "SHORT", "size": 0.1},
        {"name": "Test_Bot_C", "pair": "BTC/USDC", "direction": "LONG", "size": 0.2},
    ]

    for b in bots:
        print(f"Creating {b['name']}...")
        config = {
            "leverage": 20,
            "market_type": "future",
            "timeframe": "1m",
            "TakeProfitPct": 1.5,
            "max_steps": 10
        }
        # name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config_dict
        bot_id = add_bot(b['name'], b['pair'], b['direction'], 30, 1.5, b['size'], "Martingale", config)
        if bot_id:
            print(f"Created Bot {bot_id}: {b['name']}")
        else:
            print(f"Failed to create {b['name']} (might already exist)")

if __name__ == "__main__":
    create_bots()
