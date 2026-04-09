import sqlite3

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

def audit_bot(name_like, pair_like, phys_qty, phys_price):
    print(f"\n{'='*70}")
    print(f"BOT: {name_like}   PHYSICAL: {phys_qty} @ ${phys_price:.2f} = ${phys_qty*phys_price:.2f}")
    print(f"{'='*70}")

    q.execute("SELECT id, name, direction FROM bots WHERE name LIKE ? AND is_active=1", (name_like,))
    bots = q.fetchall()
    for bot in bots:
        bid, bname, bdir = bot
        q.execute("SELECT total_invested, avg_entry_price, current_step, cycle_id, entry_confirmed FROM trades WHERE bot_id=?", (bid,))
        tr = q.fetchone()
        if not tr: 
            print(f"  Bot {bid} {bname}: NO TRADES ROW")
            continue
        ti, avg, step, cyc, confirmed = tr
        if avg and avg > 0:
            vqty = ti / avg
        else:
            vqty = 0
        print(f"\n  Bot {bid} ({bname}, {bdir}) step={step} cycle={cyc} confirmed={confirmed}")
        print(f"  trades: total_invested=${ti:.2f}  avg=${avg:.4f}  → virtual qty={vqty:.4f}")
        print(f"  physical: {phys_qty}  diff: {vqty - phys_qty:.4f}")
        
        # Show active bot_orders for this cycle
        print(f"  Active orders (cycle={cyc}, non-reset_cleared, filled>0):")
        q.execute("""
            SELECT order_type, status, filled_amount, price, client_order_id
            FROM bot_orders 
            WHERE bot_id=? AND cycle_id=? AND filled_amount>0 AND price>0
            AND client_order_id LIKE 'CQB_%'
            AND status NOT IN ('placing','failed','auto_closed','reset_cleared')
            ORDER BY created_at
        """, (bid, cyc))
        rows = q.fetchall()
        total_qty = 0
        total_cost = 0
        for r in rows:
            otype, status, fa, pr, cid = r
            cost = fa * pr
            sign = -1 if otype in ('tp','close','adoption_reduce','dust_close','sl') else 1
            total_qty += sign * fa
            total_cost += sign * cost
            print(f"    {otype:<15} {status:<12} qty={fa:.4f} price=${pr:.2f}  CID={cid[:50]}")
        print(f"  → recompute_now: total_inv=${total_cost:.2f}  qty={total_qty:.4f}")
        
        # Show adoption orders specifically (all cycles)
        print(f"  Adoption orders (all cycles):")
        q.execute("""
            SELECT order_type, status, filled_amount, cycle_id, client_order_id
            FROM bot_orders WHERE bot_id=? AND order_type IN ('adoption','adoption_add')
            AND filled_amount>0 ORDER BY created_at DESC LIMIT 8
        """, (bid,))
        for r in q.fetchall():
            print(f"    {r[0]:<15} {r[1]:<12} qty={r[2]:.4f} cycle={r[3]}  CID={r[4][:50]}")

    # active_positions
    q.execute("SELECT bot_id, pair, side, size, entry_price FROM active_positions WHERE pair LIKE ?", (pair_like,))
    ap = q.fetchall()
    print(f"\n  active_positions: {ap}")

audit_bot('%sol%', '%SOL%', 2.62, 237.37/2.62)
audit_bot('%btc%', '%BTC%', 0.023, 1601.99/0.023)
audit_bot('%xrp%', '%XRP%', 338.2, 466.57/338.2)

c.close()
