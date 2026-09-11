#!/usr/bin/env python
"""
apply_10016_config_20260908.py — Option A (corrected): max_steps 8->5 + base_size $10->$160
+ HedgeStartStep 7->4. Operator-approved 2026-09-08. Apply-ON-FLAT only.

WHY (measured): BTC $100 min-notional auto-scales the $10 base to a $158.54 real anchor
(use_min_size=true). Old cfg_max(8,$10)=$3,322 -> O-1 2x threshold $6,645 tripped at fill 7/8
routinely ($18.3k real ladder vs $6.6k line). New: cfg_max(5,$160)=$7,846 -> threshold
$15,691; real 5-step ladder $4,051 -> never trips. base $10 vs $160 changes ZERO real
orders (both -> 0.002 BTC after min-notional + lot step; verified 2026-09-08).

SAFETY: cycle-19's LIVE position ($18,159 invested) sits ABOVE the new threshold, so
applying mid-cycle would regate 10016 within seconds (mid-cycle surprise — operator
forbid). Hence hard assertion: trades.open_qty must be 0 at execute time (apply-on-flat).
If the bot has ALREADY entered a new cycle when you run this, it aborts — rerun at the
next flat gap.

TWO-GATE: dry-run default (snapshot + assertions + planned writes, NO writes).
--execute applies. Test first on a copy:  --db <path>
"""
import argparse
import json
import os
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
SNAP_NAME = 'BOT10016_CONFIG_PRE_APPLY_SNAPSHOT_20260908.json'

OLD = {'base_size': 10.0, 'max_steps': 8, 'HedgeStartStep': 7}
NEW = {'base_size': 160.0, 'max_steps': 5, 'HedgeStartStep': 4}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--db', default=os.path.join(REPO, 'crypto_bot.db'))
    args = ap.parse_args()
    print(f"MODE: {'EXECUTE' if args.execute else 'DRY-RUN'} | DB: {args.db}")

    conn = sqlite3.connect(args.db)
    conn.execute('PRAGMA busy_timeout=10000')
    conn.row_factory = sqlite3.Row

    # PHASE 0: snapshot
    snap = {'when': time.strftime('%Y-%m-%d %H:%M:%S'), 'bots_row': None, 'trades_row': None}
    r = conn.execute("SELECT * FROM bots WHERE id=10016").fetchone()
    snap['bots_row'] = dict(r)
    t = conn.execute("SELECT * FROM trades WHERE bot_id=10016").fetchone()
    snap['trades_row'] = dict(t)
    snap_path = os.path.join(REPO, SNAP_NAME)
    with open(snap_path, 'w', encoding='utf-8') as f:
        json.dump(snap, f, indent=1, default=str)
    print(f"[SNAPSHOT] bots+trades rows -> {SNAP_NAME}")

    # PHASE 1: assertions
    fails = []
    cfg = json.loads(snap['bots_row']['config'])
    print('--- ASSERTIONS ---')
    ok = abs(float(snap['bots_row']['base_size']) - OLD['base_size']) < 1e-9
    print(f"[A1] base_size == {OLD['base_size']}: {snap['bots_row']['base_size']} -> {'OK' if ok else 'FAIL'}")
    if not ok: fails.append('base_size drifted')
    ok = int(cfg.get('max_steps')) == OLD['max_steps']
    print(f"[A2] config.max_steps == {OLD['max_steps']}: {cfg.get('max_steps')} -> {'OK' if ok else 'FAIL'}")
    if not ok: fails.append('max_steps drifted')
    ok = int(cfg.get('HedgeStartStep')) == OLD['HedgeStartStep']
    print(f"[A3] config.HedgeStartStep == {OLD['HedgeStartStep']}: {cfg.get('HedgeStartStep')} -> {'OK' if ok else 'FAIL'}")
    if not ok: fails.append('HedgeStartStep drifted')
    ok = cfg.get('use_min_size') is True
    print(f"[A4] use_min_size is True (min-notional anchor premise): {cfg.get('use_min_size')} -> {'OK' if ok else 'FAIL'}")
    if not ok: fails.append('use_min_size off — anchor premise invalid')
    ok = abs(float(snap['trades_row']['open_qty'] or 0)) < 1e-9
    ex_flat = None
    if not ok:
        # DB row may be STALE (engine down before crediting a TP fill). Operator standard
        # (2026-09-08): verify flat by EXCHANGE TRUTH, not a DB snapshot. Two independent
        # exchange facts must BOTH hold:
        #   E1: the parent's cycle-19 TP order (1200442005) status == 'filled'
        #   E2: no CQB_10016* orders resting on the exchange right now
        print('[A5] DB open_qty!=0 — DB row possibly stale (engine down). Falling back to EXCHANGE TRUTH...')
        try:
            sys.path.insert(0, REPO)
            from dotenv import load_dotenv
            load_dotenv(os.path.join(REPO, '.env'))
            from engine.exchange_interface import ExchangeInterface
            ex = ExchangeInterface(market_type='future')
            tp = ex.fetch_order('1200442005', 'BTC/USDC:USDC')
            tp_status = (tp or {}).get('status')
            e1 = tp_status == 'filled'
            resting = [o for o in (ex.fetch_open_orders('BTC/USDC:USDC') or []) if str(o.get('clientOrderId', '')).startswith('CQB_10016')]
            e2 = len(resting) == 0
            ex_flat = e1 and e2
            print(f"       E1: parent TP 1200442005 status={tp_status!r} -> {'OK' if e1 else 'FAIL'}")
            print(f"       E2: CQB_10016 orders resting on exchange: {len(resting)} -> {'OK' if e2 else 'FAIL'}")
        except Exception as e:
            print(f'       exchange check failed: {type(e).__name__}: {e}')
            ex_flat = False
        ok = bool(ex_flat)
        verdict = 'OK (exchange-verified flat)' if ok else 'FAIL (exchange shows position/orders live)'
    else:
        verdict = 'OK'
    print(f"[A5] APPLY-ON-FLAT: cycle-{snap['trades_row']['cycle_id']} open_qty={snap['trades_row']['open_qty']} -> {verdict}")
    if not ok: fails.append('parent NOT flat (DB and/or exchange)')

    # PHASE 2: planned writes (exact)
    new_cfg = dict(cfg)
    new_cfg['max_steps'] = NEW['max_steps']
    new_cfg['HedgeStartStep'] = NEW['HedgeStartStep']
    print('--- PLANNED WRITES ---')
    print(f"  [W1] UPDATE bots SET base_size={NEW['base_size']} WHERE id=10016   (10.0 -> 160.0)")
    print(f"  [W2] UPDATE bots SET config=... WHERE id=10016 : \"max_steps\": {OLD['max_steps']} -> {NEW['max_steps']}, \"HedgeStartStep\": {OLD['HedgeStartStep']} -> {NEW['HedgeStartStep']} (all other keys byte-identical)")

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
        cur.execute("UPDATE bots SET base_size=? WHERE id=10016", (NEW['base_size'],))
        print(f'  base_size rowcount={cur.rowcount}')
        cur.execute("UPDATE bots SET config=? WHERE id=10016", (json.dumps(new_cfg),))
        print(f'  config rowcount={cur.rowcount}')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print('--- POST-VERIFY ---')
    b = conn.execute("SELECT base_size, config FROM bots WHERE id=10016").fetchone()
    c = json.loads(b['config'])
    all_ok = True
    checks = [
        ('base_size', float(b['base_size']), NEW['base_size']),
        ('max_steps', int(c['max_steps']), NEW['max_steps']),
        ('HedgeStartStep', int(c['HedgeStartStep']), NEW['HedgeStartStep']),
    ]
    for name, got, want in checks:
        ok = got == want
        all_ok = all_ok and ok
        print(f'  {name}: {got} (want {want}) -> {"OK" if ok else "FAIL"}')
    # other keys untouched
    same = {k: v for k, v in c.items() if k not in ('max_steps', 'HedgeStartStep')} == {k: v for k, v in cfg.items() if k not in ('max_steps', 'HedgeStartStep')}
    print(f'  other config keys unchanged: {"OK" if same else "FAIL"}')
    all_ok = all_ok and same
    # engine's own O-1 math at new config
    from engine.database import _calculate_config_max_notional
    cmax = _calculate_config_max_notional(160.0, 1.88, 5)
    print(f'  O-1 cfg_max(new) = ${cmax:.2f} -> 2x threshold ${2*cmax:.2f}; 5-step real ladder ~$4,051 -> trips: {"NO (OK)" if 4051 < 2*cmax else "YES (FAIL)"}')
    conn.close()
    print(f'\n{"APPLY COMPLETE (post-verify OK)" if all_ok else "POST-VERIFY FAILED — investigate, do not retry blindly"}')
    sys.exit(0 if all_ok else 3)


if __name__ == '__main__':
    main()
