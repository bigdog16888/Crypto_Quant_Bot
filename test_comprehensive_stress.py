import sqlite3
import threading
import time
import sys
import subprocess
sys.path.insert(0, '.')
from engine.database import get_connection, init_db

DB_PATH = 'crypto_bot.db'

def writer_thread(thread_id, num_writes, results, use_get_connection=True):
    try:
        if use_get_connection:
            conn = get_connection()
        else:
            # Direct connection like init_db
            conn = sqlite3.connect(DB_PATH, timeout=60.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=60000")
        
        for i in range(num_writes):
            conn.execute("INSERT INTO test_concurrent (thread_id, write_num, data) VALUES (?, ?, ?)", 
                        (thread_id, i, f"data_{thread_id}_{i}"))
        conn.commit()
        results[thread_id] = ('success', num_writes)
    except Exception as e:
        results[thread_id] = ('error', str(e))

def run_mixed_stress_test():
    """Simulate forward test + live engine: mix of get_connection and direct connections"""
    print("=== MIXED STRESS TEST: Simulating forward test + live engine ===")
    print("  - 10 threads using get_connection() (live engine path)")
    print("  - 10 threads using direct sqlite3.connect() (forward test path)")
    print("  - 5 writes each = 100 total concurrent writes")
    
    # Setup: create test table
    conn = get_connection()
    conn.execute("DROP TABLE IF EXISTS test_concurrent")
    conn.execute("CREATE TABLE test_concurrent (id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER, write_num INTEGER, data TEXT)")
    conn.commit()
    conn.close()
    
    # Run concurrent writes
    results = {}
    threads = []
    start = time.time()
    
    # 10 threads with get_connection (live engine)
    for t in range(10):
        th = threading.Thread(target=writer_thread, args=(t, 5, results, True))
        threads.append(th)
        th.start()
    
    # 10 threads with direct connection (forward test sim)
    for t in range(10, 20):
        th = threading.Thread(target=writer_thread, args=(t, 5, results, False))
        threads.append(th)
        th.start()
    
    for th in threads:
        th.join()
    
    elapsed = time.time() - start
    
    # Verify results
    conn = get_connection()
    total_rows = conn.execute("SELECT COUNT(*) FROM test_concurrent").fetchone()[0]
    conn.close()
    
    # Print results
    success_count = sum(1 for v in results.values() if v[0] == 'success')
    error_count = sum(1 for v in results.values() if v[0] == 'error')
    
    print(f"RESULT: {total_rows}/100 writes committed, Errors: {error_count}, Elapsed: {elapsed:.2f}s")
    
    for tid, (status, detail) in results.items():
        if status == 'error':
            print(f"  Thread {tid}: ERROR - {detail}")
    
    if total_rows == 100 and error_count == 0:
        print("MIXED STRESS TEST: PASS")
        return True
    else:
        print("MIXED STRESS TEST: FAIL")
        return False

def run_multi_process_test():
    """Test with multiple processes (more realistic for forward test + live engine)"""
    print("\n=== MULTI-PROCESS TEST: 5 processes x 20 writes = 100 total ===")
    
    # Create test script for subprocesses
    test_script = '''
import sqlite3
import sys
sys.path.insert(0, '.')
from engine.database import get_connection

def worker(proc_id, num_writes):
    conn = get_connection()
    for i in range(num_writes):
        conn.execute("INSERT INTO test_mp (proc_id, write_num) VALUES (?, ?)", (proc_id, i))
    conn.commit()
    print(f"Process {proc_id}: {num_writes} writes committed")
    return True

if __name__ == "__main__":
    import sys
    proc_id = int(sys.argv[1])
    worker(proc_id, 20)
'''
    
    with open('test_mp_worker.py', 'w') as f:
        f.write(test_script)
    
    # Setup table
    conn = get_connection()
    conn.execute("DROP TABLE IF EXISTS test_mp")
    conn.execute("CREATE TABLE test_mp (id INTEGER PRIMARY KEY AUTOINCREMENT, proc_id INTEGER, write_num INTEGER)")
    conn.commit()
    conn.close()
    
    # Run 5 processes
    procs = []
    start = time.time()
    for p in range(5):
        proc = subprocess.Popen(['python', 'test_mp_worker.py', str(p)])
        procs.append(proc)
    
    for proc in procs:
        proc.wait()
    
    elapsed = time.time() - start
    
    # Verify
    conn = get_connection()
    total_rows = conn.execute("SELECT COUNT(*) FROM test_mp").fetchone()[0]
    conn.close()
    
    print(f"RESULT: {total_rows}/100 writes committed, Elapsed: {elapsed:.2f}s")
    if total_rows == 100:
        print("MULTI-PROCESS TEST: PASS")
        return True
    else:
        print("MULTI-PROCESS TEST: FAIL")
        return False

if __name__ == '__main__':
    run_mixed_stress_test()
    run_multi_process_test()
    print("\n=== ALL STRESS TESTS COMPLETE ===")