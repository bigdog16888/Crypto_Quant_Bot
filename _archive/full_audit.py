"""FULL STATE AUDIT - Find every discrepancy"""
import sqlite3
from engine.exchange_interface import ExchangeInterface

print("=" * 80)
print("FULL STATE AUDIT")
print("=" * 80)

# === EXCHANGE STATE ===
ex = ExchangeInterface(market_type='future')

# Positions
positions = ex.fetch_positions()
active_positions = [p for p in positions if abs(float(p.get('contracts', 0) or 0)) > 0]
print(f"\n[EXCHANGE POSITIONS] Count: {len(active_positions)}")
for p in active_positions:
    print(f"  {p.get('symbol')}: {p.get('side')} {p.get('contracts')} @ ${p.get('entryPrice')}")

# Orders  
orders = ex.fetch_open_orders()
print(f"\n[EXCHANGE ORDERS] Count: {len(orders) if orders else 0}")
by_bot = {}
for o in orders or []:
    cid = o.get('clientOrderId', '')
    if cid.startswith('CQB_'):
        parts = cid.split('_')
        bot_id = parts[1] if len(parts) > 1 else 'unknown'
        by_bot[bot_id] = by_bot.get(bot_id, 0) + 1
    else:
        by_bot['UNTAGGED'] = by_bot.get('UNTAGGED', 0) + 1
for bot_id, count in by_bot.items():
    print(f"  Bot {bot_id}: {count} orders")

# === DATABASE STATE ===
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

# Bots in trade
cur.execute('''
    SELECT t.bot_id, b.name, b.pair, b.direction, t.current_step, t.total_invested
    FROM trades t JOIN bots b ON t.bot_id = b.id
    WHERE t.total_invested > 0
''')
in_trade = cur.fetchall()
print(f"\n[DB BOTS IN TRADE] Count: {len(in_trade)}")
for r in in_trade:
    print(f"  Bot {r[0]} ({r[1]}): {r[2]} | {r[3]} | Step {r[4]} | ${r[5]:.2f}")

# ALL bots and their pairs
cur.execute('''
    SELECT b.id, b.name, b.pair, b.direction, b.is_active
    FROM bots b
    ORDER BY b.pair
''')
all_bots = cur.fetchall()
print(f"\n[ALL BOTS] Count: {len(all_bots)}")
by_pair = {}
for b in all_bots:
    pair = b[2]
    if pair not in by_pair:
        by_pair[pair] = []
    by_pair[pair].append({'id': b[0], 'name': b[1], 'dir': b[3], 'active': b[4]})

for pair, bots in by_pair.items():
    print(f"\n  {pair}:")
    for b in bots:
        active_str = "✅" if b['active'] else "❌"
        print(f"    {active_str} Bot {b['id']} ({b['name']}): {b['dir']}")

# === DISCREPANCY CHECK ===
print("\n" + "=" * 80)
print("DISCREPANCIES")
print("=" * 80)

# 1. Positions without owners
db_pairs_in_trade = set(r[2] for r in in_trade)
for p in active_positions:
    sym = p.get('symbol')
    # Check if any bot owns this
    if sym not in db_pairs_in_trade:
        # Check bots on this pair
        bots_on_pair = by_pair.get(sym, [])
        if bots_on_pair:
            print(f"\n❌ ORPHAN POSITION: {sym} ({p.get('side')} {p.get('contracts')})")
            print(f"   Bots on this pair:")
            for b in bots_on_pair:
                print(f"     Bot {b['id']} ({b['name']}): direction={b['dir']}, active={b['active']}")
            print(f"   Position side is {p.get('side').upper()}")
            matching = [b for b in bots_on_pair if b['dir'].lower() == p.get('side').lower() or 
                       (b['dir'] == 'LONG' and p.get('side') == 'long') or
                       (b['dir'] == 'SHORT' and p.get('side') == 'short')]
            if matching:
                print(f"   SHOULD BE ADOPTED BY: Bot {matching[0]['id']}")
            else:
                print(f"   NO MATCHING DIRECTION! Position is {p.get('side')}, bots are {[b['dir'] for b in bots_on_pair]}")
        else:
            print(f"\n❌ ORPHAN POSITION (NO BOT): {sym}")

conn.close()
