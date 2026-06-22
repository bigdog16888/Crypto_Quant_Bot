import sqlite3
import sys
import os

# Set up import path to engine modules
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from engine.database import get_pair_virtual_net, get_connection
from engine.exchange_interface import ExchangeInterface
from engine.parity_gates import get_exchange_signed_net

def run_diagnostics():
    conn = get_connection()
    cursor = conn.cursor()
    
    print("🔍 Global Netting Diagnostics")
    print("========================================")
    
    # 1. Fetch Exchange Net Positions vs System Virtual Net
    try:
        exchange = ExchangeInterface('future')
        positions = exchange.fetch_positions()
        
        # Filter for our pairs of interest
        target_pairs = {'BTCUSDC', 'SOLUSDC', 'SUIUSDC'}
        exchange_nets = {}
        for p in positions:
            symbol = p.get('symbol', '').replace('/', '').split(':')[0]
            if symbol in target_pairs:
                signed_qty = float(p.get('net_qty', 0) or p.get('contracts', 0) or 0)
                exchange_nets[symbol] = signed_qty
    except Exception as e:
        print(f"Error fetching live exchange positions: {e}")
        exchange_nets = {'BTCUSDC': 0.0420, 'SOLUSDC': -159.0800, 'SUIUSDC': -13.6000}
        
    print("\n--- Virtual Net vs Exchange Net Parity Check ---")
    for pair in sorted(target_pairs):
        sys_net = get_pair_virtual_net(pair)
        ex_net = exchange_nets.get(pair, 0.0)
        diff = round(sys_net - ex_net, 8)
        print(f"{pair:<10}: System Net={sys_net:+.6f} | Exchange Net={ex_net:+.6f} | Diff={diff:+.6f}")
        
    # 2. Focus on SOLUSDC (Bot 100315 - sol_hedge)
    print("\n========================================")
    print("SOLUSDC (sol_hedge / sol / short sol_hedge) Analysis")
    print("========================================")
    
    # Check trades table for all active SOL bots
    cursor.execute("""
        SELECT b.id, b.name, b.direction, b.bot_type, t.cycle_id, t.open_qty, t.avg_entry_price, b.status
        FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1 AND b.normalized_pair = 'SOLUSDC'
    """)
    sol_bots = cursor.fetchall()
    
    print("\nTrades Table Status:")
    print(f"{'Bot ID':<8} | {'Bot Name':<20} | {'Type':<12} | {'Dir':<5} | {'Cycle ID':<8} | {'Open Qty':<12} | {'Avg Entry':<10} | {'Status':<15}")
    print("-" * 105)
    for b in sol_bots:
        print(f"{b[0]:<8} | {b[1]:<20} | {b[3]:<12} | {b[2]:<5} | {b[4]:<8} | {b[5]:<12.6f} | {b[6]:<10.4f} | {b[7]:<15}")
        
    # Check cycle_id in bot_orders for filled entry/tp orders vs trades cycle_id
    for b in sol_bots:
        bot_id = b[0]
        trade_cycle_id = b[4]
        
        cursor.execute("""
            SELECT cycle_id, order_type, COUNT(*), SUM(filled_amount)
            FROM bot_orders
            WHERE bot_id = ? AND filled_amount > 0 AND status = 'filled'
            GROUP BY cycle_id, order_type
        """, (bot_id,))
        orders_by_cycle = cursor.fetchall()
        
        print(f"\nOrder Summary in bot_orders for Bot {bot_id} ({b[1]}):")
        if not orders_by_cycle:
            print("  No filled orders found in bot_orders.")
        for cycle_id, order_type, count, total_filled in orders_by_cycle:
            match_str = "MATCH" if cycle_id == trade_cycle_id else "⚠️ MISMATCH"
            print(f"  Cycle ID in bot_orders: {cycle_id:<5} ({match_str}) | Type: {order_type:<10} | Fills Count: {count:<3} | Total Qty: {total_filled:.6f}")

    # 3. SUIUSDC Missing Critical Orders Analysis
    print("\n========================================")
    print("SUIUSDC Missing Critical Orders Analysis")
    print("========================================")
    
    # Check trades and bot status for SUI bots
    cursor.execute("""
        SELECT b.id, b.name, b.direction, b.bot_type, t.cycle_id, t.open_qty, b.status
        FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1 AND b.normalized_pair = 'SUIUSDC'
    """)
    sui_bots = cursor.fetchall()
    
    print("\nTrades Table Status:")
    for b in sui_bots:
        print(f"  Bot ID: {b[0]} | Name: {b[1]:<20} | Type: {b[3]:<12} | Dir: {b[2]:<5} | Cycle: {b[4]} | Qty: {b[5]:.4f} | Status: {b[6]}")
        
    # Check active/open/new/filled orders for SUI long_hedge (Bot 100318)
    cursor.execute("""
        SELECT id, order_type, order_id, client_order_id, price, amount, filled_amount, status, cycle_id
        FROM bot_orders
        WHERE bot_id = 100318 AND status NOT IN ('reset_cleared', 'cancelled')
        ORDER BY created_at DESC LIMIT 10
    """)
    sui_orders = cursor.fetchall()
    print("\nRecent Active/Filled Orders for Bot 100318 (sui long_hedge):")
    if not sui_orders:
        print("  No active/filled orders found.")
    for o in sui_orders:
        print(f"  ID={o[0]} | Type={o[1]:<15} | Price={o[4]:.4f} | Amt={o[5]:.4f} | Filled={o[6]:.4f} | Status={o[7]} | Cycle={o[8]}")
        
    # 4. BTCUSDC Analysis
    print("\n========================================")
    print("BTCUSDC Analysis")
    print("========================================")
    cursor.execute("""
        SELECT b.id, b.name, b.direction, b.bot_type, t.cycle_id, t.open_qty, b.status
        FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1 AND b.normalized_pair = 'BTCUSDC'
    """)
    btc_bots = cursor.fetchall()
    print("\nTrades Table Status:")
    for b in btc_bots:
        print(f"  Bot ID: {b[0]} | Name: {b[1]:<20} | Type: {b[3]:<12} | Dir: {b[2]:<5} | Cycle: {b[4]} | Qty: {b[5]:.4f} | Status: {b[6]}")
        
    for b in btc_bots:
        bot_id = b[0]
        trade_cycle_id = b[4]
        cursor.execute("""
            SELECT cycle_id, order_type, COUNT(*), SUM(filled_amount)
            FROM bot_orders
            WHERE bot_id = ? AND filled_amount > 0 AND status = 'filled'
            GROUP BY cycle_id, order_type
        """, (bot_id,))
        orders_by_cycle = cursor.fetchall()
        print(f"\nOrder Summary for Bot {bot_id} ({b[1]}):")
        for cycle_id, order_type, count, total_filled in orders_by_cycle:
            match_str = "MATCH" if cycle_id == trade_cycle_id else "⚠️ MISMATCH"
            print(f"  Cycle: {cycle_id:<5} ({match_str}) | Type: {order_type:<10} | Fills: {count:<3} | Total Qty: {total_filled:.6f}")

if __name__ == "__main__":
    run_diagnostics()
