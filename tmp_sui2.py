import sqlite3
import sys

# Try the most likely DB path
for db in ['crypto_bot.db', 'data/crypto_bot.db', 'data/trading_bot.db']:
    try:
        conn = sqlite3.connect(db)
        c = conn.cursor()
        tables = [r[1] for r in c.execute("SELECT * FROM sqlite_master WHERE type='table'")]
        if tables:
            print(f"[DB: {db}] Tables: {tables}")
            
            # Step 1: Get SUI bot IDs
            c.execute("SELECT id, name, direction FROM bots WHERE pair='SUI/USDC:USDC'")
            bots = c.fetchall()
            if not bots:
                c.execute("SELECT id, name, direction FROM bots WHERE pair LIKE '%SUI%'")
                bots = c.fetchall()
            print(f"\nSUI Bots: {bots}")
            
            # Step 2: Get trade state
            if 'bot_states' in tables or 'trade_states' in tables:
                tbl = 'bot_states' if 'bot_states' in tables else 'trade_states'
                c.execute(f"SELECT * FROM {tbl} WHERE bot_id IN ({','.join(str(b[0]) for b in bots)})")
                for r in c.fetchall():
                    print(f"  State: {r}")
            
            # Look for the main "trade summary state" table
            for t in tables:
                cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
                if 'total_invested' in cols or 'avg_entry_price' in cols:
                    print(f"\n[State table: {t}] columns: {cols}")
                    if bots:
                        c.execute(f"SELECT * FROM {t} WHERE bot_id IN ({','.join(str(b[0]) for b in bots)})")
                        rows = c.fetchall()
                        for r in rows:
                            print(f"  {r}")
            
            # Step 3: Orders
            print("\n=== SUI bot_orders filled > 0 ===")
            bot_ids = ','.join(str(b[0]) for b in bots) if bots else '0'
            c.execute(f"""
                SELECT order_type, status, step, amount, filled_amount, price
                FROM bot_orders WHERE bot_id IN ({bot_ids}) AND filled_amount > 0
                ORDER BY step, order_type
            """)
            for r in c.fetchall():
                print(f"  type={r[0]}, status={r[1]}, step={r[2]}, amt={r[3]:.2f}, filled={r[4]:.2f}, price={r[5]:.4f}")
            
            # Step 4: Summary
            print("\n=== SUI NET filled (all non-TP orders) ===")
            c.execute(f"""
                SELECT b.direction, SUM(bo.filled_amount)
                FROM bot_orders bo
                JOIN bots b ON bo.bot_id = b.id
                WHERE bo.bot_id IN ({bot_ids}) AND bo.filled_amount > 0 
                  AND bo.order_type IN ('grid','GRID','entry','ENTRY')
                GROUP BY b.direction
            """)
            for r in c.fetchall():
                print(f"  direction={r[0]}, net_filled={r[1]:.4f}")
                
        conn.close()
    except Exception as e:
        print(f"[{db}]: {e}")
