import sys
import os
import time
import logging

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.reconciler import StateReconciler
from engine.exchange_interface import ExchangeInterface
from config.settings import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def run_reconciliation():
    logging.info("Starting proof-only accounting reconciliation...")
    
    exchanges = {}
    try:
        ex = ExchangeInterface(
            market_type=config.MARKET_TYPE
        )
        exchanges[config.MARKET_TYPE] = ex
    except Exception as e:
        logging.error(f"Failed to initialize exchange: {e}")
        return

    reconciler = StateReconciler(exchanges=exchanges)
    
    logging.info("1. Reconstructing offline fills (168h window)...")
    reconciler.reconstruct_offline_fills(since_hours=168)
    
    logging.info("2. Adopting missing orders from physical positions...")
    reconciler.adopt_from_physical_positions()
    
    logging.info("3. Running global reconciliation pass...")
    reconciler.reconcile_all()
    
    logging.info("Reconciliation complete. System mismatches should be cleared if cryptographically provable.")

if __name__ == "__main__":
    run_reconciliation()
