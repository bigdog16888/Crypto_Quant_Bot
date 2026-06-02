import sqlite3
import pandas as pd

def run():
    conn = sqlite3.connect('crypto_bot.db')
    
    print("=== All Hedge Children Bots ===")
    query = """
    SELECT b.id, b.name, t.open_qty, t.avg_entry_price, t.cycle_id, t.tp_order_id, b.status
    FROM trades t JOIN bots b ON b.id = t.bot_id
    WHERE b.parent_bot_id IS NOT NULL AND b.is_active = 1;
    """
    df = pd.read_sql_query(query, conn)
    print(df.to_string())
    
    print("\n=== Active TP Orders for all Hedge Children Bots ===")
    query2 = """
    SELECT o.bot_id, b.name, o.order_id, o.client_order_id, o.price, o.amount, o.status, o.cycle_id
    FROM bot_orders o JOIN bots b ON b.id = o.bot_id
    WHERE b.parent_bot_id IS NOT NULL AND o.order_type = 'tp' AND o.status IN ('pending_placement', 'open', 'new')
    ORDER BY o.bot_id;
    """
    df2 = pd.read_sql_query(query2, conn)
    print(df2.to_string())
    
    conn.close()

if __name__ == '__main__':
    run()
