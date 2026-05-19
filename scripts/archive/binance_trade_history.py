"""
Direct Binance trade history query for SOLUSDC.
Uses the same _raw_request infrastructure as the live engine.
NO filtering. Every row returned is printed raw.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.exchange_interface import ExchangeInterface
import sqlite3

ex = ExchangeInterface(market_type='future')

# Timestamps from the earlier DB query (milliseconds for Binance API):
# 100001 first cycle-26 fill: ts=1778735516 -> 1778735516000 ms
# 100001 last  cycle-26 fill: ts=1778736843 -> 1778736843000 ms
# Wide end window: ts=1778750000 -> 1778750000000 ms
#
# We go even wider on the start to catch anything Binance might have
# processed slightly before our DB recorded it:
START_MS = 1778730000000   # 5 min before first fill
END_MS   = 1778750000000   # ~3.5h after last fill

print(f"Querying Binance /fapi/v1/userTrades for SOLUSDC")
print(f"  startTime: {START_MS} ({START_MS//1000})")
print(f"  endTime  : {END_MS}   ({END_MS//1000})")
print(f"  limit    : 100")
print("=" * 100)

params = {
    'symbol':    'SOLUSDC',
    'startTime': START_MS,
    'endTime':   END_MS,
    'limit':     100,
}

raw = ex._raw_request('/fapi/v1/userTrades', params=params)

if raw is None:
    print("ERROR: API returned None — check connectivity / API key permissions")
    sys.exit(1)

if not raw:
    print("Binance returned ZERO trades in this window.")
    print("=> The 0.22 SOL has no discrete trade receipt on Binance.")
    sys.exit(0)

print(f"Binance returned {len(raw)} trades:\n")
print(f"{'time_ms':>15} {'orderId':>15} {'side':>6} {'qty':>8} {'price':>10} {'realizedPnl':>14} {'commission':>12} {'buyer':>6}")
print("-" * 100)

total_buy  = 0.0
total_sell = 0.0
for t in raw:
    side = t.get('side', '?')
    qty  = float(t.get('qty', 0))
    pnl  = float(t.get('realizedPnl', 0))
    comm = float(t.get('commission', 0))
    is_buyer = t.get('buyer', False)
    print(f"{t['time']:>15} {t['orderId']:>15} {side:>6} {qty:>8.4f} {float(t['price']):>10.4f} {pnl:>14.6f} {comm:>12.6f} {str(is_buyer):>6}")
    if side.upper() == 'BUY':
        total_buy += qty
    else:
        total_sell += qty

print("-" * 100)
print(f"  Total BUY  fills in window: {total_buy:.4f} SOL")
print(f"  Total SELL fills in window: {total_sell:.4f} SOL")
print(f"  Net in window (BUY-SELL)  : {total_buy - total_sell:.4f} SOL")

# Cross-reference: which orderIds are NOT in bot_orders?
print("\n" + "=" * 100)
print("Cross-reference: Binance orderIds vs bot_orders DB")
print("=" * 100)
conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()
for t in raw:
    oid = str(t['orderId'])
    cursor.execute("SELECT bot_id, order_type, status, filled_amount, cycle_id FROM bot_orders WHERE order_id = ?", (oid,))
    row = cursor.fetchone()
    if row:
        print(f"  [IN DB]  orderId={oid} | bot={row[0]} type={row[1]} status={row[2]} filled={row[3]} cycle={row[4]}")
    else:
        print(f"  [MISSING] orderId={oid} | side={t['side']} qty={t['qty']} price={t['price']} — NOT IN bot_orders")
conn.close()
