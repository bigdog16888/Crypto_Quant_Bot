import logging
import time
from engine.reconciler import StateReconciler
from engine.database import init_db

# Configure logging to console
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ForceRecon")

def main():
    logger.info("🚀 FORCING MANUAL RECONCILIATION")
    
    # Initialize DB (migrations)
    init_db()
    
    # Initialize Reconciler
    recon = StateReconciler()
    
    # Run reconciliation
    results = recon.reconcile_all()
    
    logger.info(f"✅ RECONCILIATION FINISHED. results: {len(results)}")
    for res in results:
        logger.info(f"   - Bot {res.bot_name} ({res.bot_id}): {res.action_taken} | {res.details}")

if __name__ == "__main__":
    main()
