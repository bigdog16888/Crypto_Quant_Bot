import subprocess
import time
import sys

def run_runner_with_timeout(seconds=45):
    print(f"Starting engine/runner.py for {seconds} seconds...")
    try:
        # Use shell=True for Windows, sys.executable ensures correct python
        proc = subprocess.Popen([sys.executable, "engine/runner.py"], 
                                stdout=subprocess.PIPE, 
                                stderr=subprocess.PIPE,
                                text=True) # text=True for string output
        
        start_time = time.time()
        while time.time() - start_time < seconds:
            if proc.poll() is not None:
                print("Process ended early!")
                out, err = proc.communicate()
                print("STDOUT:", out)
                print("STDERR:", err)
                return
            time.sleep(1)
            print(".", end="", flush=True)
        
        print("\nTime's up! Stopping runner...")
        proc.terminate()
        try:
            outs, errs = proc.communicate(timeout=5)
            print("\nRunner Output (Last few lines):")
            if outs: print('\n'.join(outs.splitlines()[-20:]))
            if errs: print('\nSTDERR:', '\n'.join(errs.splitlines()[-20:]))
        except subprocess.TimeoutExpired:
            proc.kill()
            print("\nForced kill.")
            
    except Exception as e:
        print(f"\nError running runner: {e}")

if __name__ == "__main__":
    run_runner_with_timeout()
