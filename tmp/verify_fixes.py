"""
Verify that the 3 reconciler fixes are in place in reconciler.py
"""
import sys, inspect
sys.path.insert(0, '.')
from engine.reconciler import StateReconciler

src = inspect.getsource(StateReconciler.adopt_from_physical_positions)

checks = [
    ("FIX 1 – phys_positions stores qty key",          "'qty'" in src),
    ("FIX 1 – phys_positions stores entry_price key",  "'entry_price'" in src),
    ("FIX 2 – order_type column in INSERT",             "order_type" in src and "_otype_ins" in src),
    ("FIX 3 – phys_entry_from_exchange used",           "phys_entry_from_exchange" in src),
    ("REGRESSION – side NOT in INSERT column list",     "side, price" not in src),
]

all_ok = True
for name, passed in checks:
    status = "OK" if passed else "FAIL"
    print(f"  [{status}] {name}")
    if not passed:
        all_ok = False

if all_ok:
    print("\n✅ All 3 fixes verified. reconciler.py is correct.")
else:
    print("\n❌ One or more fixes failed verification!")
    sys.exit(1)
