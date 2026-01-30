
import logging
from engine.exchange_interface import ExchangeInterface

logging.basicConfig(level=logging.INFO)

def check_limits():
    ex = ExchangeInterface(market_type='future', validate=False)
    pair = 'BTC/USDC'
    
    print(f"--- Checking Market Limits for {pair} ---")
    try:
        ex._ensure_markets()
        market = ex.exchange.market(pair)
        limits = market.get('limits', {})
        info = market.get('info', {})
        
        print(f"Limits: {limits}")
        print(f"Binance Info Filters: {info.get('filters', [])}")
        
        # logical check
        cost_min = limits.get('cost', {}).get('min')
        print(f"CCXT Cost Min: {cost_min}")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_limits()
