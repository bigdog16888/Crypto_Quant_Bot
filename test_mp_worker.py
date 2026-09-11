
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
