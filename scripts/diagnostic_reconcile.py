import sys
import os
import time
import logging

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.database import get_all_bots, get_bot_status
from engine.exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config

# Configure simple logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("DiagnosticReconcile")

def run_diagnostic():
    print("="*60)
    print("🔍 DIAGNOSTIC: VIRTUAL VS REAL POSITION RECONCILIATION")
    print("="*60)

    # 1. Fetch Exchange Positions
    print("\n1️⃣  Fetching Exchange Positions...")
    try:
        # Initialize with 'future' market type (default for this bot usually)
        exchange = ExchangeInterface(market_type='future') 
        positions = exchange.fetch_positions()
        
        exchange_map = {} # {symbol: contracts}
        
        for p in positions:
            symbol = p.get('symbol', '')
            contracts = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            if contracts != 0:
                norm_sym = normalize_symbol(symbol)
                exchange_map[norm_sym] = contracts
                print(f"   found: {symbol} = {contracts} contracts")
                
        if not exchange_map:
            print("   (No open positions on exchange)")
            
    except Exception as e:
        print(f"❌ Error fetching exchange positions: {e}")
        return

    # 2. Fetch Active Bots & Sum Virtual Positions
    print("\n2️⃣  Aggregating Bot Positions...")
    try:
        bots = get_all_bots()
        # Handle tuple return from get_all_bots: (id, name, pair, is_active, strategy_type, total_invested, current_step)
        active_bots = [b for b in bots if b[3]] # Index 3 is is_active
        
        bot_map = {} # {symbol: total_contracts}
        bot_details = {} # {symbol: [bot_id_1, bot_id_2]}
        
        for bot in active_bots:
            bot_id = bot[0]
            pair = bot[2]
            norm_pair = normalize_symbol(pair)
            
            status = get_bot_status(bot_id)
            if not status: continue
            
            invested = status.get('total_invested', 0)
            avg_price = status.get('avg_entry_price', 0)
            
            if invested > 0 and avg_price > 0:
                # Calculate contracts (approx)
                contracts = invested / avg_price
                
                # Add to sum
                bot_map[norm_pair] = bot_map.get(norm_pair, 0.0) + contracts
                
                if norm_pair not in bot_details: bot_details[norm_pair] = []
                bot_details[norm_pair].append(f"Bot {bot_id} ({contracts:.4f})")
                
        if not bot_map:
            print("   (No active bots in trade)")
        else:
            for sym, qty in bot_map.items():
                print(f"   {sym}: {qty:.4f} contracts (from {len(bot_details[sym])} bots)")

    except Exception as e:
        print(f"❌ Error fetching bot data: {e}")
        return

    # 3. Compare & Report
    print("\n3️⃣  Reconciliation Report")
    print("-" * 80)
    print(f"{'SYMBOL':<15} | {'BOTS (Sum)':<15} | {'EXCH (Net)':<15} | {'DIFF':<15} | {'STATUS':<15}")
    print("-" * 80)
    
    all_symbols = set(exchange_map.keys()) | set(bot_map.keys())
    
    for sym in sorted(all_symbols):
        bot_qty = bot_map.get(sym, 0.0)
        exch_qty = exchange_map.get(sym, 0.0)
        diff = bot_qty - exch_qty
        
        status = "✅ MATCH"
        if abs(diff) > 0.0001: # Tolerance
            if bot_qty > exch_qty:
                status = "👻 GHOST (Bot>Ex)"
            else:
                status = "🧟 ORPHAN (Ex>Bot)"
                
        print(f"{sym:<15} | {bot_qty:<15.4f} | {exch_qty:<15.4f} | {diff:<15.4f} | {status:<15}")
        
    print("-" * 80)
    print("\nDone.")

if __name__ == "__main__":
    run_diagnostic()
