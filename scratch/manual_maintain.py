import logging
import os
import sys
import time

# Add project root to path
sys.path.append(os.getcwd())

from config.settings import config
from engine.bot_executor import BotExecutor
from engine.exchange_interface import ExchangeInterface
from engine.database import get_bot_status

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ManualMaintainer")

def manual_maintain(bot_id):
    logger.info(f"🚀 Manually triggering maintenance for Bot {bot_id}")
    
    executor = BotExecutor(runner=None)
    exchange = ExchangeInterface()
    
    bot_status = get_bot_status(bot_id)
    if not bot_status:
        logger.error(f"Could not find status for bot {bot_id}")
        return
    
    logger.info(f"Bot Status: {bot_status}")
    
    # Simulate maintain_orders
    pair = bot_status['pair']
    name = bot_status['name']
    direction = bot_status['direction']
    current_price = exchange.get_last_price(pair)
    
    logger.info(f"Current Price for {pair}: {current_price}")
    
    # Call maintain_orders with None for market_snapshot to force fresh fetch
    executor.maintain_orders(
        bot_id=bot_id,
        name=name,
        pair=pair,
        direction=direction,
        bot_status=bot_status,
        current_price=current_price,
        exchange=exchange,
        market_snapshot=None,
        bot_config={}
    )

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scratch/manual_maintain.py <bot_id>")
        sys.argv.append("10022") # Default to BTC
    
    manual_maintain(int(sys.argv[1]))
