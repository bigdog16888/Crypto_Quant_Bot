"""Quick diagnostic script to check BTC and Gold bot status"""
import sqlite3
from engine.exchange_interface import ExchangeInterface

def main():
    print("=" * 60)
    print("BOT STATUS CHECK")
    print("=" * 60)
    
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    
    print("\n=== BOTS IN TRADE (DB) ===")
    c.execute("""
        SELECT t.bot_id, b.name, b.pair, t.total_invested, t.current_step, t.avg_entry_price
        FROM trades t
        JOIN bots b ON t.bot_id = b.id
        WHERE t.total_invested > 0
    """)
    trades = c.fetchall()
    for t in trades:
        print(f"  Bot {t[0]} ({t[1]}): {t[2]} | Step {t[4]} | Invested ${t[3]:.2f} | Entry ${t[5]:.4f}")
    
    print("\n=== OPEN ORDERS IN DB ===")
    c.execute("""
        SELECT o.bot_id, b.name, o.order_type, o.order_id, o.status, o.price
        FROM bot_orders o
        JOIN bots b ON o.bot_id = b.id
        WHERE o.status = 'open'
        ORDER BY o.bot_id
    """)
    orders = c.fetchall()
    for o in orders:
        print(f"  Bot {o[0]} ({o[1]}): {o[2]} | ID: {o[3]} | ${o[5]} | Status: {o[4]}")
    
    print("\n=== ORDER COUNT PER BOT ===")
    c.execute("""
        SELECT o.bot_id, b.name, COUNT(*) as cnt
        FROM bot_orders o
        JOIN bots b ON o.bot_id = b.id
        WHERE o.status = 'open'
        GROUP BY o.bot_id
    """)
    counts = c.fetchall()
    for ct in counts:
        print(f"  Bot {ct[0]} ({ct[1]}): {ct[2]} orders")
    
    conn.close()
    
    print("\n=== EXCHANGE OPEN ORDERS (BTC/USDC) ===")
    try:
        ex = ExchangeInterface(market_type='swap')
        btc_orders = ex.fetch_open_orders('BTC/USDC')
        print(f"Count: {len(btc_orders)}")
        for o in btc_orders:
            tag = o.get('clientOrderId', o.get('info', {}).get('clientOrderId', 'N/A'))
            print(f"  {o['id']} | {o['type']} | {o['side']} | ${o['price']} | Tag: {tag}")
    except Exception as e:
        print(f"Error: {e}")
    
    print("\n=== EXCHANGE OPEN ORDERS (XAU/USDT) ===")
    try:
        ex2 = ExchangeInterface(market_type='future')
        xau_orders = ex2.fetch_open_orders('XAU/USDT')
        print(f"Count: {len(xau_orders)}")
        for o in xau_orders:
            tag = o.get('clientOrderId', o.get('info', {}).get('clientOrderId', 'N/A'))
            print(f"  {o['id']} | {o['type']} | {o['side']} | ${o['price']} | Tag: {tag}")
    except Exception as e:
        print(f"Error: {e}")
    
    print("\n=== EXCHANGE POSITIONS ===")
    try:
        ex = ExchangeInterface(market_type='swap')
        positions = ex.get_positions()
        active = [p for p in positions if abs(float(p.get('contracts', 0))) > 0]
        for p in active:
            sym = p.get('symbol', 'Unknown')
            side = p.get('side', 'Unknown')
            size = p.get('contracts', 0)
            entry = p.get('entryPrice', 0)
            print(f"  {sym}: {side} | Size: {size} | Entry: ${entry}")
        if not active:
            print("  (No open positions on swap)")
    except Exception as e:
        print(f"Error: {e}")

    try:
        ex2 = ExchangeInterface(market_type='future')
        positions2 = ex2.get_positions()
        active2 = [p for p in positions2 if abs(float(p.get('contracts', 0))) > 0]
        for p in active2:
            sym = p.get('symbol', 'Unknown')
            side = p.get('side', 'Unknown')
            size = p.get('contracts', 0)
            entry = p.get('entryPrice', 0)
            print(f"  {sym}: {side} | Size: {size} | Entry: ${entry}")
        if not active2:
            print("  (No open positions on future)")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
