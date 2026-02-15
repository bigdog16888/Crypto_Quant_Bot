#!/usr/bin/env python3
"""Verify the recovery assignment for bot 41"""
import sys
import os

engine_path = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, engine_path)

from database import get_bot_status, get_trade_history

BOT_ID = 41

print("="*60)
print(f"VERIFICATION FOR BOT {BOT_ID}")
print("="*60)

# 1. Check trades table
print("\n1. TRADES TABLE STATUS:")
status = get_bot_status(BOT_ID)
if status:
    print(f"   Bot Name: {status[0]}")
    print(f"   Pair: {status[1]}")
    print(f"   Current Step: {status[2]}")
    print(f"   Total Invested: ${status[3]:.2f}")
    print(f"   Avg Entry Price: ${status[4]:.4f}")
    print(f"   Target TP Price: ${status[5]:.4f}")
    print(f"   Last Exit Price: ${status[6]:.4f}")
    print(f"   Basket Start Time: {status[8]}")
    
    # Verify expected values
    if status[2] == 1 and abs(status[3] - 660.00) < 0.01:
        print("   ✓ Trades table updated correctly")
    else:
        print("   ✗ Trades table values don't match expected")
else:
    print("   ✗ Bot not found")

# 2. Check trade_history
print("\n2. TRADE_HISTORY (Last 5 entries):")
history = get_trade_history(BOT_ID, limit=5)
if history:
    for entry in history:
        print(f"   ID:{entry[0]} | {entry[3]} | {entry[4]} | ${entry[5]:.2f} | {entry[10]} | {entry[11]}")
    
    # Check for RECOVERY entry
    recovery_found = any(entry[3] == 'RECOVERY' for entry in history)
    if recovery_found:
        print("\n   ✓ RECOVERY trade found in history")
    else:
        print("\n   ✗ No RECOVERY trade found")
else:
    print("   No trade history found")

print("\n" + "="*60)
print("VERIFICATION COMPLETE")
print("="*60)
