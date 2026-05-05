import re

def print_func(func_name, file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    in_func = False
    indent = 0
    for i, line in enumerate(lines):
        if line.strip().startswith(f"def {func_name}"):
            in_func = True
            indent = len(line) - len(line.lstrip())
            print(f"--- {func_name} ---")
            print(line, end='')
            continue
            
        if in_func:
            if line.strip() != "" and (len(line) - len(line.lstrip())) <= indent:
                break
            print(line, end='')

print_func("execute_hedge_lock", "engine/bot_executor.py")
