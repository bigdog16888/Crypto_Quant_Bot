"""Reset DB to match a flat exchange (no positions, no orders)."""
import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

import time

# Reset ALL trades to clean state
# CRITICAL: basket_start_time must be set to the current time, NOT 0!
# If set to 0, the Reconciler will sweep the last 24 hours of exchange trades
# and randomly adopt old trades (like our recent Surgical Flattens) as "new" entries!
c.execute(f'UPDATE trades SET total_invested=0, current_step=0, avg_entry_price=0, entry_confirmed=0, basket_start_time={int(time.time())}, target_tp_price=0, entry_order_id=NULL, tp_order_id=NULL')
print(f'Reset {c.rowcount} trade rows with current timestamp {int(time.time())}')

# Reset all active bot statuses to Scanning
c.execute("UPDATE bots SET status='Scanning' WHERE is_active=1")
print(f'Reset {c.rowcount} active bot statuses to Scanning')

# Delete all bot orders and trade history to prevent Reconciler offline-fill time travel bugs
c.execute("DELETE FROM bot_orders")
print(f'Deleted {c.rowcount} bot_orders (preventing ghost fills)')

c.execute("DELETE FROM trade_history")
print(f'Deleted {c.rowcount} trade_history rows')

conn.commit()

# Verify
c.execute('SELECT id, name, status, is_active FROM bots WHERE is_active=1')
print('\n=== ACTIVE BOTS (post-reset) ===')
for r in c.fetchall():
    print(r)

c.execute('SELECT bot_id, total_invested, current_step, avg_entry_price FROM trades')
print('\n=== TRADES (post-reset) ===')
for r in c.fetchall():
    print(r)

conn.close()
print('\nDB reset complete — matches flat exchange')
