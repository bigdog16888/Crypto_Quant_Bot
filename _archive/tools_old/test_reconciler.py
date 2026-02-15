import sys
import os
import logging

# Add parent to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.reconciler import StateReconciler
from engine.database import get_connection

# Configure logging
logging.basicConfig(level=logging.INFO)

def test_reconciler():
    print("🧪 TESTING RECONCILER ISOLATION 🧪")
    print("====================================")
    
    # 1. Run Reconcile
    reconciler = StateReconciler()
    results = reconciler.reconcile_all()
    
    print("\n📊 RECONCILIATION RESULTS:")
    for res in results:
        print(f"   Bot {res.bot_id} ({res.bot_name}): {res.action_taken} | Owner: {res.position_owner}")

    # 2. Check DB
    print("\n💾 DATABASE STATE (Trades):")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT t.bot_id, b.name, t.total_invested FROM trades t JOIN bots b ON t.bot_id = b.id")
    rows = cursor.fetchall()
    
    for r in rows:
        print(f"   Bot {r[0]} ({r[1]}): ${r[2]:.2f}")
        
    print("====================================")

if __name__ == "__main__":
    test_reconciler()
