import ccxt
import sqlite3
import json
import os

def run():
    ex = ccxt.binance({
        'apiKey': 'ip4npmnpIq1JxFYcMyJnVhgHJhIi1P9AnKX3UvLlAz3z2SD0XZF3OwrjzIZvm7Hq',
        'secret': 'ew5OZhCaJd3YS7BLmIUsvgbwb8obvN0n4YXzXYaImsLa298ZAXe0kkSQRdCaWHIS',
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
    ex.set_sandbox_mode(True)

    print("--- 1. EXCHANGE POSITIONS ---")
    exchange_positions = {}
    try:
        positions = ex.fetch_positions()
        for p in positions:
            size_raw = p.get('contracts', p.get('positionAmt', 0))
            if size_raw is None: size_raw = 0
            size = float(size_raw)
            if size != 0:
                sym = str(p['symbol']).split(':')[0].strip()
                notional = float(p.get('notional', 0))
                entry_price = float(p.get('entryPrice', 0))
                phys_val = size * entry_price
                print(f"Exchange {sym}: Size={size}, Entry={entry_price}, Calc_Invested=${phys_val:.2f}, API_Notional=${notional:.2f}")
                exchange_positions[sym] = phys_val
    except Exception as e:
        print("Error:", e)

    print("\n--- 2. DB VIRTUAL POSITIONS ---")
    conn = sqlite3.connect('C:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/crypto_bot.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT b.id, b.name, b.pair, b.direction, t.total_invested, t.avg_entry_price, t.current_step, b.config
        FROM bots b
        LEFT JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active = 1
    """)
    bots = c.fetchall()

    db_positions = {}
    expected_orders = 0
    active_pairs = set()

    for b in bots:
        pair = str(b['pair']).split(':')[0].strip()
        active_pairs.add(b['pair'])
        direction = b['direction'].upper()
        invested = float(b['total_invested'] or 0.0)
        step = int(b['current_step'] or 0)
        
        cfg = json.loads(b['config'] or '{}')
        max_steps = int(cfg.get('max_steps', 10))
        
        # Calculate Expected Orders roughly
        if invested > 0:
            exp = 1 if step >= max_steps else 2
        else:
            exp = 1 # Scanning typically has 0 but ui logic is fuzzy
            
        print(f"Bot {b['id']} ({b['name']}): {pair} {direction} - Invested=${invested:.2f}, Step={step}/{max_steps}, ExpectedReq={exp}")
        
        if invested > 0:
            if pair not in db_positions:
                db_positions[pair] = 0.0
            if 'SHORT' in direction:
                db_positions[pair] -= invested
            else:
                db_positions[pair] += invested
                
            expected_orders += exp
            
    print(f"\nNet DB Positions: {db_positions}")
    print(f"Net EX Positions: {exchange_positions}")
    
    print("\n--- 3. DRIFT ANALYSIS ---")
    for sym in set(list(db_positions.keys()) + list(exchange_positions.keys())):
        db_val = db_positions.get(sym, 0.0)
        ex_val = exchange_positions.get(sym, 0.0)
        diff = db_val - ex_val
        print(f"{sym}: DB=${db_val:.2f} | EX=${ex_val:.2f} | Drift=${diff:.2f}")

    print("\n--- 4. EXCHANGE OPEN ORDERS ---")
    all_ords = []
    for p in active_pairs:
        try:
            ords = ex.fetch_open_orders(p)
            all_ords.extend(ords)
        except Exception as e:
            print(f"Error {p}: {e}")

    # deduplicate
    unique_orders = list({o['id']: o for o in all_ords}.values())
    print(f"Found {len(unique_orders)} Orders (Expected Invested: {expected_orders}):")
    for o in unique_orders:
        print(f"  {o['symbol']} - ID {o['id']} ({o.get('clientOrderId','')}) - {o['side']} {o['type']} QTY={o['amount']} @ {o['price']}")

if __name__ == '__main__':
    run()
