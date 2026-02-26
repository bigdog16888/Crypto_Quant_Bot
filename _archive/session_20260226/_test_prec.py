import logging
from engine.exchange_interface import ExchangeInterface

logging.basicConfig(level=logging.DEBUG)

def test_prec():
    exchange = ExchangeInterface()
    print("BTC/USDT:", exchange.get_symbol_precision('BTC/USDT'))
    print("BTC/USDC:", exchange.get_symbol_precision('BTC/USDC'))
    print("ETH/USDC:", exchange.get_symbol_precision('ETH/USDC'))

if __name__ == "__main__":
    test_prec()
