"""ETH orphan heal — Gate 2 FINAL. Removes ONLY the stale active_positions row.
No other table is touched. No bot status writes. No REQUIRE_MANUAL_PROOF writes.

Before: pre-heal snapshot ETH_ORPHAN_PRE_HEAL_SNAPSHOT_20260904.json (repo root).
Mode: DRY-RUN by default; --execute performs the single DELETE + verification.
"""
import sys, sqlite3, time, json

DB = "crypto_bot.db"
DRY = "--execute" not in sys.argv

TARGET = (10021, "ETHUSDC")  # (bot_id, pair) — MUST match SQL bind order below

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
c = conn.cursor()

# --- pre-check: exactly the expected rows exist ---
c.execute("SELECT * FROM active_positions WHERE pair LIKE '%ETH%'")
eth_rows = [dict(r) for r in c.fetchall()]
print(f"ETH rows in active_positions: {len(eth_rows)}")
for r in eth_rows:
    print("  ", json.dumps(r))
assert len(eth_rows) == 1, f"Expected exactly 1 ETH row, found {len(eth_rows)} — ABORT"
assert eth_rows[0]["bot_id"] == 10021 and eth_rows[0]["pair"] == "ETHUSDC", \
    f"Row is not (10021, ETHUSDC) — ABORT: {eth_rows[0]}"

sql = "DELETE FROM active_positions WHERE bot_id = ? AND pair = ?"
print()
print("=" * 60)
print(f"{'DRY-RUN' if DRY else 'EXECUTE'} — target row:")
print(f"  bot_id=10021 pair=ETHUSDC side=LONG size=0.901 entry=2405.95")
print(f"SQL: {sql}" % TARGET if False else f"SQL: {sql}  (params: {TARGET})")
print("=" * 60)

if DRY:
    print("NO DELETE PERFORMED. Re-run with --execute after operator GO.")
    conn.close()
    sys.exit(0)

# --- EXECUTE ---
c.execute(sql, TARGET)
deleted = c.rowcount
conn.commit()
print(f"DELETE rowcount: {deleted}")
assert deleted == 1, f"Expected 1 row deleted, got {deleted} — ROLLBACK territory"

# --- post-verify (same connection, fresh reads) ---
c.execute("SELECT COUNT(*) AS n FROM active_positions WHERE pair LIKE '%ETH%'")
n_eth = c.fetchone()["n"]
c.execute("SELECT COUNT(*) AS n FROM active_positions")
n_total = c.fetchone()["n"]
c.execute("SELECT id, name, status FROM bots WHERE id IN (10011,10021,100002,100316,100321,100325)")
bots = [(r["id"], r["name"], r["status"]) for r in c.fetchall()]
c.execute("SELECT COUNT(*) AS n FROM manual_whitelists")
try:
    wl = c.fetchone()["n"]
except sqlite3.OperationalError:
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%whitelist%'")
    wl = f"table check: {[dict(r) for r in c.fetchall()]}"
conn.close()

print()
print("=== POST-HEAL VERIFICATION ===")
print(f"active_positions ETH rows: {n_eth} (expected 0)")
print(f"active_positions total: {n_total} (was 2)")
print("6 ETH bots (status must be UNCHANGED, still REQUIRE_MANUAL_PROOF):")
for b in sorted(bots):
    print(f"  bot {b[0]:7d} {b[1]:15s} {b[2]}")
print(f"manual_whitelists: {wl}")

assert n_eth == 0, "ETH row still present after DELETE — UNEXPECTED"
print()
print("✅ HEAL COMPLETE: stale ETH row removed; nothing else touched.")
