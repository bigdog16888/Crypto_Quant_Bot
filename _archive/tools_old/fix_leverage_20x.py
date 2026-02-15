#!/usr/bin/env python3
"""
Quick Fix: Update all active bots to use 20x leverage
"""

import sqlite3
import json
from pathlib import Path

db_path = Path(__file__).parent / "crypto_bot.db"

def main():
    print("="*80)
    print("LEVERAGE FIX: Updating all active bots to 20x leverage")
    print("="*80)
    print()
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Get all active bots
    cursor.execute("SELECT id, name, config FROM bots WHERE is_active = 1")
    bots = cursor.fetchall()
    
    print(f"Found {len(bots)} active bots\n")
    
    updated_count = 0
    
    for bot_id, name, config_json in bots:
        try:
            config = json.loads(config_json)
            
            # Get current leverage
            current_leverage = config.get('params', {}).get('leverage', 1)
            
            # Update leverage to 20
            if 'params' not in config:
                config['params'] = {}
            
            config['params']['leverage'] = 20
            
            # Update database
            new_config_json = json.dumps(config)
            cursor.execute("UPDATE bots SET config = ? WHERE id = ?", (new_config_json, bot_id))
            
            print(f"✓ {name:20s} - Updated: {current_leverage}x → 20x")
            updated_count += 1
            
        except Exception as e:
            print(f"✗ {name:20s} - ERROR: {e}")
    
    # Commit changes
    conn.commit()
    conn.close()
    
    print()
    print("="*80)
    print(f"COMPLETED: Updated {updated_count}/{len(bots)} bots")
    print("="*80)
    print()
    print("NEXT STEPS:")
    print("1. Restart the bot engine to apply changes")
    print("2. Verify leverage settings on exchange")
    print("3. Monitor margin requirements")
    print()

if __name__ == "__main__":
    main()
