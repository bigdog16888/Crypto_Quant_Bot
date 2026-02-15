"""
Patch Script: Force sync TP orders for bots 41, 43, and 44
This fixes the missing TP orders on exchange.
"""
import sqlite3
from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def get_bot_trade_info(bot_id):
    """Get trade info for a bot"""
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    
    # Get bot info
    c.execute("SELECT name, pair, direction FROM bots WHERE id=?", (bot_id,))
    bot = c.fetchone()
    if not bot:
        conn.close()
        return None
    
    # Get trade info
    c.execute("SELECT current_step, total_invested, avg_entry_price, target_tp_price FROM trades WHERE bot_id=?", (bot_id,))
    trade = c.fetchone()
    
    conn.close()
    
    if not trade:
        return None
        
    return {
        'bot_id': bot_id,
        'name': bot[0],
        'pair': bot[1],
        'direction': bot[2],
        'step': trade[0],
        'invested': trade[1],
        'entry': trade[2],
        'tp_price': trade[3]
    }

def check_exchange_orders(pair, ex, tag_prefix):
    """Check exchange for existing orders with our tag"""
    try:
        orders = ex.fetch_open_orders(pair)
        my_orders = [o for o in orders if o.get('clientOrderId', '').startswith(tag_prefix)]
        return my_orders
    except Exception as e:
        logger.error(f"Error fetching orders: {e}")
        return []

def place_tp_order(bot_id, name, pair, direction, tp_price, qty, ex):
    """Place a TP order on exchange"""
    side = 'sell' if direction == 'LONG' else 'buy'
    
    # Generate deterministic client order ID
    import hashlib
    raw = f"CQB_{bot_id}_TP_0"
    hash_suffix = hashlib.md5(raw.encode()).hexdigest()[:8]
    client_order_id = f"CQB_{bot_id}_TP_{hash_suffix}"
    
    logger.info(f"Placing TP for {name}: {side} {qty} @ ${tp_price} (ID: {client_order_id})")
    
    try:
        result = ex.create_order(
            symbol=pair,
            type='limit',
            side=side,
            amount=qty,
            price=tp_price,
            params={'clientOrderId': client_order_id, 'reduceOnly': True}
        )
        logger.info(f"✅ TP Order placed: {result.get('id')}")
        return result
    except Exception as e:
        logger.error(f"❌ Failed to place TP: {e}")
        return None

def calculate_position_size(invested, entry_price):
    """Calculate position size from invested amount and entry price"""
    if entry_price <= 0:
        return 0
    return round(invested / entry_price, 4)

def main():
    print("=" * 60)
    print("FORCE PATCH: TP Order Sync")
    print("=" * 60)
    
    # Bots to patch
    btc_bots = [41, 43]
    gold_bots = [44]
    
    # Check BTC/USDC bots
    print("\n--- BTC/USDC Bots ---")
    ex_swap = ExchangeInterface(market_type='swap')
    
    # First, check what's on exchange
    btc_exchange_orders = ex_swap.fetch_open_orders('BTC/USDC')
    print(f"Exchange Orders (BTC/USDC): {len(btc_exchange_orders)}")
    for o in btc_exchange_orders:
        print(f"  {o.get('clientOrderId', 'N/A')} | {o['type']} | {o['side']} | ${o['price']}")
    
    for bot_id in btc_bots:
        info = get_bot_trade_info(bot_id)
        if not info or info['invested'] <= 0:
            print(f"Bot {bot_id}: Not in trade, skipping")
            continue
            
        print(f"\nBot {bot_id} ({info['name']}):")
        print(f"  Pair: {info['pair']} | Dir: {info['direction']}")
        print(f"  Entry: ${info['entry']:.4f} | TP Target: ${info['tp_price']:.4f}")
        print(f"  Invested: ${info['invested']:.2f}")
        
        # Check for existing TP order
        tag_prefix = f"CQB_{bot_id}_TP"
        my_tp = [o for o in btc_exchange_orders if o.get('clientOrderId', '').startswith(tag_prefix)]
        
        if my_tp:
            print(f"  ✅ TP already exists: {my_tp[0].get('clientOrderId')}")
            continue
        
        # Calculate qty and place TP
        qty = calculate_position_size(info['invested'], info['entry'])
        if qty <= 0:
            print(f"  ⚠️ Invalid qty: {qty}")
            continue
            
        print(f"  ⚠️ MISSING TP - Will place: {qty} @ ${info['tp_price']:.4f}")
        
        # Ask confirmation
        resp = input(f"  Place TP for Bot {bot_id}? (y/n): ").strip().lower()
        if resp == 'y':
            result = place_tp_order(
                bot_id, info['name'], info['pair'], 
                info['direction'], info['tp_price'], qty, ex_swap
            )
            if result:
                # Update DB
                conn = sqlite3.connect('crypto_bot.db')
                c = conn.cursor()
                c.execute("""
                    INSERT OR REPLACE INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, client_order_id)
                    VALUES (?, ?, 'tp', ?, ?, ?, 'open', ?)
                """, (bot_id, info['step'], result.get('id'), info['tp_price'], qty, result.get('clientOrderId')))
                conn.commit()
                conn.close()
                print(f"  ✅ DB Updated")
    
    # Check Gold bot
    print("\n--- XAU/USDT Bot ---")
    ex_future = ExchangeInterface(market_type='future')
    
    xau_exchange_orders = ex_future.fetch_open_orders('XAU/USDT')
    print(f"Exchange Orders (XAU/USDT): {len(xau_exchange_orders)}")
    for o in xau_exchange_orders:
        print(f"  {o.get('clientOrderId', 'N/A')} | {o['type']} | {o['side']} | ${o['price']}")
    
    for bot_id in gold_bots:
        info = get_bot_trade_info(bot_id)
        if not info:
            print(f"Bot {bot_id}: No trade info")
            continue
            
        print(f"\nBot {bot_id} ({info['name']}):")
        print(f"  Pair: {info['pair']} | Dir: {info['direction']}")
        print(f"  Entry: ${info['entry']:.4f} | TP Target: ${info['tp_price']:.4f}")
        print(f"  Invested: ${info['invested']:.2f} | Step: {info['step']}")
        
        # Check for existing TP order
        tag_prefix = f"CQB_{bot_id}_TP"
        my_tp = [o for o in xau_exchange_orders if o.get('clientOrderId', '').startswith(tag_prefix)]
        
        if my_tp:
            print(f"  ✅ TP exists on exchange: {my_tp[0].get('clientOrderId')}")
            
            # Sync to DB if missing
            conn = sqlite3.connect('crypto_bot.db')
            c = conn.cursor()
            c.execute("SELECT id FROM bot_orders WHERE bot_id=? AND order_type='tp' AND status='open'", (bot_id,))
            db_tp = c.fetchone()
            
            if not db_tp:
                print(f"  ⚠️ TP missing from DB - Syncing...")
                o = my_tp[0]
                c.execute("""
                    INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, client_order_id)
                    VALUES (?, ?, 'tp', ?, ?, ?, 'open', ?)
                """, (bot_id, info['step'], o.get('id'), o.get('price'), o.get('amount', 0), o.get('clientOrderId')))
                conn.commit()
                print(f"  ✅ Synced to DB")
            conn.close()
        else:
            print(f"  ⚠️ No TP on exchange!")
    
    print("\n" + "=" * 60)
    print("PATCH COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    main()
