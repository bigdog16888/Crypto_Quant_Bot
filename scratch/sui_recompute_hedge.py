import engine.database
from engine.ledger import seal_trade_state

conn = engine.database.get_connection()
res = engine.database.recompute_invested_from_orders(100318)
print("recompute_invested_from_orders(100318):", res)

# Run seal_trade_state
print("Running seal_trade_state(100318)...")
seal_trade_state(100318)

# Inspect trades row after seal
trades_row = conn.execute("SELECT * FROM trades WHERE bot_id=100318").fetchone()
print("trades row for 100318 after seal:", trades_row)
