from engine.database import get_connection, recompute_invested_from_orders
import pandas as pd

def audit():
    conn = get_connection()
    cur = conn.cursor()
    
    # 1. Get all active bots
    cur.execute("SELECT id, name, pair FROM bots WHERE is_active = 1")
    bots = cur.fetchall()
    
    results = []
    for bot_id, name, pair in bots:
        # Recompute from scratch
        invested, avg_price, qty, step, hedge = recompute_invested_from_orders(bot_id)
        results.append({
            'Bot ID': bot_id,
            'Name': name,
            'Pair': pair,
            'Hedge Qty': f"{hedge:.4f}",
            'Open Qty': f"{qty:.4f}"
        })
    
    df = pd.DataFrame(results)
    print(df.to_string(index=False))

if __name__ == "__main__":
    audit()
