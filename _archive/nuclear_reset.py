"""
NUCLEAR RESET SCRIPT
Closes ALL positions, Cancels ALL orders, Resets ALL DB state.
Use with extreme caution.
"""
import sys
import os
import logging
import sqlite3
import time

# Add root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def nuclear_reset():
    logger.info("☢️ INITIATING NUCLEAR RESET ☢️")
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Get all active pairs
    cursor.execute("SELECT DISTINCT pair FROM bots WHERE is_active=1")
    pairs = [row[0] for row in cursor.fetchall()]
    logger.info(f"Target pairs: {pairs}")
    
    # 2. Initialize Exchanges
    # We need both swap (crypto) and future (gold/indices) if configured
    exchanges = {}
    try:
        exchanges['swap'] = ExchangeInterface(market_type='swap')
        logger.info("Initialized Swap Exchange")
    except Exception as e:
        logger.error(f"Failed to init Swap Exchange: {e}")

    try:
        exchanges['future'] = ExchangeInterface(market_type='future')
        logger.info("Initialized Future Exchange")
    except Exception as e:
        logger.error(f"Failed to init Future Exchange: {e}")
        
    if not exchanges:
        logger.error("No exchanges available! Aborting.")
        return

    # 3. Close Positions and Cancel Orders
    for pair in pairs:
        market_type = 'swap' # Default assumption
        if 'XAU' in pair or 'USD' not in pair: # Rough heuristic for commodities/indices
             market_type = 'future'
        
        ex = exchanges.get(market_type)
        if not ex: continue
        
        symbol = pair.split(':')[0] # Clean symbol if needed
        
        logger.info(f"Processing {pair} ({symbol})...")
        
        # Cancel Orders
        try:
            orders = ex.fetch_open_orders(symbol)
            if orders:
                logger.info(f"  Cancelling {len(orders)} orders...")
                for o in orders:
                    ex.cancel_order(o['id'], symbol)
        except Exception as e:
            logger.error(f"  ❌ Cancel failed: {e}")
            
        # Close Positions
        try:
            positions = ex.fetch_positions()
            # Filter for this symbol
            target_pos = None
            for p in positions:
                # BingX returns specific format, match symbol
                if p.get('symbol') == symbol:
                    target_pos = p
                    break
            
            if target_pos and float(target_pos.get('contracts', 0)) != 0:
                contracts = abs(float(target_pos.get('contracts')))
                side = target_pos.get('side', '').lower() # 'long' or 'short'
                
                # Close is opposite side
                close_side = 'sell' if side == 'long' else 'buy'
                
                logger.info(f"  Found position: {contracts} {side}. Closing...")
                ex.create_order(
                    symbol=symbol,
                    type='market',
                    side=close_side,
                    amount=contracts,
                    params={'reduceOnly': True}
                )
                logger.info("  ✅ Position closed.")
            else:
                logger.info("  No open position.")
                
        except Exception as e:
            logger.error(f"  ❌ Close position failed: {e}")

    # 4. Wipe Database State
    logger.info("🧹 Wiping Database State...")
    try:
        # Reset Trades
        cursor.execute("DELETE FROM trades") # Or update to 0 if we want to keep ID mapping? 
        # Actually better to reset values to 0 to keep 1:1 mapping with bots table
        cursor.execute("DELETE FROM trades")
        cursor.execute("INSERT INTO trades (bot_id) SELECT id FROM bots") # Re-init empty trades
        
        # Clear Orders
        cursor.execute("DELETE FROM bot_orders")
        
        # Clear Ownership
        cursor.execute("DELETE FROM bot_ownership_state")
        cursor.execute("DELETE FROM bot_ownership_history")
        cursor.execute("DELETE FROM active_positions")
        
        # Reset Bots Status
        cursor.execute("UPDATE bots SET status='Active'")
        
        conn.commit()
        logger.info("✅ Database Wiped & Reset.")
        
    except Exception as e:
        logger.error(f"❌ DB Reset failed: {e}")
        conn.rollback()

    logger.info("☢️ NUCLEAR RESET COMPLETE ☢️")

if __name__ == "__main__":
    nuclear_reset()
