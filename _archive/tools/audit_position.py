import os
import sys
import logging
import ccxt
import pandas as pd
from datetime import datetime

# Add root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings

# Setup simple logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("Audit")

def audit_position(symbol="BTC/USDC", target_size=0.151):
    api_key = settings.config.API_KEY
    api_secret = settings.config.API_SECRET
    testnet = settings.config.TESTNET
    
    logger.info(f"🔍 AUDIT: Analyzing {symbol} on {'TESTNET' if testnet else 'MAINNET'}...")
    logger.info(f"🎯 Target Position Size: {target_size}")

    try:
        exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'options': {'defaultType': 'future'}
        })
        if testnet:
            exchange.set_sandbox_mode(True)
            
        # Fetch Trades
        logger.info("📡 Fetching recent trades from Exchange...")
        trades = exchange.fetch_my_trades(symbol, limit=50) # Last 50 should be enough for a martingale cycle
        
        # Sort desc (newest first)
        trades.sort(key=lambda x: x['timestamp'], reverse=True)
        
        running_qty = 0.0
        audit_trail = []
        
        logger.info("-" * 60)
        logger.info(f"{'TIME':<20} | {'SIDE':<4} | {'PRICE':<10} | {'QTY':<8} | {'ROLE':<6}")
        logger.info("-" * 60)

        # Walk back to reconstruct the "Ladder"
        for t in trades:
            # We assume the position is SHORT, so we look for SELLS that built this position
            # (ignoring buys which are closures/TPs unless they are partials? 
            #  Actually, let's just look for the open sequence of SELLS)
            
            # Simple heuristic: Accumulate SELLS until we hit the target size.
            # Any BUYS encountered reset the count? No, that's complex.
            # Let's just find the last N sales that sum to ~0.151
            
            if t['side'].upper() == 'SELL':
                qty = float(t['amount'])
                price = float(t['price'])
                time_str = datetime.fromtimestamp(t['timestamp']/1000).strftime('%Y-%m-%d %H:%M:%S')
                
                running_qty += qty
                audit_trail.append(t)
                
                logger.info(f"{time_str:<20} | {t['side'].upper():<4} | {price:<10.2f} | {qty:<8.3f} | {t['maker'] or 'Taker'}")
                
                if running_qty >= (target_size * 0.99): # 1% tolerance
                    logger.info("-" * 60)
                    logger.info(f"✅ MATCH FOUND: Accumulated {running_qty:.3f} matches target {target_size}")
                    break
        
        if running_qty < (target_size * 0.99):
             logger.warning(f"⚠️ WARNING: Could not find enough recent SELLS to match {target_size}. Found {running_qty:.3f}.")
             return

        # Analyze Steps
        # In Martingale, each trade is a "Step".
        # Step 0 = First Trade
        # Step 1 = Second Trade
        # ...
        # Step Count = len(audit_trail) - 1
        
        step_count = len(audit_trail) - 1
        avg_entry = sum(float(t['price']) * float(t['amount']) for t in audit_trail) / running_qty
        
        logger.info(f"\n📊 AUDIT RESULTS:")
        logger.info(f"   • Total Trades: {len(audit_trail)}")
        logger.info(f"   • Calculated Step: {step_count}")
        logger.info(f"   • Weighted Avg Entry: {avg_entry:.2f}")
        logger.info(f"\n📜 PROVENANCE (Verify this against Bank/Exchange):")
        for i, t in enumerate(reversed(audit_trail)):
             print(f"   Step {i}: {t['amount']} @ {t['price']} ({datetime.fromtimestamp(t['timestamp']/1000)})")

        print(f"\nRECOMMENDED ACTION: UPDATE trades SET current_step={step_count}, avg_entry_price={avg_entry} WHERE bot_id=10011;")

    except Exception as e:
        logger.error(f"Audit failed: {e}")

if __name__ == "__main__":
    audit_position()
