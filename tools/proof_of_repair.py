import os
import sys
import json

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface, normalize_symbol
from engine.database import get_bot_status, get_all_bots
from engine.manager import manage_trade
from engine.strategies.martingale_strategy import MartingaleStrategy
from config.settings import config

def test_repair():
    print("🔬 PROOF OF REPAIR: TESTING SELF-HEALING LOGIC")
    print("="*60)
    
    # Target: gold long
    bot_id = None
    bots = get_all_bots()
    for b in bots:
        if b[1] == 'gold long':
            bot_id = b[0]
            bot_data = b
            break
            
    if not bot_id:
        print("❌ Could not find 'gold long' bot")
        return

    print(f"📍 Found Bot: {bot_data[1]} (ID: {bot_id})")
    
    # 1. Fetch current exchange state
    ex = ExchangeInterface(market_type='future')
    pair = bot_data[2]
    open_orders = ex.fetch_open_orders(pair)
    
    # 2. Get DB Trade status
    trade_data = get_bot_status(bot_id)
    # (name, pair, current_step, total_invested, avg_entry_price, target_tp_price, last_exit_price, last_exit_time, basket_start_time)
    
    print(f"DB State: In Trade={trade_data[3]>0}, TP={trade_data[5]}, Orders={len(open_orders)}")
    
    # 3. Use Strategy
    from engine.database import get_bot_params
    params_row = get_bot_params(bot_id)
    params = json.loads(params_row[7]) if params_row and params_row[7] else {}
    strat = MartingaleStrategy(name='gold long', params=params)
    
    # 4. RUN MANAGE_TRADE (The Core Repair Logic)
    current_price = ex.get_last_price(pair)
    mission = manage_trade(
        bot_id=bot_id,
        bot_name='gold long',
        pair=pair,
        direction='LONG',
        settings=params,
        trade_data=trade_data,
        current_price=current_price,
        strategy=strat,
        exchange_interface=ex,
        open_orders=open_orders
    )
    
    print("\n🔍 REPAIR DECISION:")
    print(json.dumps(mission, indent=2))
    
    if mission.get('action') == 'maintain_orders':
        print("\n✅ SUCCESS: Engine correctly identified missing protection and triggered REPAIR!")
        if mission.get('tp_price') > 0:
            print(f"👉 Would re-place TP at: {mission['tp_price']}")
    else:
        print("\n❌ FAILURE: Engine did not trigger repair.")

if __name__ == "__main__":
    test_repair()
