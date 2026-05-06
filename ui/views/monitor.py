import json
import streamlit as st
import time
import pandas as pd
import plotly.graph_objects as go
import ccxt
import ccxt
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from engine.exchange_interface import ExchangeInterface
from engine.database import (
    get_connection, get_bots_by_order_id, get_unread_notifications, 
    mark_notifications_read, import_position_from_exchange,
    add_manual_whitelist, clear_manual_whitelists_for_pair, get_manual_whitelists
)
from engine.reconciler import StateReconciler, ReconciliationAction
from config.settings import config as global_config
from engine.exchange_interface import normalize_symbol as _norm_universal

# --- Performance Caching Wrappers ---
@st.cache_resource(ttl=3600, show_spinner=False)
def get_exchange_instance(market_type):
    """Singleton provider for ExchangeInterface to reuse connections."""
    return ExchangeInterface(market_type=market_type)


@st.cache_data(ttl=5, show_spinner=False)
def fetch_ohlcv_cached(market_type, symbol, timeframe):
    """
    UI PERFORMANCE BATCHING 🚀
    Reads pre-fetched OHLCV data from the Engine's local JSON cache
    instead of making parallel REST API calls that stall the Dashboard.
    """
    try:
        cache_file = os.path.join(global_config.ROOT_DIR, 'data', 'market_cache.json')
        if os.path.exists(cache_file):
            with open(cache_file, 'r') as f:
                cache_data = json.load(f)
            
            # Find the symbol in the cache
            # Cache keys might be normalized (e.g., BTC/USDC)
            if symbol in cache_data and timeframe in cache_data[symbol]:
                return cache_data[symbol][timeframe]
            
        # Fallback to direct fetch if cache is missing or stale
        ex = get_exchange_instance(market_type)
        norm_symbol = symbol
        if market_type == 'future' and ':' not in symbol:
            if symbol.endswith('/USDT'): norm_symbol = f"{symbol}:USDT"
            elif symbol.endswith('/USDC'): norm_symbol = f"{symbol}:USDC"
        
        return ex.fetch_ohlcv(norm_symbol, timeframe=timeframe, limit=100)
    except Exception as e: 
        print(f"OHLCV Error for {symbol}: {e}")
        return []

@st.cache_data(ttl=5, show_spinner=False)
def fetch_positions_cached(market_type):
    try:
        ex = get_exchange_instance(market_type)
        return ex.fetch_positions()
    except Exception as e:
        print(f"UI Fetch Error: {e}")
        return []

@st.cache_data(ttl=5, show_spinner=False)
def fetch_open_orders_cached(market_type, symbol):
    try:
        ex = get_exchange_instance(market_type)
        return ex.fetch_open_orders(symbol)
    except Exception as e:
        print(f"Error fetching orders for {symbol}: {e}")
        return []

@st.cache_data(ttl=15, show_spinner=False)
def fetch_balance_cached(market_type):
    try:
        ex = get_exchange_instance(market_type)
        return ex.fetch_balance()
    except Exception: return {}
# ------------------------------------


def render_monitor_view():
    col1, col2 = st.columns([4, 1])
    with col1:
        st.header("📊 Live Market Monitor")
        st.caption(f"Last Updated: {time.strftime('%H:%M:%S')} (Local)")
    with col2:
        if st.button("🔄 Pre-Flight Sync"):
            try:
                from engine.exchange_interface import ExchangeInterface
                from engine.database import update_active_positions_snapshot
                with st.spinner("Syncing exchange..."):
                    ex = ExchangeInterface()
                    pos = ex.fetch_positions()
                    update_active_positions_snapshot(pos)
                st.toast("✅ Active positions synchronized")
                time.sleep(0.5)
                st.rerun()
            except Exception as e:
                st.error(f"Sync failed: {e}")

    # --- Notifications (Phase 9.3) ---
    try:
        # Initialize session state for notifications if not present
        if 'shown_notifications' not in st.session_state:
            st.session_state.shown_notifications = set()

        notes = get_unread_notifications(limit=5)
        if notes:
            n_ids_to_mark = []
            for n in notes:
                # n: id, timestamp, type, message, bot_id
                nid, _, ntype, msg, bid = n
                
                # Deduplicate: Only show if not already shown in this session (or handled by DB)
                # Note: DB marking might be slow, so session state is faster for UI responsiveness
                if nid not in st.session_state.shown_notifications:
                    icon = "ℹ️"
                    if ntype == 'success': icon = "✅"
                    elif ntype == 'error': icon = "❌"
                    elif ntype == 'warning': icon = "⚠️"
                    
                    st.toast(msg, icon=icon)
                    st.session_state.shown_notifications.add(nid)
                    n_ids_to_mark.append(nid)
            
            if n_ids_to_mark:
                mark_notifications_read(n_ids_to_mark)
    except Exception:
        pass # Fail silently to keep UI responsive
    
    # --- Risk Heatmap (Phase 10.2) ---
    if st.checkbox("Show Portfolio Heatmap", value=True):
        try:
            import plotly.express as px
            # Fetch active bots for visualization
            conn = get_connection()
            df_risk = pd.read_sql("SELECT name, total_invested, current_step, avg_entry_price, last_exit_price FROM trades JOIN bots ON trades.bot_id = bots.id WHERE total_invested > 0", conn)
            # conn.close() # We don't close get_connection() results usually? 
            # In database.py: get_connection uses thread local. closing it might perform actual close or skip. 
            # The pattern seems to be to rely on reuse.
            
            if not df_risk.empty:
               fig = px.treemap(
                   df_risk, 
                   path=['name'], 
                   values='total_invested',
                   color='current_step',
                   color_continuous_scale='RdYlGn_r', # Red for high step (high risk)
                   title="Active Risk Map (Size=Invested, Color=Step/Risk)"
               )
               st.plotly_chart(fig, width="stretch")
            else:
               st.info("No active positions to display in Heatmap.")
        except Exception as e:
            st.error(f"Heatmap Error: {e}")

    # --- Auto-Refresh Toggle (Default ON) ---
    auto_refresh = st.toggle("⚡ Auto-Refresh (15s) [ASync]", value=True, key="auto_refresh_toggle")
    
    # Detect if the Reconciler / Forensic Wizard is actively in use.
    wizard_active = any(bool(st.session_state[k]) for k in st.session_state if k.startswith(("forensic_trades_", "adopt_force_sel_", "trade_sel_")))

    # --- Fragment: Header Metrics (Command Center) ---
    @st.fragment(run_every=30 if auto_refresh and not wizard_active else None)
    def _header_metrics_fragment():
        # --- Command Center (Health Dashboard) ---
        try:
            conn = get_connection()
            cur = conn.cursor()
            
            # 1. Active Bots
            cur.execute("SELECT COUNT(*) FROM bots WHERE is_active = 1")
            active_count = cur.fetchone()[0]
            
            # 2. Total Invested (Exposure) from DB
            cur.execute("SELECT SUM(total_invested) FROM trades WHERE total_invested > 0")
            total_invested_res = cur.fetchone()
            total_invested_db = total_invested_res[0] if total_invested_res[0] else 0.0
            
            # 3. Calculate Global PnL (Requires live prices)
            cur.execute("SELECT t.total_invested, t.avg_entry_price, b.pair, b.direction FROM trades t JOIN bots b ON t.bot_id = b.id WHERE t.total_invested > 0 AND b.is_active = 1")
            active_trades = cur.fetchall()
            
            global_pnl_usd = 0.0
            
            # Fetch prices for active trades
            active_symbols = list(set([t[2] for t in active_trades]))
            price_map = {}
            if active_symbols:
                try:
                    ex_global = get_exchange_instance(market_type=global_config.MARKET_TYPE)
                    for sym in active_symbols:
                        price = ex_global.get_last_price(sym)
                        if price:
                            price_map[sym] = float(price)
                except Exception: pass
            
            for trade in active_trades:
                inv, entry, pair, direction = trade
                curr = price_map.get(pair, 0.0)
                if curr > 0 and entry > 0.0001: 
                    if direction == 'LONG': pnl = (curr - entry) / entry * inv
                    else: pnl = (entry - curr) / entry * inv
                    global_pnl_usd += pnl

            # 4. Fetch Multi-Asset Balances (Spot + Futures)
            futures_balance = 0.0
            spot_balance = 0.0
            total_equity = 0.0
            assets_breakdown = []

            # --- A. Futures Balance ---
            try:
                fut_data = fetch_balance_cached('future')
                if fut_data and 'total' in fut_data:
                    for asset, amount in fut_data['total'].items():
                        if amount and amount > 0:
                            assets_breakdown.append({
                                'Type': 'Futures', 'Asset': asset, 'Balance': amount,
                                'Unrealized PnL': 0.0, 'Equity': amount
                            })
                            if asset in ['USDT', 'USDC', 'USD', 'BUSD']: futures_balance += amount
            except Exception: pass

            # --- B. Spot Balance ---
            try:
                cur.execute("SELECT config FROM bots WHERE is_active = 1")
                active_configs = cur.fetchall()
                needs_spot = False
                for cfg in active_configs:
                    try:
                        c_dict = json.loads(cfg[0]) if cfg[0] else {}
                        if c_dict.get('market_type') == 'spot':
                            needs_spot = True; break
                    except: pass
                
                if needs_spot and global_config.MARKET_TYPE != 'future':
                    spot_data = fetch_balance_cached('spot')
                    if spot_data and 'total' in spot_data:
                        for asset, amount in spot_data['total'].items():
                            if amount > 0:
                                val = amount if asset in ['USDT', 'USDC', 'DAI', 'BUSD'] else 0.0
                                if val > 0: spot_balance += val
                                assets_breakdown.append({
                                    'Type': 'Spot', 'Asset': asset, 'Balance': amount,
                                    'Unrealized PnL': 0.0, 'Equity': val
                                })
            except Exception: pass

            total_equity = futures_balance + spot_balance + global_pnl_usd 
            
            # Display Metrics Grid
            m1, m2, m3, m4 = st.columns(4)
            with m1: st.metric("Total Equity", f"${total_equity:,.2f}")
            with m2: st.metric("Futures Balance", f"${futures_balance:,.2f}")
            with m3: st.metric("Active PnL", f"${global_pnl_usd:,.2f}")
            with m4: st.metric("Active Exposure", f"${total_invested_db:,.2f}")

            if assets_breakdown:
                with st.expander("💰 Detailed Asset Breakdown"):
                    st.table(pd.DataFrame(assets_breakdown))
            st.divider()
            
            # --- System Status Ribbon ---
            cur.execute("SELECT action, symbol, price FROM trade_history ORDER BY id DESC LIMIT 1")
            last_h = cur.fetchone()
            last_act_str = f"{last_h[0]}: {last_h[1]} @ {last_h[2]:,.2f}" if last_h else "NO RECENT ACTIVITY"
            st.info(f"CORE ENGINE: ONLINE | ACTIVE BOTS: {active_count} | LAST ACTION: {last_act_str}")
        except Exception as e:
            st.error(f"Dashboard Load Error: {e}")

    # Invoke Header Fragment
    _header_metrics_fragment()


    # --- Control Bar ---
    try:
        conn_b = get_connection()
        cur_b = conn_b.cursor()
        cur_b.execute("SELECT id, name, pair FROM bots WHERE is_active = 1")
        active_bots_list = cur_b.fetchall()
        # conn_b.close()
    except:
        active_bots_list = []
    
    bot_options = ["None (Symbol View)"] + [f"{b[1]} ({b[2]})" for b in active_bots_list]

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        c1a, c1b = st.columns(2)
        with c1a:
            selected_bot_str = st.selectbox("Focus Bot", bot_options, index=0, key="monitor_bot_select")
        
        target_symbol_list = list(global_config.ALLOWED_SYMBOLS)
        selected_bot_id = None
        
        if selected_bot_str != "None (Symbol View)":
            bot_name_sel = selected_bot_str.split(" (")[0]
            for b in active_bots_list:
                if b[1] == bot_name_sel:
                    selected_bot_id = b[0]
                    target_symbol_list = [b[2]] + [s for s in target_symbol_list if s != b[2]]
                    break
        else:
            active_pairs = list(set([b[2] for b in active_bots_list]))
            target_symbol_list = list(dict.fromkeys(active_pairs + target_symbol_list))

        with c1b:
            symbol = st.selectbox("Symbol", target_symbol_list, key="monitor_symbol")

    with col2:
        timeframe = st.selectbox("Timeframe", ["1m", "5m", "15m", "30m", "1h", "4h", "1d"], index=4, key="monitor_tf")
    with col3:
        # Layout hack to align button with selectboxes
        st.write("")
        st.write("")
        if st.button("🔄 Refresh Now", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    # --- Data Fetching (Parallel) ---
    with st.spinner("Fetching market data..."):
        raw_orders = []
        # Parallel fetch for speed
        with ThreadPoolExecutor(max_workers=6) as executor:
            f_ohlcv = executor.submit(fetch_ohlcv_cached, global_config.MARKET_TYPE, symbol, timeframe)
            f_pos = executor.submit(fetch_positions_cached, global_config.MARKET_TYPE)
            f_bal = executor.submit(fetch_balance_cached, global_config.MARKET_TYPE)
            
            # 🚀 ORDER FETCH FIX: Fetch all generic orders at once to prevent 
            # CCXT rate limits and symbol mapping errors in parallel threads
            try:
                ex_inst = get_exchange_instance(global_config.MARKET_TYPE)
                # Setting symbol=None fetches all active orders for the whole account
                f_orders = executor.submit(ex_inst.fetch_open_orders, None)
            except Exception as e:
                print(f"Error submitting order fetch: {e}")
                f_orders = None
            
            ohlcv_data = f_ohlcv.result()
            
            if f_orders:
                try:
                    res = f_orders.result(timeout=10)
                    if res: raw_orders.extend(res)
                except Exception as e:
                    print(f"Failed to fetch market orders: {e}")
        
        # DEDUPLICATE ORDERS (Safety mechanism)
        market_orders = list({str(o.get('id', '')): o for o in raw_orders if o}.values())

    # --- UI Layout: Tabs ---
    tab_overview, tab_charts, tab_history = st.tabs(["📊 Overview", "📈 Live Charts", "📝 Orders & History"])

    with tab_overview:
        # --- Prepare Data ---
        df_pos = pd.DataFrame()
        df_physical = pd.DataFrame()
        virtual_net_usd = 0.0
        virtual_gross_usd = 0.0
        physical_net_usd = 0.0
        virtual_by_pair = {}
        physical_by_pair = {}
        mismatched_pairs = []
        
        # --- Mismatch Alert Logic (v2.5.5) ---
        # 🛡️ ARCHITECT'S DOCTRINE: Universal Parity & Ghost Exclusion
        # We calculate the global virtual net for every pair by summing active bot positions 
        # and subtracting active (un-reset) hedges.
        
        # --- Mismatch Alert Logic ---
        try:
            # FUNDAMENTAL FIX: Use a fresh connection to bypass thread-local staleness in Streamlit
            db_path = global_config.PATHS['DB_FILE']
            with sqlite3.connect(db_path, timeout=10) as conn_fresh:
            
                # Fetch Bot Strategies (df_pos)
                query_all = """
                    SELECT b.id, b.name, b.pair, b.direction, b.strategy_type, b.config, t.current_step, t.total_invested, t.avg_entry_price, t.target_tp_price, b.is_active, b.status, b.error, t.basket_start_time, t.cycle_start_time, t.cycle_phase
                    FROM bots b
                    LEFT JOIN trades t ON b.id = t.bot_id
                    WHERE b.is_active = 1
                """
                df_pos = pd.read_sql(query_all, conn_fresh)
                
                # Fetch Physical Positions (df_physical)
                try:
                    df_physical = pd.read_sql("SELECT pair, side, size, entry_price, datetime(last_checked, 'unixepoch', 'localtime') as updated FROM active_positions", conn_fresh)
                except Exception as e:
                    st.error(f"Failed to fetch physical positions: {e}")
                    print(f"DEBUG UI ERROR: {e}")
                    df_physical = pd.DataFrame()
                
                # Initialize metrics
                hedge_amounts = {}
                diff_net = 0.0
                virtual_qty_by_pair = {}
                physical_qty_by_pair = {}
                pair_prices = {} # For converting qty back to USD for readability
                virtual_net_by_norm = {}

                # Fetch Hedged Bots (All cycles since hedges can outlive TP)
                query_h = """
                    SELECT bo.bot_id, b.pair, b.direction, bo.order_type, bo.filled_amount
                    FROM bot_orders bo
                    JOIN bots b ON bo.bot_id = b.id
                    JOIN trades t ON b.id = t.bot_id
                    WHERE b.is_active = 1
                      AND bo.order_type IN ('hedge', 'hedge_tp')
                      AND bo.status NOT IN ('canceled', 'cancelled', 'rejected', 'failed', 'reset_cleared', 'auto_closed', 'placing')
                      AND (bo.cycle_id = t.cycle_id OR (bo.cycle_id IS NULL AND t.cycle_id IS NULL))
                      AND (t.wipe_wall_ts = 0 OR bo.created_at >= t.wipe_wall_ts)
                """
                df_h = pd.read_sql(query_h, conn_fresh)
                
                # Pre-calculate hedge_amounts for the Status Column
                if not df_h.empty:
                    for b_id in df_pos['id'].unique():
                        # 🛡️ ARCHITECT'S FIX: Hedges only exist if the bot is actively in a trade (Step > 0)
                        # or specifically marked as HEDGE_EXIT_PENDING. Step 0 bots are SCANNING.
                        row_bot = df_pos[df_pos['id'] == b_id].iloc[0]
                        # c_step = int(row_bot.get('current_step', 0) if pd.notna(row_bot.get('current_step')) else 0)
                        # if c_step == 0 and "EXITING" not in str(row_bot.get('status','')).upper():
                        #     hedge_amounts[b_id] = 0.0
                        #     continue

                        h_sum = df_h[(df_h['bot_id'] == b_id) & (df_h['order_type'] == 'hedge')]['filled_amount'].sum()
                        hx_sum = df_h[(df_h['bot_id'] == b_id) & (df_h['order_type'] == 'hedge_tp')]['filled_amount'].sum()
                        hedge_amounts[b_id] = max(0.0, h_sum - hx_sum)

                # Fetch Hedged Bot IDs for heuristic missing-order detection
                hedged_bot_ids = set(df_h[df_h['filled_amount'] > 1e-8]['bot_id'].unique())

                # --- FUNDAMENTAL FIX: DATA-DRIVEN STATUS ---
                # Derive 'display_status' from current_step and status strings
                def derive_status(row):
                    if not row['is_active']: return "⚪ STOPPED"
                    
                    b_status = str(row.get('bot_status', row.get('status', ''))).upper()
                    if 'REQUIRE_MANUAL' in b_status: return "🚨 MANUAL GATE"
                    if 'CARRY_PENDING' in b_status: return "⏳ CARRY/PENDING"
                    if 'HEDGE_EXIT_PENDING' in b_status: return "🛡️ HEDGE EXITING"
                    
                    c_phase = str(row.get('cycle_phase', 'IDLE')).upper()
                    c_step = int(row.get('current_step', 0) if pd.notna(row.get('current_step')) else 0)
                    invested = float(row.get('total_invested', 0) or 0)
                    h_amt = hedge_amounts.get(row['id'], 0)
                    
                    if c_phase == 'HEDGED' or h_amt > 1e-8:
                        if "EXITING" in b_status:
                            return f"🛡️ HEDGE EXIT PENDING ({h_amt:.4f})"
                        return f"🛡️ HEDGED ({h_amt:.4f}) | Step {c_step}"
                    
                    if c_phase == 'ACTIVE' or invested > 1e-8:
                        # Binance min-notional is 5 USD. Noticeably small positions are dust.
                        if invested > 0 and invested <= 5.0: 
                            return "🟡 DUST/PARTIAL"
                        return f"🔴 IN TRADE | Step {c_step}"
                    
                    return "🟢 SCANNING"

                # Apply fix to df_pos BEFORE rendering
                df_pos['status'] = df_pos.apply(derive_status, axis=1)

                # Group active IN TRADE bots to the top
                df_pos['sort_priority'] = df_pos['status'].apply(lambda x: 1 if ("IN TRADE" in x or "HEDGED" in x) else (2 if "SCANNING" in x else 3))
                df_pos.sort_values(by=['sort_priority', 'name'], ascending=[True, True], inplace=True)

                # 1. Fetch ALL Active Bot Positions (O(1) approach)
                # v2.5.5: Prefer open_qty accumulator over invested/avg derived qty.
                query_v = """
                    SELECT b.id, b.pair, b.direction, t.open_qty, t.total_invested, t.avg_entry_price
                    FROM bots b
                    JOIN trades t ON b.id = t.bot_id
                    WHERE b.is_active = 1
                """
                df_v = pd.read_sql(query_v, conn_fresh)
                
                if not df_v.empty:
                    for _, row in df_v.iterrows():
                        invested   = float(row['total_invested'] or 0)
                        avg_price  = float(row['avg_entry_price'] or 0)
                        open_qty_v = float(row['open_qty'] or 0)
                        pair_key   = _norm_universal(row['pair'])
                        side_key   = str(row['direction']).upper()  # LONG or SHORT
                        bot_id     = row['id']
                        
                        # Derive raw qty
                        if open_qty_v > 0:
                            qty_abs = open_qty_v
                            ref_price = avg_price if avg_price > 0 else 1.0
                        elif invested > 0 and avg_price > 0:
                            qty_abs = invested / avg_price
                            ref_price = avg_price
                        else:
                            qty_abs = 0
                            ref_price = 1.0

                        if pair_key not in pair_prices:
                            pair_prices[pair_key] = ref_price

                        # 🛡️ HEDGE-AWARE NETTING:
                        # v2.5.9: 'open_qty' is already the net position (Entry - Hedge)
                        effective_qty = qty_abs
                        
                        # 🚀 HEDGE-MODE: Group by (pair, side)
                        composite_key = (pair_key, side_key)
                        virtual_qty_by_pair[composite_key] = virtual_qty_by_pair.get(composite_key, 0.0) + effective_qty

                # 2. Physical Positions (grouped by normalized pair + side)
                if not df_physical.empty:
                    for _, row in df_physical.iterrows():
                        if pd.notna(row['size']) and pd.notna(row['entry_price']):
                            qty = abs(float(row['size']))
                            price = float(row['entry_price'])
                            side = str(row['side']).upper().strip()
                            side_key = 'LONG' if side in ('BUY', 'LONG') else 'SHORT'
                            pair_key = _norm_universal(row['pair'])
                            composite_key = (pair_key, side_key)
                            if pair_key not in pair_prices:
                                pair_prices[pair_key] = price
                            physical_qty_by_pair[composite_key] = physical_qty_by_pair.get(composite_key, 0.0) + qty
                
                # 3. Symbol-Level NET comparison (One-Way Mode Awareness)
                all_symbols = set([_norm_universal(p) for p in df_v['pair']]) | set([_norm_universal(p) for p in df_physical['pair']]) if not df_physical.empty else set([_norm_universal(p) for p in df_v['pair']])
                virtual_net_usd = 0.0
                physical_net_usd = 0.0
                diff_net = 0.0
                
                # Pre-calculate virtual net quantities in memory (O(1) pass)
                for (pk, sk), q in virtual_qty_by_pair.items():
                    virtual_net_by_norm[pk] = virtual_net_by_norm.get(pk, 0.0) + (q if sk == 'LONG' else -q)

                for p in sorted(all_symbols):
                    v_net_qty = virtual_net_by_norm.get(p, 0.0)
                    ph_net_qty = physical_qty_by_pair.get((p, 'LONG'), 0.0) - physical_qty_by_pair.get((p, 'SHORT'), 0.0)
                    
                    # 🛡️ ARCHITECT'S SHIELD: Apply manual whitelists to ignore personal trades
                    whitelists = get_manual_whitelists(p)
                    for w in whitelists:
                        w_qty = float(w['qty'])
                        ph_net_qty -= w_qty if w['side'] == 'LONG' else -w_qty
                    
                    ref_price = pair_prices.get(p, 1.0)
                    net_qty_diff = abs(v_net_qty - ph_net_qty)
                    net_usd_diff = net_qty_diff * ref_price
                    
                    # Global metrics update
                    virtual_net_usd += v_net_qty * ref_price
                    physical_net_usd += ph_net_qty * ref_price

                    # DISCREPANCY DETECTION
                    if net_usd_diff > 0.01:
                        diff_net += net_usd_diff # Correctly accumulate total diff
                        v_usd_net = v_net_qty * ref_price
                        ph_usd_net = ph_net_qty * ref_price
                        signed_qty_diff = ph_net_qty - v_net_qty 
                        mismatched_pairs.append((f"{p} NET", v_usd_net, ph_usd_net, net_usd_diff, v_net_qty, ph_net_qty, signed_qty_diff, ref_price))
                
                # --- 🚀 UI UPGRADE: PROMINENT METRICS (Green/Red Part) 🚀 ---
                m_col1, m_col2, m_col3 = st.columns(3)
                with m_col1:
                    st.metric("Net Exposure (Virtual)", f"${virtual_net_usd:,.2f}")
                with m_col2:
                    st.metric("Exchange Net (Physical)", f"${physical_net_usd:,.2f}", delta=f"{physical_net_usd-virtual_net_usd:,.2f}")
                with m_col3:
                    _status = "HEALTHY" if abs(virtual_net_usd - physical_net_usd) <= 0.01 else "MISMATCH"
                    _color = "green" if _status == "HEALTHY" else "red"
                    st.markdown(f"**System Status:** <span style='color:{_color}; font-weight:bold;'>{_status}</span>", unsafe_allow_html=True)
                st.divider()

                # --- Status Indicator & Order Health ---
                try:
                    order_health_msg = ""
                    order_status_color = "green"
                    
                    # --- STATUS CONSISTENCY FIX ---
                    active_bots = df_pos[df_pos['is_active'] == 1]
                    total_orders = len(market_orders)
                    
                    # --- REALITY SYNC: Per-Bot Order Validation ---
                    try:
                        physical_order_counts = {}
                        for o in market_orders:
                            cid = str(o.get('clientOrderId') or '')
                            if cid.startswith('CQB_'):
                                try:
                                    parts = cid.split('_')
                                    if len(parts) >= 2:
                                        bid_parsed = int(parts[1])
                                        physical_order_counts[bid_parsed] = physical_order_counts.get(bid_parsed, 0) + 1
                                except: pass
                        
                        # Load pos_limit_hit flags for all bots in one query
                        try:
                            from engine.database import get_connection as _gconn
                            _conn = _gconn()
                            _plimit_rows = _conn.execute("SELECT id, pos_limit_hit FROM bots").fetchall()
                            _pos_limit_flags = {row[0]: bool(row[1]) for row in _plimit_rows}
                        except Exception:
                            _pos_limit_flags = {}

                        # Check for in-trade bots missing real physics orders
                        bots_with_missing_orders = []
                        bots_with_partial_orders = []
                        bots_pos_limit = []
                        for _, row in active_bots.iterrows():
                            bid = int(row['id'])
                            c_step = int(row.get('current_step', 0))
                            bot_invested = float(row.get('total_invested', 0) or 0)
                            is_hedged = bid in hedged_bot_ids
                            
                            actual_physical = physical_order_counts.get(bid, 0)
                            
                            # 🛡️ PENDING/RESET GATE: Bots exiting or reset should NOT trigger alerts
                            b_status_upper = str(row.get('status', '')).upper()
                            c_phase = str(row.get('cycle_phase', 'IDLE')).upper()
                            
                            # Self-Healing Status derivation for alert gating
                            is_scanning = (c_phase == 'IDLE' and bot_invested <= 1e-8 and not is_hedged)
                            
                            if "EXITING" in b_status_upper or is_scanning:
                                continue

                            if is_hedged:
                                if actual_physical == 0: bots_with_missing_orders.append(f"{row['name']} (HEDGED)")
                                continue

                            if actual_physical == 0 and bot_invested > 1e-8:
                                if _pos_limit_flags.get(bid, False): bots_pos_limit.append(row['name'])
                                else: bots_with_missing_orders.append(row['name'])
                            
                            elif actual_physical < 2 and c_step > 0 and bot_invested > 0 and not is_hedged:
                                try:
                                    cfg = json.loads(row.get('config', '{}'))
                                    max_steps = int(cfg.get('max_steps', 10))
                                    if c_step < max_steps:
                                        if _pos_limit_flags.get(bid, False): bots_pos_limit.append(row['name'])
                                        else: bots_with_partial_orders.append(f"{row['name']} ({actual_physical}/2)")
                                except: pass

                        expected_total = sum(
                            physical_order_counts.get(int(row['id']), 0) if int(row['id']) in hedged_bot_ids else
                            (min(1, physical_order_counts.get(int(row['id']), 0)) 
                            if int(row.get('current_step', 0)) == 0
                            else (1 if int(row.get('current_step', 0)) >= int(json.loads(row.get('config', '{}')).get('max_steps', 10)) else 2))
                            for _, row in active_bots.iterrows()
                        )
                    except Exception:
                        expected_total = total_orders
                        bots_with_missing_orders = []
                        bots_pos_limit = []
                    
                    if bots_with_missing_orders:
                        order_health_msg = f"⚠️ MISSING CRITICAL ORDERS: {', '.join(bots_with_missing_orders)} have 0 open limit orders!"
                        order_status_color = "red"
                    elif bots_with_partial_orders:
                        error_reasons = []
                        for b_str in bots_with_partial_orders:
                            b_name = b_str.rpartition(' (')[0] if ' (' in b_str else b_str
                            b_row = active_bots[active_bots['name'] == b_name].iloc[0] if not active_bots[active_bots['name'] == b_name].empty else None
                            if b_row is not None and b_row.get('error'): error_reasons.append(f"{b_name}: {b_row['error']}")
                            else: error_reasons.append(b_str)
                        order_health_msg = f"⚠️ MISSING GRIDS (Check ATR/Params): {', '.join(error_reasons)}"
                        order_status_color = "orange"
                    elif bots_pos_limit:
                        order_health_msg = f"🚫 POS LIMIT: {', '.join(bots_pos_limit)} at exchange max notional."
                        order_status_color = "green"
                    elif total_orders < expected_total:
                        order_health_msg = f"⚠️ EXCHANGE LAG: Found {total_orders}, Expected {expected_total} (Syncing...)."
                        order_status_color = "orange"
                    elif total_orders > expected_total:
                        order_health_msg = f"⚠️ STRAY ORDERS: Found {total_orders}, Expected only {expected_total}."
                        order_status_color = "red"
                    else:
                        order_health_msg = f"✅ ORDERS SYNCED: {total_orders} active orders."

                    if diff_net > 0.01:
                        st.markdown(f"""
                        <div style="background-color:rgba(255, 75, 75, 0.1); padding:10px; border-radius:5px; border:1px solid rgba(255, 75, 75, 0.2); margin-bottom:15px;">
                            <span style="color:#ff4b4b; font-weight:bold;">⚠️ SYSTEM MISMATCH:</span> 
                            The exchange is reporting <b>${physical_net_usd:,.2f}</b> net exposure, but the system believes it is <b>${virtual_net_usd:,.2f}</b>.
                        </div>
                        """, unsafe_allow_html=True)
                    
                    st.markdown(f"""
                    <div style="display: flex; justify-content: space-between; align-items: center; background-color: #0e1117; padding: 15px; border-radius: 10px; border: 1px solid #30363d; margin-bottom: 20px;">
                        <div>
                            <div style="font-size: 0.8rem; color: #8b949e; margin-bottom: 4px;">ORDER HEALTH</div>
                            <div style="font-size: 1.1rem; color: {order_status_color}; font-weight: bold;">{order_health_msg}</div>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

                except Exception as e:
                    st.error(f"Status Indicator Error: {e}")

                try:
                    with st.expander("🔍 Global Netting Diagnostics (v2.5.8)", expanded=False):
                        st.write(f"**Reconciliation Mode:** Global Net (Hedge-Aware)")
                        debug_data = []
                        for p_dbg in sorted(all_symbols):
                            v_dbg = virtual_net_by_norm.get(p_dbg, 0.0)
                            ph_l_dbg = physical_qty_by_pair.get((p_dbg, 'LONG'), 0.0)
                            ph_s_dbg = physical_qty_by_pair.get((p_dbg, 'SHORT'), 0.0)
                            ph_net_dbg = ph_l_dbg - ph_s_dbg
                            debug_data.append({
                                "Symbol": p_dbg,
                                "System Net": f"{v_dbg:+.4f}",
                                "Exchange Net": f"{ph_net_dbg:+.4f}",
                                "Diff Qty": f"{abs(v_dbg - ph_net_dbg):.4f}"
                            })
                        st.table(pd.DataFrame(debug_data))
                except Exception:
                    pass

                has_mismatch = len(mismatched_pairs) > 0
                if has_mismatch:
                    st.error("🚨 DATABASE DESYNC: Binance physical exchange positions drastically differ from the bots' internal ledgers.")

                is_startup_grace = False
                if not df_pos.empty:
                    try:
                        newest_start = df_pos['basket_start_time'].max()
                        if (time.time() - newest_start) < 60: is_startup_grace = True
                    except: pass

                if not has_mismatch and order_status_color == "green":
                    st.success(f"✅ **SYSTEM HEALTHY**: Contracts and orders are perfectly aligned. {order_health_msg}")
                elif is_startup_grace and order_status_color == "red":
                    st.warning(f"🟡 **SYSTEM STARTUP**: Waiting for initial sync/orders (Grace Period)...")
                else:
                    st.error(f"🚨 **SYSTEM MISMATCH DETECTED**")
                    if mismatched_pairs:
                        for row_mp in mismatched_pairs:
                            mp_pair, mp_virt, mp_phys, mp_diff, mp_vqty, mp_pqty, mp_dqty, mp_price = row_mp
                            qty_str = f" | Qty: system={mp_vqty:+.4f} exchange={mp_pqty:+.4f} diff={mp_dqty:.4f}"
                            _pair_root = mp_pair.split(' ')[0]
                            _pair_pos_capped = any(_pos_limit_flags.get(int(row['id']), False) for _, row in active_bots.iterrows() if str(row.get('pair', '')).startswith(_pair_root.split('/')[0]))
                            if _pair_pos_capped:
                                st.info(f"   🚫 **{mp_pair}**: System ${mp_virt:,.2f} vs Exchange ${mp_phys:,.2f} (Diff: ${mp_diff:,.2f}){qty_str} — *POS LIMIT*")
                            elif mp_diff > 0.01:
                                st.warning(f"   ⚠️ **{mp_pair}**: System ${mp_virt:,.2f} vs Exchange ${mp_phys:,.2f} (Diff: ${mp_diff:,.2f}){qty_str}")
                            
                            if abs(mp_dqty or 0) > 0.0001:
                                _act_col1, _act_col2, _act_col3, _act_col4 = st.columns([1,1,1,1])
                                with _act_col1:
                                    if st.button("🕵️ Forensic Adopt", key=f"forensic_{mp_pair}"):
                                        from engine.reconciler import StateReconciler
                                        sr = StateReconciler()
                                        res = sr.perform_forensic_reconstruction(_pair_root)
                                        if sum(res.values()) > 0: st.success("Forensic Success!"); time.sleep(1); st.rerun()
                                        else: st.warning("No missing proof-based fills found.")
                                with _act_col2:
                                    _side = 'LONG' if mp_dqty > 0 else 'SHORT'
                                    if st.button("📝 Mark as Manual", key=f"manual_{mp_pair}"):
                                        from ui.views.monitor import add_manual_whitelist
                                        add_manual_whitelist(_pair_root, _side, abs(mp_dqty)); st.rerun()
                                with _act_col3:
                                    if st.button("💥 Market Close", key=f"mkt_close_{mp_pair}"):
                                        ex_mkt = get_exchange_instance('future')
                                        if abs(mp_pqty) > 0.0001:
                                            ex_mkt.create_order(_pair_root, 'market', 'sell' if mp_pqty > 0 else 'buy', abs(mp_pqty), params={'reduceOnly': True})
                                        from engine.database import get_connection as _gconn_mkt
                                        _conn_mkt = _gconn_mkt()
                                        _involved_bots = [r[0] for r in _conn_mkt.execute("SELECT id FROM bots WHERE pair LIKE ?", (f"{_pair_root}%",)).fetchall()]
                                        for _bid in _involved_bots:
                                            _conn_mkt.execute("UPDATE bot_orders SET status='reset_cleared' WHERE bot_id=?", (_bid,))
                                            _conn_mkt.execute("UPDATE trades SET total_invested=0, current_step=0 WHERE bot_id=?", (_bid,))
                                        _conn_mkt.commit(); st.rerun()

                try:
                    _orphan_rows = sqlite3.connect(db_path).execute("SELECT pair, side, size, entry_price FROM active_positions WHERE bot_id=0").fetchall()
                except: _orphan_rows = []
                if _orphan_rows:
                    st.divider(); st.markdown("### 🚨 Unowned Physical Positions (Orphans)")
                    for _or in _orphan_rows:
                        _o_pair, _o_side, _o_size, _o_entry = _or
                        if st.button(f"🛑 Flatten {_o_pair} {_o_side} ({_o_size})"):
                            try:
                                get_exchange_instance('future').create_order(_o_pair, 'market', 'sell' if _o_side.upper() == 'LONG' else 'buy', _o_size, params={'reduceOnly': True})
                                st.success(f"Position closed: {_o_pair}")
                                time.sleep(1); st.rerun()
                            except Exception as e_fl:
                                if "reduceonly" in str(e_fl).lower():
                                    st.error("ReduceOnly Rejected: Binance thinks you have no position. Force closing without ReduceOnly...")
                                    get_exchange_instance('future').create_order(_o_pair, 'market', 'sell' if _o_side.upper() == 'LONG' else 'buy', _o_size)
                                    st.rerun()
                                else:
                                    st.error(f"Flatten Error: {e_fl}")

                if has_mismatch:
                    st.divider(); st.markdown("### 🧙‍♂️ Manual Link Recovery Tool")
                    try:
                        _rc = sqlite3.connect(db_path)
                        # Check if table exists first
                        _check = _rc.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reconciliation_log'").fetchone()
                        if not _check:
                            _rc.execute("CREATE TABLE reconciliation_log (id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, pair TEXT, action TEXT, details TEXT, created_at INTEGER)")
                            _rc.commit()
                        
                        _recon_rows = _rc.execute("SELECT pair, details FROM reconciliation_log WHERE action IN ('UNAUTHORIZED_LOSS', 'MANUAL_INTERVENTION') ORDER BY created_at DESC LIMIT 10").fetchall()
                        _rc.close()
                    except Exception as e_db:
                        st.warning(f"Database sync in progress: {e_db}")
                        _recon_rows = []
                    
                    active_bot_ids = [str(b[0]) for (b) in df_pos[['id']].values]
                    stray_orders = [o for o in market_orders if str(o.get('clientOrderId','')).startswith('CQB_') and o.get('clientOrderId','').split('_')[1] not in active_bot_ids]

                    if stray_orders:
                        st.info(f"Found {len(stray_orders)} stray orders.")
        except Exception as e_global:
            st.error(f"Global Monitor Error: {e_global}")

        st.divider()

        # -------------------------------------------------------------------------
        # ⚡ FRAGMENT: Exchange Reality + Bot Strategies
        # Decorated with @st.fragment so this section refreshes independently
        # without triggering a full-page rerun. The sidebar, charts tab, and
        # history tab stay completely static while this block auto-updates.
        # -------------------------------------------------------------------------
        @st.fragment(run_every=15 if auto_refresh and not wizard_active else None)
        def _bot_positions_fragment(df_physical, df_pos, virtual_gross_usd):
            # --- Physical Positions (Exchange Reality) ---
            st.subheader("🏥 Exchange Reality (Physical)")
            if not df_physical.empty:
                st.dataframe(df_physical, width="stretch")
            else:
                st.info("Exchange wallet is empty (No physical positions).")
                if virtual_gross_usd > 100:
                    st.caption("ℹ️ Note: If active bots exist, this means Longs and Shorts are perfectly hedged (Net ~0).")

            st.divider()

            # --- Virtual Positions (Bot Strategies) ---
            st.subheader("🤖 Bot Strategies (Virtual Positions)")
            if not df_pos.empty:
                # UX Improvements: Rename Status to friendly labels
                _status_map = {
                    'Scanning': '🟢 SCANNING',
                    'Waiting for Signal': '🟢 SCANNING',
                    'IN TRADE': '🔴 IN TRADE',
                    'ENTRY PENDING': '🟡 WAITING FOR FILL',
                    'Stopped': '⚪ STOPPED',
                    'STOPPED': '⚪ STOPPED',
                }
                df_pos['status'] = df_pos['status'].replace(_status_map)

                # Extract Trigger Info & Active Orders
                def extract_info(row):
                    res = {
                        'Trigger': 'N/A',
                        'Orders': '0',
                        'TP_Price': 0.0,
                        'Grid_Price': 0.0,
                        'Grid_Amount': 0.0
                    }
                    try:
                        cfg = json.loads(row.get('config', '{}') or '{}')
                        triggers = []

                        # 1. Price Trigger
                        m_p = int(cfg.get('mode_price', 0) or 0)
                        try:
                            t_p = float(cfg.get('price_threshold', 0) or 0)
                        except ValueError:
                            t_p = 0.0
                        if m_p == 1: triggers.append(f"Price > ${t_p:,.2f}")
                        elif m_p == 2: triggers.append(f"Price < ${t_p:,.2f}")

                        # 2. Indicator Triggers
                        if cfg.get('mode_rsi'):
                            r_m = int(cfg['mode_rsi'] or 0)
                            try: r_l = float(cfg.get('rsi_level', 0) or 0)
                            except ValueError: r_l = 0.0
                            triggers.append(f"RSI({'<' if r_m==1 else '>'}{r_l})")
                        if cfg.get('mode_cci'):
                            c_m = int(cfg['mode_cci'] or 0)
                            try: c_l = float(cfg.get('cci_level', 0) or 0)
                            except ValueError: c_l = 0.0
                            triggers.append(f"CCI({'<' if c_m==2 else '>'}{c_l})")
                        if cfg.get('mode_boll'):
                            triggers.append("BOLL(Outside)")
                        if cfg.get('mode_stoch'):
                            s_m = int(cfg['mode_stoch'] or 0)
                            triggers.append(f"Stoch({'Oversold' if s_m==1 else 'Overbought'})")

                        # 3. Patterns
                        for i in range(1, 5):
                            if cfg.get(f'pat_{i}_mode'):
                                p_m = int(cfg[f'pat_{i}_mode'] or 0)
                                p_c = int(cfg.get(f'pat_{i}_count', 1) or 1)
                                p_s = cfg.get(f'pat_{i}_source', 'Price')
                                triggers.append(f"{p_s}Pat({p_c}x {'Up' if p_m==1 else 'Dn'})")

                        desc_trigger = " + ".join(triggers) if triggers else "N/A"

                        ee_status = ""
                        is_in_trade = False
                        try:
                            inv = float(row.get('total_invested', 0) or 0)
                            if inv > 0: is_in_trade = True
                        except: pass

                        if is_in_trade and cfg.get('UseEarlyExit', False) and row.get('basket_start_time', 0) > 0:
                            import time as _t
                            from datetime import datetime as _dt
                            try:
                                from engine.manager import calculate_early_exit_decay as _eed
                                avg_p = float(row.get('avg_entry_price', 0) or 0)
                                tp_p  = float(row.get('target_tp_price', 0) or 0)
                                if avg_p > 0 and tp_p > 0:
                                    start_dt = _dt.fromtimestamp(row.get('basket_start_time', 0))
                                    now_dt   = _dt.fromtimestamp(_t.time())
                                    step_n   = int(row.get('current_step', 0)) + 1
                                    decayed_tp = _eed(start_dt, now_dt, step_n, tp_p, avg_p, cfg)
                                    orig_spread = abs(tp_p - avg_p)
                                    decayed_spread = abs(decayed_tp - avg_p)
                                    if orig_spread > 0:
                                        ee_pc = (1 - decayed_spread / orig_spread) * 100
                                        ee_pc = max(0.0, min(ee_pc, 100.0) if not cfg.get('EEAllowLoss', False) else ee_pc)
                                        ee_status = f" [EE: -{ee_pc:.1f}% → TP {decayed_tp:,.4f}]"
                            except Exception:
                                duration_mins = (_t.time() - row.get('basket_start_time', _t.time())) / 60.0
                                grace_mins = float(cfg.get('EEGracePeriodMins', 0.0))
                                adjusted_mins = max(0.0, duration_mins - grace_mins)
                                
                                interval_mins = float(cfg.get('DecayIntervalMins', 60.0))
                                decay_per_interval = float(cfg.get('DecayPercentPerInterval', 0.0))
                                
                                if decay_per_interval > 0 and interval_mins > 0 and adjusted_mins > 0:
                                    import math as _m
                                    intervals_passed = _m.floor(adjusted_mins / interval_mins)
                                    ee_pc = intervals_passed * decay_per_interval
                                    if not cfg.get('EEAllowLoss', False):
                                        ee_pc = min(ee_pc, 100.0)
                                    if ee_pc > 0:
                                        ee_status = f" [EE: -{ee_pc:.1f}%]"

                        hedge_status = ""
                        if is_in_trade and cfg.get('UseStepHedge', False) and int(row.get('current_step', 0)) >= int(cfg.get('HedgeStartStep', 99)):
                            hedge_status = f" 🛡️ [HEDGED @ Step {cfg.get('HedgeStartStep')}]"

                        if is_in_trade:
                            res['Trigger'] = f"In Trade{ee_status}{hedge_status} ({desc_trigger})"
                        else:
                            res['Trigger'] = desc_trigger

                        # 2. Active Orders
                        try:
                            bot_id = int(row['id'])
                        except (ValueError, TypeError):
                            bot_id = row['id']

                        my_orders = [o for o in market_orders if str(o.get('clientOrderId') or '').startswith(f"CQB_{bot_id}_")]

                        if my_orders:
                            detailed = []
                            is_hedged = False
                            for o in my_orders:
                                cid = o.get('clientOrderId', '')
                                price_val = float(o.get('price', 0.0) or 0.0)
                                if 'TP' in cid:
                                    detailed.append('TP')
                                    if 'HEDGETP' in cid:
                                        res['Hedge_TP_Price'] = price_val
                                        res['Hedge_TP_Qty'] = float(o.get('origQty', o.get('amount', 0.0)) or 0.0)
                                        res['Hedge_TP_Side'] = o.get('side', '').upper()
                                    else:
                                        res['TP_Price'] = price_val
                                elif 'GRID' in cid:
                                    detailed.append('GRID')
                                    res['Grid_Price'] = price_val
                                    res['Grid_Amount'] = float(o.get('origQty', o.get('amount', 0.0)) or 0.0)
                                elif 'HEDGE' in cid:
                                    q = float(o.get('origQty', o.get('amount', 0.0)) or 0.0)
                                    side = o.get('side', '').upper()
                                    detailed.append(f"HEDGE: {side} {q} @ ${price_val:,.2f}")
                                    is_hedged = True
                                elif 'ENTRY' in cid: detailed.append('ENTRY')
                                else: detailed.append('LIMIT')
                            count_str = f"{len(my_orders)} " + (f"({', '.join(detailed)})" if detailed else "")
                            res['Orders'] = count_str
                            if is_hedged:
                                hedge_info = [d for d in detailed if 'HEDGE' in d]
                                if hedge_info:
                                    res['Trigger'] = f"🛡️ HEDGED ({', '.join(hedge_info)}) | {res['Trigger']}"
                                else:
                                    res['Trigger'] = f"🛡️ HEDGED | {res['Trigger']}"
                        else:
                            res['Orders'] = "0"

                    except Exception as e:
                        res['Trigger'] = f"⚠️ ERR: {type(e).__name__} - {str(e)}"
                        print(f"UI Extract Error: {e}")
                    return res

                info_df = df_pos.apply(extract_info, axis=1, result_type='expand')
                df_pos['Trigger Condition'] = info_df['Trigger']
                df_pos['Active Orders'] = info_df['Orders']

                def format_tp_price(x):
                    x_val = float(x) if x is not None else 0.0
                    if x_val <= 0: return "-"
                    if x_val < 1.0:  return f"${x_val:.4f}"
                    if x_val < 10.0: return f"${x_val:.3f}"
                    return f"${x_val:,.2f}"

                def derive_tp_display(row_info):
                    h_price = row_info.get('Hedge_TP_Price', 0.0)
                    if isinstance(h_price, (int, float)) and h_price > 0:
                        side = row_info.get('Hedge_TP_Side', '')
                        qty = row_info.get('Hedge_TP_Qty', 0.0)
                        return f"🛡️ {side} {qty} @ {format_tp_price(h_price)}"
                    return format_tp_price(row_info.get('TP_Price'))

                df_pos['Active TP'] = info_df.apply(derive_tp_display, axis=1)
                df_pos['Next Grid'] = info_df.apply(
                    lambda row: f"{row['Grid_Amount']} @ {format_tp_price(row['Grid_Price'])}" if (float(row.get('Grid_Price') or 0.0)) > 0 else "-",
                    axis=1
                )

                # --- PERFORMANCE MATRIX (Enterprise Batch View) ---
                st.markdown("### ⚡ Batch Performance Matrix")
                try:
                    matrix_df = df_pos.copy()

                    def est_profit(row):
                        inv = float(row.get('total_invested', 0) or 0)
                        avg = float(row.get('avg_entry_price', 0) or 0)
                        target = float(row.get('target_tp_price', 0) or 0)
                        
                        if inv > 0 and avg > 0 and target > 0:
                            ee_full_decay = False
                            if 'Trigger Condition' in row and isinstance(row['Trigger Condition'], str):
                                if '-100.0%]' in row['Trigger Condition'] or '-100%]' in row['Trigger Condition']:
                                    ee_full_decay = True
                            if ee_full_decay:
                                return "$0.00 (Break-Even)"
                            qty = inv / avg
                            if row.get('direction', 'LONG') == 'LONG':
                                profit = (target - avg) * qty
                            else:
                                profit = (avg - target) * qty
                            try:
                                roi_pct = (profit / inv) * 100
                                try:
                                    cfg_raw = json.loads(row.get('config', '{}') or '{}')
                                    lev = float(cfg_raw.get('leverage', 1) or 1)
                                except Exception:
                                    lev = 1.0
                                roe_pct = roi_pct * lev
                                sign = "+" if profit >= 0 else ""
                                warn = " ⚠️ (Inverted TP)" if profit < 0 else ""
                                if lev > 1:
                                    return f"${profit:,.2f} ({sign}{roi_pct:.2f}% | ROE {sign}{roe_pct:.1f}%){warn}"
                                else:
                                    return f"${profit:,.2f} ({sign}{roi_pct:.2f}%){warn}"
                            except Exception:
                                return f"${profit:,.2f}"
                        return "-"

                    matrix_df['Expected Profit'] = matrix_df.apply(est_profit, axis=1)

                    current_time = time.time()
                    def time_in_trade(row):
                        inv = float(row.get('total_invested', 0) or 0)
                        # Absolute Age anchor: cycle_start_time
                        # EE Timer anchor: basket_start_time
                        cst = float(row.get('cycle_start_time', 0) or 0)
                        bst = float(row.get('basket_start_time', 0) or 0)
                        
                        # Display should show the ABSOLUTE age of the trade
                        # Fallback to BST if CST is not yet populated
                        display_start = cst if cst > 0 else bst
                        
                        if inv > 0 and display_start > 0:
                            sec = current_time - display_start
                            m, s = divmod(sec, 60)
                            h, m = divmod(m, 60)
                            return f"{int(h)}h {int(m)}m"
                        return "-"

                    matrix_df['Time in Trade'] = matrix_df.apply(time_in_trade, axis=1)

                    cols_matrix = ['name', 'pair', 'direction', 'current_step', 'total_invested', 'Active TP', 'Next Grid', 'Expected Profit', 'Time in Trade', 'status']
                    matrix_df = matrix_df[[c for c in cols_matrix if c in matrix_df.columns]]
                    matrix_df['total_invested'] = matrix_df['total_invested'].apply(lambda x: f"${float(x):,.2f}" if x and float(x) > 0 else "-")
                    st.dataframe(matrix_df, width="stretch")
                except Exception as e:
                    st.warning(f"Failed to render Batch Matrix: {e}")

                st.divider()

                # Entry Trigger Proximity
                scanning_bots = df_pos[df_pos['status'].str.contains('SCANNING', na=False)]
                if not scanning_bots.empty:
                    st.markdown("#### 🎯 Entry Trigger Proximity (Scanning Bots)")
                    st.caption("Shows how close each scanning bot is to its entry signal thresholds.")
                    for _, sbot in scanning_bots.iterrows():
                        try:
                            conn_trig = get_connection()
                            _row = conn_trig.execute("SELECT config FROM bots WHERE id=?", (sbot['id'],)).fetchone()
                            conn_trig.close()
                            conf = json.loads(_row[0]) if _row else {}
                            pair_s = sbot['pair']
                            mkt_type = conf.get('market_type', 'future')
                            _ohlcv = fetch_ohlcv_cached(mkt_type, pair_s, '15m')
                            if not _ohlcv or len(_ohlcv) < 20:
                                continue
                            _df = pd.DataFrame(_ohlcv, columns=['ts','open','high','low','close','vol'])
                            _close = _df['close']
                            indicators = []

                            mode_rsi = int(conf.get('mode_rsi', 0))
                            if mode_rsi > 0:
                                rsi_lvl = float(conf.get('rsi_level', 30))
                                from engine.indicators import rsi as calc_rsi
                                live_rsi = float(calc_rsi(_close, 14).iloc[-1])
                                pct_away = abs(live_rsi - rsi_lvl) / max(rsi_lvl, 1) * 100
                                cond = "Below" if mode_rsi == 1 else "Above"
                                triggered = (live_rsi <= rsi_lvl) if mode_rsi == 1 else (live_rsi >= rsi_lvl)
                                badge = "✅ Triggered" if triggered else ("🔥 Near" if pct_away <= 10 else ("👀 In Sight" if pct_away <= 30 else "🌑 Not Ready"))
                                indicators.append(f"RSI: {live_rsi:.1f} (target {cond} {rsi_lvl:.0f}) — {badge}")

                            mode_cci = int(conf.get('mode_cci', 0))
                            if mode_cci > 0:
                                cci_lvl = float(conf.get('cci_level', 100))
                                from engine.indicators import cci as calc_cci
                                live_cci = float(calc_cci(_df['high'], _df['low'], _close, 14).iloc[-1])
                                pct_away = abs(live_cci - cci_lvl) / max(abs(cci_lvl), 1) * 100
                                cond = "Above" if mode_cci == 1 else "Below"
                                triggered = (live_cci >= cci_lvl) if mode_cci == 1 else (live_cci <= cci_lvl)
                                badge = "✅ Triggered" if triggered else ("🔥 Near" if pct_away <= 10 else ("👀 In Sight" if pct_away <= 30 else "🌑 Not Ready"))
                                indicators.append(f"CCI: {live_cci:.1f} (target {cond} {cci_lvl:.0f}) — {badge}")

                            mode_stoch = int(conf.get('mode_stoch', 0))
                            if mode_stoch > 0:
                                from engine.indicators import stochastic
                                k, _ = stochastic(_df['high'], _df['low'], _close)
                                live_k = float(k.iloc[-1])
                                cond = "Oversold (<20)" if mode_stoch == 1 else "Overbought (>80)"
                                triggered = (live_k < 20) if mode_stoch == 1 else (live_k > 80)
                                pct_away = (live_k - 20) / 20 * 100 if mode_stoch == 1 else (80 - live_k) / 20 * 100
                                badge = "✅ Triggered" if triggered else ("🔥 Near" if pct_away <= 20 else ("👀 In Sight" if pct_away <= 60 else "🌑 Not Ready"))
                                indicators.append(f"Stoch %K: {live_k:.1f} (target {cond}) — {badge}")

                            mode_price = int(conf.get('mode_price', 0))
                            if mode_price > 0:
                                p_thresh = float(conf.get('price_threshold', 0))
                                curr_p = float(_close.iloc[-1])
                                if p_thresh > 0:
                                    cond = "Above" if mode_price == 1 else "Below"
                                    triggered = (curr_p >= p_thresh) if mode_price == 1 else (curr_p <= p_thresh)
                                    pct_away = abs(curr_p - p_thresh) / p_thresh * 100
                                    badge = "✅ Triggered" if triggered else ("🔥 Near" if pct_away <= 2 else ("👀 In Sight" if pct_away <= 10 else "🌑 Not Ready"))
                                    indicators.append(f"Price: {curr_p:.4f} (target {cond} {p_thresh:.4f}) — {badge}")

                            mode_boll = int(conf.get('mode_boll', 0))
                            if mode_boll > 0:
                                from engine.indicators import bollinger_bands
                                boll_period = int(conf.get('boll_period', conf.get('bollinger_length', 20)))
                                boll_dev = float(conf.get('boll_dev', conf.get('bollinger_std', 2.0)))
                                upper, middle, lower = bollinger_bands(_close, boll_period, boll_dev)
                                curr_p = float(_close.iloc[-1])
                                b_up = float(upper.iloc[-1])
                                b_dn = float(lower.iloc[-1])
                                if mode_boll == 1:
                                    triggered = curr_p < b_dn
                                    dist = abs(curr_p - b_dn) / curr_p * 100
                                    badge = "✅ Triggered" if triggered else ("🔥 Near" if dist <= 1 else ("👀 In Sight" if dist <= 3 else "🌑 Not Ready"))
                                    indicators.append(f"BOLL: Outside Lower (Dist: {dist:.2f}%) — {badge}")
                                elif mode_boll == 2:
                                    triggered = curr_p > b_up
                                    dist = abs(curr_p - b_up) / curr_p * 100
                                    badge = "✅ Triggered" if triggered else ("🔥 Near" if dist <= 1 else ("👀 In Sight" if dist <= 3 else "🌑 Not Ready"))
                                    indicators.append(f"BOLL: Outside Upper (Dist: {dist:.2f}%) — {badge}")

                            if int(conf.get('mode_atrp', 0)) > 0:
                                indicators.append(f"ATR % Active (Target > {conf.get('atrp_level', 0)}%)")
                            if int(conf.get('mode_atre', 0)) > 0:
                                indicators.append(f"ATR Exp Active (Mult {conf.get('atre_mult', 0)})")

                            patterns = [f"Pat {i}" for i in range(1, 5) if int(conf.get(f'pat_{i}_mode', 0)) > 0]
                            if patterns:
                                indicators.append(f"Patterns: {', '.join(patterns)} Active")

                            with st.expander(f"📡 {sbot['name']} ({pair_s}) — {'No active triggers configured' if not indicators else f'{len(indicators)} trigger(s)'}", expanded=False):
                                if indicators:
                                    for ind in indicators:
                                        st.write(f"  • {ind}")
                                else:
                                    st.caption("No entry triggers (RSI/CCI/Stoch/Price) are enabled for this bot.")
                        except Exception as e_trig:
                            st.caption(f"  ⚠️ Could not load triggers for {sbot.get('name','?')}: {e_trig}")

                st.markdown("### ⚙️ Detailed Bot State (Debug View)")
                cols = ['name', 'pair', 'direction', 'status', 'Active Orders', 'Trigger Condition', 'current_step', 'total_invested', 'avg_entry_price']
                existing_cols = [c for c in cols if c in df_pos.columns]
                st.dataframe(df_pos[existing_cols], width="stretch")
            else:
                st.info("No active bots.")

        # Invoke the fragment (passes current data snapshot into the isolated block)
        _bot_positions_fragment(df_physical, df_pos, virtual_gross_usd)

    with tab_charts:
        st.subheader(f"📈 Live Market Chart: {symbol} ({timeframe})")
        if ohlcv_data:
            try:
                df_ohlcv = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df_ohlcv['timestamp'] = pd.to_datetime(df_ohlcv['timestamp'], unit='ms')
                
                fig = go.Figure(data=[go.Candlestick(x=df_ohlcv['timestamp'],
                                open=df_ohlcv['open'],
                                high=df_ohlcv['high'],
                                low=df_ohlcv['low'],
                                close=df_ohlcv['close'],
                                name='Price')])
                
                # 🚀 UPGRADE 2: Visual Intelligence Overlays
                if selected_bot_id:
                    # Fetch current bot status for lines
                    cur_bot = df_pos[df_pos['id'] == selected_bot_id]
                    if not cur_bot.empty:
                        be = float(cur_bot.iloc[0]['avg_entry_price'] or 0)
                        tp = float(cur_bot.iloc[0]['target_tp_price'] or 0)
                        
                        # 1. Average Entry (Yellow Solid)
                        if be > 0:
                            fig.add_hline(y=be, line_dash="solid", line_color="#FFD700", 
                                          annotation_text=f"ENTRY: {be:,.4f}", 
                                          annotation_position="top left")
                        
                        # 2. Take Profit (Green Solid)
                        if tp > 0:
                            fig.add_hline(y=tp, line_dash="solid", line_color="#00FF00", 
                                          annotation_text=f"TP: {tp:,.4f}", 
                                          annotation_position="bottom right")

                    # 3. Active Grid/Safety Orders (Orange Dashed)
                    # Filter exchange orders for THIS bot specifically
                    prefix = f"CQB_{selected_bot_id}_"
                    bot_orders = [o for o in market_orders if str(o.get('clientOrderId', '')).startswith(prefix)]
                    
                    grid_orders = [o for o in bot_orders if 'GRID' in str(o.get('clientOrderId', ''))]
                    # Sort by proximity to current price
                    last_price = float(df_ohlcv['close'].iloc[-1])
                    grid_orders.sort(key=lambda x: abs(float(x.get('price', 0)) - last_price))
                    
                    # Limit to next 10 for readability as requested
                    for i, order in enumerate(grid_orders[:10]):
                        g_price = float(order.get('price', 0))
                        if g_price > 0:
                            fig.add_hline(y=g_price, line_dash="dash", line_color="#FFA500", 
                                          annotation_text=f"GRID {i+1}", 
                                          annotation_position="bottom left",
                                          opacity=0.6)

                fig.update_layout(
                    height=500,
                    margin=dict(l=10, r=10, t=30, b=10),
                    template="plotly_white",
                    xaxis_rangeslider_visible=False,
                    paper_bgcolor='rgba(0,0,0,0)',
                    plot_bgcolor='rgba(0,0,0,0)'
                )
                st.plotly_chart(fig)
                
            except Exception as e:
                st.error(f"Error rendering chart: {e}")
        else:
            st.warning(f"No market data available for {symbol} on {timeframe}.")

    with tab_history:
        # --- Open Orders Section (Live from Exchange) ---
        st.subheader("📋 Live Open Orders (from Exchange)")
        try:
             # Use the ALREADY FETCHED orders from parallel execution
             if market_orders:
                df_orders = pd.DataFrame(market_orders)
                
                # --- ENRICHMENT from DB ---
                try:
                    conn = get_connection()
                    # Fetch notes for open orders to explain "Why"
                    db_orders = pd.read_sql("SELECT client_order_id, notes FROM bot_orders WHERE status='open'", conn)
                    conn.close()
                    
                    if not db_orders.empty and 'clientOrderId' in df_orders.columns:
                        # Merge on Client Order ID
                        df_orders = df_orders.merge(db_orders, left_on='clientOrderId', right_on='client_order_id', how='left')
                        df_orders.rename(columns={'notes': 'Strategy/Logic'}, inplace=True)
                    else:
                        df_orders['Strategy/Logic'] = "N/A"
                except Exception as e:
                    pass # Fail silently on enrichment

                # Keep only relevant columns
                cols_to_keep = ['symbol', 'side', 'type', 'price', 'amount', 'Strategy/Logic', 'clientOrderId']
                df_orders = df_orders[[c for c in cols_to_keep if c in df_orders.columns]]
                
                # Formatter for price
                if 'price' in df_orders.columns:
                     df_orders['price'] = df_orders['price'].apply(lambda x: f"${x:,.2f}" if isinstance(x, (float, int)) else x)
                     
                st.dataframe(
                    df_orders, 
                    width="stretch",
                    column_config={
                        "Strategy/Logic": st.column_config.TextColumn("Strategy/Logic", width="medium")
                    }
                )
             else:
                st.info("No open orders found on the exchange for active bot pairs.")
                
        except Exception as e:
            st.error(f"Could not load open orders: {e}")

        st.divider()

        # --- Recent Trade History (Added) ---
        st.subheader("📜 Recent Activity Log")
        try:
            conn = get_connection()
            # Fetch last 200 actions for deep scrolling
            query_history = """
                SELECT 
                    datetime(timestamp, 'unixepoch', 'localtime') as Time,
                    action as Action,
                    symbol as Symbol,
                    price as Price,
                    amount as Amount,
                    pnl as 'Realized PnL',
                    notes as Details
                FROM trade_history 
                ORDER BY timestamp DESC 
                LIMIT 200
            """
            df_hist = pd.read_sql_query(query_history, conn)
            conn.close()
            
            if not df_hist.empty:
                # Format Price and PnL
                df_hist['Price'] = df_hist['Price'].apply(lambda x: f"${x:,.2f}" if isinstance(x, (int, float)) else x)
                df_hist['Realized PnL'] = df_hist['Realized PnL'].apply(lambda x: f"${x:,.2f}" if isinstance(x, (int, float)) and x != 0 else "-")
                
                st.dataframe(
                    df_hist, 
                    width="stretch", 
                    hide_index=True,
                    column_config={
                        "Details": st.column_config.TextColumn("Details", width="large", help="Detailed logic/reasoning for this action")
                    }
                )
            else:
                st.caption("No trade history available yet.")
                
        except Exception as e:
            st.error(f"Error loading trade history: {e}")

    # --- Auto-Refresh ---
    # The header and bot grid now refresh via native @st.fragment decorators.
    if auto_refresh and not wizard_active:
        st.caption(f"⏱️ Header/Grid auto-refreshing via native fragments. Last page load: {time.strftime('%H:%M:%S')}")
    elif wizard_active:
        st.warning(
            "⏸ **Auto-Refresh Paused** — Reconciler wizard is active. "
            "Refreshing now would wipe your in-progress recovery work."
        )
    else:
        st.caption("ℹ️ Tip: Auto-Refresh is OFF. Toggle it above for real-time updates.")
