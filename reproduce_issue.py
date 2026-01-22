
import sys
import os
import logging

# Add root to sys.path
sys.path.append(os.getcwd())

# Setup logging
logging.basicConfig(level=logging.INFO)

try:
    from engine.database import init_db, DB_PATH
    print(f"DB_PATH is: {DB_PATH}")
    init_db()
    print("init_db completed successfully")
except Exception as e:
    print(f"Caught exception: {e}")
    import traceback
    traceback.print_exc()
