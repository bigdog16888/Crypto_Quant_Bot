
import sys
import os
import json
import sqlite3
import pandas as pd
sys.path.append(os.getcwd())

from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.exchange_interface import ExchangeInterface
from config.settings import config

def check_signals():
    print("--- CHECKING STRATEGY SIGNALS (DIAGNOSTIC) ---")
    
    # 1. Fetch Active Bots
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, pair, config, strategy_type FROM bots WHERE is_active=1")
    bots = cursor.fetchall()
    conn.close()
    
    if not bots:
        print("❌ No active bots found in DB.")
        return

    print(f"🔍 Analyzing {len(bots)} Active Bots...")
    
    # 2. Init Exchange (for fetching candles)
    try:
        ex = ExchangeInterface(market_type='future')
    except Exception as e:
        print(f"❌ Failed to init exchange: {e}")
        return

    # 3. Check Each Bot
    triggers = 0
    for bot in bots:
        b_id, name, pair, cfg_json, s_type = bot
        try:
            cfg = json.loads(cfg_json)
            # Instantiate Strategy
            strategy = MartingaleStrategy(cfg)
            
            # Fetch Data (Mocking what Runner does)
            # We need candles. 
            # Strategy checks 'get_signal(df)'.
            
            # Fetch 100 candles
            ohlcv = ex.fetch_ohlcv(pair, timeframe=cfg.get('timeframe', '1h'), limit=100)
            if not ohlcv:
                print(f"   - {name}: ⚠️ No Data (Fetch Failed)")
                continue
                
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # Run Signal Check
            # Prepare arguments for decide_action
            current_price = float(df['close'].iloc[-1])
            
            # Mock bot_status (Assume idle for checking entry)
            bot_status = {
                'total_invested': 0.0,
                'current_step': 0,
                'avg_entry_price': 0.0
            }
            
            # Call decide_action
            # Signature: decide_action(self, bot_status: Dict, current_price: float, market_data: pd.DataFrame)
            decision = strategy.decide_action(bot_status, current_price, df)
            
            if decision:
                action = decision.get('action')
                side = decision.get('side', '')
                status_icon = "🟢"
                print(f"   - {name} ({pair}): {status_icon} Signal: {action} {side} @ {current_price}")
                triggers += 1
            else:
                print(f"   - {name} ({pair}): ⚪ No Signal (Wait)")
                
        except Exception as e:
            print(f"   - {name}: ❌ Error: {e}")

    print("-" * 30)
    print(f"📊 DIAGNOSTIC RESULT: {triggers} Bots SHOULD be triggering right now.")
    if triggers == 0:
        print("💡 Conclusion: Market conditions do not match strategy entry rules.")
    else:
        print("⚠️ Conclusion: Runner should have executed these. Check logs/balance.")

if __name__ == "__main__":
    check_signals()
