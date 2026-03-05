"""Close remaining BNB position at correct step size."""
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))

from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface()
positions = ex.fetch_positions()
print("Open positions found:")
closed = 0
for p in positions:
    size = float(p.get('contracts', 0) or 0)
    if abs(size) < 0.0001:
        continue
    symbol = p['symbol']
    side = p.get('side','').upper()
    close_side = 'sell' if side == 'LONG' else 'buy'
    
    prec = ex.get_symbol_precision(symbol)
    step = prec['step_size']
    
    # Round using ceil (up) to clear step-size precision constraint
    close_amount = ex.ceil_to_step(abs(size), step)
    
    print(f"  {symbol}: {side} size={abs(size)} step={step} -> closing {close_amount} via {close_side.upper()}")
    ex.cancel_all_orders(symbol)
    time.sleep(0.4)
    try:
        ex.create_order(symbol, 'MARKET', close_side, close_amount)
        print(f"  ✅ Closed {symbol}")
        closed += 1
    except Exception as e:
        print(f"  ❌ {symbol}: {e}")
    time.sleep(0.5)

if closed == 0 and not any(abs(float(p.get('contracts',0)))>0.0001 for p in positions):
    print("Already flat - no open positions.")
else:
    print(f"Closed {closed} position(s).")
