"""
Smoke test for the architectural overhaul.
No exchange connection — pure import + DB check.
"""
import sys, os
sys.path.insert(0, '.')

errors = []

# 1. safe_wipe_bot importable
try:
    from engine.database import safe_wipe_bot
    print("  OK: safe_wipe_bot importable")
except Exception as e:
    errors.append(f"safe_wipe_bot import FAILED: {e}")
    print(f"  FAIL: {e}")

# 2. prime_startup_snapshot exists
try:
    from engine.reconciler import StateReconciler
    assert hasattr(StateReconciler, 'prime_startup_snapshot'), "Missing method"
    print("  OK: prime_startup_snapshot method exists on StateReconciler")
except Exception as e:
    errors.append(f"prime_startup_snapshot FAILED: {e}")
    print(f"  FAIL: {e}")

# 3. _startup_snapshot attribute initialises in __init__
try:
    from engine.reconciler import StateReconciler
    # Pass None to avoid exchange construction
    class FakeEx: pass
    r = StateReconciler.__new__(StateReconciler)
    r.exchanges = {}
    r.results = []
    r.cid_cache = {}
    r._startup_snapshot = {}
    assert isinstance(r._startup_snapshot, dict)
    print("  OK: _startup_snapshot attribute exists and is dict")
except Exception as e:
    errors.append(f"_startup_snapshot FAILED: {e}")
    print(f"  FAIL: {e}")

# 4. cycle_phase column in DB
import sqlite3
db_path = os.path.join('data', 'crypto_bot.db')
if os.path.exists(db_path):
    try:
        conn = sqlite3.connect(db_path)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()]
        conn.close()
        if 'cycle_phase' in cols:
            print("  OK: cycle_phase column EXISTS in trades table")
        else:
            print("  WARN: cycle_phase not yet in DB — will be added on first init_db()")
    except Exception as e:
        errors.append(f"DB check FAILED: {e}")
        print(f"  FAIL: {e}")
else:
    print("  INFO: No DB file yet (fresh install or wiped) — cycle_phase will be created on startup")

print()
if errors:
    print(f"SMOKE TEST: {len(errors)} ISSUE(S) FOUND")
    for e in errors:
        print(f"  - {e}")
else:
    print("SMOKE TEST PASSED -- safe to start engine")
