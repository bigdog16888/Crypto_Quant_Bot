import engine.database
from engine.ledger import seal_trade_state

conn = engine.database.get_connection()

sui_bots = [10018, 100000, 100318, 100323]

print("--- Sealing all SUI bots ---")
for bid in sui_bots:
    seal_trade_state(bid)

print("\n--- Current bot status and trades row ---")
q = """
SELECT b.id, b.name, b.direction, b.is_active, b.status, t.open_qty, t.total_invested, t.avg_entry_price, t.cycle_id
FROM bots b
LEFT JOIN trades t ON t.bot_id = b.id
WHERE b.pair LIKE '%SUI%'
"""
rows = conn.execute(q).fetchall()
for r in rows:
    print(r)
