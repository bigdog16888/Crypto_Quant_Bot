import os
print("=== SEARCHING FOR CALLER GATE ===")
for root, dirs, files in os.walk("engine"):
    for f in files:
        if f.endswith(".py"):
            fpath = os.path.join(root, f)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as fp:
                    lines = fp.readlines()
            except:
                continue
            for i, line in enumerate(lines):
                s = line.strip()
                if "Startup Sync Failed" in s or "Startup parity verification FAILED" in s or "genuine-anomaly" in s:
                    print("=== Found in", fpath, "===")
                    start = max(0, i-3)
                    end = min(len(lines), i+4)
                    for j in range(start, end):
                        print("  L%d: %s" % (j+1, lines[j].rstrip()[:100]))
