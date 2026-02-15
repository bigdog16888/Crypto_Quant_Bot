import sys
import os
import logging

# Add root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import update_bot_config_value, get_bot_params

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_ID = 37

def fix_config():
    logger.info(f"Checking config for Bot {BOT_ID}...")
    params = get_bot_params(BOT_ID)
    if not params:
        logger.error(f"Bot {BOT_ID} not found!")
        return

    logger.info(f"Current config: {params[7]}")
    
    # Update mode_price to 2 (Below)
    logger.info("Updating mode_price to 2...")
    update_bot_config_value(BOT_ID, 'mode_price', 2)
    
    # Verify
    params_new = get_bot_params(BOT_ID)
    logger.info(f"New config: {params_new[7]}")
    logger.info("Done!")

if __name__ == "__main__":
    fix_config()
