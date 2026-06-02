def search():
    path = "CODEBASE_GUIDE.md"
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                if "INV-1" in line or "Invariant" in line:
                    if line_num > 400: # let's search in the latter part of guide
                        print(f"{line_num}: {line.strip()}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    search()
