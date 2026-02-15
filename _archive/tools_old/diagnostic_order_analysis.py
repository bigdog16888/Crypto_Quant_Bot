"""
Diagnostic Script: Order Analysis
Compares database state vs exchange state to identify missing orders
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from pathlib import Path
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface
from config.settings import config

DB_PATH = Path(__file__).parent.parent / "crypto_bot.db"

def analyze_orders():
    print("=" * 80)
    print("DIAGNOSTIC: Order Analysis")
    print("=" * 80)
    
    # Initialize exchange
    print("\n📡 Connecting to exchange...")
    try:
        exchange = ExchangeInterface(market_type=config.MARKET_TYPE)
        print(f"✅ Connected to {config.MARKET_TYPE}")
    except Exception as e:
        print(f"❌ Failed to connect to exchange: {e}")
        return
    
    # Get database state
    print("\n📊 DATABASE STATE:")
    print("-" * 80)
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get bots in trade
    cursor.execute("SELECT bot_id FROM trades")
    bots_in_trade = [row[0] for row in cursor.fetchall()]
    print(f"Bots in trade: {len(bots_in_trade)} → {bots_in_trade}")
    
    # Get orders from bot_orders table
    cursor.execute("""
        SELECT bot_id, order_type, order_id, price, amount, status, step, client_order_id
        FROM bot_orders 
        WHERE status = 'open'
        ORDER BY bot_id, order_type
    """)
    db_orders = cursor.fetchall()
    print(f"\nOpen orders in DB: {len(db_orders)}")
    for order in db_orders:
        bot_id, order_type, order_id, price, amount, status, step, client_id = order
        print(f"  Bot {bot_id}: {order_type} @ ${price} | {amount} | OrderID={order_id} | ClientID={client_id}")
    
    # Get trade details
    cursor.execute("""
        SELECT t.bot_id, b.name, b.pair, t.current_step, t.total_invested, 
               t.avg_entry_price, t.target_tp_price, t.basket_start_time
        FROM trades t
        JOIN bots b ON t.bot_id = b.id
    """)
    trades = cursor.fetchall()
    print(f"\nActive trades: {len(trades)}")
    for trade in trades:
        bot_id, name, pair, step, invested, avg_price, tp_price, start_time = trade
        print(f"  Bot {bot_id} ({name}): {pair} | Step={step} | Invested=${invested:.2f} | AvgEntry=${avg_price} | TP=${tp_price}")
    
    # Get ownership state
    cursor.execute("""
        SELECT bot_id, pair, state, is_owner, position_size, avg_entry_price, target_tp_price
        FROM bot_ownership_state
        WHERE state IN ('owner', 'passenger', 'pending_tp')
    """)
    ownership = cursor.fetchall()
    print(f"\nOwnership states: {len(ownership)}")
    for own in ownership:
        bot_id, pair, state, is_owner, pos_size, entry, tp = own
        owner_label = "OWNER" if is_owner else "PASSENGER"
        print(f"  Bot {bot_id}: {pair} | {owner_label} ({state}) | Pos=${pos_size:.2f} | Entry=${entry} | TP=${tp}")
    
    # Get EXCHANGE state
    print("\n📡 EXCHANGE STATE:")
    print("-" * 80)
    
    try:
        # Get positions
        positions = exchange.fetch_positions()
        open_positions = [p for p in positions if float(p.get('contracts', 0)) != 0]
        print(f"Open positions: {len(open_positions)}")
        for pos in open_positions:
            symbol = pos.get('symbol')
            size = pos.get('contracts', 0)
            side = pos.get('side')
            entry_price = pos.get('entryPrice', 0)
            unrealized_pnl = pos.get('unrealizedPnl', 0)
            print(f"  {symbol}: {side} {size} @ ${entry_price} | uPnL=${unrealized_pnl:.2f}")
        
        # Get orders
        all_orders = []
        unique_pairs = set()
        
        # Get pairs from trades
        for trade in trades:
            unique_pairs.add(trade[2])  # pair
        
        # Also check positions
        for pos in open_positions:
            unique_pairs.add(pos.get('symbol'))
        
        print(f"\nFetching orders for {len(unique_pairs)} pairs: {unique_pairs}")
        
        for pair in unique_pairs:
            try:
                orders = exchange.fetch_open_orders(pair)
                all_orders.extend(orders)
                print(f"  {pair}: {len(orders)} orders")
            except Exception as e:
                print(f"  {pair}: Error fetching orders - {e}")
        
        print(f"\nTotal open orders on exchange: {len(all_orders)}")
        for order in all_orders:
            order_id = order.get('id')
            symbol = order.get('symbol')
            side = order.get('side')
            order_type = order.get('type')
            price = order.get('price', 0)
            amount = order.get('amount', 0)
            client_id = order.get('clientOrderId', 'N/A')
            print(f"  {symbol}: {side} {order_type} @ ${price} | {amount} | ID={order_id} | ClientID={client_id}")
    
    except Exception as e:
        print(f"❌ Error fetching exchange data: {e}")
    
    # ANALYSIS
    print("\n🔍 ANALYSIS:")
    print("-" * 80)
    
    # Expected vs Actual
    print(f"\n1. POSITION SYNC:")
    print(f"   DB: {len(bots_in_trade)} bots in trade")
    print(f"   Exchange: {len(open_positions)} open positions")
    if len(bots_in_trade) == len(open_positions):
        print(f"   ✅ MATCH")
    else:
        print(f"   ❌ MISMATCH - Ghost trades or orphan positions detected!")
    
    print(f"\n2. ORDER SYNC:")
    print(f"   DB: {len(db_orders)} open orders")
    print(f"   Exchange: {len(all_orders)} open orders")
    if len(db_orders) == len(all_orders):
        print(f"   ✅ MATCH")
    else:
        print(f"   ❌ MISMATCH - Orders not synced!")
    
    print(f"\n3. EXPECTED ORDERS PER BOT:")
    for trade in trades:
        bot_id, name, pair, step, invested, avg_price, tp_price, start_time = trade
        
        # Check ownership
        is_owner = False
        for own in ownership:
            if own[0] == bot_id:  # bot_id
                is_owner = own[3]  # is_owner
                break
        
        # Expected orders
        if is_owner:
            expected_orders = 2  # 1 TP + 1 GRID (at minimum)
            print(f"   Bot {bot_id} ({name}) - OWNER:")
        else:
            expected_orders = 0  # Passengers don't place orders
            print(f"   Bot {bot_id} ({name}) - PASSENGER:")
        
        # Actual orders
        actual_orders = [o for o in db_orders if o[0] == bot_id]
        print(f"     Expected: {expected_orders} orders (TP + GRID)")
        print(f"     Actual DB: {len(actual_orders)} orders")
        
        if len(actual_orders) < expected_orders:
            print(f"     ⚠️ MISSING {expected_orders - len(actual_orders)} ORDERS!")
        elif len(actual_orders) > expected_orders:
            print(f"     ⚠️ EXTRA {len(actual_orders) - expected_orders} ORDERS!")
        else:
            print(f"     ✅ Correct")
    
    print(f"\n4. ORDER PLACEMENT TIMELINE:")
    cursor.execute("""
        SELECT bot_id, order_type, order_id, created_at, client_order_id
        FROM bot_orders
        WHERE status = 'open'
        ORDER BY created_at DESC
        LIMIT 20
    """)
    recent_orders = cursor.fetchall()
    print(f"   Last 20 open orders:")
    for order in recent_orders:
        bot_id, order_type, order_id, created_at, client_id = order
        from datetime import datetime
        timestamp = datetime.fromtimestamp(created_at).strftime('%Y-%m-%d %H:%M:%S')
        print(f"     {timestamp} - Bot {bot_id}: {order_type} | ID={order_id} | ClientID={client_id}")
    
    print(f"\n5. NOTIFICATION HISTORY:")
    cursor.execute("""
        SELECT timestamp, type, message, bot_id
        FROM notifications
        ORDER BY timestamp DESC
        LIMIT 10
    """)
    notifications = cursor.fetchall()
    print(f"   Last 10 notifications:")
    for notif in notifications:
        timestamp, notif_type, message, bot_id = notif
        from datetime import datetime
        ts = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
        print(f"     {ts} - [{notif_type}] Bot {bot_id}: {message[:60]}")
    
    conn.close()
    
    print("\n" + "=" * 80)
    print("DIAGNOSTIC COMPLETE")
    print("=" * 80)

if __name__ == "__main__":
    analyze_orders()
