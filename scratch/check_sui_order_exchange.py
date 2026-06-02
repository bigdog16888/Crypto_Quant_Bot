import sys
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface

def main():
    ex = ExchangeInterface()
    try:
        # DB has 'SUI/USDC:USDC' or SUI/USDT:USDT. Let's find the correct symbol.
        symbol = 'SUI/USDC:USDC'
        print(f"Fetching order 99142034 for symbol {symbol}...")
        order = ex.fetch_order('99142034', symbol)
        print("Order Details:")
        print(order)
    except Exception as e:
        print(f"Error: {e}")
        # Let's try fetching SUI/USDT:USDT too
        try:
            symbol = 'SUI/USDT:USDT'
            print(f"Fetching order 99142034 for symbol {symbol}...")
            order = ex.fetch_order('99142034', symbol)
            print("Order Details:")
            print(order)
        except Exception as e2:
            print(f"Error SUI/USDT: {e2}")

if __name__ == '__main__':
    main()
