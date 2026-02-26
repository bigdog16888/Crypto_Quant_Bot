
import sqlite3
import pandas as pd
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = 'crypto_bot.db'

def test_sql():
    try:
        conn = sqlite3.connect(DB_PATH)
        logger.info(f"Connected to {DB_PATH}")
        
        # Exact query from monitor.py
        query = "SELECT pair, side, size, entry_price, datetime(last_checked, 'unixepoch', 'localtime') as updated FROM active_positions"
        
        logger.info(f"Executing query: {query}")
        df = pd.read_sql(query, conn)
        
        logger.info(f"DataFrame Shape: {df.shape}")
        if not df.empty:
            print(df)
        else:
            logger.warning("DataFrame is empty!")
            
        conn.close()
    except Exception as e:
        logger.error(f"SQL Read Failed: {e}")

if __name__ == "__main__":
    test_sql()
