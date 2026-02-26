
import psutil
import sys

def kill_runner():
    print("🔪 Killing Runner Processes...")
    count = 0
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['cmdline'] and 'runner.py' in ' '.join(proc.info['cmdline']):
                print(f"   found {proc.info['pid']}: {proc.info['cmdline']}")
                proc.kill()
                count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    print(f"✅ Killed {count} processes.")

if __name__ == "__main__":
    kill_runner()
