"""
v2.0 CLEAN RESET SCRIPT
=======================
Performs a complete, safe reset for clean forward testing.

Steps:
  1. Cancel ALL open orders on the exchange (every active pair)
  2. Flatten ALL open positions with market reduce-only orders
  3. Wait and verify exchange is fully flat
  4. Reset ALL bot DB state → trades zeroed, bot_orders cancelled, bots → Scanning
     (Bot configs preserved: base_size, strategy params, etc.)

Usage:
  python clean_reset.py --dry-run    # preview only, no changes
  python clean_reset.py              # live reset (asks for YES confirmation)
"""
import sys
import time
import argparse
import logging
import json

sys.path.insert(0, r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('CLEAN_RESET')

parser = argparse.ArgumentParser()
parser.add_argument('--dry-run', action='store_true', help='Preview without executing anything')
args = parser.parse_args()
DRY = args.dry_run

if DRY:
    logger.info('*** DRY-RUN MODE - no exchange calls, no DB changes ***')
else:
    logger.info('*** LIVE RESET MODE - will cancel orders, flatten positions, wipe DB state ***')
    confirm = input('\nType YES to confirm full reset: ').strip()
    if confirm != 'YES':
        logger.info('Aborted.')
        sys.exit(0)

# ─── Connect ───────────────────────────────────────────────────────────────────
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

conn = get_connection()
bots = conn.execute('SELECT id, name, pair, direction FROM bots WHERE is_active=1').fetchall()
logger.info(f'Active bots in DB: {len(bots)}')

ex = None
try:
    ex = ExchangeInterface()
    logger.info('Exchange connected OK.')
except Exception as e:
    logger.error(f'Exchange connect FAILED: {e}')
    if not DRY:
        sys.exit(1)

# ─── Step 1: Show live state ───────────────────────────────────────────────────
logger.info('\n--- LIVE STATE BEFORE RESET ---')
open_positions = []
if ex:
    try:
        positions = ex.fetch_positions()
        open_positions = [p for p in (positions or []) if abs(float(p.get('contracts', 0) or 0)) > 0]
        logger.info(f'Open positions: {len(open_positions)}')
        for p in open_positions:
            symbol = p.get('symbol', '?')
            contracts = float(p.get('contracts', 0) or 0)
            entry = float(p.get('entryPrice', 0) or 0)
            notional = abs(contracts) * entry
            direction = 'LONG' if contracts > 0 else 'SHORT'
            logger.info(f'  {direction:5s} {symbol}: qty={abs(contracts):.4f} entry={entry:.4f} value=${notional:.2f}')
    except Exception as e:
        logger.error(f'fetch_positions: {e}')

# ─── Step 2: Cancel all open orders ───────────────────────────────────────────
logger.info('\n--- STEP 2: Cancel all open exchange orders ---')

pairs_to_check = list(set(
    b[2].split(':')[0] if ':' in b[2] else b[2]
    for b in bots
))
# Also check raw pair strings as-is
pairs_raw = list(set(b[2] for b in bots))

cancelled_total = 0
if ex:
    for pair in pairs_raw:
        try:
            open_orders = ex.fetch_open_orders(pair)
            if not open_orders:
                logger.info(f'  {pair}: no open orders')
                continue
            logger.info(f'  {pair}: {len(open_orders)} orders to cancel')
            for o in open_orders:
                oid = o.get('id')
                cid = o.get('clientOrderId', 'N/A')
                if DRY:
                    logger.info(f'    [DRY] Would cancel {oid} ({cid})')
                else:
                    try:
                        ex.cancel_order(oid, pair)
                        logger.info(f'    Cancelled {oid} ({cid})')
                        cancelled_total += 1
                        time.sleep(0.05)
                    except Exception as ce:
                        logger.warning(f'    Cancel error {oid}: {ce}')
        except Exception as e:
            logger.error(f'  {pair}: fetch_open_orders error: {e}')

logger.info(f'Orders cancelled: {cancelled_total}')

# ─── Step 3: Flatten open positions ───────────────────────────────────────────
logger.info('\n--- STEP 3: Flatten all open positions ---')

flattened_total = 0
for pos in open_positions:
    symbol = pos.get('symbol')
    contracts = float(pos.get('contracts', 0) or 0)
    pos_amt = float(pos.get('positionAmt', contracts) or contracts)

    if abs(contracts) < 1e-9:
        continue

    close_side = 'sell' if pos_amt > 0 else 'buy'
    close_qty = abs(contracts)
    direction = 'LONG' if pos_amt > 0 else 'SHORT'

    logger.info(f'  Closing {direction} {symbol}: qty={close_qty} side={close_side}')
    if DRY:
        logger.info(f'    [DRY] Would place market {close_side} {close_qty} {symbol} reduceOnly')
        continue

    try:
        cid = f'RESET_FLAT_{int(time.time())}'
        result = ex.create_order(
            symbol, 'market', close_side, close_qty,
            params={'reduceOnly': True, 'clientOrderId': cid}
        )
        logger.info(f'    Done: order_id={result.get("id")} status={result.get("status")}')
        flattened_total += 1
        time.sleep(0.2)
    except Exception as fe:
        logger.error(f'    FLATTEN FAILED for {symbol}: {fe}')

logger.info(f'Positions flattened: {flattened_total}')

# ─── Step 4: Verify flat ───────────────────────────────────────────────────────
if not DRY and flattened_total > 0:
    logger.info('\nWaiting 3 seconds for market orders to settle...')
    time.sleep(3)

    logger.info('--- STEP 4: Verifying exchange is flat ---')
    try:
        positions = ex.fetch_positions()
        remaining = [p for p in (positions or []) if abs(float(p.get('contracts', 0) or 0)) > 0]
        if remaining:
            logger.warning(f'WARNING: {len(remaining)} positions still open!')
            for p in remaining:
                logger.warning(f'  STILL OPEN: {p.get("symbol")} contracts={p.get("contracts")}')
        else:
            logger.info('All positions confirmed flat. Exchange is clean. OK')
    except Exception as e:
        logger.error(f'Verify error: {e}')
else:
    if not DRY:
        logger.info('--- STEP 4: Nothing to flatten ---')

# ─── Step 5: Reset DB state ────────────────────────────────────────────────────
logger.info('\n--- STEP 5: Resetting DB state ---')

if DRY:
    trades_with_inv = conn.execute('SELECT COUNT(*) FROM trades WHERE total_invested > 0.01').fetchone()[0]
    open_db_orders = conn.execute("SELECT COUNT(*) FROM bot_orders WHERE status IN ('open','new','placing','partial')").fetchone()[0]
    logger.info(f'  [DRY] Would zero trades for {trades_with_inv} bot(s) with investment')
    logger.info(f'  [DRY] Would cancel {open_db_orders} open DB orders')
    logger.info(f'  [DRY] Would reset all bots -> Scanning')
else:
    try:
        # Zero trades table (position state only — configs preserved)
        conn.execute("""
            UPDATE trades SET
                total_invested    = 0,
                avg_entry_price   = 0,
                current_step      = 0,
                target_tp_price   = 0,
                tp_order_id       = NULL,
                basket_start_time = NULL,
                cycle_id          = 0,
                entry_order_id    = NULL,
                cycle_phase       = NULL,
                position_side     = NULL,
                entry_confirmed   = 0
        """)
        logger.info(f'  trades: {conn.execute("SELECT changes()").fetchone()[0]} rows zeroed')

        # Mark all bot_orders as reset_cleared (dead on exchange and cleared from active engine logic)
        conn.execute("""
            UPDATE bot_orders SET status = 'reset_cleared'
        """)
        logger.info(f'  bot_orders: {conn.execute("SELECT changes()").fetchone()[0]} rows -> reset_cleared')

        # Reset all active bots to Scanning, clear errors
        conn.execute("""
            UPDATE bots SET
                status          = 'Scanning',
                last_error      = NULL,
                last_error_time = NULL,
                pos_limit_hit   = 0,
                error           = NULL
            WHERE is_active = 1
        """)
        logger.info(f'  bots: {conn.execute("SELECT changes()").fetchone()[0]} active bots -> Scanning')

        conn.commit()
        logger.info('  DB committed OK')

    except Exception as dbe:
        logger.error(f'DB reset FAILED: {dbe}')
        conn.rollback()
        sys.exit(1)

# ─── Done ──────────────────────────────────────────────────────────────────────
logger.info('\n' + '='*55)
if DRY:
    logger.info('DRY-RUN complete. Nothing changed.')
    logger.info('Run without --dry-run to execute the reset.')
else:
    logger.info('CLEAN RESET COMPLETE')
    logger.info(f'  - {cancelled_total} exchange orders cancelled')
    logger.info(f'  - {flattened_total} positions flattened')
    logger.info('  - All DB bot state zeroed and set to Scanning')
    logger.info('  - Bot configs (base_size, params) preserved')
    logger.info('')
    logger.info('Ready for forward testing. Restart the engine.')
    logger.info('Startup will run seal_all_active_bots() for a clean baseline.')
logger.info('='*55)
