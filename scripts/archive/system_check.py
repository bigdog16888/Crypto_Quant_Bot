import os
import sys
import logging
import sqlite3
import pandas as pd

# Add root to sys.path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT_DIR)

from config.settings import config
from engine.database import init_db

# Configure simplified logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger("SystemCheck")

def check_paths():
    logger.info("--- Checking Paths ---")
    logger.info(f"ROOT_DIR: {config.ROOT_DIR}")
    
    for key, path in config.PATHS.items():
        logger.info(f"{key}: {path}")
        # Verify parent directory exists
        parent = os.path.dirname(path)
        if not os.path.exists(parent):
            logger.error(f"Parent directory for {key} does not exist: {parent}")
            return False
    logger.info("✅ Paths look correct and absolute.")
    return True

def check_db():
    logger.info("\n--- Checking Database ---")
    db_path = config.PATHS['DB_FILE']
    try:
        init_db()
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [row[0] for row in cur.fetchall()]
            logger.info(f"Database exists. Tables: {tables}")
            conn.close()
            return True
        else:
            logger.error("Database file not found after init_db()!")
            return False
    except Exception as e:
        logger.error(f"Database check failed: {e}")
        return False

def check_logs():
    logger.info("\n--- Checking Logging ---")
    log_path = config.PATHS['LOG_FILE']
    try:
        # Try writing to log file
        with open(log_path, 'a') as f:
            f.write("System check write test.\n")
        logger.info(f"Successfully wrote to {log_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to write to log file: {e}")
        return False

def main():
    logger.info("🚀 STARTING SYSTEM HEALTH CHECK")
    
    paths_ok = check_paths()
    db_ok = check_db()
    logs_ok = check_logs()
    
    if paths_ok and db_ok and logs_ok:
        logger.info("\n✨ SYSTEM HEALTH CHECK PASSED ✨")
        logger.info("You can now safely run the bot using 'streamlit run ui/app.py'")
    else:
        logger.error("\n❌ SYSTEM HEALTH CHECK FAILED")
        logger.error("Please review the errors above.")

if __name__ == "__main__":
    main()
