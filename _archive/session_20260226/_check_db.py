import sqlite3, os

db = 'crypto_bot.db'
conn = sqlite3.connect(db)
c = conn.cursor()

# Trades
c.execute("""SELECT t.bot_id, b.name, b.pair, b.direction, t.total_invested, 
                    t.avg_entry_price, t.current_step, t.entry_confirmed 
             FROM trades t JOIN bots b ON t.bot_id = b.id 
             WHERE t.total_invested > 0
             ORDER BY t.bot_id""")
rows = c.fetchall()

with open('_db_state.txt', 'w') as f:
    f.write("=== TRADES (invested > 0) ===\n")
    virtual_net = 0.0
    virtual_gross = 0.0
    for r in rows:
        line = f"  Bot {r[0]} ({r[1]}): {r[2]} {r[3]} invested=${r[4]:.2f} avg_entry=${r[5]:.2f} step={r[6]} confirmed={r[7]}"
        f.write(line + "\n")
        virtual_gross += r[4]
        if r[3].upper() == 'LONG':
            virtual_net += r[4]
        else:
            virtual_net -= r[4]
    f.write(f"\n  Virtual Gross: ${virtual_gross:.2f}\n")
    f.write(f"  Virtual Net: ${virtual_net:.2f}\n\n")

    # Active positions
    f.write("=== ACTIVE_POSITIONS (Exchange Physical) ===\n")
    c.execute("SELECT pair, side, size, entry_price FROM active_positions")
    rows2 = c.fetchall()
    physical_net = 0.0
    for r in rows2:
        val = r[2] * r[3]
        side = r[1].upper()
        if side in ['BUY', 'LONG']:
            physical_net += val
        else:
            physical_net -= val
        f.write(f"  {r[0]} {r[1]} size={r[2]} entry=${r[3]:.2f} notional=${val:.2f}\n")
    f.write(f"\n  Physical Net: ${physical_net:.2f}\n\n")
    
    f.write(f"=== MISMATCH ===\n")
    f.write(f"  Diff: ${abs(virtual_net - physical_net):.2f}\n\n")
    
    # Recent recon logs
    f.write("=== RECENT RECONCILIATION LOGS (last 10) ===\n")
    try:
        c.execute("SELECT datetime(timestamp, 'unixepoch', 'localtime'), action, details FROM reconciliation_logs ORDER BY timestamp DESC LIMIT 10")
        for r in c.fetchall():
            f.write(f"  {r[0]} | {r[1]} | {r[2]}\n")
    except:
        f.write("  (no table)\n")
    
    # Recent trade history
    f.write("\n=== RECENT TRADE HISTORY (last 10) ===\n")
    try:
        c.execute("SELECT datetime(timestamp, 'unixepoch', 'localtime'), action, symbol, price, amount, notes FROM trade_history ORDER BY timestamp DESC LIMIT 10")
        for r in c.fetchall():
            f.write(f"  {r[0]} | {r[1]} | {r[2]} | price={r[3]:.2f} | amt={r[4]} | {r[5]}\n")
    except:
        f.write("  (no table)\n")

conn.close()
print("Written to _db_state.txt")
