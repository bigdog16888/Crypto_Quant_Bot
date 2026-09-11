"""Dry-run step 2: project ETH virtual net from ALL real verified fills.

Includes every bot_orders row with filled_amount > 0 and a numeric exchange
order ID, across ALL statuses (filled/closed/canceled/reset_cleared/auto_closed/
cancelled/expired) — because a physical fill on the exchange contributes to the
exchange position regardless of what the ledger later did to the row.

Non-numeric order_ids (LIVE_GUARD/OWAY_REPAIR/ghost bookkeeping CIDs) are
phantom: they have no exchange order, so their "fills" are excluded from the
projection. That exclusion IS the selective restore.

No writes. Pure projection.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

PAIR = 'ETH/USDC:USDC'
ETH_BOTS = [10011, 10021, 100002, 100316, 100321, 100325]
ENTRY_TYPES = {'entry', 'grid', 'adoption', 'adoption_add', 'carry'}
EXIT_TYPES = {'tp', 'close', 'dust_close', 'sl', 'adoption_reduce', 'flatten_close'}

conn = get_connection()
exch = ExchangeInterface()

# Exchange ground truth
positions = exch.fetch_positions()
ex_net = None
for pos in positions:
    if 'ETH' in pos.get('symbol', ''):
        ex_net = float(pos.get('net_qty') or 0)
        break
print(f'Exchange ETH net: {ex_net:+.4f}')
print()

total_proj = 0.0
total_real_entry = 0.0
total_real_exit = 0.0
total_phantom_entry = 0.0
total_phantom_exit = 0.0

for bid in ETH_BOTS:
    row_bot = conn.execute(
        "SELECT direction FROM bots WHERE id=?", (bid,)
    ).fetchone()
    direction = (row_bot[0] or 'LONG').upper() if row_bot else 'LONG'
    sign = +1.0 if direction == 'LONG' else -1.0

    trade = conn.execute(
        "SELECT open_qty FROM trades WHERE bot_id=?", (bid,)
    ).fetchone()
    db_open = float(trade[0] or 0) if trade else 0.0

    orders = conn.execute(
        """
        SELECT id, order_type, client_order_id, order_id, filled_amount, status
        FROM bot_orders
        WHERE bot_id=? AND filled_amount > 0
        ORDER BY id
        """,
        (bid,),
    ).fetchall()
    if not orders:
        print(f'Bot {bid} ({direction}): no fills at all. DB open_qty={db_open:.4f}')
        continue

    real_entry = 0.0
    real_exit = 0.0
    phantom_entry = 0.0
    phantom_exit = 0.0
    unver = 0.0
    details = []

    for db_row, otype, cid, oid, filled, status in orders:
        filled = float(filled or 0)
        ex_oid = str(oid) if oid else ''
        is_entry = str(otype).lower() in ENTRY_TYPES
        is_exit = str(otype).lower() in EXIT_TYPES
        if not is_entry and not is_exit:
            continue  # drift_note/audit/ghost rows with 0-ish fills

        if not ex_oid.isdigit():
            # bookkeeping row — phantom by definition (no exchange order can exist)
            if is_entry:
                phantom_entry += filled
            else:
                phantom_exit += filled
            details.append(f'    PHANTOM {otype:16s} {cid[:36]:36s} fill={filled:.4f} status={status}')
            continue

        try:
            order = exch.fetch_order(ex_oid, PAIR, params={'timeout': 15000})
        except Exception:
            order = None
        if order:
            ex_fill = float(order.get('filled', 0) or 0)
            # trust the exchange number over the DB number
            if is_entry:
                real_entry += ex_fill
            else:
                real_exit += ex_fill
            mark = 'REAL' if abs(ex_fill - filled) < 1e-8 else f'REAL(dbΔ={filled - ex_fill:+.4f})'
            details.append(f'    {mark:16s} {otype:16s} {cid[:36]:36s} ex_fill={ex_fill:.4f} status={status}')
        else:
            # numeric ID but order not found on exchange — treat fill as unverifiable
            unver += filled
            details.append(f'    NOT_FOUND      {otype:16s} {cid[:36]:36s} db_fill={filled:.4f} status={status}')

    bot_net_qty = real_entry - real_exit
    bot_signed = sign * bot_net_qty
    total_proj += bot_signed
    total_real_entry += real_entry
    total_real_exit += real_exit
    total_phantom_entry += phantom_entry
    total_phantom_exit += phantom_exit

    print(f'Bot {bid} ({direction}):')
    for d in details:
        print(d)
    print(f'  -> REAL net qty: {bot_net_qty:.4f} {direction} (entry={real_entry:.4f}, exit={real_exit:.4f}, '
          f'phantom_entry={phantom_entry:.4f}, phantom_exit={phantom_exit:.4f}, unverifiable={unver:.4f})')
    print(f'  -> DB open_qty currently: {db_open:.4f}  (delta to restore: {bot_net_qty - db_open:+.4f})')
    print()

print('=' * 70)
print(f'PROJECTED virtual net (real fills only): {total_proj:+.4f}')
print(f'Exchange net:                           {ex_net:+.4f}')
print(f'Gap:                                    {ex_net - total_proj:+.4f}')
print()
print(f'Totals: real_entry={total_real_entry:.4f} real_exit={total_real_exit:.4f} '
      f'phantom_entry={total_phantom_entry:.4f} phantom_exit={total_phantom_exit:.4f}')
