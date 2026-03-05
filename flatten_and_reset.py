"""
Flatten Exchange + Reset DB
---------------------------
1. Cancel ALL open orders on the exchange (every pair)
2. Close ALL open positions at market
3. Reset every bot in the DB to Scanning/Idle state (clear trades table)
RUN WITH: python flatten_and_reset.py
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))

from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection
import config.settings as config

print("=" * 60)
print("   FULL FLATTEN + DB RESET")
print("=" * 60)

ex = ExchangeInterface()

# ── Step 1: Cancel all open orders across all active pairs ──
print("\n[1/3] Fetching open positions to determine active pairs...")
try:
    positions = ex.fetch_positions()
    active_pairs = list({p['symbol'] for p in positions if abs(p.get('contracts', 0) or 0) > 0})
    print(f"      Active pairs: {active_pairs}")
except Exception as e:
    print(f"      ⚠️  Could not fetch positions: {e}")
    active_pairs = []

# Also cancel common pairs we know about
common_pairs = ['BTC/USDC:USDC', 'ETH/USDC:USDC', 'SOL/USDC:USDC',
                'BNB/USDC:USDC', 'SUI/USDC:USDC', 'XRP/USDC:USDC']
all_pairs = list(set(active_pairs + common_pairs))

print(f"\n[1/3] Cancelling all open orders across {len(all_pairs)} pairs...")
for pair in all_pairs:
    try:
        result = ex.cancel_all_orders(pair)
        print(f"      ✅ Cancelled orders for {pair}")
    except Exception as e:
        print(f"      ⚠️  {pair}: {e}")
    time.sleep(0.3)

# ── Step 2: Close all open positions at market ──
print(f"\n[2/3] Closing all open positions at market...")
try:
    positions = ex.fetch_positions()
    closed_count = 0
    for p in positions:
        size = float(p.get('contracts', 0) or 0)
        if abs(size) < 0.0001:
            continue
        symbol = p['symbol']
        side = p.get('side', '').upper()
        close_side = 'sell' if side == 'LONG' else 'buy'
        close_amount = abs(size)
        print(f"      Closing {symbol}: {side} {close_amount} contracts via {close_side.upper()} MARKET...")
        try:
            ex.create_order(symbol, 'MARKET', close_side, close_amount)
            print(f"      ✅ Closed {symbol}")
            closed_count += 1
        except Exception as e:
            print(f"      ❌ Failed to close {symbol}: {e}")
        time.sleep(0.5)
    if closed_count == 0:
        print("      ℹ️  No open positions found - already flat.")
    else:
        print(f"      ✅ Closed {closed_count} positions.")
except Exception as e:
    print(f"      ❌ Position close error: {e}")

# ── Step 3: Reset DB – clear trades, set bots to Scanning ──
print(f"\n[3/3] Resetting database – clearing trades and setting all bots to Scanning...")
try:
    conn = get_connection()

    # Clear all trade records
    conn.execute("DELETE FROM trades")

    # Reset bot status to Scanning, zero out invested tracking
    conn.execute("""
        UPDATE bots 
        SET status = 'Scanning', 
            total_invested = 0,
            current_step = 0,
            entry_order_id = NULL,
            tp_order_id = NULL
        WHERE status NOT IN ('Stopped', 'Paused')
    """)

    # Archive any lingering open/pending bot_orders as reset_cleared
    conn.execute("""
        UPDATE bot_orders
        SET status = 'reset_cleared'
        WHERE status IN ('open', 'pending', 'filled', 'closed', 'missing', 'cancelled')
    """)

    conn.commit()
    print("      ✅ Database reset complete. All bots set to Scanning.")
except Exception as e:
    print(f"      ❌ DB reset error: {e}")

print("\n" + "=" * 60)
print("   ✅ FLATTEN COMPLETE — system is now clean.")
print("   Restart the engine to begin fresh forward testing.")
print("=" * 60)
