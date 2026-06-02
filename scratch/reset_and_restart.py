import os
import psutil
import sqlite3
import time
import subprocess

# 1. Kill engine process
pid_file = 'engine.pid'
if os.path.exists(pid_file):
    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
        print(f"Killing process {pid}...")
        if psutil.pid_exists(pid):
            proc = psutil.Process(pid)
            proc.kill()
            print("Killed.")
        else:
            print("Process not active.")
    except Exception as e:
        print("Error killing process:", e)

# 2. Reset database Config Error status on hedge bots
db_path = 'crypto_bot.db'
if os.path.exists(db_path):
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        # Let's see how many bots are in Config Error state
        cur.execute("SELECT id, name, last_error FROM bots WHERE last_error = 'Config Error'")
        rows = cur.fetchall()
        print(f"Found {len(rows)} bots in Config Error:")
        for r in rows:
            print(f"  {r[0]} ({r[1]}): {r[2]}")
        
        # Reset them to None status
        cur.execute("UPDATE bots SET last_error = NULL WHERE last_error = 'Config Error'")
        # Also let's set their status back to 'Scanning' or 'IN TRADE' appropriately?
        # Wait, if they are 'Config Error', their status columns might have been set to 'Stopped' or kept as they were.
        # Let's check their current status values
        cur.execute("SELECT id, name, status FROM bots WHERE id IN (100321, 100322, 100323, 100324, 100325)")
        status_rows = cur.fetchall()
        print("Current status of the hedge children:")
        for sr in status_rows:
            print(f"  {sr[0]} ({sr[1]}): {sr[2]}")
            if sr[2] == 'Stopped':
                # If they were stopped, let's reset them to Scanning
                cur.execute("UPDATE bots SET status = 'Scanning' WHERE id = ?", (sr[0],))
                print(f"  -> Reset status of {sr[0]} to Scanning")
        
        conn.commit()
        conn.close()
        print("Database error states reset completed.")
    except Exception as e:
        print("Database error:", e)

# 3. Start engine runner
print("Starting engine/runner.py...")
# Using subprocess.Popen with DETACHED_PROCESS to run in background
try:
    DETACHED_PROCESS = 0x00000008
    proc = subprocess.Popen(['python', 'engine/runner.py'], creationflags=DETACHED_PROCESS, close_fds=True)
    print(f"Started runner.py with PID {proc.pid}")
except Exception as e:
    print("Error starting runner.py:", e)
