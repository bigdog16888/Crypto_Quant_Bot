
import sqlite3
import pandas as pd

def reconstruct_bot_state(bot_id, current_position_size):
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    
    print(f"--- Reconstructing State for Bot {bot_id} (Size: {current_position_size}) ---")
    
    # Get all filled orders for this bot, newest first
    # We strictly look for orders that *opened* margin (Entry/Grid).
    # Assuming Long bot -> Buys increase position.
    query = """
        SELECT id, price, amount, order_type, step, status, created_at 
        FROM bot_orders 
        WHERE bot_id = ? 
          AND status IN ('filled', 'closed')
          AND order_type IN ('entry', 'grid', 'buy', 'manual') 
        ORDER BY created_at DESC
    """
    
    df = pd.read_sql_query(query, conn, params=(bot_id,))
    
    if df.empty:
        print("No filled orders found.")
        return

    accumulated_size = 0.0
    contributing_orders = []
    
    for index, row in df.iterrows():
        amount = row['amount']
        step = row['step']
        print(f"Processing Order {row['id']}: {amount} @ {row['price']} (Step {step})")
        
        accumulated_size += amount
        contributing_orders.append(row)
        
        # Check if we have accounted for the full position
        # tolerance for float math
        if accumulated_size >= (current_position_size - 0.0001):
            break
            
    # Calculate Metrics from contributing orders
    # The 'current_step' should be the highest step found in the contributing stack
    max_step = max([o['step'] for o in contributing_orders]) if contributing_orders else 1
    total_invested = sum([o['amount'] * o['price'] for o in contributing_orders])
    total_size = sum([o['amount'] for o in contributing_orders])
    avg_price = total_invested / total_size if total_size > 0 else 0
    
    print(f"✅ State Reconstruction Complete:")
    print(f"   - Reconstructed Step: {max_step}")
    print(f"   - Total Size: {total_size} (Target: {current_position_size})")
    print(f"   - Total Invested: ${total_invested:.2f}")
    print(f"   - Avg Price: ${avg_price:.2f}")
    print(f"   - Contributing Orders: {len(contributing_orders)}")
    for o in contributing_orders:
        import math
    
    # Fetch bot config to get Multiplier
    c = conn.cursor()
    c.execute("SELECT base_size, martingale_multiplier FROM bots WHERE id=?", (bot_id,))
    config = c.fetchone()
    if config:
        base_size = config[0]
        multiplier = config[1]
        
        # Theoretical Step Calculation
        # Total = Base * Sum(Mult^i) ... approximate as roughly Proportional to Max Term
        # Last Term Size = Base * Mult^(Step-1)
        # Total Size is roughly Sum of geometric series
        # Let's use the Ratio approach
        if base_size > 0 and multiplier > 1:
            ratio = total_invested / base_size
            # Geometric Sum Formula: S_n = a(1-r^n)/(1-r)
            # Total ~ Base * (1 - Mult^Step) / (1 - Mult)
            # Solve for Step:
            # Total/Base * (1 - Mult) = 1 - Mult^Step
            # Mult^Step = 1 - (Total/Base)*(1-Mult)
            # Step = log(1 - ratio*(1-mult)) / log(mult)
            
            try:
                term = 1 - (ratio * (1 - multiplier))
                theoretical_step = math.log(term) / math.log(multiplier)
                print(f"   - Mathematical Truth (Physics): Step {theoretical_step:.2f}")
                print(f"     (Based on Base={base_size}, Mult={multiplier}, Ratio={ratio:.2f})")
            except Exception as e:
                print(f"   - Math Calc Failed: {e}")
                
    conn.close()

if __name__ == "__main__":
    # Test with Bot 10002 and its known approximate size ~4.95
    reconstruct_bot_state(10002, 4.952)
