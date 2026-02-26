import logging
from engine.exchange_interface import ExchangeInterface
from engine.reconciler import normalize_symbol

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("CheckPos")

def main():
    ex = ExchangeInterface(market_type='future')
    pos = ex.fetch_positions()
    logger.info(f"raw positions count: {len(pos) if pos else 0}")
    if pos:
        for p in pos:
            size = float(p.get('contracts', 0) or p.get('size', 0))
            if abs(size) > 0:
                logger.info(f"ACTIVE POSITION: {p.get('symbol')} | Size: {size}")

if __name__ == "__main__":
    main()
