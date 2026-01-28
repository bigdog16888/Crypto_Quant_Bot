"""
Comprehensive diagnostic for Bot #43 trigger logic
Tests if the bot WOULD trigger if it were active
"""
import sqlite3
import json
import pandas as pd
from engine.exchange_interface import ExchangeInterface
from engine.strategies.martingale_strategy import MartingaleStrategy

# Get bot config
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()
cur.execute('SELECT id, name, pair, direction, config, is_active FROM bots WHERE id=43')
row = cur.fetchone()
conn.close()

bot_id, name, pair, direction, config_str, is_active = row
config = json.loads(config_str)

print("=" * 60)
print(f"BOT DIAGNOSTIC: #{bot_id} - {name}")
print("=" * 60)
print(f"Pair: {pair}")
print(f"Direction: {direction}")
print(f"Active in DB: {'YES' if is_active else 'NO'}")
print()

# Get market data
print("Fetching market data...")
ex = ExchangeInterface(market_type='future', validate=False)
ohlcv = ex.fetch_ohlcv(symbol=pair, timeframe='1m', limit=100)
df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

current_price = df['close'].iloc[-1]
print(f"Current Price: ${current_price:,.2f}")
print()

# Check trigger configuration
print("TRIGGER CONFIGURATION:")
print("-" * 60)
mode_price = config.get('mode_price', 0)
threshold = config.get('price_threshold', 0.0)
print(f"Price Trigger: Mode {mode_price} (0=OFF, 1=Above, 2=Below)")
print(f"Threshold: ${threshold:,.2f}")

# Check other triggers
other_triggers = {
    'CCI': config.get('mode_cci', 0),
    'Bollinger': config.get('mode_boll', 0),
    'Stochastic': config.get('mode_stoch', 0),
    'RSI': config.get('mode_rsi', 0),
    'Pattern 1': config.get('pat_1_mode', 0),
    'Pattern 2': config.get('pat_2_mode', 0),
    'Pattern 3': config.get('pat_3_mode', 0),
    'Pattern 4': config.get('pat_4_mode', 0),
    'ATR Percentile': config.get('mode_atrp', 0),
    'ATR Expansion': config.get('mode_atre', 0),
}

active_triggers = {k: v for k, v in other_triggers.items() if v > 0}
if active_triggers:
    print("\nOther Active Triggers:")
    for k, v in active_triggers.items():
        print(f"  {k}: Mode {v}")
else:
    print("\nOther Triggers: ALL OFF")
print()

# Test the trigger logic
print("TESTING TRIGGER LOGIC:")
print("-" * 60)

# Manual check
if mode_price == 1:  # Above
    manual_result = current_price >= threshold
    print(f"Manual Check: {current_price:,.2f} >= {threshold:,.2f}? {manual_result}")
elif mode_price == 2:  # Below
    manual_result = current_price <= threshold
    print(f"Manual Check: {current_price:,.2f} <= {threshold:,.2f}? {manual_result}")
else:
    manual_result = False
    print("Price trigger is OFF")

# Strategy check
print("\nStrategy check_signals() test:")
strategy = MartingaleStrategy(name=name, params=config)
buy_signal, sell_signal = strategy.check_signals(df)
print(f"  buy_signal: {buy_signal}")
print(f"  sell_signal: {sell_signal}")
print(f"  Direction: {direction}")

should_enter = (direction == 'LONG' and buy_signal) or (direction == 'SHORT' and sell_signal)
print(f"\nSHOULD ENTER: {should_enter}")

if should_enter:
    print("\n✅ BOT WOULD TRIGGER IF ACTIVE!")
else:
    print("\n❌ Bot would NOT trigger")
    if not is_active:
        print("   Reason: Bot is INACTIVE in database")
    if not buy_signal and not sell_signal:
        print("   Reason: Trigger conditions not met")

print("\n" + "=" * 60)
print("RECOMMENDATION:")
print("=" * 60)
if not is_active:
    print("⚠️  Bot is INACTIVE. Activate it in the UI to start trading.")
elif should_enter:
    print("✅ Bot should trigger on next scan cycle!")
else:
    print("⏳ Waiting for trigger conditions to be met.")
