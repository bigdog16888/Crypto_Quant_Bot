import sys
import os
import json
import logging

# Add root to sys.path
sys.path.insert(0, '.')

# Mocking internal modules if needed, but we try to import real ones
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.manager import manage_trade
from config.settings import config

# Configure logging to see output
logging.basicConfig(level=logging.INFO, format='%(name)s - %(message)s')

class MockExchange:
    def fetch_open_orders(self, pair):
        return [] # Simulate no open orders

def test_order_generation():
    print("="*60)
    print("TESTING ORDER GENERATION (manage_trade)")
    print("="*60)

    # 1. Setup Mock Data
    bot_id = "test_bot_001"
    name = "TestBot"
    pair = "ETH/USDC"
    direction = "LONG"
    
    # Strategy Params
    params = {
        'base_size': 10.0,
        'martingale_multiplier': 1.5,
        'TakeProfitType': 'Percent',
        'TakeProfitPct': 1.0,
        'base_grid': 10.0, # Fixed grid spacing fallback
        'max_steps': 10,
        'market_type': 'future'
    }
    
    strategy = MartingaleStrategy(name=name, params=params)
    mock_exchange = MockExchange()
    
    # Trade Data: (name, pair, current_step, total_invested, avg_entry_price, target_tp_price, last_exit_price, last_exit_time)
    current_step = 0
    total_invested = 10.0
    avg_entry_price = 2000.0
    target_tp_price = 2020.0
    
    trade_data = (name, pair, current_step, total_invested, avg_entry_price, target_tp_price, 0, 0)
    
    current_price = 1990.0 # Price dropped, should trigger grid?
    
    print("\n[SCENARIO 1] Bot In Trade (Step 0), No Orders on Exchange")
    print(f"   Current Price: {current_price}")
    print(f"   Avg Entry: {avg_entry_price}")
    
    mission = manage_trade(
        bot_id=bot_id,
        bot_name=name,
        pair=pair,
        direction=direction,
        settings=params,
        trade_data=trade_data,
        current_price=current_price,
        strategy=strategy,
        exchange_interface=mock_exchange
    )
    
    print("\n[RESULT]")
    print(json.dumps(mission, indent=4))
    
    # Verification
    if mission.get('action') == 'maintain_orders':
        print("\n✅ PASSED: Mission is 'maintain_orders'")
        
        gp = mission.get('grid_price')
        tp = mission.get('tp_price')
        
        if gp and gp > 0 and tp and tp > 0:
            print(f"   Grid Price: {gp:.2f}")
            print(f"   TP Price: {tp:.2f}")
            print("   ✅ BOTH Orders are present in mission.")
        else:
            print("   ❌ FAILED: Missing Grid or TP price in mission.")
    else:
        print(f"   ❌ FAILED: Action is '{mission.get('action')}', expected 'maintain_orders'")

if __name__ == "__main__":
    test_order_generation()
