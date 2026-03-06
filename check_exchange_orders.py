import sys, os, sqlite3, json
sys.path.append(os.getcwd())
try:
    import ccxt
    import config

    exchange = getattr(ccxt, config.BINANCE_EXCHANGE_ID)({
        'apiKey': config.BINANCE_API_KEY,
        'secret': config.BINANCE_API_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })

    orders = exchange.fetch_open_orders()
    exchange_cids = [o.get('clientOrderId', '') for o in orders if o.get('clientOrderId', '').startswith('CQB_')]
    
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    c.execute("SELECT bot_id, order_type, client_order_id, pair FROM bot_orders WHERE status='open'")
    db_orders = c.fetchall()
    
    missing = [o for o in db_orders if o[2] not in exchange_cids]
    print(f"Total Physical Open Orders (CQB): {len(exchange_cids)}")
    print(f"Total Database Open Orders: {len(db_orders)}")
    print("--- MISSING ORDERS FROM EXCHANGE ---")
    for m in missing:
        print(f"Bot {m[0]} | Type: {m[1]} | CID: {m[2]} | Pair: {m[3]}")
        
    c.execute("SELECT b.id, b.name, b.pair, t.total_invested, t.avg_entry_price, b.direction FROM bots b JOIN trades t ON b.id = t.bot_id WHERE t.total_invested > 0")
    bots = c.fetchall()
    print("\n--- POSITIONS ---")
    total_sys = 0.0
    for bot in bots:
        bid, name, pair, inv, avg_p, direction = bot
        if avg_p > 0:
            qty = inv / avg_p
            total_sys += inv
        print(f"Bot {name} ({bid}): Invested=${inv:.4f} Qty={qty:.6f} {direction}")
        
    print(f"Total System Invested: ${total_sys:.4f}")
    
except Exception as e:
    print("Error:", e)
