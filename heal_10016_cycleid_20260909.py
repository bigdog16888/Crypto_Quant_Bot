#!/usr/bin/env python
"""
heal_10016_cycleid_20260909.py — advance 10016 trades.cycle_id 19 -> 20.

WHY: parent's cycle-19 TP (#1200442005, 0.231 @ 78730.9) filled during engine downtime
and was credited by PRE-COMMIT-RESOLVE at restart — row reset (IDLE, open_qty=0,
close_type=TP_HIT) but the downtime-credit path never advanced cycle_id. Next entry CID
CQB_10016_ENTRY_19_1 collides with the real filled row (id 2689) -> DEDUP-GUARD blocks
every entry attempt (~every 9s). This is the exact write reset_bot_after_cycle would make.
Operator GO 2026-09-09. Same two-gate as heal_stuck_cycles_20260907.py.

TWO-GATE: dry-run default (snapshot + assertions, NO writes). --execute applies.
"""
import argparse
import json
import os
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
SNAP_NAME = 'BOT10016_CYCLEID_PRE_HEAL_SNAPSHOT_20260909.json'

BOT = 10016
OLD_CYCLE = 19
NEW_CYCLE = 20


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--db', default=os.path.join(REPO, 'crypto_bot.db'))
    args = ap.parse_args()
    print(f"MODE: {'EXECUTE' if args.execute else 'DRY-RUN'} | DB: {args.db}")

    conn = sqlite3.connect(args.db)
    conn.execute('PRAGMA busy_timeout=10000')
    conn.row_factory = sqlite3.Row

    # PHASE 0: snapshot (trades row for 10016)
    t = conn.execute('SELECT * FROM trades WHERE bot_id=?', (BOT,)).fetchone()
    snap = {'when': time.strftime('%Y-%m-%d %H:%M:%S'), 'trades_row': dict(t)}
    snap_path = os.path.join(REPO, SNAP_NAME)
    with open(snap_path, 'w', encoding='utf-8') as f:
        json.dump(snap, f, indent=1, default=str)
    print(f"[SNAPSHOT] trades row -> {SNAP_NAME}")

    b = conn.execute('SELECT base_size, config FROM bots WHERE id=?', (BOT,)).fetchone()
    cfg = json.loads(b['config'])
    child = conn.execute('SELECT cycle_id, cycle_phase, open_qty FROM trades WHERE bot_id=100317').fetchone()

    # PHASE 1: assertions
    fails = []
    print('--- ASSERTIONS ---')

    ok = (t['cycle_id'] or 0) == OLD_CYCLE
    print(f"[A1] trades.cycle_id == {OLD_CYCLE}: {t['cycle_id']} -> {'OK' if ok else 'FAIL'}")
    if not ok: fails.append('cycle_id is not 19')

    ok = t['cycle_phase'] == 'IDLE' and abs(float(t['open_qty'] or 0)) < 1e-9 and abs(float(t['total_invested'] or 0)) < 1e-9
    print(f"[A2] row is RESET (IDLE, open_qty=0, invested=0): phase={t['cycle_phase']} qty={t['open_qty']} inv={t['total_invested']} -> {'OK' if ok else 'FAIL'}")
    if not ok: fails.append('row not reset — cycle not safely closed')

    ok = t['close_type'] == 'TP_HIT'
    print(f"[A3] close_type == TP_HIT (fill credited): {t['close_type']} -> {'OK' if ok else 'FAIL'}")
    if not ok: fails.append('close_type not TP_HIT')

    old_cid = f'CQB_{BOT}_ENTRY_{OLD_CYCLE}_1'
    new_cid = f'CQB_{BOT}_ENTRY_{NEW_CYCLE}_1'
    old_rows = conn.execute("SELECT id, status FROM bot_orders WHERE client_order_id=? AND bot_id=?", (old_cid, BOT)).fetchall()
    new_rows = conn.execute("SELECT id FROM bot_orders WHERE client_order_id=? AND bot_id=?", (new_cid, BOT)).fetchall()
    ok = len(old_rows) == 1 and old_rows[0]['status'] == 'filled'
    print(f"[A4] collision source exists exactly once, filled ({old_cid}): {[(r['id'], r['status']) for r in old_rows]} -> {'OK' if ok else 'FAIL'}")
    if not ok: fails.append('collision-source row unexpected')

    ok = len(new_rows) == 0
    print(f"[A5] target CID free ({new_cid}): rows={len(new_rows)} -> {'OK' if ok else 'FAIL'}")
    if not ok: fails.append('target CID already occupied')

    ok = int(cfg.get('max_steps')) == 5 and abs(float(b['base_size']) - 160.0) < 1e-9
    print(f"[A6] new config in place (base=160, max_steps=5): base={b['base_size']} steps={cfg.get('max_steps')} -> {'OK' if ok else 'FAIL'}")
    if not ok: fails.append('config change not live')

    ok = abs(float(child['open_qty'] or 0)) < 1e-9
    print(f"[A7] child 100317 flat (parent advance strands nothing): open_qty={child['open_qty']} -> {'OK' if ok else 'FAIL'}")
    if not ok: fails.append('child not flat')

    try:
        sys.path.insert(0, REPO)
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, '.env'))
        from engine.exchange_interface import ExchangeInterface
        ex = ExchangeInterface(market_type='future')
        btc = [p for p in (ex.fetch_positions() or []) if p['symbol'].startswith('BTC/')]
        net = sum(p['net_qty'] for p in btc)
        ok = abs(net) < 1e-9
        print(f"[A8] EXCHANGE: BTC pair flat (nothing live to strand): net={net:+.6f} -> {'OK' if ok else 'FAIL'}")
        if not ok: fails.append(f'exchange BTC net {net} — pair not flat')
    except Exception as e:
        print(f'[A8] exchange check failed: {type(e).__name__}: {e} -> FAIL')
        fails.append('exchange check failed')

    # PHASE 2: planned write
    print('--- PLANNED WRITE ---')
    print(f"  [W1] UPDATE trades SET cycle_id = {NEW_CYCLE} WHERE bot_id = {BOT} AND cycle_id = {OLD_CYCLE}   (single column, single row, guarded WHERE)")

    if fails:
        print(f"\nASSERTION FAILURES: {fails} — ABORTING, no writes.")
        sys.exit(2)
    if not args.execute:
        print('\nDRY-RUN complete. No writes. Re-run with --execute to apply.')
        conn.close()
        return

    # PHASE 3: execute + verify
    print('\n--- EXECUTING ---')
    cur = conn.cursor()
    cur.execute('BEGIN IMMEDIATE')
    try:
        cur.execute(f"UPDATE trades SET cycle_id=? WHERE bot_id=? AND cycle_id=?", (NEW_CYCLE, BOT, OLD_CYCLE))
        n = cur.rowcount
        print(f'  rowcount={n}')
        if n != 1:
            conn.rollback()
            print(f'  rowcount != 1 — ROLLED BACK, no writes.')
            sys.exit(4)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print('--- POST-VERIFY ---')
    r = conn.execute('SELECT cycle_id, cycle_phase, open_qty FROM trades WHERE bot_id=?', (BOT,)).fetchone()
    ok = r['cycle_id'] == NEW_CYCLE and r['cycle_phase'] == 'IDLE' and abs(float(r['open_qty'] or 0)) < 1e-9
    print(f"  cycle_id={r['cycle_id']} (want {NEW_CYCLE}), phase={r['cycle_phase']}, open_qty={r['open_qty']} -> {'OK' if ok else 'FAIL'}")
    conn.close()
    print(f"\n{'HEAL COMPLETE (post-verify OK)' if ok else 'POST-VERIFY FAILED'}")
    sys.exit(0 if ok else 3)


if __name__ == '__main__':
    main()
