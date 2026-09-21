import subprocess, time, os, sys

os.chdir("D:/Crypto_Quant_Bot")
log_path = "engine.log"

# Clear previous log to get a clean startup
if os.path.exists(log_path):
    os.rename(log_path, log_path + ".bak")

print("Starting engine...")
proc = subprocess.Popen(
    [sys.executable, "engine/run_engine.py"],
    stdout=open(log_path, "a"),
    stderr=subprocess.STDOUT,
    cwd="D:/Crypto_Quant_Bot"
)

print("Engine PID:", proc.pid)
print("Waiting for TRADING MODE ACTIVE in log...")

found = False
start = time.time()
timeout = 60

while time.time() - start < timeout:
    if os.path.exists(log_path):
        with open(log_path, "r") as f:
            content = f.read()
            if "TRADING MODE ACTIVE" in content:
                found = True
                break
    time.sleep(2)

if found:
    print("✅ ENGINE REACHED TRADING MODE ACTIVE")
    with open(log_path, "r") as f:
        lines = f.readlines()
    print("--- Last 25 lines of engine.log ---")
    for line in lines[-25:]:
        print(line.rstrip())
else:
    print("❌ TIMEOUT: TRADING MODE ACTIVE not found within 60s")
    if os.path.exists(log_path):
        with open(log_path, "r") as f:
            lines = f.readlines()
        print("--- Last 25 lines of engine.log ---")
        for line in lines[-25:]:
            print(line.rstrip())
    sys.exit(1)
