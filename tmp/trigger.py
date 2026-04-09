import sys
sys.path.insert(0, '.')
from engine.reconciler import StateReconciler

def run():
    r = StateReconciler()
    print("Triggering adopt_from_physical_positions natively...")
    try:
        result = r.adopt_from_physical_positions(limit_per_symbol=500)
        for bid, v in result.items():
            print(f"Bot {bid}: phys={v.get('phys_qty',0):.4f} proved={v.get('proved_qty',0):.4f} p2={v.get('p2_adopted',0)} p3={v.get('p3_adopted',0)}")
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    run()
