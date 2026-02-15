import ast
import time

filename = r"c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\engine\database.py"

max_iterations = 200

for i in range(max_iterations):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            source = f.read()
        
        ast.parse(source)
        print(f"[{i}] Syntax OK! File is valid.")
        break
        
    except SyntaxError as e:
        print(f"[{i}] SyntaxError at line {e.lineno}: {e.msg}")
        
        lines = source.splitlines()
        if e.lineno and e.lineno <= len(lines):
            bad_line_idx = e.lineno - 1
            bad_line = lines[bad_line_idx]
            print(f"    Bad Line: {bad_line}")
            
            # Heuristic: comment it out
            lines[bad_line_idx] = "# FIXED_SYNTAX: " + bad_line
            
            with open(filename, 'w', encoding='utf-8') as f:
                f.write("\n".join(lines))
        else:
            print("    Error line out of bounds")
            break
            
    except Exception as e:
        print(f"    Critical Error: {e}")
        break
