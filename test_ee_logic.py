import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
import math

# Mock settings
settings_loss_allowed = {
    'UseEarlyExit': True,
    'EEStartHours': 0.0,
    'EEHoursPC': 0.0,
    'DecayIntervalMins': 15.0,
    'DecayPercentPerInterval': 30.0,
    'EEStartLevel': 5,
    'EELevelPC': 0.0,
    'EEAllowLoss': True
}

settings_no_loss = settings_loss_allowed.copy()
settings_no_loss['EEAllowLoss'] = False

def test_decay(settings_desc, settings):
    print(f"\n--- Testing Decay ({settings_desc}) ---")
    start_time = datetime.now() - timedelta(minutes=60) # 1 hour ago
    current_time = datetime.now()
    initial_tp = 90.0 # SHORT: TP < BE
    break_even = 100.0
    
    # Replicate calculate_early_exit_decay
    duration_seconds = (current_time - start_time).total_seconds()
    duration_mins = duration_seconds / 60.0
    
    interval_mins = settings.get('DecayIntervalMins', 60.0)
    decay_per_interval = settings.get('DecayPercentPerInterval', 0.0) / 100.0
    
    ee_pc = (duration_mins / interval_mins) * decay_per_interval
    decay_factor = 1.0 - ee_pc
    
    allow_loss = settings.get('EEAllowLoss', False)
    if not allow_loss:
        if decay_factor < 0.0: decay_factor = 0.0
    else:
        max_decay = -1.0
        if decay_factor < max_decay: decay_factor = max_decay
        
    adjusted_tp = break_even + (initial_tp - break_even) * decay_factor
    print(f"Start: {start_time}, Current: {current_time}, duration_mins: {duration_mins}")
    print(f"Decay %: {ee_pc*100}%, Decay Factor: {decay_factor}")
    print(f"Initial TP: {initial_tp}, Break Even: {break_even}")
    print(f"Adjusted TP: {adjusted_tp}")

test_decay("Loss Not Allowed", settings_no_loss)
test_decay("Loss Allowed", settings_loss_allowed)

# Fast-forward time for loss allowed
def test_deep_loss():
    print(f"\n--- Testing Deep Loss (Loss Allowed) ---")
    start_time = datetime.now() - timedelta(minutes=300) # 5 hours ago
    current_time = datetime.now()
    initial_tp = 90.0 
    break_even = 100.0
    
    duration_seconds = (current_time - start_time).total_seconds()
    duration_mins = duration_seconds / 60.0
    interval_mins = settings_loss_allowed.get('DecayIntervalMins', 60.0)
    decay_per_interval = settings_loss_allowed.get('DecayPercentPerInterval', 0.0) / 100.0
    
    ee_pc = (duration_mins / interval_mins) * decay_per_interval
    decay_factor = 1.0 - ee_pc
    print(f"Raw decay factor: {decay_factor}")
    
    max_decay = -1.0
    if decay_factor < max_decay: decay_factor = max_decay
    
    adjusted_tp = break_even + (initial_tp - break_even) * decay_factor
    print(f"Capped decay factor: {decay_factor}")
    print(f"Adjusted TP: {adjusted_tp} (Should not go higher than 110.0 for a SHORT)")

test_deep_loss()
