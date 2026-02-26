
import sqlite3
import os
import sys
import re
import json
import time

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from config.settings import config
from engine.database import get_connection, add_bot, update_bot

def recover_bots():
    log_path = os.path.join(config.ROOT_DIR, 'engine.log')
    print(f"Scanning log file: {log_path}")
    
    if not os.path.exists(log_path):
        print("❌ engine.log NOT FOUND!")
        return

    # Regex patterns to find bot details
    # Pattern 1: DEBUG_SIG: ID=10005 Name=long eth rsi ... Params={...}
    # We focus on capturing the JSON Params as it contains everything
    pattern_params = re.compile(r"ID=(\d+).*?Params=({.*?})", re.DOTALL)
    
    # Fallback pattern for simple "Bot X deactivated" or similar if needed, 
    # but we really need the config JSON to restore safely.
    
    restored_count = 0
    found_bots = {}

    try:
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                # We only care about lines with "Params={" which usually appear in DEBUG_SIG or startup logs
                if "Params={" in line:
                    match = pattern_params.search(line)
                    if match:
                        bot_id = int(match.group(1))
                        json_str = match.group(2)
                        
                        try:
                            # Clean up JSON string if needed (sometimes logs cut off)
                            # Simple heuristic: try to find the matching closing brace
                            # For now, assume the log line has the full JSON if it was a single line log
                            
                            # Sometimes JSON in logs uses single quotes which is invalid JSON, but valid Python dict repr
                            # We might need `ast.literal_eval` if it's a python dict string
                            import ast
                            
                            try:
                                config_data = json.loads(json_str)
                            except json.JSONDecodeError:
                                try:
                                    config_data = ast.literal_eval(json_str)
                                except:
                                    continue # Skip if unparseable
                            
                            # Normalize Keys
                            # We need 'name', 'pair', 'direction', 'rsi_limit', 'martingale_multiplier', 'base_size'
                            # These might be in the config_data or we might need to infer them
                            
                            # Identify critical fields
                            name = config_data.get('name', f"Recovered_Bot_{bot_id}")
                            pair = config_data.get('pair', 'BTC/USDT')
                            direction = config_data.get('direction', 'LONG')
                            
                            # Strategy params
                            rsi_limit = float(config_data.get('rsi_limit', 0))
                            martingale_multiplier = float(config_data.get('martingale_multiplier', 1.5))
                            base_size = float(config_data.get('base_size', 10.0))
                            strategy_type = config_data.get('strategy_type', 'Martingale')
                            
                            # Store in dict to deduplicate (keep latest)
                            found_bots[bot_id] = {
                                'name': name,
                                'pair': pair,
                                'direction': direction,
                                'rsi_limit': rsi_limit,
                                'martingale_multiplier': martingale_multiplier,
                                'base_size': base_size,
                                'strategy_type': strategy_type,
                                'config': config_data
                            }
                            
                        except Exception as e:
                            # print(f"Failed to parse match: {e}")
                            pass

        print(f"Found {len(found_bots)} unique bots in logs.")
        
        if not found_bots:
            print("⚠️ No restorable bots found in logs.")
            return

        # Restore to DB
        conn = get_connection()
        cursor = conn.cursor()
        
        print("\n--- RESTORING BOTS ---")
        for bid, data in found_bots.items():
            print(f"Restoring Bot {bid}: {data['name']} ({data['pair']})...")
            
            # Upsert
            # Check if exists (it shouldn't since DB is empty, but for safety)
            cursor.execute("SELECT id FROM bots WHERE id = ?", (bid,))
            exists = cursor.fetchone()
            
            config_json = json.dumps(data['config'])
            
            if exists:
                # Update
                cursor.execute("""
                    UPDATE bots SET 
                        name=?, pair=?, direction=?, rsi_limit=?, martingale_multiplier=?, 
                        base_size=?, strategy_type=?, config=?, is_active=0, status='Stopped'
                    WHERE id=?
                """, (data['name'], data['pair'], data['direction'], data['rsi_limit'], 
                      data['martingale_multiplier'], data['base_size'], data['strategy_type'], 
                      config_json, bid))
            else:
                # Insert with explicit ID to preserve history linkage if possible
                try:
                    cursor.execute("""
                        INSERT INTO bots (id, name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config, is_active, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'Stopped')
                    """, (bid, data['name'], data['pair'], data['direction'], data['rsi_limit'], 
                          data['martingale_multiplier'], data['base_size'], data['strategy_type'], 
                          config_json))
                    
                    # Ensure trades table entry exists
                    cursor.execute("INSERT OR IGNORE INTO trades (bot_id) VALUES (?)", (bid,))
                    
                except Exception as e:
                    print(f"  Error inserting bot {bid}: {e}")
                    # Try without ID if that failed (let autoincrement work, but we lose history link)
                    add_bot(data['name'], data['pair'], data['direction'], data['rsi_limit'], 
                            data['martingale_multiplier'], data['base_size'], data['strategy_type'], data['config'])

            restored_count += 1
            
        conn.commit()
        print(f"\n✅ Successfully restored {restored_count} bots.")
        print("Bots are set to 'Stopped' state for safety. Please review and enable them in the UI.")

    except Exception as e:
        print(f"❌ Recovery failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    recover_bots()
