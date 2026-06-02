from engine.exchange_interface import ExchangeInterface
import logging

logging.basicConfig(level=logging.INFO)
exchange = ExchangeInterface(market_type='future')

order_ids = [
    ('103893091', 5.8),
    ('103893184', 13.4),
    ('103972946', 31.3),
    ('103983493', 72.2),
    ('104020100', 167.1),
    ('104072468', 387.1),
    ('104671645', 883.4),
    ('104751160', 263.7)
]

print("--- Comparing Database Fills vs Exchange Fills ---")
total_db = 0.0
total_ex = 0.0
for oid, db_qty in order_ids:
    total_db += db_qty
    try:
        # Fetch order from exchange using the correct wrapper
        order = exchange.fetch_order(oid, 'SUI/USDC:USDC')
        if order:
            ex_qty = float(order.get('filled', 0))
            status = order.get('status')
            total_ex += ex_qty
            diff = db_qty - ex_qty
            print(f"Order {oid}: DB={db_qty:.1f}, Exchange={ex_qty:.1f}, Diff={diff:.1f}, Status={status}")
        else:
            print(f"Order {oid}: Fetch returned None")
    except Exception as e:
        print(f"Order {oid}: Error fetching from exchange: {e}")

print(f"\nSummary: Total DB={total_db:.1f}, Total Exchange={total_ex:.1f}, Diff={total_db - total_ex:.1f}")
