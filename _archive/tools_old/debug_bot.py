import os
import sys
import json
import sqlite3

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection

def inspect_bots():
    print("🔬 BOT CONFIGURATION INSPECTOR")
    print("="*60)
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get specific bots 37 and 44
    target_ids = (37, 44)
    cursor.execute(f'''
        SELECT id, name, pair, strategy_type, config, is_active, status 
        FROM bots 
        WHERE id IN {target_ids}
    ''')
    bots = cursor.fetchall()
    
    from engine.exchange_interface import ExchangeInterface
    ex = ExchangeInterface(market_type='future')

    print(f"🔬 INSPECTING TARGET BOTS: {target_ids}")
    print("="*60)
    
    for b in bots:
        bid, name, pair, strat, config_str, active, status = b
        config = json.loads(config_str) if config_str else {}
        
        print(f"\n🤖 BOT {bid}: {name} ({pair})")
        print(f"   Status: {status} | Active: {active}")
        
        # Check Mode Price
        mode_price = config.get('mode_price', 0)
        threshold = config.get('price_threshold', 0)
        
        # Check Market Price first
        try:
            current_price = ex.get_last_price(pair)
            print(f"   💲 Market Price: {current_price}")
        except Exception as e:
            print(f"   ⚠️ Price Check Failed: {e}")
            current_price = 0.0

        # Check Mode Price
        mode_price = config.get('mode_price', 0)
        threshold = config.get('price_threshold', 0)
        
        print(f"   ⚙️  Primary Price Trigger:")
        if mode_price == 1: # Above
             is_hit = current_price > threshold
             print(f"      - Price > {threshold}: {'TRUE ✅' if is_hit else 'FALSE ❌'} ({current_price})")
        elif mode_price == 2: # Below
             is_hit = current_price < threshold
             print(f"      - Price < {threshold}: {'TRUE ✅' if is_hit else 'FALSE ❌'} ({current_price})")
        else:
             print(f"      - DISABLED")

        # Check MA Trigger (Likely Culprit)
        mode_ma = config.get('mode_ma', 0)
        ma_period = config.get('ma_period', 200)
        ma_tf = config.get('ma_tf', '1h')
        
        print(f"   ⚙️  MA Trigger (Trigger 12):")
        if mode_ma > 0:
             # Fetch OHLCV for MA
             ohlcv = ex.fetch_ohlcv(pair, timeframe=ma_tf, limit=ma_period+10)
             if ohlcv:
                 import pandas as pd
                 df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                 ma_val = df['close'].rolling(window=ma_period).mean().iloc[-1]
                 print(f"      - MA ({ma_period} {ma_tf}): {ma_val}")
                 
                 if mode_ma == 1: # Price > MA (Bullish)
                     is_pass = current_price > ma_val
                     print(f"      - Condition (Price > MA): {'TRUE ✅' if is_pass else 'BLOCKED ⛔'} (Diff: {current_price - ma_val:.2f})")
                 else: # Price < MA (Bearish)
                     is_pass = current_price < ma_val
                     print(f"      - Condition (Price < MA): {'TRUE ✅' if is_pass else 'BLOCKED ⛔'}")
             else:
                 print("      - ⚠️ Failed to fetch data for MA check")
        else:
             print("      - DISABLED")

        # Check Cooldown / Can Enter Logic
        # (id, name, pair, current_step, total_invested, avg_entry_price, target_tp_price, last_exit_price, last_exit_time, basket_start_time)
        # Note: get_bot_status in debug script might need full query if not imported
        # But we can query trades table directly for last_exit info
        
        cursor.execute("SELECT last_exit_price, last_exit_time FROM trades WHERE bot_id = ?", (bid,))
        row = cursor.fetchone()
        last_exit_price = row[0] if row else 0.0
        last_exit_time = row[1] if row else 0.0
        
        import time
        current_ts = time.time()
        
        reentry_mins = config.get('reentry_cooldown_mins', 0)
        reentry_dist = config.get('reentry_distance_pct', 0.0)
        
        print(f"   🛑 Safety Checks (can_enter):")
        can_enter = True
        
        # 1. Cooldown
        if last_exit_time > 0 and reentry_mins > 0:
            ago_mins = (current_ts - last_exit_time) / 60
            if ago_mins < reentry_mins:
                print(f"      - ⏳ COOLDOWN ACTIVE: Refusing Entry. (Exited {ago_mins:.1f}m ago < {reentry_mins}m)")
                can_enter = False
            else:
                 print(f"      - Cooldown Pass: {ago_mins:.1f}m ago > {reentry_mins}m")
        else:
            print(f"      - Cooldown Pass: No active cooldown set or no recent exit.")
            
        # 2. Distance
        if last_exit_price > 0 and reentry_dist > 0:
             dist_pct = abs(current_price - last_exit_price) / last_exit_price * 100
             if dist_pct < reentry_dist:
                 print(f"      - 📏 TOO CLOSE TO EXIT: Refusing Entry. (Dist {dist_pct:.2f}% < {reentry_dist}%)")
                 can_enter = False
             else:
                 print(f"      - Distance Pass: {dist_pct:.2f}% > {reentry_dist}%")
        else:
             print(f"      - Distance Pass: No distance check enabled.")
             
        if can_enter:
            print(f"   ✅ CAN ENTER: YES")
        else:
            print(f"   ⛔ CAN ENTER: NO")

        # --- SIMULATE STRATEGY ---
        print(f"   🧠 STRATEGY SIMULATION:")
        try:
            from engine.strategies.martingale_strategy import MartingaleStrategy
            import pandas as pd
            
            # 1. Fetch OHLCV (100 candles) for signal check
            # Default TF is 1m usually
            timeframe = config.get('timeframe', '1m')
            print(f"      - Fetching OHLCV ({timeframe})...")
            ohlcv = ex.fetch_ohlcv(pair, timeframe=timeframe, limit=100)
            
            if ohlcv:
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                
                # 2. Init Strat
                strat = MartingaleStrategy(name, config)
                print(f"      - Strat Params Mode MA: {strat.params.get('mode_ma')}")
                print(f"      - Strat Params Mode Price: {strat.params.get('mode_price')}")
                
                # 3. Check Signal
                buy_sig, sell_sig = strat.check_signals(df)
                
                bot_dir = 'LONG' # Default
                # Check DB direction if possible, else assume LONG for debug
                # (In manager, direction is stored in bots table? No, passed in params usually or fixed)
                # Actually direction is passed to manage_trade. It comes from config['direction'] or 'LONG'
                bot_dir = config.get('direction', 'LONG')
                
                print(f"      - Direction: {bot_dir}")
                print(f"      - RAW SIGNALS -> Buy: {buy_sig}, Sell: {sell_sig}")
                
                if bot_dir == 'LONG' and buy_sig:
                    print(f"      - 🚀 FINAL DECISION: ENTRY TRIGGERED!")
                elif bot_dir == 'SHORT' and sell_sig:
                    print(f"      - 🚀 FINAL DECISION: ENTRY TRIGGERED!")
                else:
                    print(f"      - 💤 FINAL DECISION: NO TRIGGER.")
            else:
                print(f"      - Failed to fetch OHLCV.")
                
        except Exception as e:
            print(f"      - Simulation Failed: {e}")
            import traceback
            traceback.print_exc()

    conn.close()

    conn.close()

if __name__ == "__main__":
    inspect_bots()
