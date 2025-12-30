import os
import time
import subprocess
import sys

# 1. Start engine
print("Starting engine mock process...")
if os.path.exists("engine.stop"): os.remove("engine.stop")

process = subprocess.Popen([sys.executable, "engine/runner.py"])
with open("engine.pid", "w") as f:
    f.write(str(process.pid))

time.sleep(5) # Let it start

# 2. Send stop signal
print("Sending stop signal...")
with open("engine.stop", "w") as f:
    f.write("stop")

# 3. Wait for it to exit
start_wait = time.time()
while process.poll() is None:
    print("Waiting for engine to stop...")
    time.sleep(2)
    if time.time() - start_wait > 30:
        print("Timeout! Force killing...")
        process.kill()
        break

print("Engine stopped successfully.")
if os.path.exists("engine.pid"):
    print("Error: engine.pid still exists!")
else:
    print("Success: engine.pid removed by runner.")
