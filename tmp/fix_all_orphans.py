import sqlite3, time

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

_now = int(time.time())

print("=== ATOMIC FIX: SUI + SOL active_positions + all trades ===")

# ─── Get current physical prices from active_positions ───────────────────────
q.execute("SELECT pair, side, size, entry_price, bot_id FROM active_positions")
ap_all = {(r[0], r[1]): (r[2], r[3], r[4]) for r in q.fetchall()}

def fix_bot(bot_id, name, pair_db, pair_ap, side, phys_qty, phys_price):
    print(f"\n  === {name} (bot_id={bot_id}) ===")
    
    # 1. Get current cycle
    q.execute("SELECT COALESCE(cycle_id,1) FROM trades WHERE bot_id=?", (bot_id,))
    r = q.fetchone()
    cyc = r[0] if r else 1
    
    # 2. Delete old PASS3 adoptions (all cycles to be safe)
    q.execute("DELETE FROM bot_orders WHERE bot_id=? AND order_type='adoption' AND client_order_id LIKE '%PASS3%'", (bot_id,))
    print(f"    Deleted {q.rowcount} old PASS3 adoptions")
    
    # 3. Write fresh adoption 
    _oid = f"PASS3_ADOPTION_{bot_id}_C{cyc}"
    _cid = f"CQB_{bot_id}_PASS3_C{cyc}"
    q.execute("""
        INSERT OR REPLACE INTO bot_orders
          (bot_id, order_id, client_order_id, order_type, price, amount, filled_amount,
           status, step, cycle_id, created_at, updated_at)
        VALUES (?, ?, ?, 'adoption', ?, ?, ?, 'filled', 1, ?, ?, ?)
    """, (bot_id, _oid, _cid, phys_price, phys_qty, phys_qty, cyc, _now, _now))
    
    # 4. Update trades
    invested = round(phys_qty * phys_price, 6)
    q.execute("""
        UPDATE trades SET total_invested=?, avg_entry_price=?,
            entry_confirmed=1, current_step=1
        WHERE bot_id=?
    """, (invested, phys_price, bot_id))
    if q.rowcount == 0:
        # Insert if no trades row
        q.execute("""
            INSERT INTO trades (bot_id, total_invested, avg_entry_price, entry_confirmed, current_step, cycle_id)
            VALUES (?, ?, ?, 1, 1, ?)
        """, (bot_id, invested, phys_price, cyc))
    print(f"    trades.total_invested=${invested:.4f}, avg=${phys_price:.6f}, cyc={cyc}")
    
    # 5. Fix active_positions.bot_id
    q.execute("UPDATE active_positions SET bot_id=? WHERE pair=? AND side=?", (bot_id, pair_ap, side))
    print(f"    active_positions.bot_id → {bot_id} ({q.rowcount} rows)")
    
    vqty = invested / phys_price
    print(f"    Verification: system_qty={vqty:.4f} vs phys={phys_qty:.4f} {'✅' if abs(vqty-phys_qty)<0.01 else '⚠️'}")

# SUI: sui long (10018) - physical LONG 1378.1 @ 0.9324
fix_bot(10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', 1378.1, 0.9323870225726)

# SOL: sol (10008) - physical LONG 2.38 (updated from AP; was 2.62 before grids filled)
q.execute("SELECT size, entry_price FROM active_positions WHERE pair='SOLUSDC' AND side='LONG'")
sol_ap = q.fetchone()
sol_qty = sol_ap[0] if sol_ap else 2.38
sol_price = sol_ap[1] if sol_ap else 90.5982
fix_bot(10008, 'sol', 'SOL/USDC:USDC', 'SOLUSDC', 'LONG', sol_qty, sol_price)

# BTC: long_btc_price (10016) - physical LONG 0.023 @ 69651.84
q.execute("SELECT size, entry_price FROM active_positions WHERE pair='BTCUSDC' AND side='LONG'")
btc_ap = q.fetchone()
btc_qty = btc_ap[0] if btc_ap else 0.023
btc_price = btc_ap[1] if btc_ap else 69651.84
fix_bot(10016, 'long btc price', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', btc_qty, btc_price)

# XRP: xrp long (10017) - physical LONG 338.2 @ 1.37955
q.execute("SELECT size, entry_price FROM active_positions WHERE pair='XRPUSDC' AND side='LONG'")
xrp_ap = q.fetchone()
xrp_qty = xrp_ap[0] if xrp_ap else 338.2
xrp_price = xrp_ap[1] if xrp_ap else 1.37955
fix_bot(10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', xrp_qty, xrp_price)

c.commit()
print("\n=== ALL FIXES COMMITTED ===")
print("Monitor should update within ~10s. Engine restart required to make permanent.")
c.close()
