from engine.oneway_netting import reconcile_oneway_pair_open_qty
from engine.exchange_interface import ExchangeInterface
import logging

logging.basicConfig(level=logging.INFO)

try:
    exchange = ExchangeInterface(market_type='future')
    res = reconcile_oneway_pair_open_qty(exchange, 'SUI/USDC:USDC')
    print("Reconcile check result for SUI/USDC:USDC:", res)
except Exception as e:
    print("Reconcile check error:", e)
