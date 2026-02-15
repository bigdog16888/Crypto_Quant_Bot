"""Check which system is being used for state tracking"""
from engine.database import get_connection
from engine.ownership import get_all_active_ownerships

print("=== TRADES TABLE (Old System) ===")
conn = get_connection()
cur = conn.cursor()
for r in cur.execute('SELECT bot_id, current_step, total_invested, avg_entry_price FROM trades WHERE total_invested > 0').fetchall():
    print(f"Bot {r[0]}: Step {r[1]} | Invested: ${r[2]:.2f} | Entry: ${r[3]:.4f}")

print("\n=== OWNERSHIP SYSTEM (New System) ===")
ownerships = get_all_active_ownerships()
print(f"Active ownerships: {len(ownerships)}")
for o in ownerships:
    print(f"{o.pair}: Owner={o.owner.bot_id if o.owner else None}, Passengers={len(o.passengers)}")

print("\n=== DIAGNOSIS ===")
print("Issue: Bots 41 and 43 are using OLD trades table")
print("       Bot 44 is using NEW ownership system")
print("       This creates state inconsistency!")
