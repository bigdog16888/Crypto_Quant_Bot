import logging
from engine.exchange_interface import ExchangeInterface

logging.basicConfig(level=logging.DEBUG)

def test_order():
    exchange = ExchangeInterface()
    try:
        # Simulating TP order for BTC
        order = exchange.create_order(
            symbol='BTC/USDC',
            type='limit',
            side='sell',
            amount=0.015,
            price=66000.5,
            params={'clientOrderId': 'TEST_TP_1', 'reduceOnly': True}
        )
        print("Success TP:", order)
    except Exception as e:
        print("Failed TP:", e)

    try:
        # Simulating Grid order for BTC
        order2 = exchange.create_order(
            symbol='BTC/USDC',
            type='limit',
            side='buy',
            amount=0.015,
            price=62000.5,
            params={'clientOrderId': 'TEST_GRID_1'}
        )
        print("Success Grid:", order2)
    except Exception as e:
        print("Failed Grid:", e)

if __name__ == "__main__":
    test_order()
