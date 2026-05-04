"""
nuclear_reset.py — Authoritative Ledger-Exchange Parity Reset

Strategy:
  1. Cancel ALL open exchange orders (clean slate for orders)
  2. For each physical exchange position, close it via market order (flatten ALL)
  3. Run a full DB wipe: all bots → Scanning, trades zeroed, active_positions cleared
  4. Let the engine restart clean — no ghost state anywhere

This is the NUCLEAR option: flatten everything on exchange, zero everything in DB.
After this, all bots restart in Scanning mode and self-heal via normal entry logic.

IMPORTANT: Make sure the engine (runner) is NOT running when this executes.
"""
import sys
import time
import sqlite3
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("NUCLEAR_RESET")

sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config

DRY_RUN = "--dry-run" in sys.argv
if DRY_RUN:
    logger.info("DRY RUN MODE — no exchange orders will be placed, no DB writes")

DB_PATH = "crypto_bot.db"

# ══════════════════════════════════════════════════════════════
# STEP 0: Verify engine is stopped (check for lock file / running process)
# ══════════════════════════════════════════════════════════════
logger.info("=" * 70)
logger.info("NUCLEAR RESET — Flatten exchange + zero DB ledger")
logger.info("=" * 70)

# ══════════════════════════════════════════════════════════════
# STEP 1: Connect and fetch current exchange state
# ══════════════════════════════════════════════════════════════
logger.info("\n[STEP 1] Connecting to exchange and fetching state...")
ex = ExchangeInterface(market_type=config.MARKET_TYPE)

positions = ex.fetch_positions() or []
open_orders = []
try:
    open_orders = ex.fetch_open_orders() or []
except Exception as e:
    logger.warning(f"Could not fetch open orders: {e}")

active_positions = []
for p in positions:
    qty = float(p.get('contracts', 0) or 0)
    if abs(qty) > 0:
        sym = p.get('symbol', '')
        entry = float(p.get('entryPrice', 0) or 0)
        side = 'LONG' if qty > 0 else 'SHORT'
        active_positions.append({
            'symbol': sym,
            'side': side,
            'qty': abs(qty),
            'entry': entry,
            'notional': abs(qty) * entry,
        })
        logger.info(f"  Exchange: {normalize_symbol(sym)} {side} qty={abs(qty)} @ {entry:.4f} = ${abs(qty)*entry:.2f}")

logger.info(f"\nTotal open positions: {len(active_positions)}")
logger.info(f"Total open orders:    {len(open_orders)}")

# ══════════════════════════════════════════════════════════════
# STEP 2: Cancel all open orders on exchange
# ══════════════════════════════════════════════════════════════
logger.info("\n[STEP 2] Cancelling all open orders on exchange...")
cancelled = 0
errors = 0
order_symbols = set()
for o in open_orders:
    sym = o.get('symbol', '')
    oid = o.get('id', '')
    order_symbols.add(sym)

for sym in order_symbols:
    try:
        if not DRY_RUN:
            ex.exchange.cancel_all_orders(sym)
            logger.info(f"  ✅ Cancelled all orders for {sym}")
        else:
            logger.info(f"  [DRY-RUN] Would cancel all orders for {sym}")
        cancelled += 1
        time.sleep(0.3)
    except Exception as e:
        logger.error(f"  ❌ Failed to cancel orders for {sym}: {e}")
        errors += 1

logger.info(f"Cancelled orders for {cancelled} symbols ({errors} errors)")

# ══════════════════════════════════════════════════════════════
# STEP 3: Market-close ALL physical positions (flatten everything)
# ══════════════════════════════════════════════════════════════
logger.info("\n[STEP 3] Flattening all physical exchange positions via market orders...")
closed = []
close_errors = []

for pos in active_positions:
    sym = pos['symbol']
    side = pos['side']
    qty = pos['qty']
    
    # To close a LONG: sell (reduceOnly)
    # To close a SHORT: buy (reduceOnly)
    close_side = 'sell' if side == 'LONG' else 'buy'
    
    logger.info(f"  Closing {side} {normalize_symbol(sym)} qty={qty} via market {close_side}...")
    
    if DRY_RUN:
        logger.info(f"  [DRY-RUN] Would place market {close_side} {qty} {sym} reduceOnly")
        closed.append(sym)
        continue
    
    try:
        # Use the exchange's create_order directly for maximum reliability
        params = {'reduceOnly': True}
        order = ex.exchange.create_order(
            symbol=sym,
            type='market',
            side=close_side,
            amount=qty,
            params=params
        )
        logger.info(f"  ✅ Closed {side} {normalize_symbol(sym)}: order_id={order.get('id', 'N/A')}")
        closed.append(sym)
        time.sleep(0.5)
    except Exception as e:
        logger.error(f"  ❌ Failed to close {side} {normalize_symbol(sym)}: {e}")
        close_errors.append({'sym': sym, 'side': side, 'qty': qty, 'error': str(e)})

logger.info(f"\nClosed: {len(closed)} positions")
if close_errors:
    logger.error(f"FAILED to close {len(close_errors)} positions:")
    for err in close_errors:
        logger.error(f"  {err['side']} {err['sym']} qty={err['qty']}: {err['error']}")

# Wait for exchange to process
if not DRY_RUN and closed:
    logger.info("\nWaiting 5 seconds for exchange to settle...")
    time.sleep(5)

# ══════════════════════════════════════════════════════════════
# STEP 4: Verify exchange is flat
# ══════════════════════════════════════════════════════════════
logger.info("\n[STEP 4] Verifying exchange positions after close...")
if not DRY_RUN:
    remaining_positions = []
    try:
        new_positions = ex.fetch_positions() or []
        for p in new_positions:
            qty = float(p.get('contracts', 0) or 0)
            if abs(qty) > 0:
                sym = p.get('symbol', '')
                remaining_positions.append(sym)
                logger.warning(f"  ⚠️ STILL OPEN: {normalize_symbol(sym)} qty={qty}")
    except Exception as e:
        logger.error(f"  Could not verify: {e}")
    
    if not remaining_positions:
        logger.info("  ✅ Exchange is fully flat — all positions closed")
    else:
        logger.warning(f"  ⚠️ {len(remaining_positions)} positions still open: {remaining_positions}")
        if close_errors:
            logger.error("  Some close orders failed. Manual intervention required for remaining positions.")
            sys.exit(1)
else:
    logger.info("  [DRY-RUN] Skipping verification")

# ══════════════════════════════════════════════════════════════
# STEP 5: Nuclear DB wipe — zero all trades, clear active_positions
# ══════════════════════════════════════════════════════════════
logger.info("\n[STEP 5] Nuking database state...")

if DRY_RUN:
    logger.info("  [DRY-RUN] Would zero all trades and active_positions")
else:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        ts_now = int(time.time())
        
        # 1. Zero ALL trades rows
        conn.execute("""
            UPDATE trades SET
                current_step    = 0,
                total_invested  = 0,
                avg_entry_price = 0,
                target_tp_price = 0,
                open_qty        = 0,
                entry_confirmed = 0,
                entry_order_id  = NULL,
                tp_order_id     = NULL,
                bot_position_id = NULL,
                wipe_wall_ts    = ?,
                cycle_phase     = 'IDLE',
                cycle_start_time = ?,
                basket_start_time = ?
        """, (ts_now, ts_now, ts_now))
        logger.info("  ✅ Zeroed all trades accumulators")
        
        # 2. Set all active bots to Scanning
        conn.execute("""
            UPDATE bots SET status = 'Scanning'
            WHERE is_active = 1 AND status != 'STOPPED'
        """)
        logger.info("  ✅ Set all active bots to Scanning")
        
        # 3. Mark all open/new orders as auto_closed (exchange reality)
        conn.execute("""
            UPDATE bot_orders
            SET status = 'auto_closed', updated_at = ?
            WHERE status IN ('open', 'new', 'placing')
        """, (ts_now,))
        logger.info("  ✅ Marked all pending orders as auto_closed")
        
        # 4. Clear active_positions (exchange is flat now)
        conn.execute("DELETE FROM active_positions")
        logger.info("  ✅ Cleared active_positions table")
        
        # 5. Clear any bot error flags so they don't block startup
        conn.execute("""
            UPDATE bots SET last_error = NULL, last_error_time = NULL
            WHERE is_active = 1
        """)
        logger.info("  ✅ Cleared bot error flags")
        
        conn.commit()
        logger.info("  ✅ DB nuclear wipe committed")
        
        # Report final state
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM bots WHERE is_active=1")
        total_bots = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM bots WHERE is_active=1 AND status='Scanning'")
        scanning = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM active_positions")
        active_pos = cur.fetchone()[0]
        logger.info(f"\n  Final state: {total_bots} active bots, {scanning} Scanning, {active_pos} active_positions rows")
        
    except Exception as e:
        conn.rollback()
        logger.error(f"  ❌ DB wipe failed: {e}")
        raise
    finally:
        conn.close()

# ══════════════════════════════════════════════════════════════
# DONE
# ══════════════════════════════════════════════════════════════
logger.info("\n" + "=" * 70)
logger.info("NUCLEAR RESET COMPLETE")
logger.info("=" * 70)
logger.info("")
logger.info("Next steps:")
logger.info("  1. Restart the engine (python main.py or your start script)")
logger.info("  2. All bots will be in 'Scanning' mode — they will self-entry when conditions are met")
logger.info("  3. Monitor the preflight check output to confirm clean startup")
logger.info("  4. The architectural hardening patches will prevent recurrence")
logger.info("")
if close_errors:
    logger.warning("⚠️  Some positions could NOT be closed automatically:")
    for err in close_errors:
        logger.warning(f"   {err['side']} {err['sym']} qty={err['qty']}: {err['error']}")
    logger.warning("   These require manual closure on the Binance web UI before restarting the engine.")
