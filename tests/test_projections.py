import os
import sys

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.strategies.mql4_strategy import MQL4Strategy

def test_projections():
    print("\n--- Testing Martingale Projections ---")
    params = {
        'base_size': 100.0,
        'martingale_multiplier': 2.0,
        'UseHedge': True,
        'HedgeStartStep': 5
    }
    strat = MQL4Strategy(params=params)
    projections = strat.calculate_projections(base_price=40000.0)
    
    for p in projections:
        print(f"Step {p['step']}: Price {p['price']}, Size ${p['order_size_usdc']}, TP: {p['tp_price']}, Hedge: {p['is_hedge']}")
        
    # Validation
    assert projections[0]['order_size_usdc'] == 100.0
    assert projections[1]['order_size_usdc'] == 200.0
    assert 'price' in projections[0]
    assert 'tp_price' in projections[0]
    assert projections[4]['is_hedge'] == True # Step 5
    assert projections[3]['is_hedge'] == False # Step 4
    
    print("\n✅ Projection logic verified!")

if __name__ == "__main__":
    test_projections()
