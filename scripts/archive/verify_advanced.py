import sys
import os
from datetime import datetime, timedelta

# Add current directory to path
sys.path.append(os.getcwd())

from engine.risk import calculate_grid_spacing_atr
from engine.manager import calculate_early_exit_decay
from config.settings import get_settings

def verify_atr_grid():
    print("\n--- Verifying ATR Dynamic Grid ---")
    settings = get_settings()
    atr_val = 0.0020 # 20 Pips
    
    # Test Level 1 (Default: ATR * 1) -> 20 Pips
    grid, tp = calculate_grid_spacing_atr(atr_val, 1, settings)
    print(f"Level 1 (ATR {atr_val}): Grid={grid:.5f}, TP={tp:.5f} | Expected: Grid ~0.0020")
    
    # Test Level 5 (Above Set1=4): Grid * 2 -> 40 Pips
    grid, tp = calculate_grid_spacing_atr(atr_val, 5, settings)
    print(f"Level 5 (ATR {atr_val}): Grid={grid:.5f}, TP={tp:.5f} | Expected: Grid ~0.0040")
    
    # Test Level 9 (Above Set2=8): Grid * 4 -> 80 Pips
    grid, tp = calculate_grid_spacing_atr(atr_val, 9, settings)
    print(f"Level 9 (ATR {atr_val}): Grid={grid:.5f}, TP={tp:.5f} | Expected: Grid ~0.0080")

def verify_early_exit():
    print("\n--- Verifying Early Exit Logic ---")
    settings = get_settings()
    settings['UseEarlyExit'] = True
    settings['EEHoursPC'] = 0.5 # 0.5% per hour
    settings['EEStartHours'] = 2.0 # Start after 2 hours
    settings['EEStartLevel'] = 100 # Disable Level decay for this test
    
    initial_tp = 1.1000
    breakeven = 1.0900
    profit_dist = initial_tp - breakeven # 0.0100 (100 pips)
    
    start_time = datetime.now() - timedelta(hours=12) # 12 hours ago
    current_time = datetime.now()
    
    # Expected: 
    # Duration = 12 hours. Start = 2 hours. Decay Duration = 10 hours.
    # Decay = 10 * 0.5% = 5%.
    # New Target = BE + (ProfitDist * 95%)
    # New Target = 1.0900 + (0.0100 * 0.95) = 1.0995
    
    new_tp = calculate_early_exit_decay(
        start_time, current_time, 
        total_orders=1, 
        initial_tp=initial_tp, 
        break_even=breakeven, 
        settings=settings
    )
    
    print(f"Initial TP: {initial_tp}, BE: {breakeven}")
    print(f"Hrs Open: 12.0. Expected Decay: 5%.")
    print(f"New TP: {new_tp:.5f}")
    
    expected = 1.0995
    if abs(new_tp - expected) < 0.0001:
        print("SUCCESS: Early Exit Decay calculated correctly.")
    else:
        print(f"FAILURE: Expected {expected}, got {new_tp}")

if __name__ == "__main__":
    verify_atr_grid()
    verify_early_exit()
