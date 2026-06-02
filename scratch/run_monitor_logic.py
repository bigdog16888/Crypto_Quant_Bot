import os
import sys
import json
import sqlite3
import pandas as pd

sys.path.append(os.getcwd())

from config.settings import config as global_config
from engine.exchange_interface import normalize_symbol as _norm_universal
from engine.database import get_pair_virtual_net as _get_virtual_net, get_manual_whitelists
from engine.parity_gates import qty_tolerance as pair_qty_tolerance

def run_monitor_logic():
    # 1. Fetch fresh data
    db_path = global_config.PATHS['DB_FILE']
    with sqlite3.connect(db_path) as conn:
        query_all = """
            SELECT b.id AS id, b.name AS name, b.pair AS pair, b.direction AS direction,
                   b.strategy_type AS strategy_type, b.config AS config, t.current_step AS current_step,
                   t.total_invested AS total_invested, t.avg_entry_price AS avg_entry_price,
                   t.target_tp_price AS target_tp_price, b.is_active AS is_active, b.status AS status,
                   b.error AS error, t.basket_start_time AS basket_start_time,
                   t.cycle_start_time AS cycle_start_time, t.cycle_phase AS cycle_phase,
                   t.open_qty AS open_qty, b.bot_type AS bot_type, b.parent_bot_id AS parent_bot_id,
                   (SELECT pb.name FROM bots pb WHERE pb.id = b.parent_bot_id) AS parent_name,
                   (SELECT pb.hedge_trigger_step FROM bots pb WHERE pb.id = b.parent_bot_id) AS parent_hedge_trigger_step
            FROM bots b
            LEFT JOIN trades t ON b.id = t.bot_id
            WHERE b.is_active = 1
        """
        df_pos_f = pd.read_sql(query_all, conn)

        try:
            df_physical_f = pd.read_sql("SELECT pair, side, size, entry_price, last_checked FROM active_positions", conn)
        except Exception:
            df_physical_f = pd.DataFrame()

        # Try fetching open orders from exchange, otherwise default to []
        market_orders_f = []
        exchange_error = None
        try:
            from engine.exchange_interface import ExchangeInterface
            ex = ExchangeInterface(market_type=global_config.MARKET_TYPE)
            market_orders_f = ex.fetch_open_orders(None)
        except Exception as e:
            exchange_error = str(e)

        query_h = """
            SELECT bo.bot_id, bo.order_type, bo.filled_amount, bo.status, bo.created_at
            FROM bot_orders bo
            WHERE bo.order_type IN ('hedge', 'hedge_tp', 'hedgetp')
              AND bo.status NOT IN ('canceled', 'cancelled', 'rejected', 'failed',
                                    'reset_cleared', 'auto_closed', 'placing')
              AND bo.filled_amount > 0
        """
        df_h_f = pd.read_sql(query_h, conn)

    # Replicate metrics calculation
    pair_prices = {}
    virtual_net_by_norm = {}
    mismatched_pairs = []

    hedge_amounts = {}
    if not df_h_f.empty:
        for b_id in df_pos_f['id'].unique():
            h_sum = df_h_f[(df_h_f['bot_id'] == b_id) & (df_h_f['order_type'] == 'hedge')]['filled_amount'].sum()
            hx_sum = df_h_f[(df_h_f['bot_id'] == b_id) & (df_h_f['order_type'] == 'hedge_tp')]['filled_amount'].sum()
            hedge_amounts[b_id] = max(0.0, h_sum - hx_sum)

    hedged_bot_ids = set(df_h_f[df_h_f['filled_amount'] > 1e-8]['bot_id'].unique())

    # Pre-calculate physical order counts for health checks
    physical_order_counts = {}
    for o in market_orders_f:
        cid = str(o.get('clientOrderId') or '')
        if cid.startswith('CQB_'):
            try:
                parts = cid.split('_')
                if len(parts) >= 2:
                    bid_parsed = int(parts[1])
                    physical_order_counts[bid_parsed] = physical_order_counts.get(bid_parsed, 0) + 1
            except Exception: pass

    # Single source of truth calculation for virtual nets
    unique_db_pairs = {}
    for _, row in df_pos_f.iterrows():
        p_key = _norm_universal(row['pair'])
        if p_key not in unique_db_pairs:
            unique_db_pairs[p_key] = row['pair']
        avg = float(row.get('avg_entry_price') or 0)
        if avg > 0 and p_key not in pair_prices:
            pair_prices[p_key] = avg

    for p_key, canonical_pair in unique_db_pairs.items():
        try:
            virtual_net_by_norm[p_key] = _get_virtual_net(canonical_pair)
        except Exception:
            virtual_net_by_norm[p_key] = 0.0

    live_physical_net_by_pair = {}
    # Use active_positions table since API key is invalid/fails
    if not df_physical_f.empty:
        for _, row in df_physical_f.iterrows():
            if pd.notna(row['size']) and pd.notna(row['entry_price']):
                qty, price, side = abs(float(row['size'])), float(row['entry_price']), str(row['side']).upper().strip()
                s_key, p_key = ('LONG' if side in ('BUY', 'LONG') else 'SHORT'), _norm_universal(row['pair'])
                if p_key not in pair_prices:
                    pair_prices[p_key] = price
                signed = qty if s_key == 'LONG' else -qty
                live_physical_net_by_pair[p_key] = live_physical_net_by_pair.get(p_key, 0.0) + signed

    # Fallback/update pair mark prices from config if any
    all_symbols = set(unique_db_pairs.keys())
    if not df_physical_f.empty:
        all_symbols |= set(_norm_universal(p) for p in df_physical_f['pair'])

    # Try fetching mark prices from exchange tickers
    try:
        from engine.exchange_interface import ExchangeInterface
        ex_live = ExchangeInterface(market_type=global_config.MARKET_TYPE)
        for p_key, canonical_pair in unique_db_pairs.items():
            try:
                live_px = ex_live.get_last_price(canonical_pair)
                if live_px and float(live_px) > 0:
                    pair_prices[p_key] = float(live_px)
            except Exception: pass
    except Exception: pass

    worst_pair_usd = 0.0
    mismatched_pair_count = 0

    for p in sorted(all_symbols):
        v_net_qty = virtual_net_by_norm.get(p, 0.0)
        ph_net_qty = live_physical_net_by_pair.get(p, 0.0)
        whitelists = get_manual_whitelists(p)
        for w in whitelists:
            ph_net_qty -= float(w['qty']) if w['side'] == 'LONG' else -float(w['qty'])

        ref_price = pair_prices.get(p, 1.0)
        net_qty_diff = abs(v_net_qty - ph_net_qty)
        net_usd_diff = net_qty_diff * ref_price
        if net_usd_diff > worst_pair_usd:
            worst_pair_usd = net_usd_diff
        if net_qty_diff > pair_qty_tolerance():
            mismatched_pair_count += 1
            mismatched_pairs.append((f"{p} NET", v_net_qty * ref_price, ph_net_qty * ref_price, net_usd_diff, v_net_qty, ph_net_qty, ph_net_qty - v_net_qty, ref_price))

    # --- PRINT NETTING DIAGNOSTICS ---
    print("\n🔍 Global Netting Diagnostics")
    print("-" * 50)
    print("Reconciliation Mode: Global Net (Hedge-Aware)")
    
    # We display the table: Symbol | System Net | Exchange Net | Diff Qty
    print(f"{'Symbol':<15} | {'System Net':<12} | {'Exchange Net':<12} | {'Diff Qty':<10}")
    print("-" * 60)
    for p_dbg in sorted(all_symbols):
        v_dbg = virtual_net_by_norm.get(p_dbg, 0.0)
        ph_net_dbg = live_physical_net_by_pair.get(p_dbg, 0.0)
        ph_l_dbg = ph_net_dbg if ph_net_dbg > 0 else 0.0
        ph_s_dbg = abs(ph_net_dbg) if ph_net_dbg < 0 else 0.0
        ph_net_dbg = ph_l_dbg - ph_s_dbg
        print(f"{p_dbg:<15} | {v_dbg:+12.4f} | {ph_net_dbg:+12.4f} | {abs(v_dbg - ph_net_dbg):10.4f}")
    print("-" * 60)
    
    _status = "HEALTHY" if mismatched_pair_count == 0 else "MISMATCH"
    print(f"Mismatched Pairs: {mismatched_pair_count}")
    print(f"Worst Pair Gap (USD): ${worst_pair_usd:,.2f}")
    print(f"System Status: {_status}")
    print("-" * 50)
    
    if mismatched_pairs:
        print("\n🚨 SYSTEM MISMATCH DETECTED")
        for row_mp in mismatched_pairs:
            mp_pair, mp_virt, mp_phys, mp_diff, mp_vqty, mp_pqty, mp_dqty, mp_price = row_mp
            print(f"   ⚠️ **{mp_pair}**: System ${mp_virt:,.2f} vs Exchange ${mp_phys:,.2f} (Diff: ${mp_diff:,.2f}) | Qty: sys={mp_vqty:+.4f} ex={mp_pqty:+.4f} diff={mp_dqty:+.4f}")
        print("-" * 50)

    # --- ORDER HEALTH ALERTS ---
    order_health_msg = ""
    order_status_color = "green"

    bots_with_missing_orders = []
    bots_with_partial_orders = []
    bots_with_margin_held = []
    for _, row in df_pos_f.iterrows():
        bid, bot_inv, c_step = int(row['id']), float(row['total_invested'] or 0), int(row.get('current_step', 0) or 0)
        actual_ph = physical_order_counts.get(bid, 0)

        # Skip bots that are legitimately idle or finishing
        if "EXITING" in str(row.get('status','')).upper() or ("SCANNING" in str(row.get('status','')).upper() and bot_inv <= 0.01):
            continue

        cycle_phase = str(row.get('cycle_phase', 'IDLE')).upper()

        if bid in hedged_bot_ids:
            if cycle_phase == 'HEDGE_EXIT_PENDING' and actual_ph == 0:
                bots_with_missing_orders.append(f"{row['name']} (HEDGE_EXIT no order)")
        elif cycle_phase == 'MARGIN_HELD':
            bots_with_margin_held.append(f"{row['name']}")
        else:
            is_missing = False
            if actual_ph == 0 and bot_inv > 0.01 and cycle_phase not in ('CARRY_PENDING', 'HEDGED'):
                is_missing = True
                if row.get('bot_type') == 'hedge_child':
                    parent_id = row.get('parent_bot_id')
                    if parent_id:
                        parent_rows = df_pos_f[df_pos_f['id'] == parent_id]
                        if not parent_rows.empty:
                            parent_status = str(parent_rows.iloc[0]['status'])
                            if "IN TRADE" in parent_status:
                                is_missing = False
            if is_missing:
                bots_with_missing_orders.append(row['name'])
            elif actual_ph == 0 and cycle_phase in ('CARRY_PENDING', 'HEDGED'):
                pass
            elif actual_ph < 2 and c_step >= 1 and bot_inv > 0.01 and row.get('bot_type', 'standard') == 'standard':
                # Suppression check
                gow_ok = True
                try:
                    from engine.oneway_netting import gate_oneway_opposite_entry
                    gow_ok, _ = gate_oneway_opposite_entry(bid, row['pair'], row['direction'])
                except Exception: pass
                if gow_ok:
                    bots_with_partial_orders.append(f"{row['name']} ({actual_ph}/2)")

    if bots_with_missing_orders:
        order_health_msg, order_status_color = f"⚠️ MISSING CRITICAL ORDERS: {', '.join(bots_with_missing_orders)}!", "red"
    elif bots_with_margin_held:
        order_health_msg, order_status_color = f"⚠️ MARGIN HELD: {', '.join(bots_with_margin_held)} — TP blocked by account margin limit. Free margin to allow TP placement.", "orange"
    elif bots_with_partial_orders:
        order_health_msg, order_status_color = f"⚠️ MISSING GRIDS: {', '.join(bots_with_partial_orders)}", "orange"
    else:
        order_health_msg = f"✅ ORDERS SYNCED: {len(market_orders_f)} active orders."

    print(f"\nOrder Health status line:")
    print(f"🩺 Order Health: :{order_status_color}[{order_health_msg}]")

if __name__ == '__main__':
    run_monitor_logic()
