
import math

def round_to_step(value: float, step: float) -> float:
    print(f"DEBUG: value={value} ({type(value)}), step={step} ({type(step)})")
    if not step or step <= 0: return value
    
    if step < 1:
        precision = int(-math.log10(step))
    else:
        precision = 0
        
    print(f"DEBUG: precision={precision}")
    
    div_res = value / step
    print(f"DEBUG: value/step = {div_res}")
    
    floored = math.floor(div_res)
    print(f"DEBUG: floored = {floored}")
    
    multiplied = floored * step
    print(f"DEBUG: multiplied = {multiplied}")
    
    result = round(multiplied, precision)
    print(f"DEBUG: result = {result}")
    return result

print("--- TEST CASE 1: 1e-05 ---")
val = 0.00216
step = 1e-05
res = round_to_step(val, step)
print(f"Final: {res}")

print("\n--- TEST CASE 2: 0.00001 ---")
val = 0.00216
step = 0.00001
res = round_to_step(val, step)
print(f"Final: {res}")
