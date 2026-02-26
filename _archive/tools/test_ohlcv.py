
import os
import sys
import logging
from engine.exchange_interface import ExchangeInterface

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_fetch_ohlcv():
    try:
        # Initialize Exchange
        exchange = ExchangeInterface()
        if not exchange.exchange:
            logger.error("Failed to init exchange")
            return

        pair = "BTC/USDC"
        timeframe = "15m"
        
        logger.info(f"Fetching OHLCV for {pair} {timeframe}...")
        
        # 1. Fetch
        ohlcv = exchange.fetch_ohlcv(pair, timeframe)
        
        if ohlcv is None: 
            logger.error("Result is None")
        elif ohlcv.empty:
            logger.error("Result is Empty DataFrame")
        else:
            logger.info(f"SUCCESS: Got {len(ohlcv)} candles.")
            latest = ohlcv.iloc[-1]
            logger.info(f"Latest Close: {latest['close']} @ {latest['timestamp']}")

    except Exception as e:
        logger.error(f"Exception: {e}")

if __name__ == "__main__":
    test_fetch_ohlcv()
