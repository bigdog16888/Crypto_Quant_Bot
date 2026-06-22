import sqlite3
import sys
import time

sys.path.append('.')
from engine.ledger import seal_trade_state
from engine.database import get_connection, save_bot_order

def run_recovery_part2():
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get current cycle of Bot 10016
    cycle_row = cursor.execute("SELECT cycle_id FROM trades WHERE bot_id = 10016").fetchone()
    current_cycle_10016 = cycle_row[0] if cycle_row else 36
    
    # Fills to process
    fills = [
        ('425672789', 0.002, 61284.0),
        ('425674775', 0.004, 61408.4),
        ('425812195', 0.008, 61504.4)
    ]
    
    print("Inserting/updating virtual netting rows on Bot 10016...")
    for fill_id, fill_qty, fill_price in fills:
        row_id = save_bot_order(
            bot_id=10016,
            order_type='virtual_netting',
            exchange_order_id=f"VN_10016_{fill_id}_{int(time.time())}",
            price=fill_price,
            amount=fill_qty,
            step=0,
            status='filled',
            client_order_id=f"CQB_10016_VNET_{fill_id}_{int(time.time())}",
            cycle_id=current_cycle_10016
        )
        print(f"Fill {fill_id} on 10016: row_id={row_id}")

    # Insert recovery virtual netting on Bot 10022 to reduce its position by 0.014
    print("Inserting recovery virtual netting on Bot 10022...")
    vn_10022_id = save_bot_order(
        bot_id=10022,
        order_type='virtual_netting',
        exchange_order_id=f'VN_10022_RECOVERY_{int(time.time())}',
        price=61636.0,
        amount=0.014,
        step=0,
        status='filled',
        client_order_id=f'CQB_10022_VNET_RECOVERY_{int(time.time())}',
        cycle_id=35
    )
    # Update filled amount
    cursor.execute(
        "UPDATE bot_orders SET filled_amount = 0.014 WHERE id = ?", (vn_10022_id,)
    )
    conn.commit()
    print(f"Recovery VN on 10022: row_id={vn_10022_id}")
        
    print("\nSealing trade states...")
    seal_trade_state(10016, force_recompute=True)
    seal_trade_state(10022, force_recompute=True)
    
    # Query final trades values
    print("\n--- Final trades table open_qty values ---")
    for bid in [10016, 10022]:
        row = cursor.execute(
            "SELECT t.bot_id, t.cycle_id, t.open_qty, b.status "
            "FROM trades t JOIN bots b ON b.id = t.bot_id "
            "WHERE t.bot_id = ?", (bid,)
        ).fetchone()
        print(f"Bot {bid}: Cycle={row[1]}, Open Qty={row[2]:.4f}, Status={row[3]}")
        
    conn.close()

if __name__ == '__main__':
    run_recovery_part2()
