"""
CQB Diagnostic Script
Run from your Crypto_Quant_Bot root folder:
    python diagnose.py

This gives a complete picture of ledger vs exchange state
without touching anything.
"""

import sqlite3
import os
import json

# Find the DB - try common locations
DB_CANDIDATES = [
    "crypto_bot.db",
    "engine/crypto_bot.db",
    "../crypto_bot.db",
]

db_path = None
for c in DB_CANDIDATES:
    if os.path.exists(c):
        db_path = c
        break

if not db_path:
    print("ERROR: Could not find crypto_bot.db")
    print("Run this script from your Crypto_Quant_Bot root folder.")
    exit(1)

print(f"Using DB: {os.path.abspath(db_path)}\n")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
c = conn.cursor()

SEP = "=" * 90

# ── 1. BOT LEDGER STATE ──────────────────────────────────────────────────────
print(SEP)
print("1. BOT LEDGER STATE (trades table)")
print(SEP)
c.execute("""
    SELECT 
        b.id, b.name, b.direction, b.status,
        COALESCE(t.total_invested, 0)   as invested,
        COALESCE(t.avg_entry_price, 0)  as avg_entry,
        COALESCE(t.open_qty, 0)         as open_qty,
        COALESCE(t.current_step, 0)     as step,
        COALESCE(t.cycle_id, 0)         as cycle_id,
        COALESCE(t.cycle_phase, 'N/A')  as phase,
        COALESCE(t.entry_confirmed, 0)  as confirmed,
        COALESCE(t.wipe_wall_ts, 0)     as wipe_wall
    FROM bots b
    LEFT JOIN trades t ON b.id = t.bot_id
    WHERE b.is_active = 1
    ORDER BY b.pair, b.direction
""")
rows = c.fetchall()
print(f"{'ID':>7} {'Name':<20} {'Dir':<6} {'Status':<30} {'Invested':>10} {'AvgEntry':>10} {'OpenQty':>9} {'Step':>5} {'Cyc':>4} {'Phase':<15} {'Conf':>5}")
print("-" * 130)
for r in rows:
    print(f"{r['id']:>7} {r['name']:<20} {r['direction']:<6} {r['status']:<30} "
          f"{r['invested']:>10.2f} {r['avg_entry']:>10.4f} {r['open_qty']:>9.4f} "
          f"{r['step']:>5} {r['cycle_id']:>4} {r['phase']:<15} {r['confirmed']:>5}")

# ── 2. ACTIVE POSITIONS SNAPSHOT ─────────────────────────────────────────────
print(f"\n{SEP}")
print("2. ACTIVE POSITIONS SNAPSHOT (what system thinks is on exchange)")
print(SEP)
c.execute("""
    SELECT ap.bot_id, b.name, ap.pair, ap.side, ap.size, ap.entry_price, ap.last_checked
    FROM active_positions ap
    LEFT JOIN bots b ON ap.bot_id = b.id
    ORDER BY ap.pair, ap.side
""")
rows = c.fetchall()
print(f"{'BotID':>7} {'Name':<20} {'Pair':<12} {'Side':<6} {'Size':>10} {'EntryPx':>10}")
print("-" * 75)
for r in rows:
    print(f"{r['bot_id']:>7} {str(r['name'] or 'ORPHAN'):<20} {r['pair']:<12} "
          f"{r['side']:<6} {r['size']:>10.4f} {r['entry_price']:>10.4f}")

# ── 3. VIRTUAL NET PER PAIR (what get_pair_virtual_net returns) ──────────────
print(f"\n{SEP}")
print("3. VIRTUAL NET PER PAIR (from bot_orders proof ledger)")
print(SEP)

c.execute("SELECT DISTINCT pair FROM bots WHERE is_active=1")
pairs = [r[0] for r in c.fetchall()]

for pair in sorted(set(p.split(':')[0].replace('/', '').upper() for p in pairs)):
    # Replicate the core of get_pair_virtual_net logic
    c.execute("""
        SELECT b.id, b.name, b.direction,
               COALESCE(t.cycle_id, -1) as cycle_id,
               COALESCE(t.wipe_wall_ts, 0) as wall,
               COALESCE(t.position_side, b.direction) as pos_side
        FROM bots b
        LEFT JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active = 1
          AND (REPLACE(REPLACE(b.pair, '/', ''), ':USDC', '') = ?
               OR REPLACE(REPLACE(b.pair, '/', ''), ':USDT', '') = ?)
    """, (pair, pair))
    bots = c.fetchall()
    if not bots:
        continue

    total = 0.0
    bot_lines = []
    for bot in bots:
        bid, bname, bdir, cyc, wall, pside = bot
        if cyc == -1:
            bot_lines.append(f"  Bot {bid} ({bname}): cycle_id=NULL → 0.0")
            continue

        c.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN cycle_id=? AND status NOT IN ('auto_closed','reset_cleared')
                    AND (? = 0 OR created_at >= ?)
                    AND order_type IN ('entry','grid','adoption_add','adoption','carry')
                    THEN filled_amount ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN cycle_id=? AND status NOT IN ('auto_closed','reset_cleared')
                    AND (? = 0 OR created_at >= ?)
                    AND order_type IN ('adoption_reduce','tp','close','dust_close','sl','virtual_netting')
                    THEN filled_amount ELSE 0 END), 0),
                ROUND(COALESCE(SUM(CASE
                    WHEN status NOT IN ('auto_closed','reset_cleared','rejected','failed')
                         AND order_type LIKE 'hedge%' AND order_type NOT LIKE '%tp%'
                    THEN filled_amount
                    WHEN status NOT IN ('auto_closed','reset_cleared','rejected','failed')
                         AND (order_type LIKE 'hedge%tp%' OR order_type LIKE 'hedgetp%')
                    THEN -filled_amount
                    ELSE 0 END), 0), 8)
            FROM bot_orders
            WHERE bot_id=?
              AND (order_type LIKE 'hedge%'
                   OR position_side=? OR position_side IS NULL
                   OR position_side='BOTH' OR position_side='')
              AND (status IN ('filled','closed','auto_closed','hedge_exited')
                   OR (status IN ('canceled','cancelled') AND filled_amount > 0))
              AND filled_amount > 0
        """, (cyc, wall, wall, cyc, wall, wall, bid, pside))
        res = c.fetchone()
        bought, sold, hedge = float(res[0]), float(res[1]), float(res[2])
        net = round(bought - sold, 8)
        signed = net - hedge if bdir.upper() == 'LONG' else -(net - hedge)
        total = round(total + signed, 8)
        bot_lines.append(f"  Bot {bid} ({bname} {bdir}): bought={bought:.4f} sold={sold:.4f} hedge={hedge:.4f} → net={net:.4f} signed={signed:.4f}")

    print(f"\n{pair}: VIRTUAL NET = {total:.6f}")
    for bl in bot_lines:
        print(bl)

# ── 4. OPEN BOT ORDERS SUMMARY ───────────────────────────────────────────────
print(f"\n{SEP}")
print("4. OPEN BOT ORDERS (status=open or new)")
print(SEP)
c.execute("""
    SELECT bo.bot_id, b.name, bo.order_type, bo.step, bo.price, bo.amount,
           bo.filled_amount, bo.status, bo.client_order_id, bo.cycle_id
    FROM bot_orders bo
    JOIN bots b ON bo.bot_id = b.id
    WHERE bo.status IN ('open','new','placing')
    ORDER BY b.name, bo.order_type
""")
rows = c.fetchall()
print(f"{'BotID':>7} {'Name':<20} {'Type':<8} {'Step':>5} {'Price':>10} {'Qty':>8} {'Filled':>8} {'Status':<10} {'CID':<40}")
print("-" * 125)
for r in rows:
    cid = str(r['client_order_id'] or '')[-35:]
    print(f"{r['bot_id']:>7} {r['name']:<20} {r['order_type']:<8} {r['step']:>5} "
          f"{r['price']:>10.4f} {r['amount']:>8.4f} {r['filled_amount']:>8.4f} "
          f"{r['status']:<10} {cid:<40}")

# ── 5. SUSPECT BOTS - REQUIRE_MANUAL_PROOF ───────────────────────────────────
print(f"\n{SEP}")
print("5. BOTS REQUIRING MANUAL PROOF")
print(SEP)
c.execute("""
    SELECT b.id, b.name, b.pair, b.direction, b.status,
           COALESCE(t.total_invested,0) as invested,
           COALESCE(t.cycle_id,0) as cycle_id,
           COALESCE(t.cycle_phase,'?') as phase
    FROM bots b LEFT JOIN trades t ON b.id=t.bot_id
    WHERE b.status LIKE '%REQUIRE%' OR b.status LIKE '%MANUAL%'
""")
rows = c.fetchall()
if rows:
    for r in rows:
        print(f"  Bot {r['id']} ({r['name']} {r['direction']} {r['pair']}): "
              f"status={r['status']} invested=${r['invested']:.2f} "
              f"cycle={r['cycle_id']} phase={r['phase']}")
else:
    print("  None")

# ── 6. BOT_ORDERS SUMMARY FOR PROBLEM BOTS ───────────────────────────────────
print(f"\n{SEP}")
print("6. BOT_ORDERS LEDGER FOR KEY BOTS (sol=10008, short btc=?, long btc price=?)")
print(SEP)

# Find BTC and SOL bots
c.execute("""
    SELECT b.id, b.name FROM bots b
    WHERE b.is_active=1 AND (
        b.pair LIKE '%SOL%' OR b.pair LIKE '%BTC%'
    )
""")
key_bots = c.fetchall()

for bot in key_bots:
    bid, bname = bot
    c.execute("""
        SELECT order_type, step, price, amount, filled_amount, status, cycle_id,
               client_order_id, created_at
        FROM bot_orders
        WHERE bot_id=?
          AND status NOT IN ('failed','placing')
        ORDER BY created_at DESC
        LIMIT 20
    """, (bid,))
    orders = c.fetchall()
    print(f"\n  --- Bot {bid} ({bname}) ---")
    print(f"  {'Type':<15} {'Step':>5} {'Price':>10} {'Qty':>8} {'Filled':>8} {'Status':<15} {'Cyc':>4}")
    print(f"  {'-'*75}")
    for o in orders:
        print(f"  {o['order_type']:<15} {o['step']:>5} {o['price']:>10.4f} "
              f"{o['amount']:>8.4f} {o['filled_amount']:>8.4f} {o['status']:<15} "
              f"{str(o['cycle_id'] or 'NULL'):>4}")

# ── 7. WIPE AUDIT - RESET_CLEARED WITHOUT PROOF ──────────────────────────────
print(f"\n{SEP}")
print("7. SUSPECT RESET_CLEARED ROWS (no wipe_proof_snapshot)")
print(SEP)
c.execute("""
    SELECT bo.bot_id, b.name, bo.order_type, bo.filled_amount, bo.price,
           bo.cycle_id, bo.created_at, bo.client_order_id
    FROM bot_orders bo
    JOIN bots b ON bo.bot_id = b.id
    WHERE bo.status = 'reset_cleared'
      AND (bo.wipe_proof_snapshot IS NULL OR bo.wipe_proof_snapshot = '')
      AND bo.filled_amount > 0
    ORDER BY bo.bot_id, bo.created_at DESC
""")
rows = c.fetchall()
print(f"{'BotID':>7} {'Name':<20} {'Type':<15} {'Filled':>8} {'Price':>10} {'Cyc':>4}")
print("-" * 75)
for r in rows:
    print(f"{r['bot_id']:>7} {r['name']:<20} {r['order_type']:<15} "
          f"{r['filled_amount']:>8.4f} {r['price']:>10.4f} {str(r['cycle_id'] or 'NULL'):>4}")

print(f"\n{SEP}")
print("DONE - Copy all output above and share it")
print(SEP)

conn.close()
