
import sqlite3
import pandas as pd
import json

DB_PATH = 'c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/crypto_bot.db'

def inspect_bots():
    conn = sqlite3.connect(DB_PATH)
    try:
        print("--- BOT CONFIGURATIONS ---")
        df = pd.read_sql_query("SELECT id, name, pair, direction, config FROM bots", conn)
        print(df.to_string())
        
        for index, row in df.iterrows():
            print(f"\n[Bot {row['id']} Config]")
            try:
                cfg = json.loads(row['config'])
                print(json.dumps(cfg, indent=2))
            except:
                print("Invalid JSON")
                
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    inspect_bots()
