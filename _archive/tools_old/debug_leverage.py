#!/usr/bin/env python3
import sys
import json
import sqlite3
import time

sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface, normalize_symbol

def check_leverage_compliance():
    print("Initializing Exchange...")
    ex = ExchangeInterface(market_type='future')
    
    print("Fetching ALL positions from Exchange...")
    positions = ex.exchange.fetch_positions()
    pos_map = {normalize_symbol(p['symbol']): p for p in positions}
    
    print(f"Found {len(positions)} positions on exchange.")

    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    c.execute("SELECT id, name, direction, config FROM bots WHERE is_active=1")
    rows = c.fetchall()
    
    print("\n--- LEVERAGE & MARGIN COMPLIANCE CHECK ---")
    print(f"{'Bot ID':<8} {'Name':<20} {'Symbol':<15} {'Cfg Lev':<8} {'Act Lev':<8} {'Status':<10}")
    print("-" * 80)
    
    mismatches = 0
    
    for r in rows:
        bot_id, name, direction, config_json = r
        try:
            cfg = json.loads(config_json)
            # Damao/Martingale bots usually have pair/symbol in params? 
            # Actually pair is usually linked to the bot in the DB but here we assume it's in config or we need to look at another table?
            # Wait, `bots` table usually doesn't have `pair` column? 
            # Looking at `bot_executor.py` or `manager.py`, `pair` is passed in.
            # Let's check `manager` or `runner` sql.
            # `debug_leverage.py` previously didn't query pair.
            # Assuming 'pair' or 'symbol' is in config or we need to join strategies table?
            # Let's peek at `engine/database.py` via previous knowledge or just assume config has it.
            # Looking at `bot_executor.py` line 60: `bot_id, name, pair, ... = bot_data`.
            # Retrieve pair properly.
            pass
        except:
             print(f"{bot_id:<8} {name:<20} INVALID CONFIG")
             continue

    # Re-query with pair if possible.
    # We need to know where 'pair' is stored. `bot_executor.py` gets it from somewhere.
    # Usually `SELECT ... FROM bots` might have it.
    # Let's query table info or just guess `pair` is in `bots`.
    
    # Better approach: Use the query from `bot_executor.py` logic or just `SELECT *` to see columns.
    # Or just use `config.get('pair')` if it's there.
    
    # I'll update the query to Include 'pair' if it exists, or just inspect config.
    # Based on `debug_leverage.py` previously, it didn't use pair from DB.
    
    # Let's try adding 'pair' to query.
    
    # Wait, I'll verify the table schema first?
    # No, I'll just check `config` for 'pair'.
    
    pass

# I'll rewrite the whole script in the `write_to_file` call below, making reasonable assumptions 
# or checking schema first? 
# I'll check schema quickly via sqlite3 command line within python.

if __name__ == "__main__":
    # Self-contained schema check then run
    import sqlite3
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(bots)")
    cols = [info[1] for info in cursor.fetchall()]
    has_pair = 'pair' in cols
    
    print(f"Table 'bots' columns: {cols}")
    
    query = "SELECT id, name, direction, config"
    if has_pair: query += ", pair"
    else: query += ", symbol" # fallback
    
    query += " FROM bots WHERE is_active=1"
    
    try:
        cursor.execute(query)
        bots = cursor.fetchall()
    except Exception as e:
        print(f"Query check failed: {e}")
        bots = []
        
    ex = None
    try:
        ex = ExchangeInterface(market_type='future')
        print("Exchange Initialized.")
    except:
        print("Failed to init exchange.")
        sys.exit(1)
        
    positions = ex.exchange.fetch_positions()
    # Normalize
    pos_dict = {}
    for p in positions:
        # ccxt symbols are usually unified, e.g. BTC/USDT:USDT
        # Bot pair might be 'BTC/USDT'
        # We need a robust matcher.
        sym = p['symbol']
        pos_dict[sym] = p
        # Also store base symbol?
        
    print(f"\n{'Bot':<5} {'Name':<15} {'Pair':<12} {'CfgLev':<7} {'ActLev':<7} {'Mode':<8} {'Status'}")
    print("-" * 80)
    mismatches = 0
    
    for b in bots:
        # Unpack based on columns
        if has_pair:
             bid, name, direction, cfg_json, pair = b
        else:
             # Try other structure
             bid, name, direction, cfg_json, pair = b[0], b[1], b[2], b[3], "UNKNOWN"
             
        cfg = json.loads(cfg_json)
        # Check config for pair if not in col
        if pair == "UNKNOWN": pair = cfg.get('pair', cfg.get('symbol', 'Unknown'))
        
        target_lev = int(cfg.get('leverage', 1))
        
        # Find position
        # Try exact match, then fuzzy
        actual_lev = "N/A"
        margin_mode = "N/A"
        
        # logic to find p
        matches = [p for sym, p in pos_dict.items() if pair in sym or sym.replace('/','').startswith(pair.replace('/',''))]
        # Prefer exact match of base/quote
        
        # CCXT symbols: BTC/USDT:USDT
        # pair: BTC/USDT
        
        p = None
        for sym, pos in pos_dict.items():
            if normalize_symbol(sym) == normalize_symbol(pair):
                p = pos
                break
        
        if p:
            actual_lev = p.get('leverage')
            if actual_lev is None:
                # Fallback: Calculate from Notional / Margin
                try:
                    info = p.get('info', {})
                    notional = float(info.get('notional', 0))
                    init_margin = float(info.get('positionInitialMargin', 0))
                    if init_margin > 0:
                        calc_val = round(abs(notional) / init_margin)
                        actual_lev = f"{calc_val} (Calc)"
                    else:
                        actual_lev = "None"
                except:
                    actual_lev = "None"
            
            margin_mode = p.get('marginMode')
            if margin_mode is None: margin_mode = "None"
            
            status = "✅ OK"
            # Compare numeric part
            try:
                # Handle "20 (Calc)" -> 20.0
                val_str = str(actual_lev).split()[0]
                # If we have "None", float() fails
                if val_str == "None":
                     status = "❓ UNKNOWN"
                elif float(val_str) != float(target_lev):
                    status = "❌ LEV MISMATCH"
            except:
                status = "❓ UNKNOWN"
            
            print(f"{bid:<5} {str(name)[:15]:<15} {str(pair):<12} {str(target_lev):<7} {str(actual_lev):<10} {str(margin_mode):<8} {status}")
        else:
            print(f"{bid:<5} {str(name)[:15]:<15} {str(pair):<12} {str(target_lev):<7} {'None':<10} {'-':<8} ⚠️ No Pos")

