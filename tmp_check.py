"""Check Early Exit status for all active bots in trade."""
import sqlite3, json, time
from datetime import datetime

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("""
    SELECT b.id, b.name, b.pair, b.direction, b.config,
           t.current_step, t.avg_entry_price, t.target_tp_price, 
           t.total_invested, t.basket_start_time
    FROM bots b JOIN trades t ON b.id=t.bot_id
    WHERE b.is_active=1 AND t.total_invested > 0
    ORDER BY b.id
""")
rows = c.fetchall()

now = time.time()
print(f"{'ID':>6} {'Name':<22} {'EE?':>4} {'Time In':>8} {'Decay%':>7} {'Curr TP':>10} {'Adj TP':>10} {'1% TP':>10}")
print("-"*90)

for row in rows:
    bot_id, name, pair, direction, config_json, step, avg_entry, curr_tp, invested, bst = row
    conf = json.loads(config_json)
    
    use_ee = conf.get('UseEarlyExit', False)
    tp_pct = float(conf.get('TakeProfitPct', 1.0))
    tp_type = conf.get('TakeProfitType', 'Percent')
    
    # Time in trade
    mins_in = (now - (bst or now)) / 60 if bst else 0
    
    # Calculate what EE-adjusted TP would be
    decay_mins = float(conf.get('DecayIntervalMins', 15.0))
    decay_pct = float(conf.get('DecayPercentPerInterval', 30.0)) / 100.0
    
    intervals = mins_in / decay_mins if decay_mins > 0 else 0
    ee_pc = intervals * decay_pct
    decay_factor = max(0.0, 1.0 - ee_pc)
    
    # Clean TP (no EE)
    if tp_type == 'Percent':
        base_tp = avg_entry * (1 + tp_pct/100) if direction=='LONG' else avg_entry * (1 - tp_pct/100)
    else:
        qty = invested / avg_entry if avg_entry > 0 else 0
        dist = conf.get('TakeProfitBase', 10) / qty if qty > 0 else 0
        base_tp = avg_entry + dist if direction=='LONG' else avg_entry - dist
    
    # BE (avg_entry)
    breakeven = avg_entry
    # EE adjusted TP
    adj_tp = breakeven + (base_tp - breakeven) * decay_factor if use_ee else base_tp
    
    ee_str = "YES" if use_ee else "no"
    decay_str = f"{ee_pc*100:.0f}%" if use_ee else "-"
    
    print(f"{bot_id:>6} {name:<22} {ee_str:>4} {mins_in:>7.0f}m {decay_str:>7} {curr_tp:>10.4f} {adj_tp:>10.4f} {base_tp:>10.4f}")

# Check recent TP adjustments in logs
print("\n=== Recent TP-related activity (last 3h) ===")
cutoff = int(now) - 10800
c.execute("""
    SELECT b.name, datetime(th.timestamp,'unixepoch') as t, th.action, th.notes
    FROM trade_history th JOIN bots b ON b.id=th.bot_id
    WHERE th.timestamp >= ? AND (th.action LIKE '%TP%' OR th.notes LIKE '%Early%' OR th.notes LIKE '%decay%')
    ORDER BY th.timestamp DESC LIMIT 20
""", (cutoff,))
rows2 = c.fetchall()
if rows2:
    for r in rows2:
        print(f"  {r[1]} | {r[0]:<20} | {r[2]:<15} | {r[3]}")
else:
    print("  No EE/TP activity found in the last 3 hours.")
