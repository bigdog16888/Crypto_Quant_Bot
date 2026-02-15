import json
import streamlit as st
import time
import pandas as pd
import plotly.graph_objects as go
import ccxt
import os
from concurrent.futures import ThreadPoolExecutor
from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection, get_bots_by_order_id, get_unread_notifications, mark_notifications_read
from config.settings import config as global_config

# --- Performance Caching Wrappers ---
@st.cache_resource(ttl=3600, show_spinner=False)
def get_exchange_instance(market_type):
    """Singleton provider for ExchangeInterface to reuse connections."""
    return ExchangeInterface(market_type=market_type)


@st.cache_data(ttl=60, show_spinner=False)
def fetch_ohlcv_cached(market_type, symbol, timeframe):
    try:
        ex = get_exchange_instance(market_type)
        return ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
    except Exception: return []

@st.cache_data(ttl=10, show_spinner=False)
def fetch_positions_cached(market_type):
    try:
        ex = get_exchange_instance(market_type)
        return ex.fetch_positions()
    except Exception: return []

@st.cache_data(ttl=10, show_spinner=False)
def fetch_open_orders_cached(market_type, symbol):
    try:
        ex = get_exchange_instance(market_type)
        return ex.fetch_open_orders(symbol)
    except Exception as e:
        print(f"Error fetching orders for {symbol}: {e}")
        return []

@st.cache_data(ttl=30, show_spinner=False)
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
            conn.close()
            
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
            if curr > 0 and entry > 0:
                if direction == 'LONG':
                    pnl = (curr - entry) / entry * inv
                else:
                    pnl = (entry - curr) / entry * inv
                global_pnl_usd += pnl

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
        conn.close()
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
        
        conn_h.close()
        
        last_act_str = f"{last_h[0]}: {last_h[1]} @ {last_h[2]:,.2f}" if last_h else "NO RECENT ACTIVITY"
        st.info(f"CORE ENGINE: ONLINE | ACTIVE BOTS: {act_count} | LAST ACTION: {last_act_str}")
    except: pass


    # --- Control Bar ---
    try:
        conn_b = get_connection()
        cur_b = conn_b.cursor()
        cur_b.execute("SELECT id, name, pair FROM bots WHERE is_active = 1")
        active_bots_list = cur_b.fetchall()
        conn_b.close()
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
    auto_refresh = st.toggle("⚡ Auto-Refresh (15s)", value=True, key="auto_refresh_toggle")

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
                SELECT b.id, b.name, b.pair, b.direction, b.strategy_type, b.config, t.current_step, t.total_invested, t.avg_entry_price, t.target_tp_price, b.is_active, b.status
                FROM bots b
                LEFT JOIN trades t ON b.id = t.bot_id
                WHERE b.is_active = 1
            """
            df_pos = pd.read_sql(query_all, conn)
            
            # Fetch Physical Positions (df_physical)
            df_physical = pd.read_sql("SELECT pair, side, size, entry_price, datetime(last_checked, 'unixepoch', 'localtime') as updated FROM active_positions", conn)

            # 1. Fetch Virtual Net Position (Sum of Signed Sizes)
            # We need to join trades with bots to get direction

            query_virtual = """
                SELECT t.total_invested, t.avg_entry_price, b.direction 
                FROM trades t
                JOIN bots b ON t.bot_id = b.id
                WHERE b.is_active = 1 AND t.total_invested > 0
            """
            df_virt = pd.read_sql(query_virtual, conn)
            conn.close()
            
            virtual_net_usd = 0.0
            virtual_gross_usd = 0.0
            
            if not df_virt.empty:
                for _, row in df_virt.iterrows():
                    amt_usd = row['total_invested']
                    virtual_gross_usd += amt_usd
                    # Long = +, Short = -
                    if row['direction'] == 'LONG':
                        virtual_net_usd += amt_usd
                    else:
                        virtual_net_usd -= amt_usd
            
            # 2. Fetch Physical Net Position (Exchange Reality)
            # In One-Way Mode, the exchange only holds a NET position per symbol.
            # Using already fetched df_physical
            physical_net_usd = 0.0
            
            if not df_physical.empty:
                for _, row in df_physical.iterrows():
                    val = row['size'] * row['entry_price']
                    # Exchange side: 'buy'/'long' -> +, 'sell'/'short' -> -
                    if str(row['side']).lower() in ['buy', 'long']:
                        physical_net_usd += abs(val)
                    else:
                        physical_net_usd -= abs(val)
            
            # 3. Compare Net Positions (Tolerance $1.0)
            diff_net = abs(virtual_net_usd - physical_net_usd)
            
            if diff_net > 1.0: # Increased tolerance from stricter checks
                st.error(f"🚨 SYSTEM MISMATCH DETECTED")
                st.write(f"Position Mismatch: System {virtual_net_usd:.2f} vs Exchange {physical_net_usd:.2f} (Diff: ${diff_net:.2f})")
            
            # 4. Check Order Health (Fundamental Logic)
            in_trade_count = len(df_pos[df_pos['status'].str.contains('IN TRADE', na=False)])
            total_orders = len(market_orders)
            # Expectation: 2 orders per bot In Trade (TP + Grid)
            expected_orders = in_trade_count * 2

            if total_orders < expected_orders:
                st.warning(f"⚠️ MISSING ORDERS: Found {total_orders}, Expected ~{expected_orders} (2 per active bot).")
            elif total_orders > expected_orders + 2: # +2 buffer
                st.info(f"ℹ️ EXTRA ORDERS DETECTED: Found {total_orders}, Expected ~{expected_orders}.")
            else:
                st.success(f"✅ ORDER HEALTHY: {total_orders} Orders Active")
            
        except Exception as e:
            st.warning(f"Could not calculate sync status: {e}")

        # ... (rest of code) ...
        
        # --- Recent Trade History (Added) ---
        st.subheader("📜 Recent Activity Log")
        try:
            conn = get_connection()
            # Fetch last 10 actions (Reduced from 20 to avoid spam perception)
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
                LIMIT 10
            """
            
            order_health_msg = ""
            order_status_color = "green"
            if total_orders < expected_orders:
                order_health_msg = f"⚠️ MISSING ORDERS: Found {total_orders}, Expected ~{expected_orders} (2 per active bot)."
                order_status_color = "red"
            elif total_orders > expected_orders:
                 order_health_msg = f"⚠️ TOO MANY ORDERS: Found {total_orders}, Expected ~{expected_orders}. (Duplicates?)"
                 order_status_color = "red"
            else:
                order_health_msg = f"✅ ORDER COUNT MATCH: Found {total_orders} (Matches {in_trade_count} active bots)."

            # Display Logic
            col1, col2 = st.columns(2)
            with col1:
                st.metric("Net Exposure (Virtual USD)", f"${virtual_net_usd:,.2f}", help="Sum of USD value of active bots (Longs - Shorts)")
            with col2:
                st.metric("Exchange Net (Physical USD)", f"${physical_net_usd:,.2f}", delta=f"{physical_net_usd-virtual_net_usd:,.2f}", help="Actual USD value of position on exchange")

            # MASTER STATUS INDICATOR
            if diff_net < 10.0 and order_status_color == "green":
                st.success(f"✅ **SYSTEM HEALTHY**: Net positions are synced. {order_health_msg}")
                if virtual_gross_usd > 100 and abs(physical_net_usd) < 20:
                     st.info(f"💡 **PERFECT HEDGE**: Longs and Shorts are balancing out. Exchange Net ~0.")
            else:
                st.error(f"🚨 **SYSTEM MISMATCH DETECTED**")
                st.write(f"1. **Position Mismatch**: System ${virtual_net_usd:,.2f} vs Exchange ${physical_net_usd:,.2f} (Diff: ${diff_net:,.2f})")
                st.write(f"2. **Order Health**: {order_health_msg}")
                st.info("The auto-healing logic in `bot_executor` runs every cycle to fix missing orders.")
                
        except Exception as e:
            st.warning(f"Could not calculate sync status: {e}")

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
            # UX Improvements: Rename Status
            df_pos['status'] = df_pos['status'].replace('Waiting for Signal', '🟢 SCANNING')
            df_pos['status'] = df_pos['status'].replace('IN TRADE', '🔴 IN TRADE')
            df_pos['status'] = df_pos['status'].replace('ENTRY PENDING', '🟡 WAITING FOR FILL')
            
            # Extract Trigger Info & Active Orders
            def extract_info(row):
                res = {'Trigger': 'N/A', 'Orders': '0'}
                try:
                    # 1. Trigger
                    cfg = json.loads(row['config'])
                    mode = cfg.get('mode_price', 0)
                    thresh = float(cfg.get('price_threshold', 0))
                    
                    if row['status'] == '🔴 IN TRADE':
                        res['Trigger'] = "In Trade"
                    elif mode == 1: res['Trigger'] = f"Price > ${thresh:,.2f}"
                    elif mode == 2: res['Trigger'] = f"Price < ${thresh:,.2f}"
                    
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
            
            # Reorder columns for readability
            cols = ['name', 'pair', 'direction', 'status', 'Active Orders', 'Trigger Condition', 'current_step', 'total_invested', 'avg_entry_price']
            # Filter strictly for columns that exist
            existing_cols = [c for c in cols if c in df_pos.columns]
            
            st.dataframe(df_pos[existing_cols], width="stretch")
        else:
            st.info("No active bots.")

    with tab_history:
        # --- Open Orders Section (Live from Exchange) ---
        st.subheader("📋 Live Open Orders (from Exchange)")
        try:
             # Use the ALREADY FETCHED orders from parallel execution
             if market_orders:
                df_orders = pd.DataFrame(market_orders)
                # Keep only relevant columns
                cols_to_keep = ['symbol', 'side', 'type', 'price', 'amount', 'clientOrderId']
                df_orders = df_orders[[c for c in cols_to_keep if c in df_orders.columns]]
                st.dataframe(df_orders, width="stretch")
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
                
                st.dataframe(df_hist, width="stretch", hide_index=True)
            else:
                st.caption("No trade history available yet.")
                
        except Exception as e:
            st.error(f"Error loading trade history: {e}")
