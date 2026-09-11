"""PRE-HEAL SNAPSHOT for ETH orphan row removal — read-only, saves to repo root.
Rows captured: the exact active_positions ETH row, the 6 ETH bots rows,
last 15 ETH bot_orders, current system_equity keys."""
import sys, json, sqlite3, time
sys.path.insert(0, ".")

DB = "crypto_bot.db"
snap = {
    "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "purpose": "pre-heal snapshot before DELETE of stale active_positions ETH row (bot 10021)",
}

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute("SELECT * FROM active_positions WHERE pair LIKE '%ETH%'")
snap["active_positions_eth"] = [dict(r) for r in c.fetchall()]

c.execute("SELECT * FROM bots WHERE id IN (10011,10021,100002,100316,100321,100325)")
snap["eth_bots"] = [dict(r) for r in c.fetchall()]

c.execute("""SELECT bot_id, client_order_id, order_type, status, filled_amount, price,
             order_id, cycle_id, updated_at FROM bot_orders
             WHERE client_order_id LIKE 'CQB_100%' AND (client_order_id LIKE '%10011%'
                OR client_order_id LIKE '%10021%' OR client_order_id LIKE '%100002%'
                OR client_order_id LIKE '%100316%' OR client_order_id LIKE '%100321%'
                OR client_order_id LIKE '%100325%')
             ORDER BY updated_at DESC LIMIT 15""")
snap["eth_bot_orders_recent_15"] = [dict(r) for r in c.fetchall()]

c.execute("SELECT key, value FROM system_equity")
snap["system_equity"] = [dict(r) for r in c.fetchall()]

c.execute("SELECT COUNT(*) AS n FROM active_positions")
snap["active_positions_total"] = c.fetchone()["n"]

conn.close()

path = f"ETH_ORPHAN_PRE_HEAL_SNAPSHOT_{time.strftime('%Y%m%d')}.json"
with open(path, "w") as f:
    json.dump(snap, f, indent=2)
print(f"saved {path}")
print()
print("=== THE ROW TO DELETE ===")
for r in snap["active_positions_eth"]:
    print(json.dumps(r, indent=1))
print()
print("=== 6 ETH BOTS (status check) ===")
for r in snap["eth_bots"]:
    print(f"bot {r['id']:7d} {r['name']:15s} status={r['status']:22s} active={r['is_active']}")
print()
print("active_positions total rows:", snap["active_positions_total"])
