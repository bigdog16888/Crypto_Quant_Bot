import os
import sys

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.strategies.martingale_strategy import MartingaleStrategy

def test_projections():
    print("\n--- Testing Martingale Projections ---")
    params = {
        'base_size': 100.0,
        'martingale_multiplier': 2.0,
        'UseHedge': True,
        'HedgeStartStep': 5
    }
    strat = MartingaleStrategy(params=params)
    projections = strat.calculate_projections(base_price=40000.0)
    
    for p in projections:
        print(f"Step {p['step']}: Price {p['price']}, Size ${p['order_size_usdc']}, TP: {p['tp_price']}, Hedge: {p['is_hedge']}")
        
    # Validation
    assert projections[0]['order_size_usdc'] == 100.0
    assert projections[1]['order_size_usdc'] == 200.0
    assert 'price' in projections[0]
    assert 'tp_price' in projections[0]
    assert projections[5]['is_hedge'] == True # Step 5
    assert projections[3]['is_hedge'] == False # Step 4
    
    print("\n✅ Projection logic verified!")

    print("\n--- Testing Next Grid Price ---")
    next_price_long = strat.calculate_next_grid_price('LONG', 40000.0, 40000.0, 0, None)
    print(f"Next Long (Step 0): {next_price_long}")
    # Base grid is 100.0 in default logic if params not set (default 100.0 in code I added?)
    # In my replacement content:     base_grid = float(self.params.get('base_grid', 100.0))
    # In test params: no base_grid, so defaults to 100.0
    # Step 0: price = avg_entry - (grid_dist * (step + 1)) = 40000 - 100 * 1 = 39900
    assert next_price_long == 39900.0

    print("✅ Next Grid Price verified!")

if __name__ == "__main__":
    test_projections()
