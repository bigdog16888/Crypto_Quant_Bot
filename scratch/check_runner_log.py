import os
import datetime

def run():
    for log in ['engine.log', 'engine_runner_debug.log']:
        if os.path.exists(log):
            mtime = os.path.getmtime(log)
            print(f"Log: {log} | Size: {os.path.getsize(log)} | Last Modified: {datetime.datetime.fromtimestamp(mtime)}")
            with open(log, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                print(f"Tail of {log}:")
                for line in lines[-20:]:
                    print("  " + line.strip())
        else:
            print(f"Log: {log} does NOT exist")

if __name__ == '__main__':
    run()
