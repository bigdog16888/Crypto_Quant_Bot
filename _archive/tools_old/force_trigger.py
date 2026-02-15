import sys
import os
import logging
import time

# Add root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import update_bot_config_value, get_bot_params

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_ID = 37

def force_trigger():
    logger.info(f"forcing Bot {BOT_ID} to trigger...")
    
    # Update mode_price to 2 (Below)
    update_bot_config_value(BOT_ID, 'mode_price', 2)
    # Update price_threshold to 72000 (Above current price ~70900)
    update_bot_config_value(BOT_ID, 'price_threshold', 72000.0)
    
    params = get_bot_params(BOT_ID)
    logger.info(f"Bot 37 Config Updated: Mode=2, Threshold=72000.0")

if __name__ == "__main__":
    force_trigger()