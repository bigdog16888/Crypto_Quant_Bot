"""
Deep diagnostic - Add logging to check_signals to see what's happening
"""
import sqlite3
import json
import pandas as pd
from engine.exchange_interface import ExchangeInterface
from engine.strategies.martingale_strategy import MartingaleStrategy

# Get bot config
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()
cur.execute('SELECT id, name, pair, direction, config FROM bots WHERE id=43')
row = cur.fetchone()
conn.close()

bot_id, name, pair, direction, config_str = row
config = json.loads(config_str)

print("=" * 70)
print(f"DEEP DIAGNOSTIC: Bot #{bot_id} - {name}")
print("=" * 70)

# Get market data
ex = ExchangeInterface(market_type='future', validate=False)
ohlcv = ex.fetch_ohlcv(symbol=pair, timeframe='1m', limit=100)
df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

current_price = df['close'].iloc[-1]
print(f"Current Price: ${current_price:,.2f}")
print()

# Check trigger configuration
print("TRIGGER CONFIGURATION:")
print("-" * 70)
mode_price = config.get('mode_price', 0)
threshold = config.get('price_threshold', 0.0)
print(f"mode_price: {mode_price} (0=OFF, 1=Above, 2=Below)")
print(f"price_threshold: ${threshold:,.2f}")
print()

# Manual trigger check (what SHOULD happen)
print("MANUAL TRIGGER CHECK:")
print("-" * 70)
if mode_price == 1:  # Above
    manual_pass = current_price >= threshold
    print(f"Price Check: {current_price:,.2f} >= {threshold:,.2f} = {manual_pass}")
    print(f"Expected: buy_allow=True, sell_allow=True if price >= threshold")
elif mode_price == 2:  # Below
    manual_pass = current_price <= threshold
    print(f"Price Check: {current_price:,.2f} <= {threshold:,.2f} = {manual_pass}")
else:
    manual_pass = False
    print("Price trigger is OFF")
print()

# Now test with actual strategy
print("ACTUAL STRATEGY CHECK:")
print("-" * 70)
strategy = MartingaleStrategy(name=name, params=config)

# Manually call check_signals and trace through the logic
print("Calling strategy.check_signals(df)...")
buy_signal, sell_signal = strategy.check_signals(df)
print(f"Result: buy_signal={buy_signal}, sell_signal={sell_signal}")
print()

# Check if direction matches
print("DIRECTION CHECK:")
print("-" * 70)
print(f"Bot Direction: {direction}")
print(f"buy_signal: {buy_signal}")
print(f"sell_signal: {sell_signal}")

should_enter = (direction == 'LONG' and buy_signal) or (direction == 'SHORT' and sell_signal)
print(f"\nShould Enter Trade: {should_enter}")
print()

# Detailed analysis
print("ANALYSIS:")
print("=" * 70)
if not buy_signal and not sell_signal:
    print("❌ BOTH signals are False!")
    print()
    print("Possible causes:")
    print("1. triggers_active == 0 (no triggers enabled)")
    print("2. buy_allow or sell_allow was set to False by a trigger")
    print()
    print("Let me check the trigger logic step by step...")
    print()
    
    # Check triggers_active
    triggers_active = 0
    if mode_price > 0:
        triggers_active += 1
        print(f"✓ Price trigger is active (mode_price={mode_price})")
        
        # Check the actual condition
        if mode_price == 1:  # Above
            if current_price < threshold:
                print(f"  ❌ BLOCKED: {current_price:,.2f} < {threshold:,.2f}")
            else:
                print(f"  ✅ PASS: {current_price:,.2f} >= {threshold:,.2f}")
        elif mode_price == 2:  # Below
            if current_price > threshold:
                print(f"  ❌ BLOCKED: {current_price:,.2f} > {threshold:,.2f}")
            else:
                print(f"  ✅ PASS: {current_price:,.2f} <= {threshold:,.2f}")
    
    # Check other triggers
    other_triggers = [
        ('mode_cci', config.get('mode_cci', 0)),
        ('mode_boll', config.get('mode_boll', 0)),
        ('mode_stoch', config.get('mode_stoch', 0)),
        ('mode_rsi', config.get('mode_rsi', 0)),
        ('pat_1_mode', config.get('pat_1_mode', 0)),
        ('pat_2_mode', config.get('pat_2_mode', 0)),
        ('pat_3_mode', config.get('pat_3_mode', 0)),
        ('pat_4_mode', config.get('pat_4_mode', 0)),
        ('mode_atrp', config.get('mode_atrp', 0)),
        ('mode_atre', config.get('mode_atre', 0)),
    ]
    
    for trigger_name, trigger_value in other_triggers:
        if trigger_value > 0:
            triggers_active += 1
            print(f"✓ {trigger_name} is active (value={trigger_value})")
    
    print()
    print(f"Total triggers_active: {triggers_active}")
    
    if triggers_active == 0:
        print("❌ PROBLEM: triggers_active == 0, so check_signals returns (False, False)")
        print("   This is the bug! Even though mode_price=1, it's not being counted.")
elif buy_signal and sell_signal:
    print("⚠️  BOTH signals are True (unusual)")
    print("   This means all triggers passed, but direction check determines entry")
    if should_enter:
        print("   ✅ Should enter based on direction")
    else:
        print("   ❌ Direction doesn't match signals")
elif should_enter:
    print("✅ Everything looks correct - bot should trigger!")
else:
    print("❌ Signal doesn't match direction")
    print(f"   Direction={direction}, buy_signal={buy_signal}, sell_signal={sell_signal}")
