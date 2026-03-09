import json

def analyze():
    with open("mismatch_debug_dump.json", "r") as f:
        data = json.load(f)

    for bot_id_str, bot_data in data.items():
        print(f"\n{'='*40}")
        print(f"BOT: {bot_id_str} ({bot_data['bot']['name']})")
        trade = bot_data['trade']
        print(f"Trade DB Total Invested: {trade['total_invested']}")
        print(f"Current Cycle ID: {trade['cycle_id']}")
        
        filled_orders = [o for o in bot_data['orders'] if o['status'] in ('filled', 'closed')]
        
        sum_invested_all_cycles = 0
        sum_invested_current_cycle = 0
        
        print(f"\nFilled Orders (Entries/Grids):")
        for o in filled_orders:
            if o['order_type'] in ['entry', 'grid']:
                cost = o['price'] * o['amount']
                sum_invested_all_cycles += cost
                
                is_current = (o['cycle_id'] == trade['cycle_id'])
                current_tag = " [CURRENT CYCLE]" if is_current else " [OLD CYCLE]"
                
                if is_current:
                    sum_invested_current_cycle += cost
                    
                print(f"  ID:{o['id']:<5} {o['order_type'].upper():<5} Step:{o['step']} Cycle:{o['cycle_id']} Price:{o['price']:.4f} Amt:{o['amount']:.4f} Cost:${cost:.2f}{current_tag}")
                
        print(f"\nSum of Orders (All Cycles): ${sum_invested_all_cycles:.2f}")
        print(f"Sum of Orders (Current Cycle {trade['cycle_id']}): ${sum_invested_current_cycle:.2f}")
        print(f"Gap (Orders in Current Cycle - DB Invested): ${sum_invested_current_cycle - trade['total_invested']:.2f}")

if __name__ == "__main__":
    analyze()
