import time
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from engine.exchange_interface import ExchangeInterface

def investigate():
    ex = ExchangeInterface()
    since_ts = int((time.time() - (86400 * 3)) * 1000) # last 3 days
    
    # 10017 -> XRP/USDC
    # 10018 -> SUI/USDC
    # Target gap SUI: $554.09
    # Target gap XRP: $397.63
    
    pairs = [
        ('XRP/USDC:USDC', '10017'),
        ('SUI/USDC:USDC', '10018')
    ]
    
    for pair, bot_id in pairs:
        print(f"\nFETCHING {pair} (Bot {bot_id})...")
        try:
            # fetch up to 1000
            fills = ex.fetch_closed_orders(pair, since=since_ts, limit=1000)
            
            bot_fills = []
            for o in fills:
                cid = o.get('clientOrderId', '')
                if f"CQB_{bot_id}_" in cid and o['status'] == 'filled':
                    cost = o.get('cost') or (float(o['price']) * float(o['amount']))
                    bot_fills.append({'ts': o['timestamp'], 'cid': cid, 'type': o['type'], 'side': o['side'], 'price': o['price'], 'qty': o['amount'], 'cost': cost})
                    
            print(f"Found {len(bot_fills)} actual fills for {pair}")
            
            # Sort by timestamp
            bot_fills.sort(key=lambda x: x['ts'])
            
            # Print last 5 fills to see what might have caused the discrepancy
            print("LAST 5 FILLS FOR THIS BOT:")
            for f in bot_fills[-5:]:
                print(f"  {f['ts']} | {f['cid']} | {f['side']} | Amt: {f['qty']} | Cost: ${f['cost']:.2f}")
                
        except Exception as e:
            print(f"Error fetching {pair}: {e}")

if __name__ == "__main__":
    investigate()
