import sqlite3

def check_mismatches():
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    
    # Check invested amounts
    c.execute("SELECT b.id, b.name, b.pair, t.total_invested FROM bots b JOIN trades t ON b.id = t.bot_id WHERE t.total_invested > 0")
    bots = c.fetchall()
    
    total_sys = sum([b[3] for b in bots])
    print(f"Total System Invested: ${total_sys:.2f}")
    
    for b in bots:
        # Check expected orders
        c.execute("SELECT COUNT(*) FROM bot_orders WHERE bot_id = ? AND status = 'open'", (b[0],))
        open_orders = c.fetchone()[0]
        # In trade means 1 GRID + 1 TP = 2 orders expected per active bot
        # (Unless it's at max steps, where GRID is 0, so 1 order expected)
        c.execute("SELECT current_step FROM trades WHERE bot_id = ?", (b[0],))
        step_row = c.fetchone()
        step = step_row[0] if step_row else 0
        
        expected = 2
        print(f"Bot {b[1]} ({b[0]}): Invested=${b[3]:.2f} | Open Orders={open_orders} | Step={step}")
    
    conn.close()

if __name__ == '__main__':
    check_mismatches()
