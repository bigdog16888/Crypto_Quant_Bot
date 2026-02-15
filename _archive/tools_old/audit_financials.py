import sys
import os
import sqlite3
from tabulate import tabulate

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection

def audit_financials():
    print("💰 STARTING FINANCIAL AUDIT 💰")
    print("=================================")
    
    # 1. Get DB State
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT b.name, b.pair, t.total_invested, t.avg_entry_price, t.current_step 
        FROM trades t JOIN bots b ON t.bot_id = b.id 
        WHERE t.total_invested > 0
    """)
    db_trades = cursor.fetchall()
    
    # 2. Get Exchange State
    try:
        exchange = ExchangeInterface(market_type='future')
        positions = exchange.fetch_positions()
        # Filter for active positions
        real_positions = {
            p['symbol']: p 
            for p in positions 
            if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0
        }
        
    except Exception as e:
        print(f"❌ Exchange Error: {e}")
        return

    # 3. Compare
    table_data = []
    
    print(f"\n🔎 AUDITING {len(db_trades)} ACTIVE TRADES:")
    
    for t in db_trades:
        name, pair, db_invested, db_price, step = t
        
        # Find matching position
        # Normalize DB pair (BTC/USDC) to Exchange format (BTC/USDC:USDC)
        # Simple fuzzy match for now
        match = None
        for sym, pos in real_positions.items():
            if pair.replace('/', '').split(':')[0] == sym.replace('/', '').split(':')[0]:
                match = pos
                break
        
        if match:
            # Exchange Values
            ex_qty = float(match.get('contracts', 0) or match.get('size', 0))
            ex_price = float(match.get('entryPrice', 0))
            ex_notional = float(match.get('notional', 0)) # Real value if available
            
            # If notional missing, calc it
            if ex_notional == 0:
                ex_notional = ex_qty * ex_price
                
            # Delta Check
            # Invested Diff
            inv_diff = abs(db_invested - ex_notional)
            inv_match = "✅" if inv_diff < 5.0 else f"❌ (${inv_diff:.2f} Diff)" # Allow small dust diff
            
            # Price Diff
            price_diff = abs(db_price - ex_price)
            price_pct = (price_diff / ex_price) * 100 if ex_price > 0 else 0
            price_match = "✅" if price_pct < 0.1 else f"❌ ({price_pct:.2f}%)"
            
            table_data.append([
                name, pair, 
                f"${db_invested:.2f}", f"${ex_notional:.2f}", inv_match,
                f"${db_price:.2f}", f"${ex_price:.2f}", price_match
            ])
            
        else:
            table_data.append([name, pair, f"${db_invested:.2f}", "MISSING", "❌ GHOST", f"${db_price:.2f}", "N/A", "❌"])

    print(tabulate(table_data, headers=[
        "Bot Name", "Pair", 
        "DB Invested", "Exch Value", "Inv Match", 
        "DB Price", "Exch Price", "Price Match"
    ], tablefmt="grid"))
    
    # 4. Check for Orphans
    print("\n🔎 CHECKING FOR ORPHAN POSITIONS (Exchange has it, DB doesn't):")
    db_pairs = [t[1].replace('/', '').split(':')[0] for t in db_trades]
    
    orphans = []
    for sym, pos in real_positions.items():
        clean_sym = sym.replace('/', '').split(':')[0]
        found = False
        for dp in db_pairs:
            if dp == clean_sym:
                found = True
                break
        
        if not found:
            qty = float(pos.get('contracts', 0) or pos.get('size', 0))
            val = float(pos.get('notional', 0) or (qty * float(pos.get('entryPrice', 0))))
            orphans.append([sym, f"{qty:.4f}", f"${val:.2f}"])
            
    if orphans:
        print(tabulate(orphans, headers=["Symbol", "Qty", "Value"], tablefmt="grid"))
    else:
        print("✅ No Orphan Positions Found.")

    print("\n=================================")

if __name__ == "__main__":
    audit_financials()
