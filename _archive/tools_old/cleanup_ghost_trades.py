"""
Ghost Trade Cleanup Utility
Removes ghost trades from database (trades table entries with no actual position on exchange)
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection, reset_bot_after_tp
from engine.exchange_interface import ExchangeInterface
from config.settings import config
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GhostCleanup")

def cleanup_ghost_trades():
    """
    Identify and clean up ghost trades:
    - Bots in trades table with $0 invested
    - Bots in trades table with no corresponding exchange position
    """
    print("=" * 80)
    print("GHOST TRADE CLEANUP")
    print("=" * 80)
    
    # Get database state
    conn = get_connection()
    cursor = conn.cursor()
    
    # Find ghost trades (in DB but no position)
    cursor.execute("""
        SELECT bot_id, total_invested, avg_entry_price 
        FROM trades
    """)
    db_trades = cursor.fetchall()
    
    print(f"\nFound {len(db_trades)} trades in database")
    
    # Initialize exchange
    try:
        exchange = ExchangeInterface(market_type=config.MARKET_TYPE)
        positions = exchange.fetch_positions()
        open_positions = {p.get('symbol'): p for p in positions if float(p.get('contracts', 0)) != 0}
        print(f"Found {len(open_positions)} open positions on exchange")
    except Exception as e:
        print(f"❌ Failed to connect to exchange: {e}")
        return
    
    # Get bot pairs
    ghosts = []
    for bot_id, invested, avg_price in db_trades:
        cursor.execute("SELECT pair FROM bots WHERE id = ?", (bot_id,))
        row = cursor.fetchone()
        if not row:
            continue
        
        pair = row[0]
        
        # Check if this is a ghost trade
        is_ghost = False
        reason = ""
        
        # Case 1: $0 invested
        if invested == 0:
            is_ghost = True
            reason = "Zero investment"
        
        # Case 2: No position on exchange
        elif pair not in open_positions:
            is_ghost = True
            reason = "No exchange position"
        
        if is_ghost:
            ghosts.append((bot_id, pair, invested, reason))
    
    print(f"\n🔍 Found {len(ghosts)} GHOST TRADES:")
    for bot_id, pair, invested, reason in ghosts:
        print(f"  Bot {bot_id}: {pair} | Invested=${invested:.2f} | Reason: {reason}")
    
    if not ghosts:
        print("\n✅ No ghost trades found!")
        return
    
    # Cleanup
    print(f"\n🧹 Cleaning up {len(ghosts)} ghost trades...")
    cleaned = 0
    for bot_id, pair, invested, reason in ghosts:
        try:
            # Reset the bot
            cursor.execute("DELETE FROM trades WHERE bot_id = ?", (bot_id,))
            
            # Also clean up ownership state
            cursor.execute("""
                UPDATE bot_ownership_state 
                SET state = 'idle', is_owner = 0, position_size = 0
                WHERE bot_id = ?
            """, (bot_id,))
            
            print(f"  ✅ Cleaned Bot {bot_id} ({reason})")
            cleaned += 1
        except Exception as e:
            print(f"  ❌ Failed to clean Bot {bot_id}: {e}")
    
    conn.commit()
    conn.close()
    
    print(f"\n✅ Cleanup complete! Removed {cleaned}/{len(ghosts)} ghost trades")
    print("=" * 80)

if __name__ == "__main__":
    cleanup_ghost_trades()
