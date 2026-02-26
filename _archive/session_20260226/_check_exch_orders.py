from engine.exchange_interface import ExchangeInterface

def get_exchange_orders():
    ex = ExchangeInterface()
    for sym in ['BTC/USDC', 'BTC/USDT', 'ETH/USDC', 'ETH/USDT']:
        orders = ex.fetch_open_orders(sym)
        if orders:
            print(f"--- OPEN ORDERS FOR {sym} ---")
            for o in orders:
                print(f"ID: {o['id']}, Side: {o['side']}, Qty: {o['amount']}, Status: {o['status']}, ClientID: {o['clientOrderId']}")

if __name__ == '__main__':
    get_exchange_orders()
