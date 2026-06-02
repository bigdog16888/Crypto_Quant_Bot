import sqlite3
import pandas as pd
import json

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()

    # 1. Run the user's SQL select query
    print("=== SELECT QUERY OUTPUT ===")
    sql = """
    SELECT b.pair, 
           SUM(CASE WHEN b.direction='LONG' THEN t.open_qty ELSE -t.open_qty END) as sys_net_qty
    FROM bots b JOIN trades t ON t.bot_id = b.id
    WHERE b.is_active = 1 AND t.open_qty > 0
    GROUP BY b.pair
    ORDER BY b.pair;
    """
    rows = cursor.execute(sql).fetchall()
    print("pair | sys_net_qty")
    print("-" * 35)
    for r in rows:
        print(f"{r[0]} | {r[1]:+.4f}")
    print()

    # 2. Replicate monitor data fetching and print Diagnostics / Order Health
    from engine.database import get_pair_virtual_net
    from engine.exchange_interface import ExchangeInterface

    # Get data
    query_all = """
        SELECT b.id AS id, b.name AS name, b.pair AS pair, b.direction AS direction, 
               b.strategy_type AS strategy_type, b.config AS config, t.current_step AS current_step, 
               t.total_invested AS total_invested, t.avg_entry_price AS avg_entry_price, 
               t.target_tp_price AS target_tp_price, b.is_active AS is_active, b.status AS status, 
               b.error AS error, t.basket_start_time AS basket_start_time, 
               t.cycle_start_time AS cycle_start_time, t.cycle_phase AS cycle_phase, 
               t.open_qty AS open_qty, b.bot_type AS bot_type, b.parent_bot_id AS parent_bot_id
        FROM bots b
        LEFT JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active = 1
    """
    df_pos_f = pd.read_sql(query_all, conn)

    ex = ExchangeInterface(market_type='future')
    market_orders_f = ex.fetch_open_orders(None) or []

    # Counts
    physical_order_counts = {}
    for o in market_orders_f:
        cid = str(o.get('clientOrderId') or '')
        if cid.startswith('CQB_'):
            try:
                parts = cid.split('_')
                if len(parts) >= 2:
                    bid_parsed = int(parts[1])
                    physical_order_counts[bid_parsed] = physical_order_counts.get(bid_parsed, 0) + 1
            except: pass

    # Get physical net
    live_physical_net_by_pair = {}
    for _pos in (ex.fetch_positions() or []):
        _amt = float(_pos.get('contracts', 0) or _pos.get('size', 0) or 0)
        if abs(_amt) < 1e-12:
            continue
        _pkey = _pos.get('symbol', '').split(':')[0].replace('/', '').upper()
        live_physical_net_by_pair[_pkey] = live_physical_net_by_pair.get(_pkey, 0.0) + _amt

    # Unique pairs
    unique_db_pairs = {p.split(':')[0].replace('/', '').upper(): p for p in df_pos_f['pair'].unique()}
    virtual_net_by_norm = {}
    for p_key, canonical_pair in unique_db_pairs.items():
        virtual_net_by_norm[p_key] = get_pair_virtual_net(canonical_pair)

    # Print Diagnostics Table
    print("=== GLOBAL NETTING DIAGNOSTICS ===")
    print("Reconciliation Mode: Global Net (Hedge-Aware)")
    print(f"{'Symbol':<15} | {'System Net':<12} | {'Exchange Net':<12} | {'Diff Qty':<10}")
    print("-" * 60)
    for p_dbg in sorted(unique_db_pairs.keys()):
        v_dbg = virtual_net_by_norm.get(p_dbg, 0.0)
        ph_net_dbg = live_physical_net_by_pair.get(p_dbg, 0.0)
        diff_qty = abs(v_dbg - ph_net_dbg)
        print(f"{p_dbg:<15} | {v_dbg:+12.4f} | {ph_net_dbg:+12.4f} | {diff_qty:10.4f}")
    print()

    # Calculate Order Health Status Line
    df_h_f = pd.read_sql("""
        SELECT bo.bot_id, bo.order_type, bo.filled_amount, bo.status, bo.created_at
        FROM bot_orders bo
        WHERE bo.order_type IN ('hedge', 'hedge_tp', 'hedgetp')
          AND bo.status NOT IN ('canceled', 'cancelled', 'rejected', 'failed',
                                'reset_cleared', 'auto_closed', 'placing')
          AND bo.filled_amount > 0
    """, conn)
    hedged_bot_ids = set(df_h_f[df_h_f['filled_amount'] > 1e-8]['bot_id'].unique())

    bots_with_missing_orders = []
    bots_with_partial_orders = []
    bots_with_margin_held = []

    # Map status
    def derive_status(row):
        if not row['is_active']: return "⚪ STOPPED"
        b_status = str(row.get('status', '')).upper()
        if 'REQUIRE_MANUAL' in b_status: return "🚨 MANUAL GATE"
        if 'CARRY_PENDING' in b_status: return "⏳ CARRY/PENDING"
        c_phase = str(row.get('cycle_phase', 'IDLE')).upper()
        invested = float(row.get('total_invested', 0) or 0)
        c_step = int(row.get('current_step', 0) if pd.notna(row.get('current_step')) else 0)
        if c_phase == 'MARGIN_HELD': return f"🚫 MARGIN HELD | Step {c_step}"
        if c_phase == 'ACTIVE' or invested > 0.01:
            if invested > 0 and invested <= 5.0: return "🟡 DUST/PARTIAL"
            if row.get('bot_type') == 'hedge_child':
                return f"🔴 HEDGE ACTIVE | Step {c_step}"
            return f"🔴 IN TRADE | Step {c_step}"
        if row.get('bot_type') == 'hedge_child':
            return "HEDGE STANDBY"
        return "🟢 SCANNING"

    df_pos_f['status'] = df_pos_f.apply(derive_status, axis=1)

    for _, row in df_pos_f.iterrows():
        bid, bot_inv, c_step = int(row['id']), float(row['total_invested'] or 0), int(row.get('current_step', 0))
        actual_ph = physical_order_counts.get(bid, 0)
        if "EXITING" in str(row.get('status','')).upper() or ("SCANNING" in str(row.get('status','')).upper() and bot_inv <= 0.01):
            continue
        cycle_phase = str(row.get('cycle_phase', 'IDLE')).upper()

        if bid in hedged_bot_ids:
            if cycle_phase == 'HEDGE_EXIT_PENDING' and actual_ph == 0:
                bots_with_missing_orders.append(f"{row['name']} (HEDGE_EXIT no order)")
        elif cycle_phase == 'MARGIN_HELD':
            bots_with_margin_held.append(row['name'])
        else:
            # Here we simulate the logic before fixing the missing critical orders for hedge_child
            if actual_ph == 0 and bot_inv > 0.01 and cycle_phase not in ('CARRY_PENDING', 'HEDGED'):
                bots_with_missing_orders.append(row['name'])
            elif actual_ph < 2 and c_step >= 1 and bot_inv > 0.01 and row.get('bot_type', 'standard') == 'standard':
                bots_with_partial_orders.append(f"{row['name']} ({actual_ph}/2)")

    if bots_with_missing_orders:
        order_health_msg = f"⚠️ MISSING CRITICAL ORDERS: {', '.join(bots_with_missing_orders)}!"
    elif bots_with_margin_held:
        order_health_msg = f"⚠️ MARGIN HELD: {', '.join(bots_with_margin_held)} — TP blocked by account margin limit. Free margin to allow TP placement."
    elif bots_with_partial_orders:
        order_health_msg = f"⚠️ MISSING GRIDS: {', '.join(bots_with_partial_orders)}"
    else:
        order_health_msg = f"✅ ORDERS SYNCED: {len(market_orders_f)} active orders."

    print("=== ORDER HEALTH STATUS LINE ===")
    print(f"Order Health: {order_health_msg}")
    print()

    # 3. Check xrp long_hedge specific orders
    child_orders = [o for o in market_orders_f if str(o.get('clientOrderId') or '').startswith("CQB_100313_")]
    print("=== XRP LONG_HEDGE OPEN ORDERS ON EXCHANGE ===")
    print(f"Count: {len(child_orders)}")
    for o in child_orders:
        print(f"  Order: ID={o.get('id')} | Price={o.get('price')} | Qty={o.get('amount')} | Side={o.get('side')} | CID={o.get('clientOrderId')}")

if __name__ == '__main__':
    run()
