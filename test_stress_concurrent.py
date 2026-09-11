import sqlite3
import threading
import time
import sys
sys.path.insert(0, '.')
from engine.database import get_connection

DB_PATH = 'crypto_bot.db'

def writer_thread(thread_id, num_writes, results):
    try:
        conn = get_connection()
        for i in range(num_writes):
            conn.execute("INSERT INTO test_concurrent (thread_id, write_num) VALUES (?, ?)", (thread_id, i))
        conn.commit()
        results[thread_id] = ('success', num_writes)
    except Exception as e:
        results[thread_id] = ('error', str(e))

def run_stress_test(num_threads=20, writes_per_thread=5):
    print(f"=== STRESS TEST: {num_threads} threads x {writes_per_thread} writes = {num_threads * writes_per_thread} total ===")
    
    # Setup: create test table
    conn = get_connection()
    conn.execute("DROP TABLE IF EXISTS test_concurrent")
    conn.execute("CREATE TABLE test_concurrent (id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER, write_num INTEGER)")
    conn.commit()
    conn.close()
    
    # Run concurrent writes
    results = {}
    threads = []
    start = time.time()
    
    for t in range(num_threads):
        th = threading.Thread(target=writer_thread, args=(t, writes_per_thread, results))
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
    
    print(f"RESULT: {total_rows}/{num_threads * writes_per_thread} writes committed, Errors: {error_count}, Elapsed: {elapsed:.2f}s")
    
    for tid, (status, detail) in results.items():
        if status == 'error':
            print(f"  Thread {tid}: ERROR - {detail}")
    
    if total_rows == num_threads * writes_per_thread and error_count == 0:
        print("STRESS TEST: PASS")
        return True
    else:
        print("STRESS TEST: FAIL")
        return False

if __name__ == '__main__':
    run_stress_test(20, 5)