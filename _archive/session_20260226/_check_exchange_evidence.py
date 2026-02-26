import logging
from engine.exchange_interface import ExchangeInterface

logging.basicConfig(level=logging.INFO)
ex = ExchangeInterface()
print("Fetching closed orders for ETH/USDC:USDC...")
history = ex.fetch_closed_orders("ETH/USDC:USDC", limit=100)
for o in history:
    cid = o.get('clientOrderId', '')
    if '10013' in cid:
        print(f"FOUND 10013: {o['id']} | {cid} | status: {o.get('status')} | qty: {o.get('amount')} | price: {o.get('price')}")
print("Fetching closed orders for BTC/USDC:USDC...")
history_btc = ex.fetch_closed_orders("BTC/USDC:USDC", limit=100)
for o in history_btc:
    cid = o.get('clientOrderId', '')
    if '10012' in cid:
        print(f"FOUND 10012: {o['id']} | {cid} | status: {o.get('status')} | qty: {o.get('amount')} | price: {o.get('price')}")
