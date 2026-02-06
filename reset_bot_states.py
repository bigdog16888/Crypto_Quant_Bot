"""Reset all bot trade states to clean slate"""
import sqlite3

def main():
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    
    print("=== RESETTING BOT STATES ===\n")
    
    # Show current state
    c.execute("""
        SELECT t.bot_id, b.name, t.total_invested, t.current_step 
        FROM trades t 
        JOIN bots b ON t.bot_id = b.id 
        WHERE t.total_invested > 0
    """)
    trades = c.fetchall()
    
    print("Current trades in DB:")
    for t in trades:
        print(f"  Bot {t[0]} ({t[1]}): ${t[2]:.2f} invested, Step {t[3]}")
    
    if not trades:
        print("  (None - already clean)")
        return
    
    confirm = input("\nReset ALL bot trade states to zero? (yes/no): ").strip().lower()
    if confirm != 'yes':
        print("Cancelled.")
        return
    
    # Reset trades table
    c.execute("UPDATE trades SET total_invested=0, current_step=0, avg_entry_price=0, target_tp_price=0, entry_confirmed=0")
    
    # Mark all orders as closed
    c.execute("UPDATE bot_orders SET status='closed' WHERE status='open'")
    
    conn.commit()
    
    print("\n✅ All bot states reset!")
    print("  - trades: total_invested, current_step, avg_entry_price set to 0")
    print("  - bot_orders: all open orders marked closed")
    
    conn.close()

if __name__ == "__main__":
    main()
