import sys
import ast

filename = r"c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\engine\database.py"

try:
    with open(filename, 'r', encoding='utf-8') as f:
        source = f.read()
    ast.parse(source)
    print("Syntax OK")
except SyntaxError as e:
    print(f"SyntaxError: {e.msg}")
    print(f"Line: {e.lineno}")
    print(f"Offset: {e.offset}")
    print(f"Text: {e.text}")
    
    # Print surrounding lines
    lines = source.splitlines()
    if e.lineno:
        start = max(0, e.lineno - 5)
        end = min(len(lines), e.lineno + 5)
        print("\nContext:")
        for i in range(start, end):
            prefix = ">> " if i + 1 == e.lineno else "   "
            print(f"{prefix}{i+1}: {lines[i]}")
except Exception as e:
    print(f"Error: {e}")
