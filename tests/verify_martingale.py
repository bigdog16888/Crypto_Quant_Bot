import sys
import os

# Add current directory to path so we can import engine
sys.path.append(os.getcwd())

from engine.risk import calculate_next_trade_size, calculate_break_even_price

def run_verification():
    print("--- Martingale Level 3 Verification ---")
    
    # Scenario: Martingale Level 3 (meaning we are about to place the 4th trade, or just placed the 3rd?)
    # "at Martingale Level 3" usually means 3 trades are OPEN.
    # Let's assume standard Blessing settings found in the file:
    # Lot: 0.01, Multiplier: 1.4, LotStep: ? (Usually small if defined)
    
    initial_lot = 0.01
    multiplier = 1.4
    lot_step = 0.01 # Standard broker LotStep is usually 0.01
    lot_decimal = 2
    
    # Recreate the sequence of trades
    # Level 1 (Initial): 0.01
    # Level 2: 0.01 * 1.4 = 0.014 -> rounded to 0.01 (min lot usually 0.01)
    # Actually MQL4: MathMax(Lots[i-1]*Mult, Lots[i-1]+Step) -> 0.01*1.4 = 0.014. 
    # If LotDecimal=2, 0.014 -> 0.01.
    # Let's trace it manually to be sure we match MQL4 behavior we saw.
    
    orders = []
    
    # Trade 1
    t1_lots = 0.01
    t1_price = 1.1000 # Buy
    orders.append({'lots': t1_lots, 'open_price': t1_price, 'type': 'BUY'})
    print(f"Trade 1: {t1_lots} @ {t1_price}")
    
    # Trade 2
    # Lots = Max(0.01 * 1.4, 0.01 + 0) = 0.014 -> 0.01
    t2_lots = calculate_next_trade_size(t1_lots, multiplier, lot_step, lot_decimal)
    t2_price = 1.0950 # Price dropped
    orders.append({'lots': t2_lots, 'open_price': t2_price, 'type': 'BUY'})
    print(f"Trade 2: {t2_lots} @ {t2_price}")
    
    # Trade 3
    # Lots = Max(0.01 * 1.4, 0.01 + 0) = 0.014 => 0.01 again? 
    # Wait, if prev was 0.01, next is 0.014 -> 0.01.
    # If Blessing logic uses exact lots for calculation but normalized for order send:
    # The file said: Lots[Index] = ND(MathMax(Lots[Index - 1] * Multiplier_, ...), LotDecimal)
    # So it DOES normalize at each step.
    # 0.01 -> 0.01 -> 0.01 -> ... this seems wrong for Multiplier 1.4.
    # Ah, MinMult! The code had logic "LotMult = ...". 
    # "LotSize(Lots[0] * LotMult)". The Lots array is pre-calculated based on BASE lot.
    # If Base Lot is 0.01.
    # Let's assume the user logic works on standard inputs where 0.01 * 1.4 rounds UP or accumulated precision?
    # Python round(0.014, 2) is 0.01.
    # Maybe standard multiplier is higher, e.g. 1.5? Or start lot 0.1?
    # Let's just use the function as written and see.
    
    t3_lots = calculate_next_trade_size(t2_lots, multiplier, lot_step, lot_decimal)
    t3_price = 1.0900
    orders.append({'lots': t3_lots, 'open_price': t3_price, 'type': 'BUY'})
    print(f"Trade 3: {t3_lots} @ {t3_price}")

    # Now we are "At Level 3". We need "Next Trade Size" (for Trade 4).
    next_trade_size = calculate_next_trade_size(t3_lots, multiplier, lot_step, lot_decimal)
    print(f"\nNext Trade Size (Level 4): {next_trade_size}")
    
    # And "New Break-even Price" for the EXISTING 3 trades
    be_price = calculate_break_even_price(orders)
    print(f"New Break-even Price (Level 3): {be_price:.5f}")

if __name__ == "__main__":
    run_verification()
