import sys
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface

def main():
    ex = ExchangeInterface()
    print("Fetching SUI/USDC trades...")
    try:
        trades = ex.fetch_my_trades('SUI/USDC:USDC', limit=100)
        print(f"Total trades fetched: {len(trades)}")
        
        # Group by order ID
        orders = {}
        for t in trades:
            oid = t['order']
            if oid not in orders:
                orders[oid] = {
                    'side': t['side'],
                    'price': t['price'],
                    'amount': 0.0,
                    'cost': 0.0,
                    'count': 0,
                    'min_time': t['timestamp'],
                    'max_time': t['timestamp']
                }
            orders[oid]['amount'] += t['amount']
            orders[oid]['cost'] += t['cost']
            orders[oid]['count'] += 1
            orders[oid]['min_time'] = min(orders[oid]['min_time'], t['timestamp'])
            orders[oid]['max_time'] = max(orders[oid]['max_time'], t['timestamp'])
            
        import datetime
        for oid, data in orders.items():
            dt_min = datetime.datetime.fromtimestamp(data['min_time']/1000.0, datetime.timezone.utc)
            dt_max = datetime.datetime.fromtimestamp(data['max_time']/1000.0, datetime.timezone.utc)
            print(f"Order: {oid} | Side: {data['side']} | Avg Price: {data['price']} | Total Qty: {data['amount']:.4f} | Total Cost: {data['cost']:.4f} | Trade Count: {data['count']} | Time: {dt_min} to {dt_max}")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    main()
