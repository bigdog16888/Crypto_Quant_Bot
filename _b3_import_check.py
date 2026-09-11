import os, sys
os.environ["TESTING_MODE"] = "True"
os.environ["PYTEST_RUNNING"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine.oneway_netting as on

# Names imported repo-wide from engine.oneway_netting (from grep). Each MUST exist.
must_exist = [
    "get_pair_open_qty_net",
    "gate_oneway_opposite_entry",
    "reconcile_oneway_pair_open_qty",
    "get_authoritative_close_qty",
    "detect_bot_ghost",
    "wipe_bot_ghost",
    "detect_hedge_child_ghost",
    "wipe_hedge_child_ghost",
    "sync_pair_to_exchange",
    "get_typical_position_size",
    "detect_unowned_exchange_positions",
]
# Intentionally removed; a test asserts it is NOT importable.
must_not_exist = ["apply_oneway_entry_cross_reduction"]

ok = True
print("=== B3: oneway_netting import check ===")
for name in must_exist:
    present = hasattr(on, name)
    print(f"  {'OK ' if present else 'MISSING'} {name}")
    if not present:
        ok = False
for name in must_not_exist:
    present = hasattr(on, name)
    print(f"  {'OK(removed)' if not present else 'UNEXPECTED-PRESENT'} {name}")
    if present:
        ok = False

print("RESULT:", "PASS - zero broken imports" if ok else "FAIL")
sys.exit(0 if ok else 1)
