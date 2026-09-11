"""Pre-execute snapshot for ETH orphan close (Option A) — 2026-09-04.

Read-only. Dumps every DB row the close + heal will touch, to repo root
(ETH_ORPHAN_PRE_EXECUTE_SNAPSHOT_20260904.json), per safe-trading-bot-ops rule 6.
No exchange calls, no writes.
"""
import json
import sqlite3
import time

SNAP_PATH = "ETH_ORPHAN_PRE_EXECUTE_SNAPSHOT_20260904.json"

conn = sqlite3.connect("file:crypto_bot.db?mode=ro", uri=True)
cur = conn.cursor()

snap = {"captured_at": time.strftime("%Y-%m-%d %H:%M:%S")}

cur.execute(
    "SELECT b.id, b.name, b.status, b.is_active, t.open_qty, t.total_invested, "
    "t.position_side, t.cycle_id FROM bots b LEFT JOIN trades t ON b.id=t.bot_id "
    "WHERE b.pair='ETH/USDC:USDC' ORDER BY b.id"
)
snap["eth_bots"] = [
    dict(zip(["id", "name", "status", "is_active", "open_qty", "total_invested",
              "position_side", "cycle_id"], r))
    for r in cur.fetchall()
]

cur.execute(
    "SELECT bot_id, client_order_id, order_type, status, filled_amount, price, "
    "order_id, cycle_id, updated_at FROM bot_orders "
    "WHERE bot_id IN (SELECT id FROM bots WHERE pair='ETH/USDC:USDC') "
    "ORDER BY updated_at DESC LIMIT 30"
)
snap["eth_bot_orders_recent_30"] = [
    dict(zip(["bot_id", "client_order_id", "order_type", "status", "filled_amount",
              "price", "order_id", "cycle_id", "updated_at"], r))
    for r in cur.fetchall()
]

cur.execute("SELECT bot_id, pair, side, size, entry_price, last_updated "
            "FROM active_positions WHERE pair LIKE '%ETH%'")
snap["eth_active_positions"] = [
    dict(zip(["bot_id", "pair", "side", "size", "entry_price", "last_updated"], r))
    for r in cur.fetchall()
]

cur.execute("SELECT * FROM manual_whitelists WHERE pair LIKE '%ETH%'")
cols = [d[0] for d in cur.description]
snap["eth_manual_whitelists"] = [dict(zip(cols, r)) for r in cur.fetchall()]

cur.execute("SELECT key, value FROM system_equity WHERE key IN "
            "('ENGINE_STARTED_AT','STARTING_EQUITY')")
snap["system_equity"] = [dict(zip(["key", "value"], r)) for r in cur.fetchall()]

cur.execute("SELECT COUNT(*) FROM bot_orders WHERE client_order_id LIKE 'CQB_ORPH%'")
snap["existing_CQB_ORPH_rows"] = cur.fetchone()[0]

conn.close()

with open(SNAP_PATH, "w", encoding="utf-8") as f:
    json.dump(snap, f, indent=2, default=str)

print(json.dumps(snap, indent=1, default=str))
print(f"\n[snapshot written to {SNAP_PATH}]")
