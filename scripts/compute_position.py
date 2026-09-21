import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# Get bots for BTC/USDC
c.execute("""SELECT id, pair, direction FROM bots WHERE pair = 'BTC/USDC:USDC'""")
bots = {row[0]: {'pair': row[1], 'direction': row[2]} for row in c.fetchall()}
print('Bots:', bots)

# Get bot_orders for these bots
c.execute("""SELECT bot_id, order_type, amount FROM bot_orders WHERE bot_id IN (10001,10002,10003,10004,10005,10006,10016,10022,100317,100318,200000,200001,200002,200003,351911,467712,574485,582361,621668,705780,748892,847718)""")
orders = c.fetchall()

# Compute net position per bot: assume amount is positive, need to determine if it's buy or sell based on bot direction and order_type
# For simplicity, we'll assume:
# - For LONG bots: entry, grid, tp, flatten_close are BUY (positive), but actually tp and flatten_close are SELL (negative)
# - For SHORT bots: entry, grid are SELL (negative), tp, flatten_close are BUY (positive)
# However we don't have exact mapping. Let's instead look at the trades table which has position_side and open_qty.
# But let's first try with bot_orders using heuristic: if order_type contains 'entry' or 'grid' treat as opening position, if contains 'tp' or 'flatten_close' treat as closing.
# We'll need to know the side of the opening.

# Let's get the bots direction and compute net as:
# For each order:
#   if direction == 'LONG':
#       if order_type in ('entry', 'grid'): change = +amount
#       elif order_type in ('tp', 'flatten_close'): change = -amount
#       else: change = 0
#   else if direction == 'SHORT':
#       if order_type in ('entry', 'grid'): change = -amount
#       elif order_type in ('tp', 'flatten_close'): change = +amount
#       else: change = 0
net = {}
for bot_id, order_type, amount in orders:
    if bot_id not in bots:
        continue
    direction = bots[bot_id]['direction']
    change = 0
    if direction == 'LONG':
        if order_type in ('entry', 'grid'):
            change = amount
        elif order_type in ('tp', 'flatten_close'):
            change = -amount
    elif direction == 'SHORT':
        if order_type in ('entry', 'grid'):
            change = -amount
        elif order_type in ('tp', 'flatten_close'):
            change = amount
    net[bot_id] = net.get(bot_id, 0) + change

print('\nNet position from bot_orders (heuristic):')
total_net = 0
for bot_id, qty in net.items():
    print('Bot {} ({}): {:.8f}'.format(bot_id, bots[bot_id]['direction'], qty))
    total_net += qty
print('Total net: {:.8f} BTC'.format(total_net))

# Get exchange position from unowned_position_alerts for BTCUSDC
c.execute("""SELECT SUM(exchange_qty), SUM(db_qty) FROM unowned_position_alerts WHERE normalized_pair = 'BTCUSDC'""")
row = c.fetchone()
exchange_sum = row[0] if row[0] is not None else 0
db_sum = row[1] if row[1] is not None else 0
print('\nFrom unowned_position_alerts:')
print('  Sum exchange_qty: {:.8f}'.format(exchange_sum))
print('  Sum db_qty: {:.8f}'.format(db_sum))
print('  Difference (exchange - db): {:.8f}'.format(exchange_sum - db_sum))

# Also compute per alert
c.execute("""SELECT id, bot_id, exchange_qty, db_qty FROM unowned_position_alerts WHERE normalized_pair = 'BTCUSDC' ORDER BY id""")
alerts = c.fetchall()
print('\nIndividual alerts:')
for alert_id, bot_id, ex_qty, db_qty in alerts:
    drift = ex_qty - db_qty
    print('  Alert {}: bot_id={}, exchange_qty={:.8f}, db_qty={:.8f}, drift={:.8f}'.format(alert_id, bot_id, ex_qty, db_qty, drift))

conn.close()