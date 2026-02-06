"""Debug StateReconciler logic"""
import logging
import sys
from engine.reconciler import StateReconciler, PositionOwner, ExchangePosition, BotState
from engine.exchange_interface import normalize_symbol

# Setup basic logging
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("DebugReconciler")

print("=== Debugging StateReconciler ===")
reconciler = StateReconciler()

print("\n1. Fetching Exchange Positions...")
positions = reconciler.fetch_all_exchange_positions()
print(f"Found positions for {len(positions)} symbols.")
for sym, pos_list in positions.items():
    print(f"  Symbol: '{sym}' (Norm: '{normalize_symbol(sym)}')")
    for p in pos_list:
        print(f"    Side: {p.side}, Size: {p.size}, Entry: {p.entry_price}")

print("\n2. Testing Bot matching for BTC/USDC (Bot 41)...")
# Simulate Bot 41 state
bot_pair = "BTC/USDC"
bot_norm = normalize_symbol(bot_pair)
print(f"Bot Pair: '{bot_pair}' (Norm: '{bot_norm}')")

# Logic from reconcile_all
position_list = positions.get(bot_pair, [])
print(f"Direct lookup count: {len(position_list)}")

if not position_list:
    print("Direct lookup failed. Trying fuzzy match...")
    for p_sym, p_data in positions.items():
        if normalize_symbol(p_sym) == bot_norm:
            position_list = p_data
            print(f"  Fuzzy matched with '{p_sym}'")
            break

print(f"Final position list count: {len(position_list)}")

target_side = 'long'
position = None
if position_list:
    # 1. Try to find exact side match (Hedge Mode)
    for p in position_list:
        print(f"  Checking pos side '{p.side}' vs target '{target_side}'")
        if str(p.side).lower() == target_side:
            position = p
            print("    -> Match found (Exact side)")
            break
    
    # 2. If no exact side match, and only 1 position exists, check if it's 'both' (One-Way Mode)
    if not position and len(position_list) == 1:
        p = position_list[0]
        print(f"  Checking fallback (One-Way) for side '{p.side}'")
        if str(p.side).lower() in ['both', 'none', target_side]:
            position = p
            print("    -> Match found (Fallback)")

if position:
    print(f"\n✅ SUCCESS: Found matching position: {position.size} @ {position.entry_price}")
else:
    print("\n❌ FAILURE: No matching position found! This causes the reset.")
