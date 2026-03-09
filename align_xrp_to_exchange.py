"""
Set total_invested to exactly match exchange notional.
Exchange: qty=778.2, entryPrice=1.37464312516 -> notional=$1069.75
We trust the exchange as ground truth.
"""
import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

# Exact exchange values
EXCHANGE_QTY = 778.2
EXCHANGE_ENTRY_PRICE = 1.37464312516
EXCHANGE_NOTIONAL = EXCHANGE_QTY * EXCHANGE_ENTRY_PRICE  # ~1069.75

conn = get_connection()
c = conn.cursor()

c.execute("SELECT total_invested, avg_entry_price FROM trades WHERE bot_id=10017")
before = c.fetchone()
print(f"Before: total_invested=${before[0]:.2f}, avg_entry_price={before[1]:.6f}")

c.execute("""
    UPDATE trades SET 
        total_invested=?,
        avg_entry_price=?
    WHERE bot_id=10017
""", (EXCHANGE_NOTIONAL, EXCHANGE_ENTRY_PRICE))
conn.commit()

print(f"After:  total_invested=${EXCHANGE_NOTIONAL:.2f}, avg_entry_price={EXCHANGE_ENTRY_PRICE:.6f}")
print("Done. System now aligned to exchange.")
conn.close()
