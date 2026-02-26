import sys
import os
import logging
import time

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.runner import BotRunner
from engine.reconciler import StateReconciler
from config.settings import config

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ForceReconcile")

def force_reconciliation():
    logger.info("🔧 FORCING SYSTEM RECONCILIATION...")
    
    # Initialize Exchanges Directly (Bypassing Runner Lock)
    from engine.exchange_interface import ExchangeInterface
    exchanges = {
        'future': ExchangeInterface(market_type='future')
    }
    logger.info("✅ Exchanges Initialized for Debugging")

    # Initialize Reconciler
    reconciler = StateReconciler(exchanges)
    
    # Run
    logger.info("🔄 Running reconcile_all()...")
    results = reconciler.reconcile_all()
    
    logger.info("📊 RESULTS:")
    for res in results:
        logger.info(f"   > Bot {res.bot_name} ({res.pair}): {res.action_taken} - {res.details}")

if __name__ == "__main__":
    force_reconciliation()
