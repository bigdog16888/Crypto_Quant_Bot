import logging
import time
from engine.exchange_interface import ExchangeInterface
from config.settings import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SurgicalFlatten")

def surgical_flatten():
    ex = ExchangeInterface(market_type='future')
    
    # 1. Cancel ALL orders for the target symbols
    # Use symbols from browser/diagnostic
    symbols = ['BTC/USDT', 'BTC/USDC', 'ETH/USDC', 'ETH/USDT']
    for sym in symbols:
        try:
            logger.info(f"Cancelling orders for {sym}...")
            ex.cancel_all_orders(sym)
        except Exception as e:
            logger.error(f"Failed to cancel {sym}: {e}")

    # 2. Fetch and close positions
    logger.info("Fetching active positions...")
    positions = ex.fetch_positions()
    
    if not positions:
        logger.warning("No positions found via ex.fetch_positions()!")
        # Fallback to raw check if mapping failed
        res = ex._raw_request('/fapi/v2/account')
        if res and 'positions' in res:
            for p in res['positions']:
                if float(p.get('positionAmt', 0)) != 0:
                    logger.info(f"Found RAW position: {p['symbol']} {p['positionAmt']}")
                    # Manual close
                    side = 'sell' if float(p['positionAmt']) > 0 else 'buy'
                    qty = abs(float(p['positionAmt']))
                    try:
                        ex.create_order(symbol=p['symbol'], type='market', side=side, amount=qty)
                        logger.info(f"Successfully closed {p['symbol']}")
                    except Exception as e:
                        logger.error(f"Failed to close RAW {p['symbol']}: {e}")
    else:
        for pos in positions:
            sym = pos['symbol']
            qty = abs(pos['contracts'])
            side = 'sell' if pos['side'] == 'long' else 'buy'
            
            if qty > 0:
                logger.info(f"Closing position: {sym} | qty={qty} | side={side}")
                try:
                    ex.create_order(symbol=sym, type='market', side=side, amount=qty)
                    logger.info(f"Successfully closed {sym}")
                except Exception as e:
                    logger.error(f"Failed to close {sym}: {e}")

    logger.info("Surgical Flattening complete.")

if __name__ == '__main__':
    surgical_flatten()
