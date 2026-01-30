
import sys
import os
import sqlite3
import json
import logging

# Add root to sys.path
sys.path.append(os.getcwd())

from engine.database import get_connection, get_all_bots, get_bot_status
from engine.manager import manage_trade
from engine.strategies.martingale_strategy import MartingaleStrategy

# Mock Exchange Interface
class MockExchange:
    def fetch_open_orders(self, pair):
        return [] # Simulate EMPTY orders

def inspect_bots():
    print("=== INSPECTING BOTS ===")
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, name, pair, direction, strategy_type, config, is_active FROM bots WHERE is_active=1")
    bots = cursor.fetchall()
    
    for bot in bots:
        bot_id, name, pair, direction, strategy_type, config_json, is_active = bot
        print(f"\nBot: {name} ({pair}) [{direction}]")
        
        params = json.loads(config_json)
        print(f"  Config: UseEarlyExit={params.get('UseEarlyExit')}, MaximizeProfit={params.get('MaximizeProfit')}")
        print(f"  MaxSteps={params.get('max_steps', 10)}")
        
        trade_data = get_bot_status(bot_id)
        # (name, pair, current_step, total_invested, avg_entry_price, target_tp_price, last_exit_price, last_exit_time, basket_start_time)
        
        if not trade_data or trade_data[3] == 0:
            print("  Status: IDLE (No position)")
            continue
            
        current_step = trade_data[2]
        print(f"  Status: IN TRADE (Step {current_step})")
        print(f"  Invested: {trade_data[3]}, AvgPrice: {trade_data[4]}")
        
        # Simulate manage_trade
        strategy = MartingaleStrategy(name, params)
        # Mock current price as slightly below entry (drawdown)
        current_price = trade_data[4] * 0.99
        
        # Determine strict structure for trade_data passing
        # manage_trade expects specific tuple
        
        print("  Running manage_trade simulation...")
        try:
            mission = manage_trade(
                bot_id=bot_id,
                bot_name=name,
                pair=pair,
                direction=direction,
                settings=params,
                trade_data=trade_data,
                current_price=current_price,
                strategy=strategy,
                exchange_interface=MockExchange()
            )
            
            print(f"  MISSION: {mission.get('action')}")
            if mission.get('action') == 'maintain_orders':
                print(f"    TP Price: {mission.get('tp_price')}")
                print(f"    Grid Price: {mission.get('grid_price')}")
            
        except Exception as e:
            print(f"  ERROR executing manage_trade: {e}")

if __name__ == "__main__":
    inspect_bots()
