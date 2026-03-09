import json

with open('mismatch_debug_dump.json', 'r') as f:
    data = json.load(f)

for bot_id, info in data.items():
    print(f"\n==== {bot_id.upper()} ====")
    trade = info.get('trade', {})
    print(f"Trade Total Invested (DB): ${trade.get('total_invested')}")
    print(f"Trade Step (DB): {trade.get('current_step')}")
    print(f"Cycle ID: {trade.get('cycle_id')}")
    
    orders = info.get('orders', [])
    calc_invested = 0.0
    for o in orders:
        if o['order_type'] == 'entry' and o['status'] == 'filled':
            price = float(o['price'] or 0)
            amt = float(o['filled_amount'] or o['amount'] or 0)
            calc_invested += (price * amt)
            print(f"  [FILLED] Order {o['order_id']} | Step {o['step']} | Price: {price} | Amt: {amt} | Value: ${price*amt:.2f}")
    
    print(f"Calculated Total Invested: ${calc_invested:.2f}")

    if bot_id == 'bot_10017':
        print(f"User reported Exchange XRP is $607.72. Gap: ${607.72 - calc_invested:.2f}")
    if bot_id == 'bot_10018':
        print(f"User reported Exchange SUI is $803.63. Gap: ${803.63 - calc_invested:.2f}")
