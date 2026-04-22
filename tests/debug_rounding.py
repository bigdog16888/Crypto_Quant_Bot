
import sys
import os

# Add parent directory to path to import engine
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.exchange_interface import ExchangeInterface

def test_precision():
    print("=== GLOBAL PRECISION AUDIT ===")
    
    test_cases = [
        # (value, step, expected)
        (10.7, 0.1, 10.7),    # The SUI failure case
        (5.6, 0.1, 5.6),      # Another common failure case
        (2.1, 0.1, 2.1),
        (0.00216, 0.00001, 0.00216),
        (100.758, 0.01, 100.75),  # Floor check
        (10.699999999999999, 0.1, 10.7), # REAL Float Approximation of 10.7 (approx 16-17 digits)
    ]
    
    success = True
    for val, step, expected in test_cases:
        actual = ExchangeInterface.round_to_step(val, step)
        status = "✅ PASS" if actual == expected else "❌ FAIL"
        if actual != expected: success = False
        print(f"Value: {val:<6} | Step: {step:<6} | Expected: {expected:<6} | Actual: {actual:<6} | Status: {status}")

    print("\n=== CEILING AUDIT ===")
    ceil_cases = [
        (10.60000000000001, 0.1, 10.7),
        (10.60000000000000, 0.1, 10.6),
    ]
    for val, step, expected in ceil_cases:
        actual = ExchangeInterface.ceil_to_step(val, step)
        status = "✅ PASS" if actual == expected else "❌ FAIL"
        if actual != expected: success = False
        print(f"Value: {val} | Step: {step} | Expected: {expected} | Actual: {actual} | Status: {status}")

    if success:
        print("\n🏆 AUDIT COMPLETE: ALL PRECISION GUARDIANS VERIFIED.")
    else:
        print("\n🚨 AUDIT FAILED: PRECISION ERRORS STILL DETECTED.")

if __name__ == "__main__":
    test_precision()
