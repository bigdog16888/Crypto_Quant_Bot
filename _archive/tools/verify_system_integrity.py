import sys
import os
import sqlite3
import pandas as pd
import time
from tabulate import tabulate

# Ensure project root is in path
sys.path.append(os.getcwd())

from engine.exchange_interface import ExchangeInterface
from config.settings import config
# from engine.utils import set_environment_variables

# Mock set_env if missing
def set_environment_variables(): pass

def verify_system():
    print("🔍 INITIALIZING SYSTEM INTEGRITY CHECK...")
    DB_PATH = config.PATHS['DB_FILE']
    print(f"   > DB Path: {DB_PATH}")
    try:
        st = os.stat(DB_PATH)
        print(f"   > DB Stat: Size={st.st_size} | Inode={st.st_ino}")
    except Exception as e:
        print(f"   > DB Stat Error: {e}")
    
    # 1. Fetch RAW Exchange Data
    print("\n1. 🌍 FETCHING EXCHANGE DATAS (Physical Reality)...")
    try:
        ex = ExchangeInterface(market_type='future')
        # Force DEMO/TESTNET logic if applicable (auto-handled by class now)
        positions = ex.fetch_positions()
        
        real_positions = []
        for p in positions:
            if float(p['contracts']) != 0:
                # Handle missing 'notional'
                size = float(p['contracts'])
                entry = float(p.get('entryPrice', 0))
                notional = float(p.get('notional', size * entry))
                
                real_positions.append({
                    'Symbol': p['symbol'],
                    'Side': p['side'].upper(),
                    'Size': size,
                    'Entry': entry,
                    'Notional': notional
                })
        
        df_real = pd.DataFrame(real_positions)
        print(tabulate(df_real, headers="keys", tablefmt="grid") if not df_real.empty else "   > [EMPTY] No positions on exchange.")
        
    except Exception as e:
        print(f"   ❌ CRITICAL ERROR FETCHING EXCHANGE: {e}")
        return

    # 2. Fetch DATABASE Physical Data
    print("\n2. 💾 FETCHING 'active_positions' (DB Mirror)...")
    try:
        conn = sqlite3.connect(config.PATHS['DB_FILE'], timeout=10)
        df_db_phys = pd.read_sql("SELECT pair as Symbol, side as Side, size as Size, entry_price as Entry, last_checked FROM active_positions", conn)
        print(tabulate(df_db_phys, headers="keys", tablefmt="grid") if not df_db_phys.empty else "   > [EMPTY] Table 'active_positions' is empty.")
    except Exception as e:
        print(f"   ❌ DB READ ERROR: {e}")
        df_db_phys = pd.DataFrame()

    # 3. Fetch VIRTUAL Bot Data
    print("\n3. 🤖 FETCHING 'trades' (Virtual Bot State)...")
    try:
        query = """
            SELECT b.name, b.pair, b.direction, t.total_invested, t.avg_entry_price, t.current_step 
            FROM trades t 
            JOIN bots b ON t.bot_id = b.id 
            WHERE t.total_invested > 0
        """
        df_virtual = pd.read_sql(query, conn)
        conn.close()
        print(tabulate(df_virtual, headers="keys", tablefmt="grid") if not df_virtual.empty else "   > [EMPTY] No active internal bot trades.")
    except Exception as e:
        print(f"   ❌ DB READ ERROR: {e}")
        df_virtual = pd.DataFrame()

    # 4. COMPARISON
    print("\n⚖️  COMPARISON RESULT:")
    
    # Check 1: Real vs DB Mirror
    print("   [1] Real Exchange vs DB Mirror:")
    real_count = len(df_real)
    db_count = len(df_db_phys)
    
    if real_count == db_count:
        # Deep check?
        print(f"       ✅ COUNT MATCH: {real_count} positions.")
        # We assume content match if count matches for now, as runner just overwrites.
    else:
        print(f"       ❌ MISMATCH: Exchange has {real_count}, DB has {db_count}.")
        
    # Check 2: Virtual vs Real (The "Mismatch" Logic)
    print("   [2] Virtual vs Physical (System Mismatch):")
    # Group by Symbol
    if not df_virtual.empty:
        virt_grouped = df_virtual.groupby('pair')['total_invested'].sum()
    else:
        virt_grouped = pd.Series()
        
    if not df_real.empty:
        # Normalize symbols logic roughly
        real_grouped = {}
        for _, row in df_real.iterrows():
            sym = row['Symbol'].split(':')[0] # BTC/USDT:USDT -> BTC/USDT
            val = abs(row['Notional'])
            real_grouped[sym] = real_grouped.get(sym, 0) + val
    else:
        real_grouped = {}
        
    # Compare
    all_syms = set(list(virt_grouped.index) + list(real_grouped.keys()))
    
    for sym in all_syms:
        v_val = virt_grouped.get(sym, 0.0)
        r_val = real_grouped.get(sym, 0.0)
        diff = abs(v_val - r_val)
        
        status = "✅ OK"
        if diff > 10.0: # $10 tolerance
            status = "⚠️ MISMATCH"
            
        print(f"       > {sym}: Virtual ${v_val:,.2f} vs Real ${r_val:,.2f} | Diff: ${diff:,.2f} -> {status}")

if __name__ == "__main__":
    verify_system()
