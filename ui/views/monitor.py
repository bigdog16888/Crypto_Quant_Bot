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
from engine.database import get_connection, get_bots_by_order_id, get_unread_notifications, mark_notifications_read, import_position_from_exchange
from engine.reconciler import StateReconciler, ReconciliationAction
from config.settings import config as global_config

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
    st.header("📊 Live Market Monitor")
    st.caption(f"Last Updated: {time.strftime('%H:%M:%S')} (Local)")

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
                    # Use get_last_price which is standardized in ExchangeInterface
                    price = ex_global.get_last_price(sym)
                    if price:
                        price_map[sym] = float(price)
            except Exception:
                pass
        
        for trade in active_trades:
            inv, entry, pair, direction = trade
            curr = price_map.get(pair, 0.0)
            
            # --- 🛡️ PnL EXPLOSION SAFEGUARD ---
            # If entry price is abnormally low (e.g. 1.0 for BTC/XAU), it's likely corrupted data
            # BTC is usually > 20k, XAU > 2k. Floor of $10 is safe.
            if curr > 0 and entry > 10.0: 
                if direction == 'LONG':
                    pnl = (curr - entry) / entry * inv
                else:
                    pnl = (entry - curr) / entry * inv
                global_pnl_usd += pnl
            elif entry > 0:
                # Log or warn about suspected corruption in console
                print(f"⚠️ SUSPECTED DATA CORRUPTION: Bot on {pair} has avg_entry={entry}. Skipping PnL calculation.")

        # 4. Fetch Multi-Asset Balances (Spot + Futures)
        futures_balance = 0.0
        spot_balance = 0.0
        total_equity = 0.0
        assets_breakdown = []

        # --- A. Fetch Futures Balance ---
        try:
            fut_data = fetch_balance_cached('future')
            if fut_data:
                if 'total' in fut_data:
                    for asset, amount in fut_data['total'].items():
                        if amount and amount > 0:
                            u_pnl = 0.0
                            assets_breakdown.append({
                                'Type': 'Futures',
                                'Asset': asset,
                                'Balance': amount,
                                'Unrealized PnL': u_pnl,
                                'Equity': amount + u_pnl
                            })
                            if asset in ['USDT', 'USDC', 'USD', 'BUSD']:
                                futures_balance += amount
        except Exception as e: 
            print(f"Error fetching futures balance: {e}")

        # --- B. Fetch Spot Balance ---
        try:
            cur.execute("SELECT config FROM bots WHERE is_active = 1")
            active_configs = cur.fetchall()
            
            needs_spot = False
            for cfg in active_configs:
                try:
                    c_dict = json.loads(cfg[0]) if cfg[0] else {}
                    if c_dict.get('market_type') == 'spot':
                        needs_spot = True
                        break
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
        # conn.close() 
    except Exception as e:
        st.error(f"Dashboard Load Error: {e}")
        active_count = 0
        total_invested_db = 0.0
        global_pnl_usd = 0.0
        futures_balance = 0.0
        spot_balance = 0.0
        total_equity = 0.0
        assets_breakdown = []

    # Display Metrics Grid
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Total Equity", f"${total_equity:,.2f}")
    with m2:
        st.metric("Futures Balance", f"${futures_balance:,.2f}")
    with m3:
        color = "normal" if global_pnl_usd >= 0 else "inverse"
        st.metric("Active PnL", f"${global_pnl_usd:,.2f}")
    with m4:
        st.metric("Active Exposure", f"${total_invested_db:,.2f}")

    if assets_breakdown:
        with st.expander("💰 Detailed Asset Breakdown"):
            st.table(pd.DataFrame(assets_breakdown))

    st.divider()
    
    # --- 1. System Status Ribbon ---
    try:
        conn_h = get_connection()
        cur_h = conn_h.cursor()
        cur_h.execute("SELECT COUNT(*) FROM bots WHERE is_active = 1")
        act_count = cur_h.fetchone()[0]
        cur_h.execute("SELECT action, symbol, price FROM trade_history ORDER BY id DESC LIMIT 1")
        last_h = cur_h.fetchone()
        
        # Fundamental Health Check Logic
        # 1. Active Bots vs Orders
        # Expectation: Each IN_TRADE bot should have 2 orders (TP + Grid) if fully active
        cur_h.execute("SELECT id, status FROM bots WHERE is_active = 1")
        active_bots_data = cur_h.fetchall()
        bots_in_trade = [b for b in active_bots_data if 'IN TRADE' in b[1] or 'Pending' in b[1]]
        expected_orders_min = len(bots_in_trade) * 2
        
        # We need actual open orders count (fetched below, but we need it here for ribbon)
        # We'll use a quick cached fetch or previous logic. 
        # For the ribbon, we might just use the visual indicator below.
        
        # conn_h.close()
        
        last_act_str = f"{last_h[0]}: {last_h[1]} @ {last_h[2]:,.2f}" if last_h else "NO RECENT ACTIVITY"
        st.info(f"CORE ENGINE: ONLINE | ACTIVE BOTS: {act_count} | LAST ACTION: {last_act_str}")
    except: pass


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
        if st.button("🔄 Refresh Now"):
            st.cache_data.clear()
            st.rerun()

    # Auto-Refresh Toggle (Default ON) - Capture state here, execute later
    auto_refresh = st.toggle("⚡ Auto-Refresh (5s) [ASync]", value=True, key="auto_refresh_toggle")

    # --- Data Fetching (Parallel) ---
    with st.spinner("Fetching market data..."):
        # Parallel fetch for speed
        with ThreadPoolExecutor(max_workers=4) as executor:
            f_ohlcv = executor.submit(fetch_ohlcv_cached, global_config.MARKET_TYPE, symbol, timeframe)
            f_pos = executor.submit(fetch_positions_cached, global_config.MARKET_TYPE)
            f_bal = executor.submit(fetch_balance_cached, global_config.MARKET_TYPE)
            # Orders logic: Fetch ALL open orders to ensure multi-pair bots (e.g. BTC + XAU) are covered
            f_orders = executor.submit(fetch_open_orders_cached, global_config.MARKET_TYPE, None)
            
            # other results used in respective sections
            raw_orders = f_orders.result()
            ohlcv_data = f_ohlcv.result()
            # DEDUPLICATE ORDERS (Safety mechanism)
            market_orders = list({o['id']: o for o in raw_orders}.values())

    # --- UI Layout: Tabs ---
    tab_overview, tab_charts, tab_history = st.tabs(["📊 Overview", "📈 Live Charts", "📝 Orders & History"])

    with tab_overview:
        # --- Prepare Data ---
        df_pos = pd.DataFrame()
        df_physical = pd.DataFrame()
        
        # --- Mismatch Alert Logic ---
        try:
            conn = get_connection()
            
            # Fetch Bot Strategies (df_pos)
            query_all = """
                SELECT b.id, b.name, b.pair, b.direction, b.strategy_type, b.config, t.current_step, t.total_invested, t.avg_entry_price, t.target_tp_price, b.is_active, b.status, t.basket_start_time
                FROM bots b
                LEFT JOIN trades t ON b.id = t.bot_id
                WHERE b.is_active = 1
            """
            df_pos = pd.read_sql(query_all, conn)
            
            # Fetch Physical Positions (df_physical)
            try:
                # FUNDAMENTAL FIX: Use a fresh connection to bypass thread-local staleness
                db_path = global_config.PATHS['DB_FILE']
                conn_fresh = sqlite3.connect(db_path, timeout=10)
                
                df_physical = pd.read_sql("SELECT pair, side, size, entry_price, datetime(last_checked, 'unixepoch', 'localtime') as updated FROM active_positions", conn_fresh)
                conn_fresh.close()
                
                # DEBUG VISUALIZATION REMOVED
                pass
                
            except Exception as e:
                st.error(f"Failed to fetch physical positions: {e}")
                print(f"DEBUG UI ERROR: {e}")
                df_physical = pd.DataFrame()

            # --- FUNDAMENTAL FIX: DATA-DRIVEN STATUS ---
            # Derive 'display_status' from total_invested to ensure UI is always accurate to reality
            def derive_status(row):
                if not row['is_active']: return "⚪ STOPPED"
                if row['total_invested'] > 0: return "🔴 IN TRADE"
                # Check for 'Pending' based on order counts if needed, but 'Scanning' is safe default for active/non-invested
                return "🟢 SCANNING"

            # Apply fix to df_pos BEFORE rendering
            df_pos['status'] = df_pos.apply(derive_status, axis=1)

            # --- PER-PAIR MISMATCH COMPARISON (matches reconciler logic) ---
            # Helper: normalize symbol to strip colon suffix (BTC/USDC:USDC → BTC/USDC)
            def _norm(sym):
                s = str(sym).split(':')[0].strip()
                return s

            # 1. Fetch Virtual Positions (grouped by normalized pair)
            query_virtual = """
                SELECT b.pair, t.total_invested, t.avg_entry_price, b.direction 
                FROM trades t
                JOIN bots b ON t.bot_id = b.id
                WHERE b.is_active = 1 AND t.total_invested > 0
            """
            df_virt = pd.read_sql(query_virtual, conn)
            
            virtual_net_usd = 0.0
            virtual_gross_usd = 0.0
            virtual_by_pair = {}  # {normalized_pair: signed_usd}
            
            if not df_virt.empty:
                for _, row in df_virt.iterrows():
                    amt_usd = row['total_invested']
                    virtual_gross_usd += amt_usd
                    pair_key = _norm(row['pair'])
                    signed = amt_usd if row['direction'] == 'LONG' else -amt_usd
                    virtual_net_usd += signed
                    virtual_by_pair[pair_key] = virtual_by_pair.get(pair_key, 0.0) + signed
            
            # 2. Physical Positions (grouped by normalized pair)
            physical_net_usd = 0.0
            physical_by_pair = {}  # {normalized_pair: signed_usd}
            
            if not df_physical.empty:
                for _, row in df_physical.iterrows():
                    val = row['size'] * row['entry_price']
                    side = str(row['side']).upper().strip()
                    pair_key = _norm(row['pair'])
                    signed = abs(val) if side in ['BUY', 'LONG'] else -abs(val)
                    physical_net_usd += signed
                    physical_by_pair[pair_key] = physical_by_pair.get(pair_key, 0.0) + signed
            
            # 3. Per-pair comparison (1% tolerance, min $5 floor)
            all_pairs = set(list(virtual_by_pair.keys()) + list(physical_by_pair.keys()))
            mismatched_pairs = []
            for p in all_pairs:
                v = virtual_by_pair.get(p, 0.0)
                ph = physical_by_pair.get(p, 0.0)
                pair_diff = abs(v - ph)
                tolerance = max(5.0, 0.01 * max(abs(v), abs(ph)))  # 1% of larger side, min $5
                if pair_diff > tolerance:
                    mismatched_pairs.append((p, v, ph, pair_diff))
            
            diff_net = abs(virtual_net_usd - physical_net_usd)
            # (Mismatch and Order Health are displayed in the MASTER STATUS INDICATOR section below)
            
        except Exception as e:
            st.warning(f"Could not calculate sync status: {e}")


        
        # --- Status Indicator & Order Health ---
        try:
            order_health_msg = ""
            order_status_color = "green"
            
            # --- STATUS CONSISTENCY FIX ---
            in_trade_bots = df_pos[df_pos['total_invested'] > 0]
            total_orders = len(market_orders)
            
            # Calculate Expected Orders dynamically per bot
            expected_orders = 0
            # 1. Bots already in trade
            for _, b_row in in_trade_bots.iterrows():
                try:
                    cfg_json = json.loads(b_row['config'])
                    max_s = int(cfg_json.get('max_steps', 10))
                except:
                    max_s = 10
                
                if b_row['current_step'] >= max_s:
                    expected_orders += 1  # Just TP
                else:
                    expected_orders += 2  # TP + Grid
            
            # 2. Bots waiting for entry fill
            entry_pending_bots = df_pos[df_pos['status'].str.contains('WAITING FOR FILL', na=False)]
            expected_orders += len(entry_pending_bots)
            
            if total_orders < expected_orders:
                order_health_msg = f"⚠️ MISSING ORDERS: Found {total_orders}, Expected ~{expected_orders}."
                order_status_color = "red"
            elif total_orders > expected_orders:
                 order_health_msg = f"⚠️ TOO MANY ORDERS: Found {total_orders}, Expected ~{expected_orders}."
                 order_status_color = "red"
            else:
                order_health_msg = f"✅ ORDERS SYNCED: {total_orders} active orders."

            # Display Metrics
            col1, col2 = st.columns(2)
            with col1:
                st.metric("Net Exposure (Virtual USD)", f"${virtual_net_usd:,.2f}")
            with col2:
                st.metric("Exchange Net (Physical USD)", f"${physical_net_usd:,.2f}", delta=f"{physical_net_usd-virtual_net_usd:,.2f}")

                # MASTER STATUS INDICATOR
                has_mismatch = len(mismatched_pairs) > 0
                
                # --- STARTUP GRACE PERIOD AUDIT ---
                # Delay RED status for 60s during startup to allow entry orders to fire.
                is_startup_grace = False
                if not df_pos.empty:
                    # Check if any bot session is < 60s old
                    try:
                        newest_start = df_pos['basket_start_time'].max()
                        if (time.time() - newest_start) < 60:
                            is_startup_grace = True
                    except: pass

                status_color = "red"
                if not has_mismatch and order_status_color == "green":
                    st.success(f"✅ **SYSTEM HEALTHY**: Net positions are synced. {order_health_msg}")
                elif is_startup_grace and order_status_color == "red":
                    st.warning(f"🟡 **SYSTEM STARTUP**: Waiting for initial sync/orders (Grace Period)...")
                    st.caption(f"Reason: {order_health_msg}")
                else:
                    st.error(f"🚨 **SYSTEM MISMATCH DETECTED**")
                    if mismatched_pairs:
                        for mp_pair, mp_virt, mp_phys, mp_diff in mismatched_pairs:
                            st.write(f"   • **{mp_pair}**: System ${mp_virt:,.2f} vs Exchange ${mp_phys:,.2f} (Diff: ${mp_diff:,.2f})")
                    else:
                        st.write(f"1. **Position Mismatch**: System ${virtual_net_usd:,.2f} vs Exchange ${physical_net_usd:,.2f}")
                    st.write(f"2. **Order Health**: {order_health_msg}")

                # --- MANUAL LINK RECOVERY TOOL (Always available if mismatch exists) ---
                if has_mismatch:
                    st.divider()
                    st.markdown("### 🧙‍♂️ Manual Link Recovery Tool")
                    st.caption("Accurately restore bot links for manual trades or disconnected assets.")
                    
                    # 1. Detect Rogue Positions via Reconciler (Explicitly)
                    reconciler = StateReconciler(exchanges={'future': get_exchange_instance('future'), 'spot': get_exchange_instance('spot')})
                    bot_states = reconciler.get_bot_states()
                    success, all_positions = reconciler.fetch_all_exchange_positions()
                    all_pairs = list(set([b.pair for b in bot_states]))
                    # Filter only pairs with mismatches to save time
                    m_pairs = [mp[0] for mp in mismatched_pairs]
                    all_orders_recon = reconciler.fetch_all_exchange_orders(m_pairs)
                    
                    recon_results = reconciler.resolve_net_mismatch(bot_states, all_positions, all_orders_recon, force_adoption=False)
                    rogue_results = [r for r in recon_results if r.action_taken == ReconciliationAction.ROGUE_POSITION]
                    
                    # Pre-calculate scanning bots available for adoption
                    # Derive candidates from df_pos which has 12+ columns
                    candidates_df = df_pos[df_pos['status'].str.contains('SCANNING', case=False, na=False)]
                    scanning_bot_options = {f"{row['name']} (#{row['id']}) [{row['pair']}]": row['id'] for _, row in candidates_df.iterrows()}

                    if rogue_results:
                        for rogue in rogue_results:
                            with st.expander(f"🔴 Rogue Position: {rogue.pair}", expanded=True):
                                st.write(f"**Details:** {rogue.details}")
                                st.info("Forensic Evidence Required: Select a recent fill to prove ownership.")
                                
                                # Forensic Search
                                if st.button(f"🔍 Perform Forensic Search ({rogue.pair})", key=f"forensic_btn_{rogue.pair}"):
                                     ex = get_exchange_instance('future')
                                     trades = ex.fetch_my_trades(rogue.pair, limit=10)
                                     if trades:
                                         st.session_state[f"forensic_trades_{rogue.pair}"] = trades
                                     else:
                                         st.warning("No recent fills found on exchange for this pair.")
                                
                                if f"forensic_trades_{rogue.pair}" in st.session_state:
                                     trades = st.session_state[f"forensic_trades_{rogue.pair}"]
                                     # Let user select a trade
                                     trade_options = {
                                         f"{t['side'].upper()} {t['amount']} @ {t['price']} (ID:{t['orderId']})": t 
                                         for t in trades
                                     }
                                     selected_trade_label = st.selectbox("Select Evidence Fill:", list(trade_options.keys()), key=f"trade_sel_{rogue.pair}")
                                     selected_trade = trade_options[selected_trade_label]
                                     
                                     # Actions
                                     res_col1, res_col2 = st.columns(2)
                                     
                                     with res_col1:
                                         if scanning_bot_options:
                                             selected_target = st.selectbox("Link to Bot:", list(scanning_bot_options.keys()), key=f"adopt_sel_{rogue.pair}")
                                             if st.button("🔗 Adopt with Proof", key=f"adopt_btn_{rogue.pair}", type="primary"):
                                                 # Get current position size/price
                                                 pair_norm = _norm(rogue.pair)
                                                 pos_data = all_positions.get(pair_norm, [])
                                                 if pos_data:
                                                     total_size = sum(p.size for p in pos_data)
                                                     avg_entry = sum(p.size * p.entry_price for p in pos_data) / total_size if total_size > 0 else 0
                                                     side = pos_data[0].side
                                                     bot_id_ad = scanning_bot_options[selected_target]
                                                     
                                                     if import_position_from_exchange(bot_id_ad, rogue.pair, total_size, avg_entry, side):
                                                         st.success(f"✅ Bot #{bot_id_ad} has adopted the position using Proof ID {selected_trade['orderId']}!")
                                                         # Clear forensic cache
                                                         del st.session_state[f"forensic_trades_{rogue.pair}"]
                                                         time.sleep(1)
                                                         st.rerun()
                                         else:
                                             st.warning("No 'Scanning' bots available to adopt this position.")
                                     
                                     with res_col2:
                                         if st.button("🛑 Market Close", key=f"close_btn_{rogue.pair}"):
                                             try:
                                                 ex = get_exchange_instance('future')
                                                 pair_norm = _norm(rogue.pair)
                                                 pos_data = all_positions.get(pair_norm, [])
                                                 if pos_data:
                                                     for p in pos_data:
                                                         close_side = 'sell' if p.side.upper() == 'LONG' else 'buy'
                                                         ex.create_order(p.symbol, 'market', close_side, p.size)
                                                     st.success(f"✅ Market close orders sent!")
                                                     if f"forensic_trades_{rogue.pair}" in st.session_state: del st.session_state[f"forensic_trades_{rogue.pair}"]
                                                     time.sleep(1)
                                                     st.rerun()
                                             except Exception as e:
                                                 st.error(f"Failed to close: {e}")
                    
                    # 2. Stray Bot Order Recovery (DNA Link)
                    # Orders with CQB_ prefix that are not matched to currently active bot IDs
                    active_bot_ids = [str(b[0]) for (b) in active_bots_list]
                    stray_orders = []
                    unknown_orders = []
                    
                    for o in market_orders:
                        cid = o.get('clientOrderId', '')
                        if cid.startswith('CQB_'):
                            # Check if it belongs to an active bot
                            parts = cid.split('_')
                            if len(parts) > 1 and parts[1] not in active_bot_ids:
                                stray_orders.append(o)
                        else:
                            unknown_orders.append(o)

                    if stray_orders:
                        with st.expander(f"🩹 {len(stray_orders)} Stray Bot Orders Detected", expanded=True):
                            st.warning("These orders belong to the system (DNA match) but use unknown Bot IDs (e.g. from a previous run).")
                            st.dataframe(pd.DataFrame(stray_orders)[['symbol', 'side', 'price', 'amount', 'clientOrderId']], width="stretch")
                            
                            # Adoption logic for stray orders
                            st.info("You can adopt these orders to a new bot to resume management.")
                            col_s1, col_s2 = st.columns(2)
                            with col_s1:
                                if scanning_bot_options:
                                    selected_target = st.selectbox("Link Strays to Bot:", list(scanning_bot_options.keys()), key="stray_adopt_sel")
                                    if st.button("🔗 Adopt Stray Orders", type="primary"):
                                        # Logic to adopt - basically update the DB or just rely on the bot taking over the symbol
                                        # For orders, we might need to update the client_order_id to the new bot's ID?
                                        # Actually, if we link the POSITION, the bot will manage the orders if we fix its internal ID.
                                        # For now, let's keep it simple: link them or cancel them.
                                        st.info("Adoption would re-link these signatures. (Feature in progress or simply use 'Market Close' for safety).")
                            with col_s2:
                                if st.button("🗑️ Cancel All Stray Orders"):
                                    try:
                                        ex = get_exchange_instance('future')
                                        for o in stray_orders:
                                            ex.cancel_order(o['id'], o['symbol'])
                                        st.success(f"✅ {len(stray_orders)} stray orders cancelled!")
                                        time.sleep(1)
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Failed to cancel: {e}")

                    if unknown_orders:
                        with st.expander(f"⚠️ {len(unknown_orders)} Unknown Exchange Orders", expanded=False):
                            st.caption("These orders have no system signature (Manual trades).")
                            st.dataframe(pd.DataFrame(unknown_orders)[['symbol', 'side', 'price', 'amount']], width="stretch")
                    
                    if not rogue_results and not stray_orders and not unknown_orders:
                        st.info("No rogue positions or stray orders detected. The system is in sync with the exchange DNA.")

        except Exception as e:
            st.warning(f"Could not calculate order health: {e}")


        st.divider()

        # --- Physical Positions (Exchange Reality) ---
        st.subheader("🏥 Exchange Reality (Physical)")
        if not df_physical.empty:
            st.dataframe(df_physical, width="stretch")
            # Calculate Physical Net on the fly if needed, but we already have physical_net_usd from above
        else:
            st.info("Exchange wallet is empty (No physical positions).")
            if virtual_gross_usd > 100:
                st.caption("ℹ️ Note: If active bots exist, this means Longs and Shorts are perfectly hedged (Net ~0).")

        st.divider()

        # --- Virtual Positions (Bot Strategies) ---
        st.subheader("🤖 Bot Strategies (Virtual Positions)")
        if not df_pos.empty:
            # UX Improvements: Rename Status to friendly labels
            df_pos['status'] = df_pos['status'].replace('Scanning', '🟢 SCANNING')
            df_pos['status'] = df_pos['status'].replace('Waiting for Signal', '🟢 SCANNING')  # legacy
            df_pos['status'] = df_pos['status'].replace('IN TRADE', '🔴 IN TRADE')
            df_pos['status'] = df_pos['status'].replace('ENTRY PENDING', '🟡 WAITING FOR FILL')
            df_pos['status'] = df_pos['status'].replace('Stopped', '⚪ STOPPED')
            df_pos['status'] = df_pos['status'].replace('STOPPED', '⚪ STOPPED')
            
            # Extract Trigger Info & Active Orders
            def extract_info(row):
                res = {'Trigger': 'N/A', 'Orders': '0'}
                try:
                    cfg = json.loads(row['config'])
                    triggers = []

                    # 1. Price Trigger
                    m_p = cfg.get('mode_price', 0)
                    t_p = float(cfg.get('price_threshold', 0))
                    if m_p == 1: triggers.append(f"Price > ${t_p:,.2f}")
                    elif m_p == 2: triggers.append(f"Price < ${t_p:,.2f}")

                    # 2. Indicator Triggers
                    if cfg.get('mode_rsi'):
                        r_m = cfg['mode_rsi']
                        r_l = cfg.get('rsi_level', 0)
                        triggers.append(f"RSI({'<' if r_m==1 else '>'}{r_l})")
                    if cfg.get('mode_cci'):
                        c_m = cfg['mode_cci']
                        c_l = cfg.get('cci_level', 0)
                        triggers.append(f"CCI({'<' if c_m==2 else '>'}{c_l})")
                    if cfg.get('mode_boll'):
                        b_m = cfg['mode_boll']
                        triggers.append("BOLL(Outside)")
                    if cfg.get('mode_stoch'):
                        s_m = cfg['mode_stoch']
                        triggers.append(f"Stoch({'Oversold' if s_m==1 else 'Overbought'})")
                    
                    # 3. Patterns and others
                    for i in range(1, 5):
                        if cfg.get(f'pat_{i}_mode'):
                            p_m = cfg[f'pat_{i}_mode']
                            p_c = cfg.get(f'pat_{i}_count', 1)
                            p_s = cfg.get(f'pat_{i}_source', 'Price')
                            triggers.append(f"{p_s}Pat({p_c}x {'Up' if p_m==1 else 'Dn'})")

                    desc_trigger = " + ".join(triggers) if triggers else "N/A"

                    if row['status'] == '🔴 IN TRADE':
                        res['Trigger'] = f"In Trade ({desc_trigger})"
                    else:
                        res['Trigger'] = desc_trigger
                    
                    # 2. Active Orders
                    bot_id = row['id']
                    # Filter market_orders (deduplicated)
                    my_orders = [o for o in market_orders if o.get('clientOrderId', '').startswith(f"CQB_{bot_id}_")]
                    if my_orders:
                        types = [o['type'].upper() for o in my_orders]
                        # Try to identify TP/Grid/Entry based on ID
                        detailed = []
                        for o in my_orders:
                            cid = o.get('clientOrderId', '')
                            if 'TP' in cid: detailed.append('TP')
                            elif 'GRID' in cid: detailed.append('GRID')
                            elif 'ENTRY' in cid: detailed.append('ENTRY')
                            else: detailed.append('LIMIT')
                        
                        res['Orders'] = f"{len(my_orders)} " + (f"({', '.join(detailed)})" if detailed else "")
                    else:
                            res['Orders'] = "0"
                            
                except Exception as e: 
                    print(e)
                return res

            info_df = df_pos.apply(extract_info, axis=1, result_type='expand')
            df_pos['Trigger Condition'] = info_df['Trigger']
            df_pos['Active Orders'] = info_df['Orders']
            
            # --- PERFORMANCE MATRIX (Enterprise Batch View) ---
            # Calculate dense metrics for 20+ bots
            st.markdown("### ⚡ Batch Performance Matrix")
            try:
                matrix_df = df_pos.copy()
                
                # 1. PnL Estimate Column
                def est_profit(row):
                    if row['total_invested'] > 0 and row['avg_entry_price'] > 0 and row['target_tp_price'] > 0:
                        qty = row['total_invested'] / row['avg_entry_price']
                        if row['direction'] == 'LONG':
                            return (row['target_tp_price'] - row['avg_entry_price']) * qty
                        else:
                            return (row['avg_entry_price'] - row['target_tp_price']) * qty
                    return 0.0
                
                matrix_df['Expected Profit'] = matrix_df.apply(est_profit, axis=1)
                
                # 2. Time in Trade Column
                current_time = time.time()
                def time_in_trade(row):
                    if row['total_invested'] > 0 and row['basket_start_time'] > 0:
                        sec = current_time - row['basket_start_time']
                        m, s = divmod(sec, 60)
                        h, m = divmod(m, 60)
                        return f"{int(h)}h {int(m)}m"
                    return "-"
                
                matrix_df['Time in Trade'] = matrix_df.apply(time_in_trade, axis=1)
                
                # 3. Format columns
                cols_matrix = ['name', 'pair', 'direction', 'current_step', 'total_invested', 'Expected Profit', 'Time in Trade', 'status']
                matrix_df = matrix_df[[c for c in cols_matrix if c in matrix_df.columns]]
                
                matrix_df['total_invested'] = matrix_df['total_invested'].apply(lambda x: f"${x:,.2f}" if x > 0 else "-")
                matrix_df['Expected Profit'] = matrix_df['Expected Profit'].apply(lambda x: f"${x:,.2f}" if x > 0 else "-")
                
                st.dataframe(matrix_df, width="stretch")
            except Exception as e:
                st.warning(f"Failed to render Batch Matrix: {e}")
                
            st.divider()
            
            st.markdown("### ⚙️ Detailed Bot State (Debug View)")
            
            # Reorder columns for readability
            cols = ['name', 'pair', 'direction', 'status', 'Active Orders', 'Trigger Condition', 'current_step', 'total_invested', 'avg_entry_price']
            # Filter strictly for columns that exist
            existing_cols = [c for c in cols if c in df_pos.columns]
            
            st.dataframe(df_pos[existing_cols], width="stretch")
        else:
            st.info("No active bots.")


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
                
                # If a bot is focused and in trade, add entry lines
                if selected_bot_id:
                    # Fetch current bot status for lines
                    cur_bot = df_pos[df_pos['id'] == selected_bot_id]
                    if not cur_bot.empty:
                        be = float(cur_bot.iloc[0]['avg_entry_price'] or 0)
                        tp = float(cur_bot.iloc[0]['target_tp_price'] or 0)
                        
                        if be > 0:
                            fig.add_hline(y=be, line_dash="dash", line_color="blue", 
                                          annotation_text=f"Avg Entry: {be:,.2f}")
                        if tp > 0:
                            fig.add_hline(y=tp, line_dash="dash", line_color="green", 
                                          annotation_text=f"Take Profit: {tp:,.2f}")

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
            # Fetch last 20 actions
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
                LIMIT 20
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

    # --- Auto-Refresh Upgrade ---
    if auto_refresh:
        from streamlit_autorefresh import st_autorefresh
        # 🚀 UI PERFORMANCE BATCHING: Now that rendering is async/cached, we can safely refresh every 5s
        st_autorefresh(interval=5000, limit=None, key="monitor_autorefresh")
    else:
        st.caption("ℹ️ Tip: Auto-Refresh is OFF. Toggle it in the sidebar for real-time updates.")
