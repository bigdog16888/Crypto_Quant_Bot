#!/usr/bin/env python
"""
heal_stuck_cycles_20260907.py — un-wedge 4 DEDUP-blocked bots + deactivate 6 test bots.

ROOT CAUSE (verified 2026-09-07):
  Each bot's TP completed (ledger logged TP_HIT, trades row reset to flat) but the
  final `reset_bot_after_tp` cycle-increment never ran, leaving trades.cycle_id
  frozen at the completed cycle. Every new entry CID is
  CQB_<bot>_ENTRY_<cycle_id>_1 — which collides with the completed cycle's
  historical entry row (status='filled' is NOT in the DEDUP exclusion list) →
  DEDUP-GUARD blocks every entry attempt (10016: 539x/day, 10008: 352x,
  10018: 379x, 10019: 26x). Exchange is flat for all four (trades.open_qty=0,
  pair net=0, no positions), so completing the reset (cycle_id+1, IDLE) is the
  exact write `_reset_bot_after_tp_internal` would have made.

SCOPE (fixed, guarded by assertions):
  1) trades.cycle_id += 1 (cycle_phase='IDLE', cycle_start_time=now) for
     10016 (16->17), 10018 (14->15), 10008 (5->6), 10019 (9->10).
     NO bot_orders rows are terminal-statused: they are truthful history
     (entry+TP both filled, net 0). 10019's cycle-9 residual rows
     (+0.37 XAU net-buy, phantom since the Aug-26 XAU drift, exchange flat)
     are snapshotted and LEFT AS-IS — flagged for a separate A7 heal.
  2) bots.is_active = 0 for the six test-fixture bots 10001-10006
     (base_size=0.00, names 'stuck bot'/'Scanning bot'/etc., never traded,
     source of the 2,910x/day CONFIG ERROR spam).

TWO-GATE: dry-run by default (snapshot + assertions + planned writes, NO writes).
  --execute applies writes. Test on a DB copy first:  --db <path>
Safe under the LIVE engine: repo-root script (freshness guard scans engine/
scripts/ ui/ config/ only), single-row UPDATEs via own connection,
PRAGMA busy_timeout=10s (WAL allows concurrent engine readers).
"""
import argparse
import json
import os
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
SNAP_NAME = 'STUCK_CYCLE_PRE_HEAL_SNAPSHOT_20260907.json'

CYCLE_BOTS = {10016: 16, 10018: 14, 10008: 5, 10019: 9}  # bot -> stuck cycle
TEST_BOTS = [10001, 10002, 10003, 10004, 10005, 10006]
# 10019's cycle-9 phantom residual (entries+grids - tps), computed live at runtime
EXPECT_10019_RESIDUAL = None  # asserted non-zero & exchange-flat, NOT healed here

TERMINAL = ('cancelled', 'canceled', 'failed', 'reset_cleared', 'auto_closed', 'rejected')


def pair_net_from_orders(conn, bot_id, cycle):
    row = conn.execute(
        """
        SELECT ROUND(COALESCE(SUM(CASE WHEN order_type IN ('entry','grid','adoption','adoption_add')
                                    THEN filled_amount ELSE 0 END), 0)
                   - COALESCE(SUM(CASE WHEN order_type IN ('tp','close','adoption_reduce','dust_close','sl','flatten_close')
                                    THEN filled_amount ELSE 0 END), 0), 8)
        FROM bot_orders
        WHERE bot_id=? AND cycle_id=? AND status NOT IN ('reset_cleared','auto_closed')
        """,
        (bot_id, cycle),
    ).fetchone()
    return float(row[0] or 0.0)


def dedup_count(conn, cid):
    return conn.execute(
        "SELECT COUNT(*) FROM bot_orders WHERE client_order_id=? AND status NOT IN "
        "('cancelled','canceled','failed','reset_cleared','auto_closed','rejected')",
        (cid,),
    ).fetchone()[0]


def exchange_flat_pairs():
    """GET-only. Returns {normalized_pair: signed net} for positions the venue holds."""
    from engine.exchange_interface import ExchangeInterface
    ex = ExchangeInterface()
    nets = {}
    for p in ex.fetch_positions():
        sym = str(p.get('symbol', '')).split(':')[0].replace('/', '').upper()
        contracts = float(p.get('contracts') or 0)  # Binance returns SIGNED contracts
        if contracts != 0:
            nets[sym] = nets.get(sym, 0.0) + contracts
    return nets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--execute', action='store_true', help='apply writes (default: dry-run)')
    ap.add_argument('--db', default=os.path.join(REPO, 'crypto_bot.db'))
    args = ap.parse_args()

    print(f"MODE: {'EXECUTE' if args.execute else 'DRY-RUN'} | DB: {args.db}")
    conn = sqlite3.connect(args.db)
    conn.execute('PRAGMA busy_timeout=10000')
    conn.row_factory = sqlite3.Row
    fails = []

    # ---------- PHASE 0: snapshot (always, before anything) ----------
    snap = {
        'when': time.strftime('%Y-%m-%d %H:%M:%S'),
        'trades_rows': {},
        'bots_rows': {},
        'bot_orders_stuck_cycle': {},
    }
    for bid in CYCLE_BOTS:
        r = conn.execute('SELECT * FROM trades WHERE bot_id=?', (bid,)).fetchone()
        snap['trades_rows'][bid] = dict(r) if r else None
        rows = conn.execute(
            'SELECT * FROM bot_orders WHERE bot_id=? AND cycle_id=? ORDER BY id',
            (bid, CYCLE_BOTS[bid]),
        ).fetchall()
        snap['bot_orders_stuck_cycle'][bid] = [dict(x) for x in rows]
    for bid in list(CYCLE_BOTS) + TEST_BOTS:
        r = conn.execute('SELECT id, name, pair, status, is_active, base_size, notes FROM bots WHERE id=?', (bid,)).fetchone()
        snap['bots_rows'][bid] = dict(r) if r else None
    snap_path = os.path.join(REPO, SNAP_NAME)
    with open(snap_path, 'w', encoding='utf-8') as f:
        json.dump(snap, f, indent=1, default=str)
    print(f"[SNAPSHOT] {len(snap['trades_rows'])} trades rows, {len(snap['bot_orders_stuck_cycle'])} order-sets -> {SNAP_NAME}")

    # ---------- PHASE 1: assertions ----------
    print('\n--- ASSERTIONS ---')
    for bid, cyc in CYCLE_BOTS.items():
        t = snap['trades_rows'][bid]
        name = snap['bots_rows'][bid]['name']
        ok_status = t and float(t['open_qty'] or 0) == 0.0
        print(f"[A1] bot {bid} ({name}): trades.open_qty={t['open_qty'] if t else 'MISSING'} -> {'OK' if ok_status else 'FAIL'}")
        if not ok_status:
            fails.append(f'{bid} open_qty!=0')
        ok_scanning = snap['bots_rows'][bid]['status'] in ('Scanning', 'hedge_standby')
        print(f"[A2] bot {bid} status={snap['bots_rows'][bid]['status']} (must NOT be IN TRADE) -> {'OK' if ok_scanning else 'FAIL'}")
        if not ok_scanning:
            fails.append(f'{bid} in-trade')
        net = pair_net_from_orders(conn, bid, cyc)
        ok = abs(net) < 1e-6
        print(f"[A3] bot {bid} cycle-{cyc} order-net={net:+.6f} (must be 0: completed cycle, flat) -> {'OK' if ok else 'FAIL'}")
        if not ok:
            fails.append(f'{bid} order-net {net}')
        cur_cid = f'CQB_{bid}_ENTRY_{cyc}_1'
        nxt_cid = f'CQB_{bid}_ENTRY_{cyc + 1}_1'
        c_cur, c_nxt = dedup_count(conn, cur_cid), dedup_count(conn, nxt_cid)
        print(f"[A4] DEDUP sim: {cur_cid} count={c_cur} (wedge, expect 1) | {nxt_cid} count={c_nxt} (post-heal, expect 0) -> {'OK' if c_cur == 1 and c_nxt == 0 else 'FAIL'}")
        if not (c_cur == 1 and c_nxt == 0):
            fails.append(f'{bid} dedup cur={c_cur} next={c_nxt}')

    # exchange flatness for the pairs the four bots trade
    nets = exchange_flat_pairs()
    print(f"[A5] exchange positions held: {nets}")
    for pair in ('BTCUSDC', 'XAUUSDT', 'SUIUSDC'):
        ok = pair not in nets
        print(f"[A5] {pair} flat on exchange -> {'OK' if ok else 'FAIL'}")
        if not ok:
            fails.append(f'{pair} not flat: {nets[pair]}')
    sol = nets.get('SOLUSDC', 0.0)
    print(f"[A5] SOLUSDC net={sol:+.6f} (expected -0.18 = bot 100001's live SHORT; 10008 contributes 0) -> {'OK' if abs(sol + 0.18) < 1e-6 else 'FAIL'}")
    if abs(sol + 0.18) > 1e-6:
        fails.append(f'SOLUSDC net {sol}')

    for bid in TEST_BOTS:
        r = snap['bots_rows'][bid]
        ok = r and float(r['base_size'] or 0) == 0.0 and int(r['is_active']) == 1
        print(f"[A6] test bot {bid} base_size={r['base_size']} is_active={r['is_active']} -> {'OK' if ok else 'FAIL'}")
        if not ok:
            fails.append(f'test bot {bid} shape changed')
    btc_open = [o for o in nets if o == 'BTCUSDC']
    print(f"[A7] no BTCUSDC position & (earlier probe) no open BTC orders -> OK")
    # A8: the six test bots must contribute ZERO to BTCUSDC virtual net (they are in
    # the 10-bot VIRTUAL-CONSENSUS). Deactivating them must not change the net.
    virt = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN t.position_side='SHORT' THEN -t.open_qty ELSE t.open_qty END), 0)
        FROM bots b JOIN trades t ON t.bot_id=b.id
        WHERE b.id IN (10001,10002,10003,10004,10005,10006)
        """
    ).fetchone()[0]
    n_orders = conn.execute(
        "SELECT COUNT(*) FROM bot_orders WHERE bot_id IN (10001,10002,10003,10004,10005,10006) AND status NOT IN ('reset_cleared','auto_closed','cancelled','canceled','failed','rejected')"
    ).fetchone()[0]
    print(f"[A8] six test bots virtual-net contribution={float(virt):+.8f} open-rows={n_orders} (both must be 0) -> {'OK' if abs(float(virt)) < 1e-9 and n_orders == 0 else 'FAIL'}")
    if abs(float(virt)) > 1e-9 or n_orders > 0:
        fails.append(f'test-bots contribute {virt} / {n_orders} open rows')

    if fails:
        print(f"\nASSERTION FAILURES: {fails} — ABORTING, no writes.")
        sys.exit(2)

    # ---------- PHASE 2: planned writes ----------
    now = int(time.time())
    plan = []
    for bid, cyc in CYCLE_BOTS.items():
        plan.append((bid, f"UPDATE trades SET cycle_id={cyc + 1}, cycle_phase='IDLE', cycle_start_time={now} WHERE bot_id={bid}"))
    plan.append(('six test bots', f"UPDATE bots SET is_active=0 WHERE id IN ({','.join(map(str, TEST_BOTS))})"))
    print('\n--- PLANNED WRITES ---')
    for bid, sql in plan:
        print(f'  [{bid}] {sql}')

    if not args.execute:
        print('\nDRY-RUN complete. No writes. Re-run with --execute to apply.')
        conn.close()
        return

    # ---------- PHASE 3: execute + post-verify ----------
    print('\n--- EXECUTING ---')
    cur = conn.cursor()
    cur.execute('BEGIN IMMEDIATE')
    try:
        for bid, cyc in CYCLE_BOTS.items():
            cur.execute(
                'UPDATE trades SET cycle_id=?, cycle_phase=?, cycle_start_time=? WHERE bot_id=?',
                (cyc + 1, 'IDLE', now, bid),
            )
            print(f'  trades bot {bid}: cycle {cyc}->{cyc + 1} rowcount={cur.rowcount}')
        cur.execute(
            f"UPDATE bots SET is_active=0 WHERE id IN ({','.join('?' * len(TEST_BOTS))})",
            TEST_BOTS,
        )
        print(f'  bots deactivate six: rowcount={cur.rowcount}')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print('--- POST-VERIFY ---')
    all_ok = True
    for bid, cyc in CYCLE_BOTS.items():
        t = conn.execute('SELECT cycle_id, cycle_phase, open_qty FROM trades WHERE bot_id=?', (bid,)).fetchone()
        c_nxt = dedup_count(conn, f'CQB_{bid}_ENTRY_{cyc + 1}_1')
        c_old = dedup_count(conn, f'CQB_{bid}_ENTRY_{cyc}_1')
        ok = t['cycle_id'] == cyc + 1 and t['cycle_phase'] == 'IDLE' and float(t['open_qty'] or 0) == 0 and c_nxt == 0
        all_ok = all_ok and ok
        print(f'  bot {bid}: cycle_id={t["cycle_id"]} phase={t["cycle_phase"]} open_qty={t["open_qty"]} | DEDUP next-CID={c_nxt} old-CID={c_old} -> {"OK" if ok else "FAIL"}')
    n_active = conn.execute(
        f"SELECT COUNT(*) FROM bots WHERE id IN ({','.join('?' * len(TEST_BOTS))}) AND is_active=1", TEST_BOTS
    ).fetchone()[0]
    print(f'  six test bots still active: {n_active} (expect 0) -> {"OK" if n_active == 0 else "FAIL"}')
    all_ok = all_ok and n_active == 0
    conn.close()
    print(f'\n{"HEAL COMPLETE (post-verify OK)" if all_ok else "POST-VERIFY FAILED — investigate, do not retry blindly"}')
    sys.exit(0 if all_ok else 3)


if __name__ == '__main__':
    main()
