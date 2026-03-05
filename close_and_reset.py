"""Close remaining BNB/USDC position and reset the database cleanly."""
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))

from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection

ex = ExchangeInterface()

# Close remaining BNB position
print("Closing BNB/USDC position at market...")
try:
    positions = ex.fetch_positions()
    for p in positions:
        size = float(p.get('contracts', 0) or 0)
        if abs(size) < 0.0001:
            continue
        symbol = p['symbol']
        side = p.get('side', '').upper()
        close_side = 'sell' if side == 'LONG' else 'buy'
        close_amount = abs(size)
        print(f"  Closing {symbol}: {side} {close_amount} via {close_side.upper()} MARKET")
        ex.cancel_all_orders(symbol)
        time.sleep(0.3)
        ex.create_order(symbol, 'MARKET', close_side, close_amount)
        print(f"  OK: {symbol} closed")
        time.sleep(0.5)
except Exception as e:
    print(f"  Error: {e}")

# Database reset
print("\nResetting database...")
conn = get_connection()
conn.execute("DELETE FROM trades")
conn.execute("UPDATE bots SET status='Scanning' WHERE status NOT IN ('Stopped','Paused')")
conn.execute("UPDATE bot_orders SET status='reset_cleared' WHERE status IN ('open','pending','filled','closed','missing','cancelled')")
conn.execute("DELETE FROM active_positions")
conn.commit()

bots_scanning = conn.execute("SELECT COUNT(*) FROM bots WHERE status='Scanning'").fetchone()[0]
trades_remaining = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

print(f"  Bots set to Scanning: {bots_scanning}")
print(f"  Trades remaining:     {trades_remaining}")
print("\nDone. Restart the engine.")
