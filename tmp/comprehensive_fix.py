import sqlite3, time

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

print("=== COMPREHENSIVE DB REPAIR ===")
print("Engine is running with OLD code — these fixes set stable state for restart with new code.")
print()

# ─── BTC (long_btc_price, bot_id=10016) ───────────────────────────────────────
# Problem: adoption was deleted → old reconciler computed CARRY(0.006) as true_qty
#          → wrote adoption=0.017 → recomputed again → total_invested=$418 (CARRY only)
# Fix: Delete ALL adoptions for BTC cycle=10 and write a CORRECT 0.023 adoption
#      Also update trades.total_invested to the correct value (0.023 × exchange_entry)
print("=== BTC: long_btc_price (10016) ===")

# Physical: 0.023 BTC @ $69,651.84
BTC_PHYS_QTY = 0.023
BTC_PHYS_PRICE = 69651.84  # from active_positions.entry_price

# Delete ALL PASS3 adoptions for BTC cycle=10
q.execute("DELETE FROM bot_orders WHERE bot_id=10016 AND order_type='adoption' AND cycle_id=10 AND client_order_id LIKE '%PASS3%'")
print(f"  Deleted {q.rowcount} existing P3 adoption(s) for cycle=10")

# Write a fresh adoption for the FULL gap (nothing proved at cycle=10 other than CARRY which is excluded by new code)
_oid = f"PASS3_ADOPTION_10016_C10"
_cid = f"CQB_10016_PASS3_C10"
_now = int(time.time())
q.execute("""
    INSERT INTO bot_orders
      (bot_id, order_id, client_order_id, order_type, price, amount, filled_amount,
       status, step, cycle_id, created_at, updated_at)
    VALUES (?, ?, ?, 'adoption', ?, ?, ?, 'filled', 1,
            (SELECT COALESCE(cycle_id, 1) FROM trades WHERE bot_id=?), ?, ?)
""", (10016, _oid, _cid, BTC_PHYS_PRICE, BTC_PHYS_QTY, BTC_PHYS_QTY, 10016, _now, _now))
print(f"  Wrote adoption qty={BTC_PHYS_QTY} @ ${BTC_PHYS_PRICE}")

# Update trades to correct values
BTC_INVESTED = round(BTC_PHYS_QTY * BTC_PHYS_PRICE, 4)
q.execute("""
    UPDATE trades SET total_invested=?, avg_entry_price=?, entry_confirmed=1, current_step=1
    WHERE bot_id=10016
""", (BTC_INVESTED, BTC_PHYS_PRICE))
print(f"  Updated trades: total_invested=${BTC_INVESTED:.2f}, avg=${BTC_PHYS_PRICE:.2f}")

# ─── SOL (bot_id=10008) ────────────────────────────────────────────────────────
# Problem: trades.total_invested=0, entry_confirmed=0  
#          All cycle=12 bot_orders are reset_cleared → recompute(new code) returns 0
#          active_positions.bot_id=10008 (already fixed)
# Fix: Similar to BTC — write a direct adoption for full physical qty,  
#      update trades to correct values so monitor reports correctly
print()
print("=== SOL: long sol (10008) ===")

SOL_PHYS_QTY = 2.62
SOL_PHYS_PRICE = 90.5982  # from active_positions.entry_price

# Check current cycle
q.execute("SELECT COALESCE(cycle_id, 12) FROM trades WHERE bot_id=10008")
r = q.fetchone()
sol_cycle = r[0] if r else 12

# Delete old PASS3 adoptions
q.execute("DELETE FROM bot_orders WHERE bot_id=10008 AND order_type='adoption' AND client_order_id LIKE '%PASS3%'")
print(f"  Deleted {q.rowcount} old P3 adoptions")

# Write full adoption
_oid = f"PASS3_ADOPTION_10008_C{sol_cycle}"
_cid = f"CQB_10008_PASS3_C{sol_cycle}"
q.execute("""
    INSERT INTO bot_orders
      (bot_id, order_id, client_order_id, order_type, price, amount, filled_amount,
       status, step, cycle_id, created_at, updated_at)
    VALUES (?, ?, ?, 'adoption', ?, ?, ?, 'filled', 1, ?, ?, ?)
""", (10008, _oid, _cid, SOL_PHYS_PRICE, SOL_PHYS_QTY, SOL_PHYS_QTY, sol_cycle, _now, _now))
print(f"  Wrote adoption qty={SOL_PHYS_QTY} @ ${SOL_PHYS_PRICE}")

SOL_INVESTED = round(SOL_PHYS_QTY * SOL_PHYS_PRICE, 4)
q.execute("""
    UPDATE trades SET total_invested=?, avg_entry_price=?, entry_confirmed=1, current_step=1
    WHERE bot_id=10008
""", (SOL_INVESTED, SOL_PHYS_PRICE))
print(f"  Updated trades: total_invested=${SOL_INVESTED:.2f}, avg=${SOL_PHYS_PRICE:.2f}")

# Also ensure active_positions is linked
q.execute("UPDATE active_positions SET bot_id=10008 WHERE pair='SOLUSDC' AND (bot_id=0 OR bot_id IS NULL)")
print(f"  active_positions bot_id fix: {q.rowcount} row(s)")

# ─── XRP (bot_id=10017) ────────────────────────────────────────────────────────
# Problem: Oscillating between 334.5 and 338.2 (3.7 gap, entry reset_cleared)  
# Fix: Write correct 338.2 adoption (overwriting 334.5) and update trades
print()
print("=== XRP: xrp long (10017) ===")

XRP_PHYS_QTY = 338.2
XRP_PHYS_PRICE = 1.37955  # from active_positions

q.execute("SELECT cycle_id FROM trades WHERE bot_id=10017")
r = q.fetchone()
xrp_cycle = r[0] if r else 42

q.execute("DELETE FROM bot_orders WHERE bot_id=10017 AND order_type='adoption' AND client_order_id LIKE '%PASS3%'")
print(f"  Deleted {q.rowcount} old P3 adoptions")

_oid = f"PASS3_ADOPTION_10017_C{xrp_cycle}"
_cid = f"CQB_10017_PASS3_C{xrp_cycle}"
q.execute("""
    INSERT INTO bot_orders
      (bot_id, order_id, client_order_id, order_type, price, amount, filled_amount,
       status, step, cycle_id, created_at, updated_at)
    VALUES (?, ?, ?, 'adoption', ?, ?, ?, 'filled', 1, ?, ?, ?)
""", (10017, _oid, _cid, XRP_PHYS_PRICE, XRP_PHYS_QTY, XRP_PHYS_QTY, xrp_cycle, _now, _now))
print(f"  Wrote adoption qty={XRP_PHYS_QTY} @ ${XRP_PHYS_PRICE}")

XRP_INVESTED = round(XRP_PHYS_QTY * XRP_PHYS_PRICE, 4)
q.execute("""
    UPDATE trades SET total_invested=?, avg_entry_price=?
    WHERE bot_id=10017
""", (XRP_INVESTED, XRP_PHYS_PRICE))
print(f"  Updated trades: total_invested=${XRP_INVESTED:.2f}, avg=${XRP_PHYS_PRICE:.4f}")

c.commit()

# ─── VERIFY ───────────────────────────────────────────────────────────────────
print()
print("=== VERIFICATION ===")
for bid, name, phys_qty in [(10016, 'long_btc_price', BTC_PHYS_QTY), (10008, 'sol', SOL_PHYS_QTY), (10017, 'xrp_long', XRP_PHYS_QTY)]:
    q.execute("SELECT total_invested, avg_entry_price FROM trades WHERE bot_id=?", (bid,))
    r = q.fetchone()
    if r:
        ti, avg = r
        vqty = ti/avg if avg else 0
        match = abs(vqty - phys_qty) < 0.01
        print(f"  {name}: system={vqty:.4f} phys={phys_qty:.4f} {'✅' if match else '⚠️'}")

print()
print("IMPORTANT: These adoption fixes will be OVERWRITTEN by the reconciler if engine keeps running")
print("with OLD code. The oscillation fix in reconciler.py prevents this only with NEW code.")
print("RESTART THE ENGINE to load the new code and make these fixes permanent.")
c.close()
