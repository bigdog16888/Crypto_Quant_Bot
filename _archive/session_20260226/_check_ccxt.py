from engine.exchange_interface import ExchangeInterface
import json

try:
    ex = ExchangeInterface('future')
    positions = ex.fetch_positions()
    print('=== CCXT LIVE POSITIONS ===')
    for p in positions:
        print(f"{p.get('symbol')} | {p.get('side')} | {p.get('contracts', 0)}")
except Exception as e:
    print(f"Error: {e}")
