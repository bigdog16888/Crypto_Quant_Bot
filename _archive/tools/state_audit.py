import sqlite3
import json
import os
import sys

# Add parent dir to path to import engine
sys.path.append(os.getcwd())

DB_PATH = os.path.join(os.getcwd(), "crypto_bot.db")

def audit_system():
    print("--- 🔍 SYSTEM AUDIT START ---")
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # 1. Active Bots Check
        cursor.execute("SELECT id, name, pair, config, strategy_type, direction FROM bots WHERE is_active=1")
        bots = cursor.fetchall()
        print(f"\n[ACTIVE BOTS] Count: {len(bots)}", flush=True)
        
        active_bot_map = {}
        for b in bots:
            try:
                bid, name, pair, config_str, stype, direction = b
                active_bot_map[bid] = {'name': name, 'pair': pair, 'dir': direction}
                # print(f"  Bot {bid}: {name}", flush=True) # Silenced for stability
            except Exception as e:
                print(f"  ERROR processing bot: {e}", flush=True)

        # 2. Active Trades Check
        print(f"\n[ACTIVE TRADES (Virtual Position)]", flush=True)

        cursor.execute("SELECT bot_id, total_invested, current_step, avg_entry_price FROM trades WHERE total_invested > 0")
        trades = cursor.fetchall()
        
        trading_bots = []
        for t in trades:
            bid, invested, step, entry = t
            trading_bots.append(bid)
            b_info = active_bot_map.get(bid, {'name': 'Unknown', 'pair': '???'})
            print(f"  Bot {bid} ({b_info['name']}): Invested=${invested:.2f} | Step {step} | Entry={entry}")

        # 3. Open Orders Check (Corrected Schema)
        print(f"\n[OPEN ORDERS (DB)]")
        
        # 3a. Primary Orders (Entry/TP) from trades table
        print("  Fetching Trade Orders...", flush=True)
        try:
            cursor.execute("SELECT bot_id, entry_order_id, tp_order_id FROM trades WHERE total_invested > 0")
            trade_orders = cursor.fetchall()
            print(f"  - Found {len(trade_orders)} active trades with orders.", flush=True)
        except Exception as e:
            print(f"  - Error fetching trades: {e}", flush=True)
            trade_orders = []
        
        db_orders = []
        for t in trade_orders:
            bid, entry_oid, tp_oid = t
            if entry_oid: db_orders.append({'bot_id': bid, 'type': 'ENTRY', 'id': entry_oid})
            if tp_oid: db_orders.append({'bot_id': bid, 'type': 'TP', 'id': tp_oid})
            
        # 3b. Grid Orders from bot_orders table
        print("  Fetching Grid Orders...", flush=True)
        try:
            cursor.execute("SELECT bot_id, order_id, order_type, price, amount FROM bot_orders")
            grid_orders = cursor.fetchall()
            print(f"  - Found {len(grid_orders)} grid orders.", flush=True)
            for g in grid_orders:
                bid, oid, otype, px, amt = g
                db_orders.append({'bot_id': bid, 'type': f"GRID-{otype}", 'id': oid, 'price': px})
        except Exception as e:
            print(f"  - Error fetching grid orders: {e}", flush=True)

        print(f"  Total Tracked Orders in DB: {len(db_orders)}", flush=True)

        for o in db_orders:
            b_name = active_bot_map.get(o['bot_id'], {}).get('name', 'Unknown')
            print(f"  Bot {o['bot_id']} ({b_name}): {o['type']} [{o['id']}]")

        # 4. Exchange Verification (Mock/Real)
        print(f"\n[EXCHANGE VERIFICATION]")
        try:
            from engine.exchange_interface import ExchangeInterface
            from config import config
            
            # Initialize Spot and Future (if needed)
            market_types = set()
            for b in bots:
                try:
                    cfg = json.loads(b[3])
                    market_types.add(cfg.get('market_type', 'spot'))
                except: pass
            if not market_types: market_types.add('spot')
            
            all_real_orders = []
            
            for mt in market_types:
                print(f"  Checking Exchange: {mt}...")
                try:
                    ex = ExchangeInterface(market_type=mt)
                    # We need to check orders for ALL active pairs
                    active_pairs = set(b[2] for b in bots)
                    
                    for pair in active_pairs:
                        orders = ex.fetch_open_orders(symbol=pair)
                        if orders:
                            for o in orders:
                                all_real_orders.append(o)
                                print(f"    FOUND: {o['symbol']} {o['side']} {o['type']} {o['amount']} @ {o['price']} (ID: {o['id']})")
                except Exception as e:
                    print(f"    Failed to fetch form {mt}: {e}")
            
            # 5. Matching Logic
            print(f"\n[MATCHING RESULTS]")
            real_order_ids = set(str(o['id']) for o in all_real_orders)
            db_order_ids = set(str(o['id']) for o in db_orders)
            
            matched = db_order_ids.intersection(real_order_ids)
            missing_on_exchange = db_order_ids - real_order_ids
            ghosts_on_exchange = real_order_ids - db_order_ids
            
            print(f"  ✅ MATCHED: {len(matched)}")
            for mid in matched:
                print(f"    - {mid} (Verified)")
                
            if missing_on_exchange:
                print(f"  ❌ MISSING ON EXCHANGE (DB says Open, but not found): {len(missing_on_exchange)}")
                for oid in missing_on_exchange:
                    print(f"    - {oid}")
                    
            if ghosts_on_exchange:
                print(f"  👻 GHOST ORDERS (Exchange has them, DB doesn't): {len(ghosts_on_exchange)}")
                for oid in ghosts_on_exchange:
                    print(f"    - {oid}")
                    
        except Exception as e:
            print(f"Exchange Verification Failed: {e}")

        conn.close()

        
    except Exception as e:
        print(f"Audit Failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    audit_system()
