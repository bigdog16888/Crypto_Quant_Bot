from engine.database import calculate_step_from_position
import math

def sim_recovery(total_invested, db_base_size, db_mult):
    if total_invested <= db_base_size * 1.1:
        return 1
    elif db_mult > 1:
        simulated_total = db_base_size
        simulated_step = 1
        current_order_size = db_base_size
        
        while simulated_total < (total_invested * 0.95): # 5% tolerance for slippage
            simulated_step += 1
            current_order_size *= db_mult
            simulated_total += current_order_size
            if simulated_step >= 50: # infinite loop safety
                break
                
        return simulated_step
    return max(1, round(total_invested / db_base_size))

steps = [
  (10.0, 10.0, 1.05),
  (20.5, 10.0, 1.05),
  (50.0, 10.0, 1.05),
  (164270, 150.0, 1.05) # Large short btc position
]

for t, b, m in steps:
    print(f"Invested=${t} Base=${b} Mult={m} -> Step={sim_recovery(t,b,m)}")
