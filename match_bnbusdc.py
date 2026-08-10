import json
from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection

ex = ExchangeInterface(market_type='future')

# The new fetch_my_trades handles 7-day window pagination internally.
# Just call it with a wide time range (30 days back from now).
all_trades = ex.fetch_my_trades('BNB/USDC:USDC', since=None, limit=1000, until=None)

print(f'# Fetched {len(all_trades)} trades via internal pagination')
print('=== ALL BNBUSDC EXCHANGE TRADES ===')
for t in all_trades:
    print(json.dumps(t, separators=(',', ':')))

conn = get_connection()
cur = conn.cursor()
cur.execute('SELECT * FROM bot_orders WHERE bot_id IN (10007, 100314) ORDER BY bot_id, id')
bot_orders = cur.fetchall()
columns = [desc[0] for desc in cur.description]

print('=== ALL BOT_ORDERS FOR BOTS 10007 AND 100314 ===')
for row in bot_orders:
    print(dict(zip(columns, row)))

# Build lookup by order_id
bot_orders_by_oid = {}
for row in bot_orders:
    row_dict = dict(zip(columns, row))
    oid = row_dict.get('order_id')
    if oid and oid.strip():
        bot_orders_by_oid.setdefault(oid, []).append(row_dict)

print('=== MATCHING EXCHANGE TRADES TO BOT_ORDERS BY ORDER_ID ===')
matched_count = 0
unmatched = []
for t in all_trades:
    oid = str(t.get('order') or t.get('orderId') or '').strip()
    if not oid:
        unmatched.append({'exchange_trade': t, 'reason': 'missing order_id'})
        continue
    if oid in bot_orders_by_oid:
        matches = bot_orders_by_oid[oid]
        print(f'MATCH: exchange order {oid} -> {len(matches)} bot_order row(s)')
        matched_count += 1
        first = matches[0]
        print(f'  Exchange: {t.get("side")} {t.get("amount")} @ {t.get("price")} (ts={t.get("timestamp")})')
        print(f'  Bot Order: bot_id={first.get("bot_id")}, type={first.get("order_type")}, filled={first.get("filled_amount")}, status={first.get("status")}')
    else:
        unmatched.append({'exchange_trade': t, 'reason': 'no matching bot_order'})
        print(f'UNMATCHED: exchange order {oid} ({t.get("side")} {t.get("amount")} @ {t.get("price")})')

print()
print(f'Summary: {matched_count} matched, {len(unmatched)} unmatched exchange trades')
if unmatched:
    print('Unmatched exchange trades:')
    for u in unmatched:
        t = u['exchange_trade']
        print(f'  {t.get("side")} {t.get("amount")} @ {t.get("price")} | order={t.get("order") or t.get("orderId")} | reason={u["reason"]}')