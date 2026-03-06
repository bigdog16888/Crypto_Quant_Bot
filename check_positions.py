import sys, os, sqlite3
from dotenv import load_dotenv
import ccxt

load_dotenv()

try:
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    ex = ccxt.binance({
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_API_SECRET'),
        'options': {'defaultType': 'future'}
    })
    ex.options['warnOnFetchOpenOrdersWithoutSymbol'] = False
    
    # Get active pairs first
    c.execute("SELECT DISTINCT pair FROM bots WHERE status IN ('IN TRADE', 'Scanning')")
    pairs = [row[0] for row in c.fetchall()]
    
    exchange_cids = []
    
    # Fetch orders per pair
    for pair in pairs:
        try:
            # Reformat CQB engine pair format (BTC/USDC:USDC) to CCXT CCXT format (BTC/USDC)
            ccxt_pair = pair.split(':')[0]
            orders = ex.fetch_open_orders(ccxt_pair)
            for o in orders:
                cid = o.get('clientOrderId', '')
                if cid.startswith('CQB_'):
                    exchange_cids.append(cid)
        except Exception as e:
            pass

    c.execute("SELECT bot_id, order_type, client_order_id FROM bot_orders WHERE status='open'")
    db_orders = c.fetchall()
    
    missing = [o for o in db_orders if o[2] not in exchange_cids]
    print(f"Total Physical Open Orders (CQB): {len(exchange_cids)}")
    print(f"Total Database Open Orders: {len(db_orders)}")
    print("--- MISSING ORDERS FROM EXCHANGE ---")
    for m in missing:
        print(f"Bot {m[0]} -> {m[1]} ({m[2]})")

    print("\n--- POSITIONS ---")
    positions = ex.fetch_positions()
    
    c.execute("SELECT b.id, b.name, b.pair, t.total_invested, t.avg_entry_price, b.direction FROM bots b JOIN trades t ON b.id = t.bot_id WHERE t.total_invested > 0")
    bots = c.fetchall()
    
    aggregated_virtual = 0.0
    aggregated_physical = 0.0
    for bot in bots:
        bid, name, pair, inv, avg_p, direction = bot
        qty = inv / avg_p if avg_p > 0 else 0
        aggregated_virtual += inv
        
        # normalize pair symbol
        norm_pair = pair.split(':')[0].replace('/', '')
        phys = next((p for p in positions if p.get('symbol', '').replace('/', '') == norm_pair), None)
        
        if phys:
            phys_size = float(phys.get('contracts', 0))
            phys_notional = float(phys.get('notional', 0))
            aggregated_physical += abs(phys_notional)
            diff = abs(inv - abs(phys_notional))
            # Print if there's a difference > $0.10
            if diff > 0.10:
                print(f"⚠️ MISMATCH | Bot {name} ({bid}) on {pair} | System Qty: {qty:.4f} (${inv:.2f}) | Exch Qty: {phys_size:.4f} (${abs(phys_notional):.2f}) | Diff: ${diff:.2f}")
        else:
            print(f"⚠️ NO PHYSICAL POS | Bot {name} ({bid}) on {pair} | System expects Qty: {qty:.4f} (${inv:.2f})")
            
    print(f"\nTotal System Invested: ${aggregated_virtual:.2f}")
    print(f"Total Exchange Notional (Matched pairs): ${aggregated_physical:.2f}")

except Exception as e:
    import traceback
    traceback.print_exc()
