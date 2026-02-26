import ccxt
from config.settings import config
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FlattenCCXT")

def flatten():
    exchange = ccxt.binance({
        'apiKey': config.API_KEY,
        'secret': config.API_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
    
    if config.TESTNET or config.DEMO_TRADING:
        base_url = 'https://demo-fapi.binance.com'
        exchange.urls['api']['fapiPublic'] = f"{base_url}/fapi/v1"
        exchange.urls['api']['fapiPrivate'] = f"{base_url}/fapi/v1"
        exchange.urls['api']['fapi'] = base_url
        
    try:
        positions = exchange.fetch_positions()
        for p in positions:
            amt = float(p.get('positionAmt', 0))
            if amt != 0:
                sym = p['symbol']
                logger.info(f"Closing {sym} amt {amt}")
                side = 'buy' if amt < 0 else 'sell'
                try:
                    exchange.create_market_order(sym, side, abs(amt))
                    logger.info(f"Closed {sym}")
                except Exception as e:
                    logger.error(f"Failed to close {sym}: {e}")
    except Exception as e:
        logger.error(f"Failed to fetch positions: {e}")

if __name__ == '__main__':
    flatten()
