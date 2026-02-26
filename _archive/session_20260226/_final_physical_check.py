from engine.exchange_interface import ExchangeInterface
import engine.config as config

try:
    ex = ExchangeInterface()
    positions = ex.fetch_positions()
    print('--- LIVE PHYSICAL POSITIONS (FINAL CHECK) ---')
    found = False
    for p in positions:
        notional = abs(float(p.get('notional', 0)))
        if notional > 1.0:
            print(f"SYMBOL: {p.get('symbol')} | NOTIONAL: ${notional:.2f} | SIZE: {p.get('size') or p.get('contracts', 0)}")
            found = True
    if not found:
        print("✅ Binance is 100% FLAT. No physical positions found.")
except Exception as e:
    print(f"Physical Verification Failed: {e}")
