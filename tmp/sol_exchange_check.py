"""
Check the SOL exchange physical position to establish ground truth,
then re-anchor the SOL bot DB state to that physical position.
"""
import sys
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection

ex = ExchangeInterface('future')
positions = ex.fetch_positions()

print("=== Exchange Physical SOL Positions ===")
sol_pos = [p for p in positions if 'SOL' in str(p.get('symbol', ''))]
for p in sol_pos:
    sym = p.get('symbol')
    contracts = float(p.get('contracts', 0) or 0)
    notional = float(p.get('notional', 0) or p.get('initialMargin', 0) or 0)
    entry = float(p.get('entryPrice', 0) or 0)
    side = p.get('side', '')
    print(f"  {sym}: side={side} contracts={contracts} notional=${abs(notional):.2f} entry=${entry:.4f}")

print()
# Check bot 10008 SOL LONG state
conn = get_connection()
trade = conn.execute("SELECT total_invested, avg_entry_price, current_step FROM trades WHERE bot_id=10008").fetchone()
print(f"DB state: invested=${trade[0]:.2f}, avg_entry=${trade[1]:.4f}, step={trade[2]}")
conn.close()
