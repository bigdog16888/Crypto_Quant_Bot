import os
import sys
import subprocess
import time

def get_newest_modified_time(directories):
    newest_time = 0.0
    newest_file = None
    for d in directories:
        if not os.path.exists(d):
            continue
        for root, dirs, files in os.walk(d):
            # Skip cache directories
            if '__pycache__' in root or '.pytest_cache' in root:
                continue
            for file in files:
                if file == 'verify_deployment.py':
                    continue
                if file.endswith('.py') or file.endswith('.json'):
                    path = os.path.join(root, file)
                    try:
                        mtime = os.path.getmtime(path)
                        if mtime > newest_time:
                            newest_time = mtime
                            newest_file = path
                    except Exception:
                        pass
    return newest_time, newest_file

def get_runner_pid():
    try:
        out = subprocess.check_output("netstat -ano", shell=True).decode()
        for line in out.splitlines():
            if "19888" in line and "LISTENING" in line:
                parts = line.strip().split()
                return parts[-1]
    except Exception as e:
        print(f"Error checking netstat: {e}")
    return None

def get_process_start_time(pid):
    try:
        # Use PowerShell to get start time as unix timestamp
        cmd = f'powershell -Command "([DateTimeOffset](Get-Process -Id {pid}).StartTime).ToUnixTimeSeconds()"'
        out = subprocess.check_output(cmd, shell=True).decode().strip()
        return int(out)
    except Exception as e:
        print(f"Error getting process start time: {e}")
    return None

def main():
    print("============================================================")
    print("DEPLOYMENT VERIFICATION GATE")
    print("============================================================")
    
    # 1. Find newest modified file
    src_dirs = ["engine", "scripts", "ui"]
    newest_time, newest_file = get_newest_modified_time(src_dirs)
    if not newest_file:
        print("❌ [ERROR] No source files found to check.")
        sys.exit(1)
        
    newest_dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(newest_time))
    print(f"Newest modified file: {newest_file}")
    print(f"Last modified at:     {newest_dt} (Unix: {int(newest_time)})")
    
    # 2. Check running process
    pid = get_runner_pid()
    if not pid:
        print("\n⚠️ [WARNING] No active engine runner process found listening on port 19888!")
        print("Please start the engine runner using: python engine/runner.py")
        sys.exit(2)
        
    start_time = get_process_start_time(pid)
    if not start_time:
        print(f"\n❌ [ERROR] Could not determine start time for runner process (PID: {pid}).")
        sys.exit(1)
        
    start_dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time))
    print(f"Runner Process PID:   {pid}")
    print(f"Process Started at:   {start_dt} (Unix: {start_time})")
    
    # Compare (allow 2 seconds buffer for file system write delay or clock differences)
    if start_time < (newest_time - 2.0):
        print("\n🛑 [DEPLOY ERROR] OUTDATED CODE RUNNING!")
        print(f"The running process (started {start_dt}) is older than the newest modified file ({newest_dt}).")
        print("Please kill the old process and restart the runner:")
        print(f"  PowerShell: Stop-Process -Id {pid} -Force; python engine/runner.py")
        sys.exit(3)
        
    print("\n✅ [DEPLOY SUCCESS] Runner process is up-to-date and running the latest code.")
    sys.exit(0)

if __name__ == '__main__':
    main()
