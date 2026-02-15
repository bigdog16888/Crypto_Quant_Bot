
import os
import sys
import time
import logging
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config

# Setup logging
logging.basicConfig(level=logging.ERROR) # Only show errors from modules
logger = logging.getLogger()

def print_section(title):
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)

def get_system_status():
    print_section("SYSTEM STATUS REPORT")
    
    conn = get_connection()
    from engine.database import DB_PATH
    print(f"📂 DB_PATH: {DB_PATH}")
    cursor = conn.cursor()
    
    # 1. BOT STATISTICS
    cursor.execute("SELECT COUNT(*) FROM bots")
    total_bots = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM bots WHERE is_active = 1")
    active_bots = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM bots WHERE is_active = 1 AND status = 'IN TRADE'")
    triggered_internally = cursor.fetchone()[0]
    
    # Get details of in-trade bots
    cursor.execute('''
        SELECT b.name, b.pair, b.direction, t.total_invested, t.current_step 
        FROM bots b 
        JOIN trades t ON b.id = t.bot_id 
        WHERE b.is_active = 1 AND t.total_invested > 0
    ''')
    triggered_bots = cursor.fetchall()
    
    print(f"🤖 TOTAL BOTS: {total_bots}")
    print(f"🟢 ACTIVE BOTS: {active_bots}")
    print(f"🔥 TRIGGERED (In Trade): {len(triggered_bots)}")
    
    if triggered_bots:
        print("\n   [Triggered Bots Details]")
        for b in triggered_bots:
            print(f"   - {b[0]:<20} | {b[1]:<10} | {b[2]:<5} | ${b[3]:.2f} (Step {b[4]})")

    # 2. ORDER STATISTICS
    # DB Orders
    cursor.execute("SELECT COUNT(*) FROM bot_orders WHERE status = 'open'")
    db_open_orders = cursor.fetchone()[0]
    
    print(f"\n📂 DB OPEN ORDERS: {db_open_orders}")
    
    # 3. EXCHANGE VERIFICATION
    print_section("EXCHANGE VERIFICATION")
    
    try:
        ex = ExchangeInterface(config.MARKET_TYPE)
        
        # Open Orders
        # We need to check all active pairs
        cursor.execute("SELECT DISTINCT pair FROM bots WHERE is_active = 1")
        pairs = [row[0] for row in cursor.fetchall()]
        
        total_exch_orders = 0
        exch_orders_map = {}
        
        print(f"Checking orders for {len(pairs)} pairs...")
        for pair in pairs:
            orders = ex.fetch_open_orders(pair)
            if orders:
                count = len(orders)
                total_exch_orders += count
                exch_orders_map[pair] = count
                # Detailed logging for mismatch diagnosis
                # for o in orders:
                #    print(f"     [Order] {o['id']} {o['side']} {o.get('type')} {o.get('price')}")
        
        print(f"🏦 EXCHANGE OPEN ORDERS: {total_exch_orders}")
        if total_exch_orders != db_open_orders:
            print(f"⚠️  MISMATCH: DB ({db_open_orders}) != Exchange ({total_exch_orders})")
            for pair, count in exch_orders_map.items():
                print(f"   - {pair}: {count}")
        else:
             print("✅ Orders Synced")

        # Positions
        positions = ex.fetch_positions()
        active_positions = [p for p in positions if float(p['contracts']) > 0]
        
        print(f"\n📈 ACTIVE POSITIONS ON EXCHANGE: {len(active_positions)}")
        for p in active_positions:
            print(f"   - {p['symbol']} {p['side'].upper()} x {p['contracts']} (Entry: {p['entryPrice']})")
            
        # 4. CROSS-CHECK (The "Verification" part)
        print_section("CROSS-CHECK VERIFICATION")
        
        mismatches = 0
        
        # Group Bots by Pair
        bot_groups = {}
        for b in triggered_bots:
            name, pair, direction, invested, step = b
            
            # Skip bots that are "Triggered" but have not filled (Step 0)
            # They have Open Orders but no Position exposure yet.
            if int(step) == 0:
                 continue

            sym = pair.replace('/', '').split(':')[0].upper()
            if sym not in bot_groups: bot_groups[sym] = {'LONG': 0.0, 'SHORT': 0.0, 'details': []}
            
            d_key = direction.upper()
            bot_groups[sym][d_key] += float(invested)
            bot_groups[sym]['details'].append(f"{name} ({d_key} ${float(invested):.2f})")

        # Check each group against Exchange
        for sym, data in bot_groups.items():
            net_system = data['LONG'] - data['SHORT']
            
            # Find Exchange Position
            exch_net = 0.0
            found_p = None
            for p in active_positions:
                p_sym = p['symbol'].replace('/', '').split(':')[0].upper()
                if p_sym == sym:
                    qty = float(p['contracts'])
                    price = float(p['entryPrice'])
                    val = qty * price
                    if p['side'].upper() == 'SHORT': val = -val
                    exch_net = val
                    found_p = p
                    break
            
            # Compare Net
            diff = abs(net_system - exch_net)
            if diff > 20.0: # Tolerance
                print(f"❌ MISMATCH for {sym}:")
                print(f"   - System Net: ${net_system:.2f} (Long: ${data['LONG']:.2f}, Short: ${data['SHORT']:.2f})")
                print(f"   - Exchange Net: ${exch_net:.2f}")
                print(f"   - Bots: {', '.join(data['details'])}")
                mismatches += 1
            else:
                 print(f"✅ PASSED for {sym}: System Net ${net_system:.2f} ≈ Exchange ${exch_net:.2f}")
                 print(f"   - Bots Contributing: {len(data['details'])}")

        # Legacy check removal: We shouldn't check individual bots vs full position anymore.
        # But we should ensure every "Triggered" bot is part of a valid group (which is handled above).

        print("\n🔎 Checking for Orphaned Exchange Positions...")
        for p in active_positions:
            # Normalize position symbol: remove slash, split by colon
            pos_sym = p['symbol'].replace('/', '').split(':')[0].upper()
            found_bot = False
            
            # Check if any ACTIVE bot owns this position
            cursor.execute("SELECT id, name, direction FROM bots WHERE is_active = 1")
            active_bots = cursor.fetchall()
            
            for bot_id, name, direction in active_bots:
                # Basic symbol check (assuming name or pair matches, but safer to rely on pair from DB if available)
                # Let's re-fetch with pair
                pass 
            
            # Re-query efficiently
            cursor.execute("SELECT id, name, pair, direction FROM bots WHERE is_active = 1")
            active_bots = cursor.fetchall()
            
            for bot_id, name, pair, direction in active_bots:
                bot_str_sym = pair.replace('/', '').split(':')[0].upper()
                if bot_str_sym == pos_sym:
                    # Direction check
                    if p['side'].upper() == direction.upper() or p['side'].upper() == 'BOTH':
                        
                        # Now check if this bot is actually IN TRADE in DB
                        cursor.execute("SELECT total_invested FROM trades WHERE bot_id = ?", (bot_id,))
                        row = cursor.fetchone()
                        if row and row[0] > 0:
                            found_bot = True
                        break
            
            if not found_bot:
                print(f"🧟 ZOMBIE POS DETECTED: {p['symbol']} {p['side']} x {p['contracts']} (No active bot in trade owns this)")
                mismatches += 1

        if mismatches == 0:
            print("\n✅ SYSTEM INTEGRITY: PASS")
        else:
            print(f"\n❌ SYSTEM INTEGRITY: FAIL ({mismatches} mismatches found)")

    except Exception as e:
        print(f"❌ Exchange connection failed: {e}")

if __name__ == "__main__":
    get_system_status()
