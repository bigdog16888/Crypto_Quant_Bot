import ccxt
from config.settings import config
from engine.exchange_interface import ExchangeInterface

def check_symbol(symbol, market_type='spot'):
    print(f"Checking symbol {symbol} for market type {market_type} (Testnet: {config.TESTNET})...")
    try:
        ex = ExchangeInterface(market_type=market_type)
        # Try to load markets
        ex.exchange.load_markets()
        if symbol in ex.exchange.markets:
            print(f"FOUND: Symbol {symbol} found!")
        else:
            print(f"MISSING: Symbol {symbol} NOT found in available markets.")
    except Exception as e:
        print(f"Error checking symbol: {e}")

if __name__ == "__main__":
    check_symbol("0G/USDT", 'future')
    check_symbol("0G/USDT", 'spot')
