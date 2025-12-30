import os
import sys
import time
from datetime import datetime, timedelta

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.manager import calculate_early_exit_decay

def test_decay_logic():
    print("\n--- Testing Accelerated Decay Logic ---")
    start_time = datetime.now() - timedelta(minutes=61) # 61 minutes ago
    current_time = datetime.now()
    initial_tp = 1000.0
    break_even = 900.0
    
    # 30% reduction every 15 minutes
    # 60 mins / 15 mins = 4 intervals. 4 * 30% = 120% decay (should hit BE)
    settings = {
        'UseEarlyExit': True,
        'DecayIntervalMins': 15.0,
        'DecayPercentPerInterval': 30.0,
        'EEAllowLoss': False
    }
    
    adjusted_tp = calculate_early_exit_decay(start_time, current_time, 0, initial_tp, break_even, settings)
    print(f"Time: 60 mins | Initial: {initial_tp} | BE: {break_even} | Adjusted: {adjusted_tp}")
    
    if adjusted_tp == break_even:
        print("✅ Decay Success: TP reached Break Even after 100%+ decay.")
    else:
        print(f"❌ Decay Error: Expected {break_even}, got {adjusted_tp}")

def test_emergency_signal():
    print("\n--- Testing Emergency Signal File creation ---")
    # This just ensures we can write the signal
    if os.path.exists("engine.emergency"): os.remove("engine.emergency")
    
    with open("engine.emergency", "w") as f:
        f.write("test")
    
    if os.path.exists("engine.emergency"):
        print("✅ Emergency Signal file created successfully.")
        os.remove("engine.emergency")
    else:
        print("❌ Failed to create emergency signal.")

if __name__ == "__main__":
    test_decay_logic()
    test_emergency_signal()
