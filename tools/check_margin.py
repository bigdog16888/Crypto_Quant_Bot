import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.exchange_interface import ExchangeInterface
from config.settings import config

def check():
    ex = ExchangeInterface(market_type='future')
    # Check Position Mode
    try:
        mode = ex.exchange.fapiPrivateGetPositionSideDual()
        print(f"Hedge Mode (Dual Side): {mode.get('dualSidePosition')}")
    except Exception as e:
        print(f"Could not fetch position mode: {e}")

    balance = ex.fetch_balance()
    print("Balances:")
    for asset, info in balance.get('total', {}).items():
        if info > 0:
            print(f"  {asset}: {info}")
    
    # Check BTC/USDC info
    pair = 'BTC/USDC'
    ex.exchange.load_markets() # This line is redundant as load_markets is called again below, but keeping it as per instruction.
    # BTC/USDC:USDC or BTC/USDT:USDT
    symbol = "BTC/USDT:USDT" if config.API_KEY.startswith('binance') else "BTC/USDC:USDC"
    print(f"Checking {symbol} markets...")
    try:
        ex.exchange.load_markets()
        market = ex.exchange.market(symbol)
        print(f"Min Amount: {market['limits']['amount']['min']}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check()
