import sqlite3
import json

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

cur.execute('SELECT id, name, pair, direction, strategy_type, config FROM bots WHERE pair="BTC/USDC" AND is_active=1 ORDER BY id')
bots = cur.fetchall()

print('=' * 80)
print('BTC/USDC BOTS CONFIGURATION')
print('=' * 80)
print()

for bot in bots:
    bid, name, pair, direction, strat, cfg = bot
    config = json.loads(cfg) if cfg else {}
    
    print(f'Bot {bid}: {name:20} {direction:5} {strat}')
    
    indicators = []
    if config.get('use_rsi'): indicators.append('RSI')
    if config.get('use_bollinger'): indicators.append('BB')
    if config.get('use_stochastic'): indicators.append('STOCH')
    if config.get('use_cci'): indicators.append('CCI')
    if config.get('use_atr_volatility'): indicators.append('ATR')
    if config.get('price_action_patterns'): indicators.append('PATTERN')
    if config.get('market_state_filter'): indicators.append('VOLATILITY')
    
    print(f'  Indicators: {", ".join(indicators) if indicators else "NONE - always triggers!"}')
    print(f'  Entry logic: {"ALL must be true" if config.get("require_all_true") else "ANY can trigger"}')
    print()

print()
print('ANALYSIS:')
print('-' * 80)
print('If these bots have DIFFERENT trigger conditions, they should NOT all')
print('trigger simultaneously. The fact that they did suggests:')
print()
print('  1. They all have the SAME (or no) trigger conditions, OR')
print('  2. Market conditions satisfied ALL their different triggers at once, OR')
print('  3. There is a bug in the signal detection logic')
print()
print('YOUR ANSWER: Look above. If you see many bots with the SAME indicators,')
print('            then YES, they were supposed to all trigger together.')
print('            This is a DESIGN issue, not a BUG.')
