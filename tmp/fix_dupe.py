import sqlite3
import os
import sys
from pathlib import Path

# Add project root to sys.path so we can import engine modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.database import DB_PATH

conn = sqlite3.connect(DB_PATH)
conn.execute("DELETE FROM bot_orders WHERE order_type='adoption'")
conn.commit()
print(f"DELETED ALL ADOPTIONS from {DB_PATH}")
