import sys
import os
import time

# Add the project root to the path
sys.path.insert(0, 'D:/Crypto_Quant_Bot')

from engine.database import get_connection, get_all_active_trades_for_pair
from engine.exchange_interface import ExchangeInterface
from config.settings import config

def main():
    symbol = 'XAUUSDT'
    print(f"Checking active trades for {symbol}...")
    
    # Get active trades for the pair
    trades = get_all_active_trades_for_pair(symbol)
    if not trades:
        print(f"No active trades found for {symbol}.")
        return
    
    # Calculate net quantity per bot
    bot_net = {}
    for trade in trades:
        bot_id = trade['bot_id']
        qty = trade['quantity']
        side = trade['side']
        if side.upper() == 'BUY':
            bot_net[bot_id] = bot_net.get(bot_id, 0) + qty
        else:  # SELL
            bot_net[bot_id] = bot_net.get(bot_id, 0) - qty
    
    print(f"Net quantities per bot: {bot_net}")
    
    # Identify bots with short positions (negative net)
    shorts = {bot_id: qty for bot_id, qty in bot_net.items() if qty < 0}
    if not shorts:
        print(f"No short positions found for {symbol}.")
        return
    
    print(f"Short positions to close: {shorts}")
    
    # Get exchange interface
    exchange = ExchangeInterface(market_type='future')
    
    # Get database connection and start transaction
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # We'll collect results to show at the end
        results = []
        
        for bot_id, net_qty in shorts.items():
            amount = abs(net_qty)  # positive amount to buy
            print(f"\nPlacing market BUY order for bot {bot_id}: {amount} {symbol}")
            
            # Determine cycle_id: use the most recent cycle_id for this bot from bot_orders, or 0 if none
            cursor.execute(
                "SELECT cycle_id FROM bot_orders WHERE bot_id = ? AND symbol = ? ORDER BY id DESC LIMIT 1",
                (bot_id, symbol)
            )
            row = cursor.fetchone()
            cycle_id = row[0] if row else 0
            print(f"  Using cycle_id: {cycle_id}")
            
            # Generate a client order ID (CQB_ prefix will be added by create_order_with_receipt if not provided)
            # We'll let the function auto-generate it by not providing newClientOrderId in params.
            params = {}
            
            # Call create_order_with_receipt
            result = exchange.create_order_with_receipt(
                cursor=cursor,
                bot_id=bot_id,
                cycle_id=cycle_id,
                symbol=symbol,
                type='MARKET',
                side='BUY',
                amount=amount,
                cqb_order_type='close',  # We choose 'close' as the order type
                price=None,
                params=params,
                post_only=False,
                human_approved=False,
                _call_site="flatten_xauusdt.py:main"
            )
            
            print(f"  Order result: {result}")
            results.append((bot_id, amount, result))
        
        # Commit the transaction
        conn.commit()
        print("\nAll orders placed successfully. Transaction committed.")
        
        # Print summary
        print("\n=== SUMMARY ===")
        for bot_id, amount, result in results:
            order_id = result.get('id', 'N/A')
            client_order_id = result.get('clientOrderId', 'N/A')
            status = result.get('status', 'N/A')
            print(f"Bot {bot_id}: BUY {amount} {symbol} -> Order ID: {order_id}, Client ID: {client_order_id}, Status: {status}")
            
    except Exception as e:
        print(f"\nError occurred: {e}")
        print("Rolling back transaction...")
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    main()