import os

RUNNER_PATH = "engine/runner.py"
TARGET_STR = '""", (exch_notional, entry_price, b_id))'
INSERT_STR = '                                      cursor.execute("UPDATE bots SET status=\'In Trade\' WHERE id=?", (b_id,))'

def patch_runner():
    if not os.path.exists(RUNNER_PATH):
        print(f"Error: {RUNNER_PATH} not found.")
        return

    with open(RUNNER_PATH, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    new_lines = []
    patched = False
    
    for line in lines:
        new_lines.append(line)
        if TARGET_STR in line and not patched:
            # Check if next line is already the patch
            idx = lines.index(line)
            if idx + 1 < len(lines) and "UPDATE bots SET status='In Trade'" in lines[idx+1]:
                print("Patch already applied.")
                return
            
            print("Found target. Inserting patch.")
            new_lines.append(INSERT_STR + "\n")
            patched = True

    if patched:
        with open(RUNNER_PATH, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        print("Successfully patched runner.py")
    else:
        print("Target string not found!")

if __name__ == "__main__":
    patch_runner()
