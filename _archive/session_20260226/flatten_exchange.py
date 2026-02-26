import time
from engine.exchange_interface import ExchangeInterface
from config.settings import config
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Flatten")

def flatten_all():
    exchange = ExchangeInterface()
    
    # 1. Cancel all open orders for all symbols
    logger.info("Canceling all open orders...")
    try:
        symbols = ['BTC/USDC', 'ETH/USDC', 'BTC/USDT', 'ETH/USDT']
        for sym in symbols:
            try:
                exchange.cancel_all_orders(sym)
                logger.info(f"Canceled orders for {sym}")
            except Exception as e:
                logger.warning(f"Failed to cancel {sym}: {e}")
                
        # 2. Close all positions
        logger.info("Fetching positions...")
        positions = exchange.fetch_positions()
        
        for pos in positions:
            symbol = pos['symbol']
            qty = pos.get('contracts', 0)
            if qty == 0:
                continue
            
            logger.info(f"Position on {symbol}: {qty} ({pos.get('side')})")
            
            # If position is long we sell to close, if short we buy to close
            if pos.get('side', '').lower() == 'long':
                order_side = 'sell'
            elif pos.get('side', '').lower() == 'short':
                order_side = 'buy'
            else:
                # Fallback if positionAmt is used by ccxt in some versions
                amt = pos.get('positionAmt', 0)
                if amt == 0: continue
                order_side = 'sell' if float(amt) > 0 else 'buy'
                qty = abs(float(amt))
            
            try:
                exchange.create_order(
                    symbol=symbol,
                    type='market',
                    side=order_side,
                    amount=qty
                )
                logger.info(f"Market closed {symbol} position of {qty}")
            except Exception as e:
                logger.error(f"Failed to close {symbol}: {e}")
                
        logger.info("Flattening complete.")
        
    except Exception as e:
        logger.error(f"Error during flattening: {e}")

if __name__ == '__main__':
    flatten_all()
