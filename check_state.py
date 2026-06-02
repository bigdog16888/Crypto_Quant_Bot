#!/usr/bin/env python3
"""
check_state.py — Bot state diagnostic tool (v3.8.0+)

Usage:
    python check_state.py                   # All bots
    python check_state.py --pair SUIUSDC    # Filter by normalized pair
    python check_state.py --bot 100318      # Single bot by ID
    python check_state.py --pair SUIUSDC --bot 100318

Outputs:
    • Per-bot state table (trades + bots join)
    • Hedge netting table (parent ↔ child relationships + virtual net)
    • Open orders summary (non-cancelled/rejected)
    • "SYSTEM HEALTHY" or itemised issue list
"""

import argparse
import os
import sys

# ── Allow running from repo root without installing the package ──────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sqlite3


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_db_path():
    """Find the SQLite database file."""
    candidates = [
        os.path.join(os.path.dirname(__file__), 'crypto_bot.db'),
        os.path.join(os.path.dirname(__file__), 'trading_bot.db'),
        os.path.join(os.path.dirname(__file__), 'bot.db'),
        os.path.join(os.path.dirname(__file__), 'data', 'trading_bot.db'),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    # Try to read from config/database
    try:
        from engine.database import DB_PATH
        return DB_PATH
    except Exception:
        pass
    raise FileNotFoundError(
        "Cannot locate database file. Pass DB path via DB_PATH env var."
    )


def fmt(v, decimals=4):
    if v is None:
        return 'NULL'
    try:
        return f'{float(v):.{decimals}f}'
    except (TypeError, ValueError):
        return str(v)


def col(text, width):
    return str(text)[:width].ljust(width)


RESET  = '\033[0m'
RED    = '\033[91m'
YELLOW = '\033[93m'
GREEN  = '\033[92m'
BOLD   = '\033[1m'
CYAN   = '\033[96m'


def warn(msg):
    return f'{YELLOW}{msg}{RESET}'


def err(msg):
    return f'{RED}{msg}{RESET}'


def ok(msg):
    return f'{GREEN}{msg}{RESET}'


def header(msg):
    return f'{BOLD}{CYAN}{msg}{RESET}'


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Bot state diagnostic tool')
    parser.add_argument('--pair', help='Filter by normalized pair (e.g. SUIUSDC)')
    parser.add_argument('--bot', type=int, help='Filter by bot ID')
    parser.add_argument('--db', help='Path to SQLite database file')
    args = parser.parse_args()

    db_path = args.db or os.environ.get('DB_PATH') or get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    issues = []

    # ── Build WHERE clause ───────────────────────────────────────────────────
    filters = []
    params = []
    if args.pair:
        filters.append("b.normalized_pair = ?")
        params.append(args.pair.upper())
    if args.bot:
        filters.append("b.id = ?")
        params.append(args.bot)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    # ── 1. Bot state table ───────────────────────────────────────────────────
    print()
    print(header("═" * 110))
    print(header(f"  BOT STATE REPORT  —  db: {db_path}"))
    if args.pair:
        print(header(f"  Filter: pair={args.pair}"))
    if args.bot:
        print(header(f"  Filter: bot_id={args.bot}"))
    print(header("═" * 110))
    print()

    bot_rows = cursor.execute(f"""
        SELECT b.id, b.name, b.direction, b.bot_type, b.status,
               b.normalized_pair,
               b.hedge_child_bot_id,
               COALESCE(t.current_step, 0)        AS step,
               COALESCE(t.open_qty, 0)            AS open_qty,
               COALESCE(t.avg_entry_price, 0)     AS avg_price,
               COALESCE(t.total_invested, 0)      AS invested,
               COALESCE(t.cycle_id, 0)            AS cycle_id,
               COALESCE(t.entry_confirmed, 0)     AS entry_confirmed,
               COALESCE(t.cycle_phase, 'UNKNOWN') AS cycle_phase
        FROM bots b
        LEFT JOIN trades t ON b.id = t.bot_id
        {where}
        ORDER BY b.normalized_pair, b.id
    """, params).fetchall()

    if not bot_rows:
        print(warn("  No bots found matching the given filter."))
        print()
        return

    # Header
    h = (
        col("ID", 8) + col("NAME", 30) + col("TYPE", 14) +
        col("DIR", 6) + col("STATUS", 22) + col("STEP", 5) +
        col("OPEN_QTY", 12) + col("AVG_PRICE", 12) +
        col("PHASE", 10) + col("EC", 4) + col("CID", 6)
    )
    print(BOLD + h + RESET)
    print("─" * 110)

    bot_index = {}  # id → row
    for r in bot_rows:
        bot_index[r['id']] = r
        ec_flag = '' if r['entry_confirmed'] else warn('✗')
        status_disp = r['status'] or ''

        # Flag suspicious states
        line_flags = []
        if r['step'] > 0 and float(r['open_qty']) <= 0.0001:
            line_flags.append('GHOST_STEP')
            issues.append(f"Bot {r['id']} ({r['name']}): step={r['step']} but open_qty≈0 [GHOST_STEP]")
        if r['step'] == 0 and float(r['invested']) > 0.01:
            line_flags.append('PHANTOM_INVESTED')
            issues.append(f"Bot {r['id']} ({r['name']}): step=0 but invested={r['invested']:.4f} [PHANTOM_INVESTED]")
        if r['avg_price'] == 0 and float(r['open_qty']) > 0.001:
            line_flags.append('ZERO_AVG_PRICE')
            issues.append(f"Bot {r['id']} ({r['name']}): open_qty={r['open_qty']:.4f} but avg_price=0 [ZERO_AVG_PRICE]")

        flag_str = (' ← ' + ', '.join(line_flags)) if line_flags else ''

        line = (
            col(r['id'], 8) + col(r['name'], 30) + col(r['bot_type'], 14) +
            col(r['direction'], 6) + col(status_disp, 22) + col(r['step'], 5) +
            col(fmt(r['open_qty']), 12) + col(fmt(r['avg_price']), 12) +
            col(r['cycle_phase'], 10) + col(r['entry_confirmed'], 4) + col(r['cycle_id'], 6)
        )
        if line_flags:
            print(err(line) + err(flag_str))
        else:
            print(line)

    print()

    # ── 2. Hedge netting table ───────────────────────────────────────────────
    # Find all parent→child hedge relationships visible in current filter
    hedge_pairs = []
    for r in bot_rows:
        if r['bot_type'] == 'hedge_child':
            continue  # we'll pick these up from the parent side
        child_id = r['hedge_child_bot_id']
        if not child_id:
            continue
        # Only show if child is also in filter scope (or no filter)
        child_row = bot_index.get(child_id)
        if child_row is None and (args.pair or args.bot):
            # child might be out of filter — fetch separately
            child_row = cursor.execute(
                "SELECT b.id, b.name, b.direction, b.bot_type, "
                "COALESCE(t.open_qty,0) as open_qty, "
                "COALESCE(t.avg_entry_price,0) as avg_price, "
                "COALESCE(t.current_step,0) as step "
                "FROM bots b LEFT JOIN trades t ON b.id=t.bot_id WHERE b.id=?",
                (child_id,)
            ).fetchone()
        if child_row:
            hedge_pairs.append((r, child_row))

    if hedge_pairs:
        print(header("── HEDGE NETTING TABLE ──────────────────────────────────────────────────"))
        print()
        hdr2 = (
            col("PARENT_ID", 10) + col("PARENT_NAME", 28) + col("LONG_QTY", 12) +
            col("CHILD_ID", 10) + col("CHILD_NAME", 28) + col("SHORT_QTY", 12) +
            col("NET_QTY", 12) + col("NOTE", 20)
        )
        print(BOLD + hdr2 + RESET)
        print("─" * 110)

        for parent, child in hedge_pairs:
            long_qty  = float(parent['open_qty']) if parent['direction'] == 'LONG' else 0.0
            short_qty = float(child['open_qty'])  if child['direction'] == 'SHORT' else float(child['open_qty'])
            net = long_qty - short_qty

            note = ''
            if abs(net) > 0.01:
                note = warn(f'UNBALANCED Δ={net:+.4f}')
                issues.append(
                    f"Hedge pair {parent['id']}↔{child['id']}: LONG={long_qty:.4f} SHORT={short_qty:.4f} NET={net:+.4f}"
                )
            else:
                note = ok('BALANCED')

            line = (
                col(parent['id'], 10) + col(parent['name'], 28) + col(fmt(long_qty), 12) +
                col(child['id'], 10) + col(child['name'], 28) + col(fmt(short_qty), 12) +
                col(fmt(net), 12) + str(note)
            )
            print(line)

        print()

    # ── 3. Open orders summary ───────────────────────────────────────────────
    order_filters = []
    order_params = []
    if args.bot:
        order_filters.append("bo.bot_id = ?")
        order_params.append(args.bot)
    elif args.pair:
        order_filters.append("b.normalized_pair = ?")
        order_params.append(args.pair.upper())
    order_where = ("WHERE " + " AND ".join(order_filters)) if order_filters else ""

    open_orders = cursor.execute(f"""
        SELECT bo.bot_id, b.name, bo.order_type, bo.status,
               bo.filled_amount, bo.amount, bo.price, bo.cycle_id,
               bo.created_at
        FROM bot_orders bo
        JOIN bots b ON b.id = bo.bot_id
        {order_where}
        AND bo.status NOT IN ('cancelled','canceled','rejected','expired','filled','closed')
        ORDER BY bo.bot_id, bo.created_at DESC
    """, order_params).fetchall()

    print(header("── OPEN ORDERS ──────────────────────────────────────────────────────────"))
    print()
    if open_orders:
        oh = (
            col("BOT_ID", 8) + col("BOT_NAME", 28) + col("TYPE", 12) +
            col("STATUS", 16) + col("FILLED", 10) + col("AMOUNT", 10) +
            col("PRICE", 12) + col("CID", 6)
        )
        print(BOLD + oh + RESET)
        print("─" * 100)
        for o in open_orders:
            print(
                col(o['bot_id'], 8) + col(o['name'], 28) + col(o['order_type'], 12) +
                col(o['status'], 16) + col(fmt(o['filled_amount']), 10) +
                col(fmt(o['amount']), 10) + col(fmt(o['price']), 12) + col(o['cycle_id'], 6)
            )
    else:
        print(ok("  No open orders."))
    print()

    # ── 4. Summary ───────────────────────────────────────────────────────────
    print(header("═" * 110))
    if issues:
        print(err(f"  ⚠  ISSUES FOUND ({len(issues)}):"))
        for iss in issues:
            print(err(f"     • {iss}"))
    else:
        print(ok("  ✅  SYSTEM HEALTHY — no anomalies detected."))
    print(header("═" * 110))
    print()

    conn.close()
    return 1 if issues else 0


if __name__ == '__main__':
    sys.exit(main())
