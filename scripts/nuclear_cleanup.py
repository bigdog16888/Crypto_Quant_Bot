"""
NUCLEAR CLEANUP: Cancel ALL exchange orders, reset ALL phantom bot states.
Run ONLY when bot runner is STOPPED.
"""
import sys, os, sqlite3, logging, time
from datetime import datetime

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

try:
    from config.settings import config
    from engine.exchange_interface import ExchangeInterface
except ImportError as e:
    logger.error(f"Import failed: {e}")
    sys.exit(1)

DB_PATH = os.path.join(project_root, 'crypto_bot.db')

# Bots to clean
PHANTOM_BOTS = [32, 33, 34, 35, 36, 37, 38, 39, 41, 43]  # BTC/USDC passengers with phantom state
ACTIVE_BOTS = [44]  # XAU/USDT - in trade but with rogue orders
ALL_BOTS = PHANTOM_BOTS + ACTIVE_BOTS

# Pairs to cancel orders on
PAIRS_TO_CLEAN = ['XAU/USDT:USDT', 'BTC/USDC:USDC']


def cancel_all_exchange_orders(exchange):
    """Cancel ALL open orders on all pairs we trade."""
    total_cancelled = 0
    
    for pair in PAIRS_TO_CLEAN:
        print(f"\n--- Checking open orders for {pair} ---")
        try:
            open_orders = exchange.fetch_open_orders(pair, force_refresh=True)
            if not open_orders:
                print(f"  No open orders on {pair}")
                continue
            
            print(f"  Found {len(open_orders)} open orders on {pair}:")
            for o in open_orders:
                cid = o.get('clientOrderId', '?')
                oid = o.get('id', '?')
                price = o.get('price', '?')
                side = o.get('side', '?')
                amount = o.get('remaining', o.get('amount', '?'))
                print(f"    [{side}] {cid} | id={oid} | price={price} | qty={amount}")
            
            # Cancel each order individually for reliability
            for o in open_orders:
                oid = o.get('id')
                cid = o.get('clientOrderId', '?')
                try:
                    exchange.exchange.cancel_order(oid, pair)
                    print(f"    ✅ Cancelled: {cid} ({oid})")
                    total_cancelled += 1
                except Exception as e:
                    print(f"    ❌ Cancel failed for {cid}: {e}")
                time.sleep(0.1)  # Rate limit
                
        except Exception as e:
            print(f"  ❌ Error fetching orders for {pair}: {e}")
    
    return total_cancelled


def reset_database():
    """Reset all phantom/rogue bot states in the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # --- PHANTOM BOTS (32-39, 41, 43): Reset to clean state ---
        print(f"\n--- Resetting PHANTOM bots: {PHANTOM_BOTS} ---")
        for bot_id in PHANTOM_BOTS:
            cursor.execute("""
                UPDATE trades SET 
                    current_step = 0, 
                    total_invested = 0, 
                    avg_entry_price = 0, 
                    target_tp_price = 0,
                    entry_confirmed = 0,
                    basket_start_time = 0,
                    entry_order_id = NULL, 
                    tp_order_id = NULL,
                    bot_position_id = NULL,
                    close_type = 'NUCLEAR_CLEANUP'
                WHERE bot_id = ?
            """, (bot_id,))
            cursor.execute("UPDATE bots SET status = 'Waiting for Signal' WHERE id = ?", (bot_id,))
            print(f"  ✅ Bot {bot_id}: trades zeroed, status='Waiting for Signal'")
        
        # --- ACTIVE BOT 44: Full reset ---
        print(f"\n--- Resetting ACTIVE bot 44 ---")
        cursor.execute("""
            UPDATE trades SET 
                current_step = 0, 
                total_invested = 0, 
                avg_entry_price = 0, 
                target_tp_price = 0,
                entry_confirmed = 0,
                basket_start_time = 0,
                entry_order_id = NULL, 
                tp_order_id = NULL,
                bot_position_id = NULL,
                close_type = 'NUCLEAR_CLEANUP'
            WHERE bot_id = 44
        """)
        cursor.execute("UPDATE bots SET status = 'Waiting for Signal' WHERE id = 44", )
        print(f"  ✅ Bot 44: trades zeroed, status='Waiting for Signal'")
        
        # --- Clean bot_orders table for ALL bots ---
        print(f"\n--- Cleaning bot_orders table ---")
        for bot_id in ALL_BOTS:
            cursor.execute("UPDATE bot_orders SET status = 'cancelled' WHERE bot_id = ? AND status = 'open'", (bot_id,))
            affected = cursor.rowcount
            if affected > 0:
                print(f"  ✅ Bot {bot_id}: {affected} open orders marked cancelled")
        
        # --- Clean ownership state ---
        print(f"\n--- Cleaning ownership state ---")
        for bot_id in ALL_BOTS:
            cursor.execute("""
                UPDATE bot_ownership_state SET 
                    state = 'idle', 
                    is_owner = 0, 
                    position_size = 0, 
                    avg_entry_price = 0, 
                    target_tp_price = 0,
                    entry_order_id = NULL, 
                    tp_order_id = NULL, 
                    owner_id = NULL
                WHERE bot_id = ?
            """, (bot_id,))
            if cursor.rowcount > 0:
                print(f"  ✅ Bot {bot_id}: ownership reset to idle")
        
        # --- Clean active_positions ---
        print(f"\n--- Cleaning active_positions ---")
        for pair in ['XAU/USDT:USDT', 'BTC/USDC:USDC', 'XAU/USDT', 'BTC/USDC']:
            cursor.execute("DELETE FROM active_positions WHERE pair = ?", (pair,))
            if cursor.rowcount > 0:
                print(f"  ✅ Deleted active_position for {pair}")
        
        conn.commit()
        print(f"\n✅ DATABASE CLEANUP COMMITTED")
        
    except Exception as e:
        conn.rollback()
        print(f"\n❌ DATABASE ERROR: {e}")
        raise
    finally:
        conn.close()


def verify_state():
    """Print final state for verification."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    print(f"\n{'='*60}")
    print(f"VERIFICATION: Final Bot States")
    print(f"{'='*60}")
    
    cursor.execute("""
        SELECT b.id, b.name, b.status, t.total_invested, t.avg_entry_price, t.entry_order_id, t.tp_order_id
        FROM bots b LEFT JOIN trades t ON b.id = t.bot_id 
        ORDER BY b.id
    """)
    for r in cursor.fetchall():
        print(f"  Bot {r[0]:3d} | {r[1]:20s} | {r[2]:25s} | inv={r[3]} | eid={r[5]} | tid={r[6]}")
    
    cursor.execute("SELECT COUNT(*) FROM bot_orders WHERE status = 'open'")
    open_count = cursor.fetchone()[0]
    print(f"\n  Open orders in bot_orders: {open_count}")
    
    conn.close()


def main():
    print(f"{'='*60}")
    print(f"NUCLEAR CLEANUP - {datetime.now()}")
    print(f"{'='*60}")
    
    # Step 1: Cancel exchange orders
    print(f"\n[STEP 1] CANCEL ALL EXCHANGE ORDERS")
    try:
        exchange = ExchangeInterface(market_type='future')
        cancelled = cancel_all_exchange_orders(exchange)
        print(f"\n  Total cancelled: {cancelled}")
    except Exception as e:
        print(f"\n  ❌ Exchange connection failed: {e}")
        print(f"  Proceeding to DB cleanup anyway...")
    
    # Step 2: Reset database
    print(f"\n[STEP 2] RESET DATABASE")
    reset_database()
    
    # Step 3: Verify
    verify_state()
    
    print(f"\n{'='*60}")
    print(f"NUCLEAR CLEANUP COMPLETE")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
