import logging
from engine.exchange_interface import ExchangeInterface
from config.settings import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Diagnostic")

def run_diagnostic():
    logger.info(f"Config: TESTNET={config.TESTNET}, DEMO={config.DEMO_TRADING}")
    logger.info(f"API Key: {config.API_KEY[:5]}...")
    
    ex = ExchangeInterface(market_type='future')
    
    logger.info("Fetching raw account data...")
    res = ex._raw_request('/fapi/v2/account')
    
    if res:
        logger.info("Success! Found account data.")
        positions = res.get('positions', [])
        active_pos = [p for p in positions if float(p.get('positionAmt', 0)) != 0]
        
        logger.info(f"Total positions on exchange: {len(positions)}")
        logger.info(f"Active positions found: {len(active_pos)}")
        
        for p in active_pos:
            logger.info(f"POS: {p['symbol']} | Amt: {p['positionAmt']} | Unrealized: {p['unrealizedProfit']}")
    else:
        logger.error("Failed to fetch account data.")

if __name__ == '__main__':
    run_diagnostic()
