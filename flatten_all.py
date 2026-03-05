from config.settings import config
from engine.exchange_interface import ExchangeInterface
import ccxt

ex = ExchangeInterface('future')

try:
    positions = ex.exchange.fetch_positions()
    for p in positions:
        amt = float(p.get('positionAmt', 0) or 0)
        symbol = p.get('symbol')
        if amt != 0:
            print(f"Flattening {symbol}: {amt}")
            side = 'sell' if amt > 0 else 'buy'
            qty = abs(amt)
            ex.exchange.create_order(
                symbol=symbol,
                type='market',
                side=side,
                amount=qty,
                params={'reduceOnly': True}
            )
            print(f"  -> Successfully closed {symbol}")
            
    # Then cancel all open orders
    print("Canceling all open orders...")
    for symbol in ["BTCUSDC", "ETHUSDC", "SOLUSDC", "BNBUSDC", "XRPUSDC", "SUIUSDC"]:
        try:
            ex.exchange.cancel_all_orders(symbol)
            print(f"  -> Canceled orders for {symbol}")
        except Exception as e:
            pass
            
except Exception as e:
    print("Error flattening positions:", e)
    import traceback
    traceback.print_exc()

import sqlite3
import time

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
c.execute("DELETE FROM trades")
c.execute("UPDATE bot_orders SET status='reset_cleared' WHERE status IN ('open', 'filled', 'missing', 'closed')")
c.execute("UPDATE bots SET status='Scanning'")
conn.commit()
conn.close()
print("Database cleanly wiped and reset to Scanning.")
