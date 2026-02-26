import os
import sys
import psutil
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LockTest")

# Add root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.runner import ApplicationLock

def test_smart_lock():
    pid_file = "test_engine.pid"
    
    # Clean start
    if os.path.exists(pid_file):
        os.remove(pid_file)

    print(f"1. Creating dummy PID file pointing to THIS process ({os.getpid()})...")
    # This process is 'python tools/test_pid_lock.py', NOT 'runner.py'
    # So the smart lock SHOULD detect mismatch and overwrite it.
    with open(pid_file, 'w') as f:
        f.write(str(os.getpid()))

    print("2. Attempting to acquire lock with Smart Logic...")
    lock = ApplicationLock(pid_file)
    success = lock.acquire()

    if success:
        print("✅ SUCCESS: Smart Lock detected PID mismatch and acquired lock!")
        # Verify new PID is written
        with open(pid_file, 'r') as f:
            new_pid = int(f.read().strip())
        print(f"   New PID in file: {new_pid} (Should match {os.getpid()})")
    else:
        print("❌ FAILED: Smart Lock incorrectly blocked execution.")

    # Cleanup
    lock.release()
    if os.path.exists(pid_file):
        os.remove(pid_file)

if __name__ == "__main__":
    test_smart_lock()
