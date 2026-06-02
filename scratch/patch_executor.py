import os

path = r'engine/bot_executor.py'
if not os.path.exists(path):
    print("Error: bot_executor.py not found at", path)
    exit(1)

with open(path, 'r', encoding='utf-8') as f:
    code = f.read()

# Note the trailing space at the end of target1's first line:
target1 = """            # 🚀 FUNDAMENTAL FIX: Inject missing SQLite configuration 
            # so the strategy doesn't fallback to $150 and 2.0x
            bot_config['base_size'] = base_size
            bot_config['martingale_multiplier'] = martingale_multiplier
            bot_config['rsi_limit'] = rsi_limit"""

replacement1 = """            # 🚀 FUNDAMENTAL FIX: Inject missing SQLite configuration 
            # so the strategy doesn't fallback to $150 and 2.0x
            bot_config['base_size'] = base_size
            bot_config['martingale_multiplier'] = martingale_multiplier
            bot_config['rsi_limit'] = rsi_limit

            # Get bot_type from config or DB
            bot_type = bot_config.get('bot_type')
            if not bot_type:
                try:
                    from engine.database import get_connection as _gc_type
                    with _gc_type() as _conn:
                        _res = _conn.execute("SELECT bot_type FROM bots WHERE id=?", (bot_id,)).fetchone()
                        bot_type = _res[0] if _res else 'standard'
                except Exception:
                    bot_type = 'standard'
            bot_config['bot_type'] = bot_type"""

if target1 in code:
    code = code.replace(target1, replacement1)
    print("Target 1 replaced successfully.")
else:
    # Try with single-line split search
    print("Target 1 NOT found in file.")

with open(path, 'w', encoding='utf-8') as f:
    f.write(code)

print("Patch complete.")
