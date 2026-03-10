import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface()
positions = ex.fetch_positions()

conn = get_connection()
c = conn.cursor()

for p in positions:
    if 'XAU' not in p['symbol']:
        continue

    ex_entry = float(p['entryPrice'])
    ex_qty   = abs(float(p['contracts']))
    
    c.execute("SELECT b.id, b.name, t.avg_entry_price, t.total_invested FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.pair LIKE '%XAU%'")
    rows = c.fetchall()
    for bot_id, name, db_entry, db_inv in rows:
        # Recalculate total_invested from real qty and real entry
        correct_inv = ex_qty * ex_entry
        print(f"Bot {bot_id} ({name}):")
        print(f"  DB  avg_entry_price:  {db_entry:.4f}")
        print(f"  EX  avg_entry_price:  {ex_entry:.4f}")
        print(f"  DB  total_invested:   {db_inv:.2f}")
        print(f"  EX  total_invested:   {correct_inv:.2f} (qty={ex_qty} @ {ex_entry:.2f})")
        
        confirm = input(f"\n  Correct DB to entry={ex_entry:.4f}, invested={correct_inv:.2f}? (y/n): ")
        if confirm.strip().lower() == 'y':
            c.execute("UPDATE trades SET avg_entry_price=?, total_invested=? WHERE bot_id=?",
                      (ex_entry, correct_inv, bot_id))
            conn.commit()
            print(f"  ✅ Updated bot {bot_id}")
        else:
            print(f"  ⏭️  Skipped.")

conn.close()
