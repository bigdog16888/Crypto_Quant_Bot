import logging
from engine.reconciler import StateReconciler
logging.basicConfig(level=logging.ERROR)
r = StateReconciler()
r.prime_startup_snapshot()
results = r.reconcile_all(force_adoption=True)
for res in results:
    print(f"Bot {res.bot_name} ({res.pair}): Action -> {res.action_taken.name}\nDetails: {res.details}\n")
