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

    # --- PERFORMANCE: SHARED DATA LOADER ---
    def _fetch_fresh_monitor_data():
        """Fetches the latest bot and physical state from DB + Exchange for fragments."""
        m_orders = []
        exchange_error = None
        try:
            db_path = global_config.PATHS['DB_FILE']
            with sqlite3.connect(db_path, timeout=10) as conn_fresh:
                # 1. Fetch Bot Strategies (Explicit Aliases for mapping safety)
                query_all = """
                    SELECT b.id AS id, b.name AS name, b.pair AS pair, b.direction AS direction, 
                           b.strategy_type AS strategy_type, b.config AS config, t.current_step AS current_step, 
                           t.total_invested AS total_invested, t.avg_entry_price AS avg_entry_price, 
                           t.target_tp_price AS target_tp_price, b.is_active AS is_active, b.status AS status, 
                           b.error AS error, t.basket_start_time AS basket_start_time, 
                           t.cycle_start_time AS cycle_start_time, t.cycle_phase AS cycle_phase, 
                           t.open_qty AS open_qty
                    FROM bots b
                    LEFT JOIN trades t ON b.id = t.bot_id
                    WHERE b.is_active = 1
                """
                df_p = pd.read_sql(query_all, conn_fresh)
                
                # 2. Fetch Physical Positions
                try:
                    df_ph = pd.read_sql("SELECT pair, side, size, entry_price, last_checked FROM active_positions", conn_fresh)
                except:
                    df_ph = pd.DataFrame()
                
                # 3. Fetch Market Orders (Live from Exchange)
                try:
                    ex = get_exchange_instance(global_config.MARKET_TYPE)
                    m_orders = ex.fetch_open_orders(None)
                except Exception as e:
                    exchange_error = f"Exchange Order Sync Error: {e}"
                
                # 4. Fetch Hedge Orders
                # ── ARCH NOTE ──────────────────────────────────────────────────────────
                # Hedge orders are physical exchange SHORT positions. They are NOT
                # bounded by wipe_wall_ts (which is a virtual accounting fence for
                # ENTRY fills only). A hedge placed in cycle 5 is still open on the
                # exchange in cycle 37 unless explicitly closed (reset_cleared / auto_closed).
                # Removing the wipe_wall_ts filter here ensures:
                #   1. hedged_bot_ids correctly includes bots with pre-reset hedges
                #   2. hedge_amounts correctly sums all outstanding hedge exposure
                #   3. MISSING CRITICAL ORDERS alert is not falsely fired for HEDGED bots
                # ───────────────────────────────────────────────────────────────────────
                query_h = """
                    SELECT bo.bot_id, bo.order_type, bo.filled_amount, bo.status, bo.created_at
                    FROM bot_orders bo
                    WHERE bo.order_type IN ('hedge', 'hedge_tp', 'hedgetp')
                      AND bo.status NOT IN ('canceled', 'cancelled', 'rejected', 'failed',
                                            'reset_cleared', 'auto_closed', 'placing')
                      AND bo.filled_amount > 0
                """
                df_h = pd.read_sql(query_h, conn_fresh)
                
                return df_p, df_ph, m_orders, df_h, exchange_error
        except Exception as e:
            print(f"Fragment Data Fetch Error: {e}")
            return pd.DataFrame(), pd.DataFrame(), [], pd.DataFrame(), str(e)

    # --- Fragment: Header Metrics (Command Center) ---
    @st.fragment(run_every=30 if auto_refresh and not wizard_active else None)
    def _header_metrics_fragment():
        # Display Sync Status within fragment
        st.caption(f"  ⚡ Header Sync: {time.strftime('%H:%M:%S')}")
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

            cur.execute(
                "SELECT COUNT(*) FROM trades t JOIN bots b ON b.id=t.bot_id "
                "WHERE b.is_active=1 AND t.total_invested > 0.01"
            )
            bots_in_trade = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT COALESCE(SUM(t.open_qty * t.avg_entry_price), 0) "
                "FROM trades t JOIN bots b ON b.id=t.bot_id "
                "WHERE b.is_active=1 AND t.open_qty > 1e-8 AND t.avg_entry_price > 0"
            )
            open_qty_notional = float(cur.fetchone()[0] or 0.0)
            scanning_count = max(0, int(active_count) - bots_in_trade)

            # Display Metrics Grid
            m1, m2, m3, m4 = st.columns(4)
            with m1: st.metric("Total Equity", f"${total_equity:,.2f}")
            with m2: st.metric("Futures Balance", f"${futures_balance:,.2f}")
            with m3: st.metric("Active PnL", f"${global_pnl_usd:,.2f}")
            with m4: st.metric("Total Invested", f"${total_invested_db:,.2f}",
                              help="Sum of trades.total_invested across active bots (ledger exposure).")

            r2a, r2b, r2c, r2d = st.columns(4)
            with r2a: st.metric("Active Bots", f"{active_count}")
            with r2b: st.metric("In Trade", f"{bots_in_trade}")
            with r2c: st.metric("Scanning", f"{scanning_count}")
            with r2d: st.metric("Open Qty (Notional)", f"${open_qty_notional:,.2f}",
                              help="open_qty × avg_entry_price from trades table.")

            if assets_breakdown:
                with st.expander("💰 Detailed Asset Breakdown"):
                    st.table(pd.DataFrame(assets_breakdown))
            st.divider()
            
            # 5. Adoptions Count (Forensic Audit)
            cur.execute("SELECT COUNT(*) FROM reconciliation_logs WHERE action LIKE '%ADOPTION%' AND timestamp > ?", (int(time.time()) - 86400,))
            adoptions_today = cur.fetchone()[0]

            # --- System Status Ribbon ---
            cur.execute("SELECT action, symbol, price FROM trade_history ORDER BY id DESC LIMIT 1")
            last_h = cur.fetchone()
            last_act_str = f"{last_h[0]}: {last_h[1]} @ {last_h[2]:,.2f}" if last_h else "NO RECENT ACTIVITY"
            
            # Integrated Status Ribbon
            st.info(f"⚡ CORE: ONLINE | ACTIVE: {active_count} | ADOPTIONS (24h): {adoptions_today} | LAST: {last_act_str}")
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
        # --- FRAGMENTED OVERVIEW (v3.1.2) ---

        # Decorated with @st.fragment so this section refreshes independently
        # without triggering a full-page rerun.
        # -------------------------------------------------------------------------
        @st.fragment(run_every=15 if auto_refresh and not wizard_active else None)
        def _bot_positions_fragment():
            # 🚀 SINGLE SOURCE OF TRUTH FETCH
            df_pos_f, df_physical_f, market_orders_f, df_h_f, ex_err = _fetch_fresh_monitor_data()
            st.caption(f"  ⚡ Grid Sync: {time.strftime('%H:%M:%S')}")
            
            if ex_err:
                st.warning(f"⚠️ {ex_err}")
            
            # --- Mismatch Alert Logic (v3.0.9) ---
            # Integrated directly into fragment for real-time parity tracking
            try:
                # Initialize metrics
                hedge_amounts = {}
                physical_qty_by_pair = {}
                pair_prices = {} 
                virtual_net_by_norm = {}
                mismatched_pairs = []

                # Pre-calculate hedge_amounts
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
                        except: pass

                # Apply Display Status Mapping
                def derive_status(row):
                    if not row['is_active']: return "⚪ STOPPED"
                    b_status = str(row.get('status', '')).upper()
                    if 'REQUIRE_MANUAL' in b_status: return "🚨 MANUAL GATE"
                    if 'CARRY_PENDING' in b_status: return "⏳ CARRY/PENDING"
                    if 'HEDGE_EXIT_PENDING' in b_status: return "🛡️ HEDGE EXITING"
                    
                    c_phase = str(row.get('cycle_phase', 'IDLE')).upper()
                    c_step = int(row.get('current_step', 0) if pd.notna(row.get('current_step')) else 0)
                    invested = float(row.get('total_invested', 0) or 0)
                    h_amt = hedge_amounts.get(row['id'], 0)
                    
                    if c_phase == 'HEDGED' or h_amt > 1e-8:
                        if "EXITING" in b_status: return f"🛡️ HEDGE EXIT PENDING ({h_amt:.4f})"
                        return f"🛡️ HEDGED ({h_amt:.4f}) | Step {c_step}"
                    if c_phase == 'MARGIN_HELD':
                        return f"🚫 MARGIN HELD | Step {c_step}"
                    
                    # Consistent threshold for 'In Trade'
                    if c_phase == 'ACTIVE' or invested > 0.01:
                        if invested > 0 and invested <= 5.0: return "🟡 DUST/PARTIAL"
                        return f"🔴 IN TRADE | Step {c_step}"
                    
                    return "🟢 SCANNING"

                df_pos_f['status'] = df_pos_f.apply(derive_status, axis=1)
                df_pos_f['Active Orders'] = df_pos_f['id'].apply(lambda x: physical_order_counts.get(int(x), 0))
                
                # Highlight missing orders per-row
                def highlight_health(row):
                    bid, inv = int(row['id']), float(row['total_invested'] or 0)
                    ord_count = physical_order_counts.get(bid, 0)
                    status = str(row['status'])
                    if "IN TRADE" in status and ord_count == 0 and "CARRY" not in str(row.get('cycle_phase','')):
                        return f"⚠️ {status}"
                    return status
                
                df_pos_f['status'] = df_pos_f.apply(highlight_health, axis=1)
                df_pos_f['sort_priority'] = df_pos_f['status'].apply(lambda x: 1 if ("IN TRADE" in x or "HEDGED" in x) else (2 if "SCANNING" in x else 3))
                df_pos_f.sort_values(by=['sort_priority', 'name'], ascending=[True, True], inplace=True)

                # ═══════════════════════════════════════════════════════════════════════
                # [v3.1.4] GLOBAL NETTING — SINGLE SOURCE OF TRUTH FIX
                # ═══════════════════════════════════════════════════════════════════════
                # BEFORE (broken): Built virtual_qty_by_pair from trades.open_qty +
                # inline hedge arithmetic. trades.open_qty is a cached snapshot that is
                # NOT updated synchronously when hedges fill, causing false-positive
                # mismatches (e.g. XRP: System=-743 vs Exchange=0 when hedge filled).
                #
                # AFTER (correct): Use get_pair_virtual_net(pair) — the authoritative
                # proof-only computation from actual bot_orders fills. It already handles
                # entries, exits, partial-cancel-fills, hedges, hedge_tp, wipe_wall, and
                # cycle_id boundaries. One function → one source of truth → no false alarms.
                # ═══════════════════════════════════════════════════════════════════════
                from engine.database import get_pair_virtual_net as _get_virtual_net

                # Build unique pair map: normalized_key → canonical DB pair string
                unique_db_pairs = {}  # p_key → canonical pair (e.g. 'XRP/USDC:USDC')
                for _, row in df_pos_f.iterrows():
                    p_key = _norm_universal(row['pair'])
                    if p_key not in unique_db_pairs:
                        unique_db_pairs[p_key] = row['pair']
                    avg = float(row.get('avg_entry_price') or 0)
                    if avg > 0 and p_key not in pair_prices:
                        pair_prices[p_key] = avg

                # Populate virtual_net_by_norm from authoritative source
                for p_key, canonical_pair in unique_db_pairs.items():
                    try:
                        virtual_net_by_norm[p_key] = _get_virtual_net(canonical_pair)
                    except Exception as _vne:
                        virtual_net_by_norm[p_key] = 0.0

                # Physical net: prefer LIVE exchange positions (one-way signed contracts).
                # Summing active_positions LONG/SHORT rows can double-count when SNAP-ALLOCATE
                # splits one net exchange position across multiple bot_id rows.
                live_physical_net_by_pair = {}
                try:
                    ex_phys = get_exchange_instance(global_config.MARKET_TYPE)
                    for _pos in (ex_phys.fetch_positions() or []):
                        _amt = float(_pos.get('contracts', 0) or _pos.get('size', 0) or 0)
                        if abs(_amt) < 1e-12:
                            continue
                        _pkey = _norm_universal(_pos.get('symbol', ''))
                        live_physical_net_by_pair[_pkey] = live_physical_net_by_pair.get(_pkey, 0.0) + _amt
                        _epx = float(_pos.get('entryPrice', 0) or 0)
                        if _epx > 0 and _pkey not in pair_prices:
                            pair_prices[_pkey] = _epx
                except Exception as _lp_err:
                    st.caption(f"⚠️ Live position fetch failed, using DB snapshot: {_lp_err}")

                if not live_physical_net_by_pair and not df_physical_f.empty:
                    for _, row in df_physical_f.iterrows():
                        if pd.notna(row['size']) and pd.notna(row['entry_price']):
                            qty, price, side = abs(float(row['size'])), float(row['entry_price']), str(row['side']).upper().strip()
                            s_key, p_key = ('LONG' if side in ('BUY', 'LONG') else 'SHORT'), _norm_universal(row['pair'])
                            if p_key not in pair_prices:
                                pair_prices[p_key] = price
                            signed = qty if s_key == 'LONG' else -qty
                            live_physical_net_by_pair[p_key] = live_physical_net_by_pair.get(p_key, 0.0) + signed

                # 🔥 HEATMAP: Enrich pair_prices with LIVE ticker prices
                # Overwrite stale avg_entry_price with real mark prices for accurate distance %
                try:
                    ex_live = get_exchange_instance(global_config.MARKET_TYPE)
                    for p_key, canonical_pair in unique_db_pairs.items():
                        try:
                            live_px = ex_live.get_last_price(canonical_pair)
                            if live_px and float(live_px) > 0:
                                pair_prices[p_key] = float(live_px)
                        except Exception:
                            pass
                except Exception:
                    pass

                # Union of all known symbols (virtual + physical)
                all_symbols = set(unique_db_pairs.keys())
                if not df_physical_f.empty:
                    all_symbols |= set(_norm_universal(p) for p in df_physical_f['pair'])

                worst_pair_usd = 0.0
                mismatched_pair_count = 0

                for p in sorted(all_symbols):
                    v_net_qty = virtual_net_by_norm.get(p, 0.0)
                    ph_net_qty = live_physical_net_by_pair.get(p, 0.0)
                    whitelists = get_manual_whitelists(p)
                    for w in whitelists: ph_net_qty -= float(w['qty']) if w['side'] == 'LONG' else -float(w['qty'])
                    
                    ref_price = pair_prices.get(p, 1.0)
                    net_qty_diff = abs(v_net_qty - ph_net_qty)
                    net_usd_diff = net_qty_diff * ref_price
                    if net_usd_diff > worst_pair_usd:
                        worst_pair_usd = net_usd_diff
                    # Per-pair threshold: virtual and physical must match in qty space.
                    if net_usd_diff > 1.00:
                        mismatched_pair_count += 1
                        mismatched_pairs.append((f"{p} NET", v_net_qty * ref_price, ph_net_qty * ref_price, net_usd_diff, v_net_qty, ph_net_qty, ph_net_qty - v_net_qty, ref_price))

                # --- FRAGMENT UI RENDERING ---
                # Do NOT sum virtual USD across unrelated symbols — that produces nonsense
                # totals (e.g. BTC qty * BTC price + LINK qty * LINK price).
                m_col1, m_col2, m_col3 = st.columns(3)
                with m_col1: st.metric("Mismatched Pairs", mismatched_pair_count)
                with m_col2: st.metric("Worst Pair Gap (USD)", f"${worst_pair_usd:,.2f}")
                with m_col3:
                    _status = "HEALTHY" if mismatched_pair_count == 0 else "MISMATCH"
                    _color = "green" if _status == "HEALTHY" else "red"
                    st.markdown(f"**System Status:** <span style='color:{_color}; font-weight:bold;'>{_status}</span>", unsafe_allow_html=True)
                
                st.divider()

                # Order Health Alerts
                order_health_msg = ""
                order_status_color = "green"

                bots_with_missing_orders = []
                bots_with_partial_orders = []
                bots_with_margin_held = []
                for _, row in df_pos_f.iterrows():
                    bid, bot_inv, c_step = int(row['id']), float(row['total_invested'] or 0), int(row.get('current_step', 0))
                    actual_ph = physical_order_counts.get(bid, 0)
                    
                    # Skip bots that are legitimately idle or finishing
                    if "EXITING" in str(row.get('status','')).upper() or ("SCANNING" in str(row.get('status','')).upper() and bot_inv <= 0.01):
                        continue
                        
                    cycle_phase = str(row.get('cycle_phase', 'IDLE')).upper()

                    if bid in hedged_bot_ids:
                        # A bot in pure HEDGED phase legitimately has 0 physical orders:
                        # its entry TP has been closed and it's holding a hedge SHORT
                        # on the exchange, dormant, waiting for the hedge exit signal.
                        # Only flag as MISSING if it's in HEDGE_EXIT_PENDING (actively
                        # unwinding) but has no close/exit order placed.
                        if cycle_phase == 'HEDGE_EXIT_PENDING' and actual_ph == 0:
                            bots_with_missing_orders.append(f"{row['name']} (HEDGE_EXIT no order)")
                        # If in pure HEDGED: no alert — it's intentionally dormant
                    elif cycle_phase == 'MARGIN_HELD':
                        # Engine tried to place orders but Binance rejected with margin insufficiency.
                        # This is not a missing orders bug — it's a real account-level constraint.
                        # Show as orange warning, not red critical.
                        bots_with_margin_held.append(f"{row['name']}")
                    else:
                        # Stricter health: Step 1+ bots should generally have 2 orders (TP + Grid)
                        # We flag as MISSING CRITICAL if they have 0 orders but are in trade
                        if actual_ph == 0 and bot_inv > 0.01 and cycle_phase not in ('CARRY_PENDING', 'HEDGED'):
                            bots_with_missing_orders.append(row['name'])
                        elif actual_ph == 0 and cycle_phase in ('CARRY_PENDING', 'HEDGED'):
                            pass # Engine is intentionally holding without orders
                        # We flag as MISSING GRIDS if they have only 1 order but are mid-cycle (Step 1+)
                        elif actual_ph < 2 and c_step >= 1 and bot_inv > 0.01:
                            bots_with_partial_orders.append(f"{row['name']} ({actual_ph}/2)")

                if bots_with_missing_orders:
                    order_health_msg, order_status_color = f"⚠️ MISSING CRITICAL ORDERS: {', '.join(bots_with_missing_orders)}!", "red"
                elif bots_with_margin_held:
                    order_health_msg, order_status_color = f"⚠️ MARGIN HELD: {', '.join(bots_with_margin_held)} — TP blocked by account margin limit. Free margin to allow TP placement.", "orange"
                elif bots_with_partial_orders:
                    order_health_msg, order_status_color = f"⚠️ MISSING GRIDS: {', '.join(bots_with_partial_orders)}", "orange"
                else:
                    order_health_msg = f"✅ ORDERS SYNCED: {len(market_orders_f)} active orders."

                st.caption(f"  🩺 Order Health: :{order_status_color}[{order_health_msg}]")

                # --- Global Netting Diagnostics (Restored to Fragment) ---
                try:
                    with st.expander("🔍 Global Netting Diagnostics", expanded=False):
                        st.write("**Reconciliation Mode:** Global Net (Hedge-Aware)")
                        debug_data = []
                        for p_dbg in sorted(all_symbols):
                            v_dbg = virtual_net_by_norm.get(p_dbg, 0.0)
                            ph_net_dbg = live_physical_net_by_pair.get(p_dbg, 0.0)
                            ph_l_dbg = ph_net_dbg if ph_net_dbg > 0 else 0.0
                            ph_s_dbg = abs(ph_net_dbg) if ph_net_dbg < 0 else 0.0
                            ph_net_dbg = ph_l_dbg - ph_s_dbg
                            debug_data.append({
                                "Symbol": p_dbg, "System Net": f"{v_dbg:+.4f}",
                                "Exchange Net": f"{ph_net_dbg:+.4f}", "Diff Qty": f"{abs(v_dbg - ph_net_dbg):.4f}"
                            })
                        st.table(pd.DataFrame(debug_data))
                except: pass

                if mismatched_pairs:
                    st.error("🚨 SYSTEM MISMATCH DETECTED")
                    for row_mp in mismatched_pairs:
                        mp_pair, mp_virt, mp_phys, mp_diff, mp_vqty, mp_pqty, mp_dqty, mp_price = row_mp
                        st.warning(f"   ⚠️ **{mp_pair}**: System ${mp_virt:,.2f} vs Exchange ${mp_phys:,.2f} (Diff: ${mp_diff:,.2f}) | Qty: sys={mp_vqty:+.4f} ex={mp_pqty:+.4f} diff={mp_dqty:.4f}")
                        
                        # ── Confirmation-gate keys ──────────────────────────────────
                        _ck_adopt  = f"_confirm_adopt_{mp_pair}"
                        _ck_manual = f"_confirm_manual_{mp_pair}"
                        _ck_close  = f"_confirm_close_{mp_pair}"
                        
                        _act_c1, _act_c2, _act_c3 = st.columns(3)

                        # ── 🕵️ Forensic Adopt (2-step) ───────────────────────────────
                        with _act_c1:
                            if not st.session_state.get(_ck_adopt):
                                if st.button("🕵️ Forensic Adopt", key=f"f_{mp_pair}"):
                                    st.session_state[_ck_adopt] = True
                                    st.rerun()
                            else:
                                st.caption(f"⚠️ Adopt exchange qty into ledger for **{mp_pair}**?")
                                cc1, cc2 = st.columns(2)
                                with cc1:
                                    if st.button("✅ Confirm Adopt", key=f"fc_{mp_pair}", type="primary"):
                                        st.session_state[_ck_adopt] = False
                                        from engine.reconciler import StateReconciler
                                        _p_clean = mp_pair.split(' ')[0]
                                        clear_manual_whitelists_for_pair(_p_clean)
                                        StateReconciler().perform_forensic_reconstruction(_p_clean)
                                        st.rerun()
                                with cc2:
                                    if st.button("❌ Cancel", key=f"fcancel_{mp_pair}"):
                                        st.session_state[_ck_adopt] = False
                                        st.rerun()

                        # ── 📝 Manual Whitelist (2-step) ─────────────────────────────
                        with _act_c2:
                            if not st.session_state.get(_ck_manual):
                                if st.button("📝 Manual", key=f"m_{mp_pair}"):
                                    st.session_state[_ck_manual] = True
                                    st.rerun()
                            else:
                                _side_str = 'LONG' if mp_dqty > 0 else 'SHORT'
                                st.caption(f"⚠️ Whitelist {_side_str} {abs(mp_dqty):.4f} for **{mp_pair.split(' ')[0]}**?")
                                cc1, cc2 = st.columns(2)
                                with cc1:
                                    if st.button("✅ Confirm Manual", key=f"mc_{mp_pair}", type="primary"):
                                        st.session_state[_ck_manual] = False
                                        add_manual_whitelist(mp_pair.split(' ')[0], _side_str, abs(mp_dqty))
                                        st.rerun()
                                with cc2:
                                    if st.button("❌ Cancel", key=f"mcancel_{mp_pair}"):
                                        st.session_state[_ck_manual] = False
                                        st.rerun()

                        # ── 💥 Close / Reset (2-step, most dangerous) ───────────────
                        with _act_c3:
                            if not st.session_state.get(_ck_close):
                                if st.button("💥 Close", key=f"c_{mp_pair}"):
                                    st.session_state[_ck_close] = True
                                    st.rerun()
                            else:
                                st.error(f"🚨 IRREVERSIBLE: Close exchange pos + reset ALL {mp_pair.split(' ')[0]} bots?")
                                cc1, cc2 = st.columns(2)
                                with cc1:
                                    if st.button("🔴 YES, CLOSE", key=f"cc_{mp_pair}"):
                                        st.session_state[_ck_close] = False
                                        _p_clean = mp_pair.split(' ')[0]
                                        ex_m = get_exchange_instance('future')
                                        clear_manual_whitelists_for_pair(_p_clean)
                                        from engine.parity_gates import proof_flatten_pair
                                        _ccxt_pair = _p_clean
                                        if ':' not in _ccxt_pair and _ccxt_pair.endswith('/USDC'):
                                            _ccxt_pair = f"{_ccxt_pair}:USDC"
                                        elif ':' not in _ccxt_pair and _ccxt_pair.endswith('/USDT'):
                                            _ccxt_pair = f"{_ccxt_pair}:USDT"
                                        flat = proof_flatten_pair(
                                            ex_m, _ccxt_pair, human_approved=True,
                                        )
                                        if flat.get('success'):
                                            st.success(
                                                f"Proof flatten OK: cancelled {flat.get('cancelled_orders', 0)} orders, "
                                                f"reset {len(flat.get('bots_reset', []))} bots."
                                            )
                                        else:
                                            st.error(flat.get('error', 'Proof flatten failed'))
                                            for err in flat.get('errors', []):
                                                st.warning(str(err))
                                        st.rerun()
                                with cc2:
                                    if st.button("❌ Cancel", key=f"ccancel_{mp_pair}"):
                                        st.session_state[_ck_close] = False
                                        st.rerun()

                # --- Position Details & Grids ---
                st.subheader("🤖 Active Bot Positions")
                
                # Extract Trigger Info, Active Orders, and EE/Profit Metrics
                indicator_cache_f = {} # Per-fragment local cache
                def extract_info(row):
                    res = {
                        'Trigger': 'N/A', 'Orders': '0', 'TP_Price': 0.0, 
                        'Grid_Price': 0.0, 'Grid_Amount': 0.0, 
                        'Expected_Profit': 0.0, 'EE_Status': '-', 'Basket_Age': '-', 'Cycle_Age': '-',
                        'TP_Price_Str': '-', 'Grid_Price_Str': '-',
                        'Action_Age': '-', 'Trade_Age': '-'
                    }
                    def _clean(val):
                        if pd.isna(val) or val is None: return 0.0
                        try: return float(val)
                        except: return 0.0
                    try:
                        cfg_raw = row.get('config')
                        cfg = json.loads(cfg_raw if cfg_raw else '{}')
                        # Fetch current market price
                        pair_key = _norm_universal(row.get('pair', ''))
                        current_price = _clean(pair_prices.get(pair_key, 0.0))
                        
                        # Helper for Indicators (Cached per fragment run)
                        def get_indicator_val(p, tf, itype, period=14):
                            cache_key = (p, tf, itype, period)
                            if cache_key in indicator_cache_f: return indicator_cache_f[cache_key]
                            try:
                                ex_obj = get_exchange_instance(global_config.MARKET_TYPE)
                                ohlcv = ex_obj.fetch_ohlcv(p, timeframe=tf, limit=max(100, period*2))
                                if not ohlcv: return None
                                df = pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
                                from engine import indicators
                                if itype == 'RSI': val = float(indicators.rsi(df['c'], period).iloc[-1])
                                elif itype == 'CCI': val = float(indicators.cci(df['h'], df['l'], df['c'], period).iloc[-1])
                                else: val = None
                                indicator_cache_f[cache_key] = val
                                return val
                            except: return None

                        # 1. Trigger Description (Entry Conditions)
                        triggers = []
                        m_p = int(cfg.get('mode_price', 0) or 0)
                        t_p = float(cfg.get('price_threshold', 0) or 0)
                        
                        dist_pct = 0.0
                        if current_price > 0 and t_p > 0:
                            dist_pct = abs(current_price - t_p) / t_p * 100
                            
                        # Proximity Labels for Price Entry
                        entry_label = ""
                        if dist_pct < 0.5: entry_label = "🟢 [IN RANGE]"
                        elif dist_pct < 2.0: entry_label = "🟡 [SOON]"
                        else: entry_label = "⚪ [FAR]"

                        if m_p == 1: 
                            dist_str = f" ({((current_price - t_p)/t_p*100):.1f}%)" if current_price > 0 and t_p > 0 else ""
                            triggers.append(f"{entry_label} Price > ${t_p:,.2f}{dist_str}")
                        elif m_p == 2: 
                            dist_str = f" ({((current_price - t_p)/t_p*100):.1f}%)" if current_price > 0 and t_p > 0 else ""
                            triggers.append(f"{entry_label} Price < ${t_p:,.2f}{dist_str}")
                            
                        if cfg.get('mode_rsi'): 
                            target_rsi = float(cfg.get('rsi_level', 0))
                            tf_rsi = cfg.get('rsi_tf', '15m')
                            curr_rsi = get_indicator_val(row.get('pair'), tf_rsi, 'RSI')
                            if curr_rsi is not None:
                                dist_rsi = abs(curr_rsi - target_rsi)
                                label = "🟢 [IN RANGE]" if dist_rsi < 2 else ("🟡 [SOON]" if dist_rsi < 8 else "⚪ [FAR]")
                                triggers.append(f"{label} RSI({target_rsi}): {curr_rsi:.1f}")
                            else:
                                triggers.append(f"RSI({target_rsi})")

                        if cfg.get('mode_cci'): 
                            target_cci = float(cfg.get('cci_level', 0))
                            tf_cci = cfg.get('cci_tf', '5m')
                            curr_cci = get_indicator_val(row.get('pair'), tf_cci, 'CCI')
                            if curr_cci is not None:
                                dist_cci = abs(curr_cci - target_cci)
                                label = "🟢 [IN RANGE]" if dist_cci < 15 else ("🟡 [SOON]" if dist_cci < 40 else "⚪ [FAR]")
                                triggers.append(f"{label} CCI({target_cci}): {curr_cci:.1f}")
                            else:
                                triggers.append(f"CCI({target_cci})")

                        if cfg.get('mode_stoch'): 
                            tf_stoch = cfg.get('stoch_tf', '15m')
                            # Note: stochastic returns (%K, %D). We'll use %K.
                            try:
                                ex_obj = get_exchange_instance(global_config.MARKET_TYPE)
                                ohlcv = ex_obj.fetch_ohlcv(row.get('pair'), timeframe=tf_stoch, limit=60)
                                if ohlcv:
                                    df = pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
                                    from engine import indicators
                                    k, d = indicators.stochastic(df['h'], df['l'], df['c'])
                                    curr_k = float(k.iloc[-1])
                                    label = "🟢 [IN RANGE]" if (curr_k < 20 or curr_k > 80) else "⚪ [FAR]"
                                    triggers.append(f"{label} Stoch: {curr_k:.1f}")
                            except:
                                triggers.append(f"Stoch({tf_stoch})")

                        if cfg.get('mode_boll'): 
                            tf_boll = cfg.get('boll_tf', '15m')
                            try:
                                ex_obj = get_exchange_instance(global_config.MARKET_TYPE)
                                ohlcv = ex_obj.fetch_ohlcv(row.get('pair'), timeframe=tf_boll, limit=60)
                                if ohlcv:
                                    df = pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
                                    from engine import indicators
                                    u, m, l = indicators.bollinger_bands(df['c'])
                                    curr_p = float(df['c'].iloc[-1])
                                    curr_u = float(u.iloc[-1])
                                    curr_l = float(l.iloc[-1])
                                    dist_u = abs(curr_p - curr_u) / curr_u * 100
                                    dist_l = abs(curr_p - curr_l) / curr_l * 100
                                    label = "🟢 [IN RANGE]" if (dist_u < 0.2 or dist_l < 0.2) else ("🟡 [SOON]" if (dist_u < 1.0 or dist_l < 1.0) else "⚪ [FAR]")
                                    triggers.append(f"{label} Bollinger")
                            except:
                                triggers.append(f"Bollinger")
                        
                        desc_trigger = " + ".join(triggers) if triggers else "Trend/Dynamic"
                        
                        inv = _clean(row.get('total_invested'))
                        is_in_trade = inv > 0.01 or str(row.get('cycle_phase', '')).upper() == 'ACTIVE'
                        
                        # 🎯 HEATMAP REFACTOR: If In Trade, show TP/Grid distance as primary status
                        if is_in_trade:
                            # We'll calculate this after fetching orders below, so for now just placeholder
                            res['Trigger'] = desc_trigger
                        else:
                            res['Trigger'] = desc_trigger
                        
                        # 2. Order Tracking
                        bot_id = int(row['id'])
                        my_orders = [o for o in market_orders_f if str(o.get('clientOrderId') or '').startswith(f"CQB_{bot_id}_")]
                        if my_orders:
                            detailed = []
                            for o in my_orders:
                                cid = str(o.get('clientOrderId', ''))
                                price_val = _clean(o.get('price'))
                                if 'TP' in cid:
                                    detailed.append('TP')
                                    res['TP_Price'] = price_val
                                elif 'GRID' in cid:
                                    detailed.append('GRID')
                                    # If multiple grids, show the one closest to current price
                                    if res['Grid_Price'] == 0:
                                        res['Grid_Price'] = price_val
                                        res['Grid_Amount'] = _clean(o.get('amount'))
                                elif 'ENTRY' in cid: detailed.append('ENTRY')
                                elif 'HEDGE' in cid: detailed.append('HEDGE')
                            res['Orders'] = f"{len(my_orders)} ({', '.join(detailed[:5])}{'...' if len(detailed)>5 else ''})"
                            
                        # Enrich TP and Grid with distance
                        if res['TP_Price'] > 0 and current_price > 0:
                            dist = (current_price - res['TP_Price']) / res['TP_Price'] * 100
                            res['TP_Price_Str'] = f"${res['TP_Price']:,.2f} ({dist:+.1f}%)"
                        else:
                            res['TP_Price_Str'] = f"${res['TP_Price']:,.2f}" if res['TP_Price'] > 0 else "-"
                            
                        if res['Grid_Price'] > 0 and current_price > 0:
                            dist = (current_price - res['Grid_Price']) / res['Grid_Price'] * 100
                            res['Grid_Price_Str'] = f"{res['Grid_Amount']:.4f} @ ${res['Grid_Price']:,.2f} ({dist:+.1f}%)"
                        else:
                            res['Grid_Price_Str'] = f"{res['Grid_Amount']:.4f} @ ${res['Grid_Price']:,.2f}" if res['Grid_Price'] > 0 else "-"
                        
                        # 🎯 HEATMAP ENRICHMENT (Final Step): 
                        if is_in_trade:
                            if res['TP_Price'] > 0:
                                dist = abs(current_price - res['TP_Price']) / res['TP_Price'] * 100
                                label = "🟢 [READY]" if dist < 0.2 else ("🟡 [NEAR]" if dist < 1.0 else "⚪ [DISTANCE]")
                                res['Trigger'] = f"{label} TP Proximity: {dist:.1f}%"
                            elif res['Grid_Price'] > 0:
                                dist = abs(current_price - res['Grid_Price']) / res['Grid_Price'] * 100
                                label = "🟡 [NEAR]" if dist < 1.0 else "⚪ [DISTANCE]"
                                res['Trigger'] = f"{label} Grid Proximity: {dist:.1f}%"
                            else:
                                res['Trigger'] = f"⚠️ NO ORDERS (Adopted?)"
                        
                        # 3. Expected Profit
                        o_qty = _clean(row.get('open_qty'))
                        tp_p = _clean(res['TP_Price'] or row.get('target_tp_price'))
                        avg_p = _clean(row.get('avg_entry_price'))
                        if o_qty > 1e-8 and tp_p > 1e-8 and avg_p > 1e-8:
                            side = str(row.get('direction', 'LONG')).upper()
                            if side == 'SHORT': res['Expected_Profit'] = (avg_p - tp_p) * o_qty
                            else: res['Expected_Profit'] = (tp_p - avg_p) * o_qty
                        
                        # 4. Early Exit (EE) Status
                        b_start = _clean(row.get('basket_start_time'))
                        if is_in_trade and cfg.get('UseEarlyExit') and b_start > 0:
                            ee_start_h = _clean(cfg.get('EEStartHours'))
                            elapsed_h = (time.time() - b_start) / 3600
                            if elapsed_h > ee_start_h:
                                decay_mins = _clean(cfg.get('DecayIntervalMins', 15))
                                decay_pct = _clean(cfg.get('DecayPercentPerInterval', 10))
                                intervals = (elapsed_h - ee_start_h) * 60 / decay_mins
                                total_decay = min(100.0, intervals * decay_pct)
                                if total_decay >= 100.0:
                                    res['EE_Status'] = f"ACTIVE (100%) 🔴"
                                else:
                                    # Show intervals remaining until 100%
                                    intervals_to_full = (100.0 - total_decay) / decay_pct
                                    mins_to_full = intervals_to_full * decay_mins
                                    h_to_full = mins_to_full / 60
                                    if h_to_full < 1.0:
                                        time_to_full = f"{mins_to_full:.0f}m"
                                    else:
                                        time_to_full = f"{h_to_full:.1f}h"
                                    res['EE_Status'] = f"ACTIVE ({total_decay:.0f}%) → 100% in {time_to_full}"
                            else:
                                # Show time remaining until EE activates
                                wait_h = ee_start_h - elapsed_h
                                if wait_h < 1.0:
                                    res['EE_Status'] = f"Wait {wait_h*60:.0f}m"
                                else:
                                    res['EE_Status'] = f"Wait {wait_h:.1f}h"
                        
                        # 5. Pos Age (Position Age = basket_start_time = when entry filled)
                        b_start = _clean(row.get('basket_start_time'))
                        if is_in_trade and b_start > 0:
                            b_age_h = (time.time() - b_start) / 3600
                            if b_age_h < 1.0: res['Action_Age'] = f"{b_age_h*60:.0f}m"
                            elif b_age_h < 24.0: res['Action_Age'] = f"{b_age_h:.1f}h"
                            else: res['Action_Age'] = f"{b_age_h/24:.1f}d"
                        
                        # 6. Cycle Age (cycle_start_time = when the cycle began = last TP exit)
                        c_start = _clean(row.get('cycle_start_time'))
                        if is_in_trade and c_start > 0:
                            c_age_h = (time.time() - c_start) / 3600
                            if c_age_h < 1.0: res['Trade_Age'] = f"{c_age_h*60:.0f}m"
                            else: res['Trade_Age'] = f"{c_age_h:.1f}h"

                    except Exception as e:
                        print(f"Error extracting info for bot {row.get('id')}: {e}")
                    return res

                info_df = df_pos_f.apply(extract_info, axis=1, result_type='expand')
                df_pos_f['Trigger Condition'] = info_df['Trigger']
                df_pos_f['Active Orders'] = info_df['Orders']
                df_pos_f['Active TP'] = info_df['TP_Price_Str']
                df_pos_f['Next Grid'] = info_df['Grid_Price_Str']
                
                # Visual Profit Feedback
                def format_profit(row):
                    x = row['Expected_Profit']
                    status = str(row.get('status', '')).upper()
                    if 'SCANNING' in status or abs(x) < 0.001:
                        return "-"
                    return f"${x:,.2f}"

                df_pos_f['Expected Profit'] = info_df.apply(format_profit, axis=1)
                df_pos_f['EE Status'] = info_df['EE_Status']
                df_pos_f['Pos Age'] = info_df['Action_Age']   # basket_start_time: when this entry filled
                df_pos_f['Cycle Age'] = info_df['Trade_Age']  # cycle_start_time: when the cycle started (last TP)
                df_pos_f['Total Invested'] = df_pos_f['total_invested'].apply(
                    lambda x: f"${x:,.2f}" if x > 0.01 else "-"
                )
                df_pos_f['Open Qty'] = df_pos_f['open_qty'].apply(
                    lambda x: f"{float(x):.4f}" if pd.notna(x) and float(x) > 1e-8 else "-"
                )
                df_pos_f['Avg Entry'] = df_pos_f['avg_entry_price'].apply(
                    lambda x: f"${float(x):,.4f}" if pd.notna(x) and float(x) > 0 else "-"
                )

                st.dataframe(
                    df_pos_f[[
                        'name', 'pair', 'direction', 'status', 'Trigger Condition',
                        'Total Invested', 'Open Qty', 'Avg Entry',
                        'Pos Age', 'Expected Profit', 'EE Status', 'Cycle Age',
                        'Active TP', 'Next Grid', 'Active Orders',
                    ]],
                    width="stretch",
                    hide_index=True
                )

                # Manual Link Recovery Section
                active_bot_ids = [str(b_id) for b_id in df_pos_f['id'].values]
                stray_orders = [o for o in market_orders_f if str(o.get('clientOrderId','')).startswith('CQB_') and o.get('clientOrderId','').split('_')[1] not in active_bot_ids]
                
                # --- Orphan Position Detection (Restored to Fragment) ---
                try:
                    _orphan_rows = sqlite3.connect(global_config.PATHS['DB_FILE']).execute("SELECT pair, side, size, entry_price FROM active_positions WHERE bot_id=0").fetchall()
                    if _orphan_rows:
                        st.error("🚨 Unowned Physical Positions (Orphans) Found!")
                        for i, _or in enumerate(_orphan_rows):
                            _o_pair, _o_side, _o_size, _o_entry = _or
                            if st.button(f"💥 Flatten {_o_pair} {_o_side} ({_o_size})", key=f"flat_{_o_pair}_{i}"):
                                get_exchange_instance('future').create_order(_o_pair, 'market', 'sell' if _o_side.upper() == 'LONG' else 'buy', _o_size, params={'reduceOnly': True})
                                st.rerun()
                except: pass

                if stray_orders:
                    with st.expander(f"🧙‍♂️ Manual Link Recovery Tool ({len(stray_orders)} Strays)", expanded=False):
                        st.info("Found orders on exchange belonging to bots that are no longer active.")
                        for i, o in enumerate(stray_orders):
                            st.write(f"• {o.get('symbol')} {o.get('side')} {o.get('amount')} @ {o.get('price')} (ID: {o.get('clientOrderId')})")
                            if st.button(f"Cancel {o.get('id')}", key=f"cancel_{o.get('id')}_{i}"):
                                get_exchange_instance('future').cancel_order(o.get('id'), o.get('symbol')); st.rerun()

            except Exception as e_frag:
                st.error(f"Fragment Error: {e_frag}")
                import traceback
                st.code(traceback.format_exc())

        # EXECUTE FRAGMENT
        _bot_positions_fragment()

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
                    # Fetch bot entry/tp directly from DB — df_pos_f only exists inside
                    # the fragment scope and is not accessible here at the tab level.
                    try:
                        _conn_chart = get_connection()
                        _chart_row = _conn_chart.execute(
                            "SELECT t.avg_entry_price, t.open_qty "
                            "FROM trades t WHERE t.bot_id = ?",
                            (selected_bot_id,)
                        ).fetchone()
                        # Best available TP: look for a live TP order on exchange for this bot
                        _chart_prefix = f"CQB_{selected_bot_id}_"
                        _tp_orders = [o for o in market_orders
                                      if str(o.get('clientOrderId', '')).startswith(_chart_prefix)
                                      and 'TP' in str(o.get('clientOrderId', ''))]
                        be = float(_chart_row[0] or 0) if _chart_row else 0.0
                        tp = float(_tp_orders[0].get('price', 0)) if _tp_orders else 0.0
                    except Exception as _ce:
                        be, tp = 0.0, 0.0

                    # 1. Average Entry (Yellow Solid)
                    if be > 0:
                        fig.add_hline(y=be, line_dash="solid", line_color="#FFD700",
                                      annotation_text=f"ENTRY: {be:,.4f}",
                                      annotation_position="top left")

                    # 2. Take Profit (Green Solid — from live exchange order)
                    if tp > 0:
                        fig.add_hline(y=tp, line_dash="solid", line_color="#00FF00",
                                      annotation_text=f"TP: {tp:,.4f}",
                                      annotation_position="bottom right")

                    # 3. Active Grid/Safety Orders (Orange Dashed)
                    prefix = f"CQB_{selected_bot_id}_"
                    bot_orders_chart = [o for o in market_orders
                                        if str(o.get('clientOrderId', '')).startswith(prefix)]
                    grid_orders = [o for o in bot_orders_chart
                                   if 'GRID' in str(o.get('clientOrderId', ''))]
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

    # The header and bot grid now refresh via native @st.fragment decorators.
    if auto_refresh and not wizard_active:
        st.caption(f"ℹ️ Fragments auto-updating every 15-30s. [Page Base Time: {time.strftime('%H:%M:%S')}]")
    elif wizard_active:
        st.warning(
            "⏸ **Auto-Refresh Paused** — Reconciler wizard is active. "
            "Refreshing now would wipe your in-progress recovery work."
        )
    else:
        st.caption("ℹ️ Tip: Auto-Refresh is OFF. Toggle it above for real-time updates.")
