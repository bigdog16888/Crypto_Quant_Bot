"""Place TP orders for Bot 41 and 43"""
import sqlite3
from engine.exchange_interface import ExchangeInterface

def main():
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    
    print("=== Placing TP Orders ===\n")
    
    c.execute("""
        SELECT t.bot_id, b.name, b.pair, b.direction, t.total_invested, t.avg_entry_price, t.target_tp_price, t.current_step
        FROM trades t
        JOIN bots b ON t.bot_id = b.id
        WHERE t.bot_id IN (41, 43) AND t.total_invested > 0
    """)
    trades = c.fetchall()
    
    ex = ExchangeInterface(market_type='swap')
    
    # Check existing orders
    orders = ex.fetch_open_orders('BTC/USDC')
    print(f"Existing BTC/USDC orders: {len(orders)}")
    for o in orders:
        print(f"  {o.get('clientOrderId', 'N/A')} | ${o.get('price')}")
    
    print()
    
    for t in trades:
        bot_id, name, pair, direction, invested, entry, tp_price, step = t
        
        # Check if already has TP
        tag_prefix = f"CQB_{bot_id}_TP"
        my_tp = [o for o in orders if o.get('clientOrderId', '').startswith(tag_prefix)]
        
        if my_tp:
            print(f"Bot {bot_id} ({name}): ✅ Already has TP order")
            continue
        
        # Calculate qty
        qty = round(invested / entry, 4)
        side = 'sell' if direction == 'LONG' else 'buy'
        
        print(f"Bot {bot_id} ({name}):")
        print(f"  Entry: ${entry:.4f} | TP: ${tp_price:.4f}")
        print(f"  Qty: {qty} | Side: {side}")
        
        # Generate client ID
        import hashlib
        raw = f"CQB_{bot_id}_TP_{step}"
        client_id = f"CQB_{bot_id}_TP_{hashlib.md5(raw.encode()).hexdigest()[:8]}"
        
        try:
            result = ex.create_order(
                symbol=pair,
                type='limit',
                side=side,
                amount=qty,
                price=tp_price,
                params={'clientOrderId': client_id}
            )
            print(f"  ✅ TP Placed: {result.get('id')}")
            
            # Save to DB
            c.execute("""
                INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, client_order_id)
                VALUES (?, ?, 'tp', ?, ?, ?, 'open', ?)
            """, (bot_id, step, result.get('id'), tp_price, qty, client_id))
            conn.commit()
            print(f"  ✅ Saved to DB")
            
        except Exception as e:
            print(f"  ❌ Failed: {e}")
    
    conn.close()
    print("\nDone!")

if __name__ == "__main__":
    main()
