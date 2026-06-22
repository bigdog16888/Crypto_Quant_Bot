import sqlite3
import sys
import time

sys.path.append('.')
from engine.ledger import seal_trade_state
from engine.database import get_connection, save_bot_order

def run_recovery():
    conn = get_connection()
    cursor = conn.cursor()
    
    print("Executing DB Recovery...")
    
    # 1. BTC Recovery:
    # Mark order 425924649 as filled (amount 0.029) on Bot 10022
    cursor.execute(
        "UPDATE bot_orders SET status = 'filled', filled_amount = 0.029 WHERE order_id = '425924649'"
    )
    conn.commit()
    print("Stamped BTC order 425924649 as filled.")
    
    # Get current cycle for Bot 10016
    cycle_row = cursor.execute("SELECT cycle_id FROM trades WHERE bot_id = 10016").fetchone()
    current_cycle_10016 = cycle_row[0] if cycle_row else 36
    
    # Insert missing virtual netting rows on Bot 10016
    # For 425920820 (qty 0.015, price 61581.5)
    vn1 = save_bot_order(
        bot_id=10016,
        order_type='virtual_netting',
        exchange_order_id=f"VN_10016_425920820_{int(time.time())}",
        price=61581.5,
        amount=0.015,
        step=0,
        status='filled',
        client_order_id=f"CQB_10016_VNET_425920820_{int(time.time())}",
        cycle_id=current_cycle_10016
    )
    print(f"Inserted virtual netting row for 425920820 on Bot 10016: Row ID {vn1}")
    
    # For 425924649 (qty 0.029, price 61665.4)
    vn2 = save_bot_order(
        bot_id=10016,
        order_type='virtual_netting',
        exchange_order_id=f"VN_10016_425924649_{int(time.time())}",
        price=61665.4,
        amount=0.029,
        step=0,
        status='filled',
        client_order_id=f"CQB_10016_VNET_425924649_{int(time.time())}",
        cycle_id=current_cycle_10016
    )
    print(f"Inserted virtual netting row for 425924649 on Bot 10016: Row ID {vn2}")
    
    # 2. ETH Recovery:
    # Mark order 369419132 as filled (amount 0.065) on Bot 10011
    cursor.execute(
        "UPDATE bot_orders SET status = 'filled', filled_amount = 0.065 WHERE order_id = '369419132'"
    )
    conn.commit()
    print("Stamped ETH order 369419132 as filled.")
    
    # 3. Seal states
    print("\nSealing trade states...")
    seal16 = seal_trade_state(10016, force_recompute=True)
    seal22 = seal_trade_state(10022, force_recompute=True)
    seal11 = seal_trade_state(10011, force_recompute=True)
    
    # Print results from trades table
    print("\n--- Final Trades Table Status ---")
    for bid in [10016, 10022, 10011]:
        row = cursor.execute(
            "SELECT t.bot_id, t.cycle_id, t.open_qty, b.status "
            "FROM trades t JOIN bots b ON b.id = t.bot_id "
            "WHERE t.bot_id = ?", (bid,)
        ).fetchone()
        print(f"Bot {bid}: Cycle={row[1]}, Open Qty={row[2]:.4f}, Status={row[3]}")
        
    conn.close()

if __name__ == '__main__':
    run_recovery()
