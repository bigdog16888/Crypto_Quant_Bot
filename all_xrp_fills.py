import time
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from engine.exchange_interface import ExchangeInterface

def investigate():
    ex = ExchangeInterface()
    # Look back 7 days
    since_ts = int((time.time() - (86400 * 7)) * 1000)
    
    print(f"\nFETCHING ALL XRP fills (bot 10017, last 7 days)...")
    try:
        fills = ex.fetch_closed_orders('XRP/USDC:USDC', since=since_ts, limit=1000)
        print(f"Total exchange orders returned: {len(fills)}")
        
        bot_fills = []
        for o in fills:
            cid = o.get('clientOrderId', '')
            if 'CQB_10017_' in cid and o['status'] == 'filled':
                cost = (float(o['price']) * float(o['amount']))
                bot_fills.append({
                    'ts': o['timestamp'], 'cid': cid, 
                    'side': o['side'], 'qty': o['amount'], 
                    'price': o['price'], 'cost': cost
                })
        
        bot_fills.sort(key=lambda x: x['ts'])
        print(f"\nALL actual fills for bot 10017 ({len(bot_fills)} total):")
        total = 0
        for f in bot_fills:
            if f['side'] == 'buy':
                total += f['cost']
                prefix = "+"
            else:
                total -= f['cost']
                prefix = "-"
            print(f"  {prefix}${f['cost']:.2f}  | {f['cid']} | qty={f['qty']} @ {f['price']}")
        print(f"\nNet position cost (buys - sells): ${total:.2f}")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    investigate()
