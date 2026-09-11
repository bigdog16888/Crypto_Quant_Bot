"""INV-31 B1 completion: wire _reset_to_hedge_standby Phase 2 through WriteQueue.
Adds _reset_to_hedge_standby_status_force_internal + replaces the Phase 2 block.
Anchor-verified; aborts on mismatch.
"""
import sys, ast

PATH = "engine/bot_executor.py"
src = open(PATH, encoding="utf-8").read()
orig = src
applied = []

def rep(old, new, count=1, label=""):
    global src
    n = src.count(old)
    if n != count:
        print(f"ABORT [{label}]: expected {count}, found {n}")
        print("--- old ---")
        print(old[:500])
        sys.exit(1)
    src = src.replace(old, new)
    applied.append(label)

# 1. Add status-force internal fn right after _reset_to_hedge_standby_internal
rep(
    '    conn.execute(\n'
    '        "UPDATE bots SET status = \'hedge_standby\', cascade_started_at = 0 WHERE id = ?",\n'
    '        (child_bot_id,)\n'
    '    )\n'
    '    conn.commit()\n'
    '\n\n',
    '    conn.execute(\n'
    '        "UPDATE bots SET status = \'hedge_standby\', cascade_started_at = 0 WHERE id = ?",\n'
    '        (child_bot_id,)\n'
    '    )\n'
    '    conn.commit()\n'
    '\n\n'
    'def _reset_to_hedge_standby_status_force_internal(child_bot_id: int):\n'
    '    """\n'
    '    INV-31 WriteQueue internal (B1 fix) — post-seal status force of\n'
    '    _reset_to_hedge_standby: re-assert status hedge_standby after\n'
    '    seal_trade_state (which may overwrite status). Runs inside WriteQueue\n'
    '    serialization — must NOT be called directly.\n'
    '    """\n'
    '    from engine.database import get_connection\n'
    '    conn = get_connection()\n'
    '    conn.execute(\n'
    '        "UPDATE bots SET status = \'hedge_standby\', cascade_started_at = 0 WHERE id = ?",\n'
    '        (child_bot_id,)\n'
    '    )\n'
    '    conn.commit()\n'
    '\n\n',
    1, "add status_force internal")

# 2. Wire Phase 2 of _reset_to_hedge_standby
rep(
    '    # ═════════════════════════════════════════════════════════════════════════\n'
    '    # PHASE 2 — DB UPDATE (only reached after exchange is settled or qty was 0)\n'
    '    # ═════════════════════════════════════════════════════════════════════════\n'
    '    conn.execute(\n'
    '        "UPDATE trades SET cycle_id = ? WHERE bot_id = ?",\n'
    '        (parent_cycle_id, child_bot_id)\n'
    '    )\n'
    '    conn.execute(\n'
    '        "UPDATE bots SET status = \'hedge_standby\', cascade_started_at = 0 WHERE id = ?",\n'
    '        (child_bot_id,)\n'
    '    )\n'
    '    conn.commit()\n'
    '\n'
    '    # After commit — seal reads the new cycle_id, finds no fills, returns zeros\n'
    '    from engine.ledger import seal_trade_state\n'
    '    seal_trade_state(child_bot_id, force_recompute=True)\n'
    '\n'
    '    # Force status to hedge_standby if seal overwrote it\n'
    '    conn.execute(\n'
    '        "UPDATE bots SET status = \'hedge_standby\', cascade_started_at = 0 WHERE id = ?",\n'
    '        (child_bot_id,)\n'
    '    )\n'
    '    conn.commit()\n',
    '    # ═════════════════════════════════════════════════════════════════════════\n'
    '    # PHASE 2 — DB UPDATE (only reached after exchange is settled or qty was 0)\n'
    '    # INV-31: all trades/bots writes route through the WriteQueue singleton.\n'
    '    # ═════════════════════════════════════════════════════════════════════════\n'
    '    from engine.write_queue import WriteQueue\n'
    '    WriteQueue().put_and_wait(\n'
    '        _reset_to_hedge_standby_internal,\n'
    '        child_bot_id, parent_cycle_id\n'
    '    )\n'
    '\n'
    '    # After commit — seal reads the new cycle_id, finds no fills, returns zeros\n'
    '    from engine.ledger import seal_trade_state\n'
    '    seal_trade_state(child_bot_id, force_recompute=True)\n'
    '\n'
    '    # Force status to hedge_standby if seal overwrote it\n'
    '    WriteQueue().put_and_wait(\n'
    '        _reset_to_hedge_standby_status_force_internal,\n'
    '        child_bot_id\n'
    '    )\n',
    1, "wire Phase 2 via WriteQueue")

try:
    ast.parse(src)
except SyntaxError as e:
    print(f"ABORT: syntax error: {e}")
    sys.exit(1)

open(PATH, "w", encoding="utf-8", newline="").write(src)
print(f"OK — {len(applied)} groups applied.")
for a in applied:
    print(f"  ✓ {a}")
print(f"lines: {len(orig.splitlines())} -> {len(src.splitlines())}")
