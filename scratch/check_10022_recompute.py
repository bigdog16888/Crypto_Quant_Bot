from engine.database import recompute_invested_from_orders
import logging

logging.basicConfig(level=logging.INFO)
bot_id = 10022
res = recompute_invested_from_orders(bot_id)
print(f"Bot {bot_id} Recompute Results: {res}")
