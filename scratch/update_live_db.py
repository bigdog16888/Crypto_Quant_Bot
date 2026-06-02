import sqlite3

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    print("--- UPDATING 100316 ORDER TO PENDING_PLACEMENT ---")
    cursor.execute("""
    UPDATE bot_orders
    SET status = 'pending_placement', order_id = '326830607', notes = 'Manually reset to pending_placement for self-healing'
    WHERE client_order_id = 'CQB_100316_TP_78_BE_FB' AND bot_id = 100316;
    """)
    conn.commit()
    print("Done. Rows updated:", cursor.rowcount)
    
    # Also, let's verify trades table tp_order_id is NULL for bot 100316
    # so maintain_orders processes it
    cursor.execute("""
    UPDATE trades
    SET tp_order_id = NULL
    WHERE bot_id = 100316;
    """)
    conn.commit()
    print("Trades table tp_order_id cleared. Rows updated:", cursor.rowcount)

if __name__ == '__main__':
    run()
