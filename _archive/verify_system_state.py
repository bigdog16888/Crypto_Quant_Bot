import os
import sys
import time
import sqlite3
import logging
from engine.database import get_connection, get_all_bots, get_bot_status, get_bot_order_ids
from engine.exchange_interface import ExchangeInterface

# Setup minimal logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("Verifier")

def get_next_grid_price(entry_price, step, step_size_pct=1.0, mult=1.0, direction='LONG'):
    # Rough calc for display purposes
    # Assumes standard martingale logic:
    # Next Price = Entry * (1 +/- (Step% * (Mult^Step)))
    # This is an approximation if the strat uses fixed drops vs compounded.
    # Assuming standard fixed drop for now.
    
    if entry_price <= 0: return 0.0
    
    drop_pct = (step_size_pct / 100.0) * (mult ** step)
    
    if direction.upper() == 'LONG':
        return entry_price * (1 - drop_pct)
    else:
        return entry_price * (1 + drop_pct)

def verify_system():
    print("\n=== 🔍 SYSTEM INTEGRITY & STATE VERIFICATION 🔍 ===\n")
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. DATABASE STATE
    print("--- 🤖 BOT DATABASE STATE ---")
    active_bots = get_all_bots()
    bots_in_trade = 0
    total_db_invested = 0.0
    
    bot_map = {} # ID -> Info
    
    for bot in active_bots:
        # bot: id, name, pair, is_active, strategy_type, total_invested, current_step
        b_id, b_name, b_pair, b_active, b_strat, b_inv, b_step = bot
        b_inv = b_inv or 0.0
        b_step = b_step or 0
        
        # Get full details
        cursor.execute("SELECT direction, avg_entry_price, target_tp_price, config FROM bots JOIN trades ON bots.id = trades.bot_id WHERE bots.id = ?", (b_id,))
        res = cursor.fetchone()
        
        direction = "LONG"
        avg_entry = 0.0
        tp_price = 0.0
        
        if res:
             direction, avg_entry, tp_price, config_json = res
        else:
             # Look in bots table only
             cursor.execute("SELECT direction, config FROM bots WHERE id=?", (b_id,))
             res2 = cursor.fetchone()
             direction = res2[0] if res2 else "LONG"
        
        status_icon = "🟢" if b_active else "🔴"
        trade_status = "IN TRADE" if b_inv > 0 else "WAITING"
        
        prefix = f"{status_icon} [{b_id}] {b_name:<20} | {b_pair} ({direction})"
        
        if b_inv > 0:
            bots_in_trade += 1
            total_db_invested += b_inv
            
            # Next Grid Calc (Approx)
            next_grid = get_next_grid_price(avg_entry, b_step, 1.0, 1.0, direction) # Using dummy mult for now
            
            print(f"{prefix}")
            print(f"      STATUS: {trade_status} (Step {b_step})")
            print(f"      💰 Invested: ${b_inv:.2f} @ ${avg_entry:.4f}")
            print(f"      🎯 TP Price: ${tp_price:.4f}")
            print(f"      📉 Next Grid: ~${next_grid:.4f} (Est)")
            
            # Check DB Orders
            orders = get_bot_order_ids(b_id)
            g_orders = orders.get('grid_orders', [])
            print(f"      📦 Open Orders (DB): {len(g_orders)} Grid + {'1 TP' if orders.get('tp_order_id') else 'NO TP'}")
            
        else:
            print(f"{prefix} | {trade_status}")

        bot_map[b_id] = {'name': b_name, 'pair': b_pair, 'direction': direction}

    print(f"\nStats: {bots_in_trade} Bots in Trade | Total Invested (DB): ${total_db_invested:.2f}\n")
    
    # 2. EXCHANGE STATE
    print("--- 🏦 EXCHANGE STATE (Simulated/Real) ---")
    try:
        # Initialize basic interface - assumes default config
        ex = ExchangeInterface() 
        positions = ex.fetch_positions() # Using standard fetch_positions
        success = True
        
        if not positions:
            print("No open positions on exchange.")
        else:
            for p in positions:
                # p is dict
                sym = p.get('symbol')
                size = float(p.get('contracts') or p.get('size') or 0)
                side = p.get('side')
                pnl = p.get('unrealizedPnl')
                
                if size == 0: continue
                
                # Match to bot
                owner = "❓ UNKNOWN/MANUAL"
                for bid, bdata in bot_map.items():
                    # Simple fuzzy match for verification
                    if bdata['pair'].replace('/','').upper() in sym.replace('/','').upper():
                        owner = f"🤖 Bot {bid} ({bdata['name']})"
                        
                print(f"   pos: {sym:<10} | {side.upper():<5} | Size: {size} | PnL: ${pnl} | Owner: {owner}")
                
    except Exception as e:
        print(f"⚠️ Exchange Connection Failed/Skipped: {e}")
        
    print("\n--- 📜 LOG CHECK (Last 5 Errors) ---")
    try:
        if os.path.exists("trading_bot.log"):
            with open("trading_bot.log", "r") as f:
                lines = f.readlines()
                errors = [l.strip() for l in lines if "ERROR" in l or "CRITICAL" in l or "WARNING" in l]
                for err in errors[-5:]:
                    print(f"   {err}")
        else:
            print("   No log file found.")
    except:
        pass

    print("\n--- 🛠️ RUNNING RECONCILIATION PROOF 🛠️ ---")
    
    # DEBUG: Check get_bot_status for all bots first
    print("DEBUG: Checking get_bot_status() return values:")
    active_b = get_all_bots()
    for b in active_b:
        s = get_bot_status(b[0])
        print(f"   Bot {b[0]}: {s}")

    try:
        from engine.reconciler import StateReconciler
        reconciler = StateReconciler()
        results = reconciler.reconcile_all()
        
        for res in results:
            print(f"   🔧 Bot {res.bot_name}: {res.action_taken} -> {res.details}")
            
    except Exception:
        import traceback
        traceback.print_exc()

    print("\n--- 🤖 SYSTEM STATE POST-FIX ---")
    # Re-fetch bots to show updated state
    active_bots_after = get_all_bots()
    for bot in active_bots_after:
        b_id, b_name, b_pair, b_active, b_strat, b_inv, b_step = bot
        b_inv = b_inv or 0.0
        if b_inv > 0:
             print(f"   🟢 Bot {b_id} ({b_name}) STILL IN TRADE: ${b_inv:.2f}")
        elif b_id == 43: # Specifically check the problem bot
             print(f"   ✅ Bot {b_id} ({b_name}) IS NOW CLEAN (IDLE)")

    print("\n=== VERIFICATION COMPLETE ===")

if __name__ == "__main__":
    verify_system()
