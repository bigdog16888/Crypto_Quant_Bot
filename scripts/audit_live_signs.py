"""Read-only live-DB sign audit + fresh exchange positions (pair parity re-verification).

Answers, with raw numbers:
1. Does ANY active bot have bots.direction != trades.position_side while holding
   open_qty? (Decides whether the Ticket5 sign issue is fixture-only or a live
   data problem — the four-check evidence for the get_pair_virtual_net contract.)
2. Current frozen/unfrozen status of LINK/ETH bot set (DB rows, fresh read).
3. Pair virtual nets three ways: the REAL get_pair_virtual_net function,
   an independent read-only SQL replication, and a FRESH fetch_positions.
   (LINK should be ~ -31.03, SOL ~ -34.98, ETH ~ 0.079 if today's readings were
   computed with correct sign logic.)

NO WRITES. Exchange calls are GET-only (fetch_positions). DB opened mode=ro
for the independent path; engine import only issues SELECTs.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIVE_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crypto_bot.db")

import sqlite3

ro = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
ro.row_factory = sqlite3.Row

print("=" * 78)
print("=== 1. direction vs position_side — active bots holding open_qty > 0 ===")
print("=" * 78)
rows = ro.execute("""
    SELECT b.id, b.name, b.pair, b.direction, b.status, b.is_active,
           t.open_qty, t.position_side
    FROM bots b JOIN trades t ON t.bot_id = b.id
    WHERE b.is_active = 1 AND COALESCE(t.open_qty, 0) > 0
    ORDER BY b.pair, b.id
""").fetchall()
mismatch = 0
for r in rows:
    d = (r["direction"] or "").upper()
    p = (r["position_side"] or "").upper()
    flag = ""
    if d != p:
        mismatch += 1
        flag = "   <<< MISMATCH"
    print(f"  {r['id']:>7} {str(r['pair'])[:20]:<22} dir={d:<5} pos_side={p:<5} "
          f"open_qty={r['open_qty']:<12} status={str(r['status'])[:20]}{flag}")
print(f"\n  rows checked: {len(rows)}   direction/position_side mismatches: {mismatch}")

print()
print("=" * 78)
print("=== 2. LINK / ETH bot set — current DB status (fresh read) ===")
print("=" * 78)
WATCH = (10011, 10012, 10021, 100002, 100316, 100321, 100325,
         10020, 100320, 100319, 10001, 100001)
q = ",".join(str(x) for x in WATCH)
for r in ro.execute(f"""
    SELECT b.id, b.name, b.pair, b.direction, b.status, b.is_active, b.bot_type,
           COALESCE(t.open_qty, 0) AS oq, COALESCE(t.position_side, '?') AS ps
    FROM bots b LEFT JOIN trades t ON t.bot_id = b.id
    WHERE b.id IN ({q}) ORDER BY b.id
""").fetchall():
    print(f"  {r['id']:>7} {str(r['name'])[:24]:<26} {str(r['pair'])[:16]:<18} "
          f"dir={str(r['direction'])[:5]:<6} status={str(r['status'])[:22]:<24} "
          f"open_qty={r['oq']:<10} pos_side={r['ps']}")

print()
print("=" * 78)
print("=== 3. Pair virtual nets, three independent ways ===")
print("=" * 78)
from engine.exchange_interface import ExchangeInterface, normalize_symbol
from engine.database import get_pair_virtual_net

ex = ExchangeInterface()
positions = ex.fetch_positions()
print("\n-- raw fetch_positions() rows with non-zero net --")
ex_nets = {}
for p in positions:
    sym = p.get("symbol", "")
    nq = float(p.get("net_qty") or 0)
    ex_nets[sym] = nq
    if abs(nq) > 0:
        print(f"  {sym:<20} net_qty={nq:<12} side={p.get('side')} contracts={p.get('contracts')}")

pairs = [dict(r) for r in ro.execute(
    "SELECT DISTINCT pair, normalized_pair FROM bots WHERE is_active = 1").fetchall()]
print(f"\n-- active pairs in DB: {[p['pair'] for p in pairs]} --")

covered = set()
for pr in pairs:
    sym = pr["pair"]
    norm = str(pr["normalized_pair"] or normalize_symbol(sym)).upper()
    covered.add(sym)
    rows = ro.execute("""
        SELECT COALESCE(t.open_qty, 0.0) AS oq, COALESCE(t.position_side, 'LONG') AS ps
        FROM bots b JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1
          AND (b.normalized_pair = ? OR b.pair = ? OR b.normalized_pair = ? OR b.pair = ?)
    """, (norm, sym, sym, norm)).fetchall()
    indep = sum(float(r["oq"]) * (-1.0 if r["ps"].upper() == "SHORT" else 1.0) for r in rows)
    fn = get_pair_virtual_net(sym)
    exn = ex_nets.get(sym)
    if exn is None:
        # Demo FAPI returns position symbols without the settlement suffix
        # ('ETH/USDC'); pair symbols carry it ('ETH/USDC:USDC').
        exn = ex_nets.get(sym.split(":")[0])
    ex_disp = f"{exn:.6f}" if exn is not None else "no-position-row"
    agree = "OK" if (exn is not None and abs(fn - exn) < 1e-6) else "DIFF"
    print(f"  {sym:<20} fn={fn:>12.8f}  indep_sql={indep:>12.8f}  exchange={ex_disp:>14}  [{agree}]")

# Orphan check must normalize symbols the same way (suffix-insensitive)
orphans = {}
for s, n in ex_nets.items():
    if abs(n) > 0 and not any(s == p.split(":")[0] for p in covered):
        orphans[s] = n
print(f"\n-- exchange positions NOT covered by any active bot pair: {orphans or 'none'} --")
print("  (ETH ~ +3.076 here would confirm the unattributed orphan again)")

ro.close()
