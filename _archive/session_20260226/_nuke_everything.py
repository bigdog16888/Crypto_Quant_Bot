import os
import time
import sqlite3
import logging
from engine.exchange_interface import ExchangeInterface

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("NukeAndPave")

def nuke_everything():
    # 1. Stop the Engine
    stop_file = "engine.stop"
    if not os.path.exists(stop_file):
        with open(stop_file, 'w') as f:
            f.write("Stop for nuke")
        logger.info(f"Created {stop_file}. Waiting 5 seconds for Engine to halt...")
        time.sleep(5)
    
    # 2. Flatten Exchange
    logger.info("Flattening Exchange...")
    try:
        from flatten_exchange import flatten_all
        flatten_all()
    except Exception as e:
        logger.error(f"Error flattening exchange: {e}")

    # 3. Wipe Database States
    logger.info("Wiping Database...")
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM bot_orders")
    cursor.execute("DELETE FROM trades")
    cursor.execute("UPDATE bots SET status='Stopped'")
    conn.commit()
    conn.close()
    logger.info("Database wiped. All bots set to Stopped.")

    # 4. Remove Stop file so it can start again
    if os.path.exists(stop_file):
        os.remove(stop_file)
        logger.info(f"Removed {stop_file}.")

if __name__ == "__main__":
    nuke_everything()
