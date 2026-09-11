"""Dry-run: verify each ETH bot's filled orders against the exchange, one by one.

This is the same process used for LINK's 7 child CIDs: fetch real per-order
exchange status for every 'filled' row, so we can see exactly which fills
are real vs phantom BEFORE writing anything to the DB.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

PAIR = 'ETH/USDC:USDC'
ETH_BOTS = [10011, 10021, 100002, 100316, 100321, 100325]

conn = get_connection()
exch = ExchangeInterface()

# Current exchange position (ground truth)
print('=== ETH Exchange Position (ground truth) ===')
positions = exch.fetch_positions()
ex_net = None
for pos in positions:
    if 'ETH' in pos.get('symbol', ''):
        ex_net = float(pos.get('net_qty') or 0)
        print(f'  net_qty={ex_net} side={pos.get("side")} contracts={pos.get("contracts")}')
        break
print()

results = {'verified': 0, 'mismatched': 0, 'not_found': 0}
per_bot_verified = {}

print('=== Per-Order Exchange Verification (ETH bots, cycle-current fills) ===')
print('NOTE: only orders with numeric exchange order IDs can be verified via')
print('      /fapi/v1/order. CIDs used as order_id (LIVE_GUARD/OWAY_REPAIR rows)')
print('      are internal bookkeeping, not real exchange orders.')
print()

for bid in ETH_BOTS:
    orders = conn.execute(
        """
        SELECT id, order_type, client_order_id, order_id, amount, filled_amount, status, cycle_id, step
        FROM bot_orders
        WHERE bot_id=? AND status IN ('filled', 'closed') AND filled_amount > 0
        ORDER BY cycle_id, step, id
        """,
        (bid,),
    ).fetchall()
    if not orders:
        per_bot_verified[bid] = 0.0
        continue
    print(f'Bot {bid}:')
    bot_verif_qty = 0.0
    for o in orders:
        db_row, otype, cid, oid, amount, filled, status, cycle, step = o
        ex_oid = str(oid) if oid else ''
        is_numeric = ex_oid.isdigit()
        if not is_numeric:
            # Internal bookkeeping row (LIVE_GUARD / OWAY_REPAIR audit) — cannot
            # exist on the exchange. Flag as PHANTOM for restore purposes.
            print(f'  {db_row} | {otype:15s} | {cid:35s} | ex_id={ex_oid[:22]:22s} | db_fill={filled:.4f} | ⚠️ NON-EXCHANGE CID (bookkeeping row)')
            results['not_found'] += 1
            continue
        try:
            order = exch.fetch_order(ex_oid, PAIR, params={'timeout': 15000})
            if order:
                ex_fill = float(order.get('filled', 0) or 0)
                if abs(ex_fill - float(filled)) < 1e-8:
                    results['verified'] += 1
                    bot_verif_qty += float(filled)
                    print(f'  {db_row} | {otype:15s} | {cid:35s} | ex_id={ex_oid:22s} | db_fill={filled:.4f} | ex_fill={ex_fill:.4f} | ✅ REAL')
                else:
                    results['mismatched'] += 1
                    print(f'  {db_row} | {otype:15s} | {cid:35s} | ex_id={ex_oid:22s} | db_fill={filled:.4f} | ex_fill={ex_fill:.4f} | ❌ MISMATCH')
            else:
                results['not_found'] += 1
                print(f'  {db_row} | {otype:15s} | {cid:35s} | ex_id={ex_oid:22s} | db_fill={filled:.4f} | ❌ NOT_FOUND')
        except Exception as e:
            results['not_found'] += 1
            print(f'  {db_row} | {otype:15s} | {cid:35s} | ex_id={ex_oid:22s} | db_fill={filled:.4f} | ❌ ERROR: {e}')
    per_bot_verified[bid] = bot_verif_qty
    print()

print('=== Summary ===')
print(f'Verified (real fills):  {results["verified"]}')
print(f'Mismatched:            {results["mismatched"]}')
print(f'Not found / phantom:   {results["not_found"]}')
print()
print('Per-bot verified entry-side qty (grid+entry only, numeric ex IDs):')
for bid, q in per_bot_verified.items():
    print(f'  Bot {bid}: {q:.4f}')
