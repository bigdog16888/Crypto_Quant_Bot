"""
Windows Service Setup for Crypto Quant Bot using NSSM

NSSM (Non-Sucking Service Manager) allows running Python scripts as Windows Services.

Benefits:
- Auto-start on system boot (even before user login)
- Auto-restart on crash
- Resource management
- Logging

Usage:
    # Install service
    python service_install.py install

    # Remove service
    python service_install.py remove

    # Start/Stop manually
    net start CryptoQuantBot
    net stop CryptoQuantBot
"""

import os
import sys
import subprocess
import winreg
import shutil

# Configuration
SERVICE_NAME = "CryptoQuantBot"
DISPLAY_NAME = "Crypto Quant Bot Trading System"
DESCRIPTION = "Automated cryptocurrency trading bot with multi-bot position management"
PYTHON_PATH = sys.executable
SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine", "runner.py")
WORKING_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(WORKING_DIR, "logs")

# NSSM Parameters
NSSM_PARAMS = {
    "AppDirectory": WORKING_DIR,
    "AppExecutable": PYTHON_PATH,
    "AppParameters": f'"{SCRIPT_PATH}"',
    "AppWorkingDirectory": WORKING_DIR,
    "AppStdout": os.path.join(LOG_DIR, "service_stdout.log"),
    "AppStderr": os.path.join(LOG_DIR, "service_stderr.log"),
    "AppStdoutCreationDisposition": 4,  # TRUNCATE_EXISTING
    "AppStderrCreationDisposition": 4,
    "AppRotateFiles": 1,
    "AppRotateOnline": 1,
    "AppRotateBytes": 1048576,  # 1MB
    "AppExit": "Restart",
    "AppRestartDelay": 5000,  # 5 seconds
    "AppStopMethodSkip": 6,
    "AppStopMethodConsole": 1,
    "DisplayName": DISPLAY_NAME,
    "Description": DESCRIPTION,
    "Start": "demand",  # Manual start (change to "auto" for auto-start)
    "Type": "own",
    "Error": "normal",
    "DependOnService": "",  # Add dependencies if needed (e.g., "Tcpip")
}


def find_nssm():
    """Find NSSM executable"""
    # Check common locations
    paths = [
        r"C:\Program Files\nssm\nssm.exe",
        r"C:\Program Files (x86)\nssm\nssm.exe",
        r"C:\nssm\nssm.exe",
    ]
    
    # Try PATH
    try:
        result = subprocess.run(["where", "nssm"], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip().split("\n")[0]
    except:
        pass
    
    # Check local paths
    for path in paths:
        if os.path.exists(path):
            return path
    
    return None


def run_nssm_command(command: list) -> bool:
    """Run NSSM command"""
    nssm = find_nssm()
    if not nssm:
        print("❌ NSSM not found! Please install NSSM first:")
        print("   Download: https://nssm.cc/download")
        print("   Or run:   choco install nssm")
        return False
    
    try:
        result = subprocess.run([nssm] + command, capture_output=True, text=True)
        if result.returncode == 0:
            return True
        else:
            print(f"NSSM Error: {result.stderr}")
            return False
    except Exception as e:
        print(f"Error running NSSM: {e}")
        return False


def install_service():
    """Install the bot as a Windows service"""
    global NSSM_PARAMS
    
    print(f"\n{'=' * 60}")
    print("Installing Crypto Quant Bot as Windows Service")
    print(f"{'=' * 60}")
    
    # Ensure log directory exists
    os.makedirs(LOG_DIR, exist_ok=True)
    print(f"📁 Log directory: {LOG_DIR}")
    
    # Check dependencies
    if not os.path.exists(SCRIPT_PATH):
        print(f"❌ Script not found: {SCRIPT_PATH}")
        return False
    
    if not os.path.exists(PYTHON_PATH):
        print(f"❌ Python not found: {PYTHON_PATH}")
        return False
    
    nssm = find_nssm()
    if not nssm:
        print("❌ NSSM not found!")
        return False
    
    print(f"✅ Python: {PYTHON_PATH}")
    print(f"✅ Script: {SCRIPT_PATH}")
    print(f"✅ NSSM: {nssm}")
    
    # Remove existing service first
    print("\n🔄 Removing existing service (if any)...")
    run_nssm_command(["remove", SERVICE_NAME, "confirm"])
    
    # Install service
    print("\n📦 Creating service...")
    
    nssm_params = NSSM_PARAMS.copy()
    
    # Ask about auto-start
    print("\nChoose startup mode:")
    print("  [1] Manual - Start when you choose (recommended for development)")
    print("  [2] Auto   - Start automatically when Windows boots")
    
    choice = input("Selection [1-2]: ").strip()
    if choice == "2":
        nssm_params["Start"] = "auto"
    
    # Build NSSM command
    cmd = ["install", SERVICE_NAME]
    for key, value in nssm_params.items():
        cmd.extend([key, str(value)])
    
    if run_nssm_command(cmd):
        print("\n✅ Service installed successfully!")
        print(f"\n📋 Service: {SERVICE_NAME}")
        print(f"   Display: {DISPLAY_NAME}")
        print(f"   Startup: {'Auto' if nssm_params['Start'] == 'auto' else 'Manual'}")
        
        print(f"\n📝 Management Commands:")
        print(f"   Start:  net start {SERVICE_NAME}")
        print(f"   Stop:   net stop {SERVICE_NAME}")
        print(f"   Status: sc query {SERVICE_NAME}")
        print(f"   Logs:   {LOG_DIR}\\*.log")
        
        print(f"\n🛠️  NSSM GUI:")
        print(f"   nssm edit {SERVICE_NAME}")
        
        # Ask to start service
        start = input("\nStart the service now? [y/N]: ").strip().lower()
        if start == "y":
            subprocess.run(["net", "start", SERVICE_NAME], capture_output=True)
            print("✅ Service started!")
        
        return True
    else:
        print("❌ Failed to install service")
        return False


def remove_service():
    """Remove the Windows service"""
    print(f"\n{'=' * 60}")
    print(f"Removing {SERVICE_NAME} service")
    print(f"{'=' * 60}")
    
    if run_nssm_command(["remove", SERVICE_NAME, "confirm"]):
        print("✅ Service removed successfully")
        return True
    else:
        print("❌ Failed to remove service")
        return False


def show_status():
    """Show service status"""
    try:
        result = subprocess.run(
            ["sc", "query", SERVICE_NAME],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"\n✅ {SERVICE_NAME} is installed")
            # Parse status
            for line in result.stdout.split("\n"):
                if "STATE" in line:
                    print(f"   {line.strip()}")
            return True
        else:
            print(f"\n❌ {SERVICE_NAME} is not installed")
            return False
    except Exception as e:
        print(f"Error checking status: {e}")
        return False


def show_logs():
    """Tail recent logs"""
    import glob
    
    log_files = glob.glob(os.path.join(LOG_DIR, "*.log"))
    if not log_files:
        print("No log files found")
        return
    
    # Show last 50 lines of most recent log
    latest = max(log_files, key=os.path.getmtime)
    try:
        with open(latest, "r") as f:
            lines = f.readlines()
            for line in lines[-50:]:
                print(line.rstrip())
    except Exception as e:
        print(f"Error reading log: {e}")


def main():
    """Main entry point"""
    print("\n" + "=" * 60)
    print("Crypto Quant Bot - Windows Service Manager")
    print("=" * 60)
    
    if len(sys.argv) < 2:
        print("\nCommands:")
        print("  install   - Install as Windows service")
        print("  remove    - Remove Windows service")
        print("  status    - Show service status")
        print("  logs      - Show recent logs")
        print("  start     - Start service")
        print("  stop      - Stop service")
        print("  restart   - Restart service")
        sys.exit(1)
    
    command = sys.argv[1].lower()
    
    if command == "install":
        install_service()
    elif command == "remove":
        remove_service()
    elif command == "status":
        show_status()
    elif command == "logs":
        show_logs()
    elif command == "start":
        subprocess.run(["net", "start", SERVICE_NAME], capture_output=True)
        print("Start command sent")
    elif command == "stop":
        subprocess.run(["net", "stop", SERVICE_NAME], capture_output=True)
        print("Stop command sent")
    elif command == "restart":
        subprocess.run(["net", "stop", SERVICE_NAME], capture_output=True)
        time.sleep(2)
        subprocess.run(["net", "start", SERVICE_NAME], capture_output=True)
        print("Restart command sent")
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    import time
    main()
