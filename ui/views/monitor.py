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
            # If entry price is abnormally low (e.g. 0.0001 for BTC/XAU), it's likely corrupted data
            # Changed floor to 0.0001 to support cheap altcoins like XRP and SUI
            if curr > 0 and entry > 0.0001: 
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
        if st.button("🔄 Refresh Now", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    # Auto-Refresh Toggle (Default ON) - Capture state here, execute later
    auto_refresh = st.toggle("⚡ Auto-Refresh (15s) [ASync]", value=True, key="auto_refresh_toggle")

    # Detect if the Reconciler / Forensic Wizard is actively in use.
    # Suppressing auto-refresh while the wizard is open prevents the page
    # from reloading mid-operation and wiping the user's in-progress state.
    # We must check if the values are actually truthy, as Streamlit leaves stale keys behind.
    wizard_active = any(bool(st.session_state[k]) for k in st.session_state if k.startswith(("forensic_trades_", "adopt_force_sel_", "trade_sel_")))

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
        
        # --- Mismatch Alert Logic ---
        try:
            # FUNDAMENTAL FIX: Use a fresh connection to bypass thread-local staleness in Streamlit
            db_path = global_config.PATHS['DB_FILE']
            conn_fresh = sqlite3.connect(db_path, timeout=10)
            
            # Fetch Bot Strategies (df_pos)
            query_all = """
                SELECT b.id, b.name, b.pair, b.direction, b.strategy_type, b.config, t.current_step, t.total_invested, t.avg_entry_price, t.target_tp_price, b.is_active, b.status, t.basket_start_time
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

            # --- FUNDAMENTAL FIX: DATA-DRIVEN STATUS ---
            # Derive 'display_status' from current_step to ensure UI is always accurate to engine structure
            def derive_status(row):
                if not row['is_active']: return "⚪ STOPPED"
                
                # If current_step > 0, the engine is maintaining orders (Grid/TP)
                c_step = int(row.get('current_step', 0) if pd.notna(row.get('current_step')) else 0)
                if c_step > 0:
                    # Binance min-notional is 5 USD. Noticeably small positions are dust.
                    if pd.notna(row['total_invested']) and float(row['total_invested']) <= 5.0: 
                        return "🟡 DUST/PARTIAL"
                    return "🔴 IN TRADE"
                return "🟢 SCANNING"

            # Apply fix to df_pos BEFORE rendering
            df_pos['status'] = df_pos.apply(derive_status, axis=1)

            # Group active IN TRADE bots to the top
            df_pos['sort_priority'] = df_pos['status'].apply(lambda x: 1 if "IN TRADE" in x else (2 if "SCANNING" in x else 3))
            df_pos.sort_values(by=['sort_priority', 'name'], ascending=[True, True], inplace=True)

            virtual_qty_by_pair = {}
            physical_qty_by_pair = {}
            pair_prices = {} # For converting qty back to USD for readability
            
            # Helper: normalize symbol to strip colon suffix and slashes (BTC/USDC:USDC → BTCUSDC)
            from engine.exchange_interface import normalize_symbol
            _norm = normalize_symbol

            # 1. Fetch Virtual Positions (grouped by normalized pair)
            # 🚀 AUTHORITATIVE FIX: Use trades.total_invested/avg_entry_price as the virtual qty source.
            # The previous approach summed bot_orders, which excluded 'reset_cleared' fills —
            # real fills where a bot reset its TP cycle without closing the physical position.
            # The trades table is always updated correctly across resets, so it is the true ledger.
            query_virtual = """
                SELECT b.pair, b.direction,
                       t.total_invested, t.avg_entry_price
                FROM bots b
                JOIN trades t ON b.id = t.bot_id
                WHERE b.is_active = 1 AND t.total_invested > 0 AND t.avg_entry_price > 0
            """
            df_virt = pd.read_sql(query_virtual, conn_fresh)
            conn_fresh.close()
            
            if not df_virt.empty:
                for _, row in df_virt.iterrows():
                    # Derive qty from total_invested / avg_entry_price — always authoritative across resets.
                    invested = float(row['total_invested'] or 0)
                    avg_price = float(row['avg_entry_price'] or 0)
                    if invested <= 0 or avg_price <= 0:
                        continue
                    qty_abs = invested / avg_price
                        
                    pair_key = _norm(row['pair'])
                    if pair_key not in pair_prices:
                        pair_prices[pair_key] = avg_price
                    
                    signed_qty = qty_abs if row['direction'] == 'LONG' else -qty_abs
                    
                    virtual_qty_by_pair[pair_key] = virtual_qty_by_pair.get(pair_key, 0.0) + signed_qty
            
            # 2. Physical Positions (grouped by normalized pair)
            
            if not df_physical.empty:
                for _, row in df_physical.iterrows():
                    if pd.notna(row['size']) and pd.notna(row['entry_price']):
                        qty = float(row['size'])
                        price = float(row['entry_price'])
                        val = abs(qty) * price
                        
                        side = str(row['side']).upper().strip()
                        pair_key = _norm(row['pair'])
                        if pair_key not in pair_prices: pair_prices[pair_key] = price
                        
                        signed_usd = val if side in ['BUY', 'LONG'] else -val
                        signed_qty = abs(qty) if side in ['BUY', 'LONG'] else -abs(qty)
                        
                        physical_net_usd += signed_usd
                        physical_qty_by_pair[pair_key] = physical_qty_by_pair.get(pair_key, 0.0) + signed_qty
            
            # 3. Per-pair Net Quantity comparison (Strict 1:1 match, $1.00 USD value tolerance for dust)
            all_pairs = set(list(virtual_qty_by_pair.keys()) + list(physical_qty_by_pair.keys()))
            virtual_net_usd = 0.0 # Recalculate precisely with normalized physical ref_prices
            physical_net_usd = 0.0
            
            for p in all_pairs:
                v_net_qty = virtual_qty_by_pair.get(p, 0.0)
                ph_net_qty = physical_qty_by_pair.get(p, 0.0)
                
                # Check Size Mismatch
                diff_qty = abs(v_net_qty - ph_net_qty)
                ref_price = pair_prices.get(p, 1.0)
                diff_usd = diff_qty * ref_price
                
                # Apply identical USD accumulation
                virtual_net_usd += v_net_qty * ref_price
                physical_net_usd += ph_net_qty * ref_price
                
                if diff_usd > 1.0: # True mismatch greater than $1 of precision dust
                    direction_str = "NET"
                    # Format as USD for human readability but based strictly on qty mismatch
                    v_usd_display = abs(v_net_qty) * ref_price * (1 if v_net_qty >= 0 else -1)
                    ph_usd_display = abs(ph_net_qty) * ref_price * (1 if ph_net_qty >= 0 else -1)
                    mismatched_pairs.append((f"{p} {direction_str}", v_usd_display, ph_usd_display, diff_usd, v_net_qty, ph_net_qty, diff_qty, ref_price))
            
            diff_net = abs(virtual_net_usd - physical_net_usd)
            # (Mismatch and Order Health are displayed in the MASTER STATUS INDICATOR section below)
            
        except Exception as e:
            st.warning(f"Could not calculate sync status: {e}")


        
        # --- Status Indicator & Order Health ---
        try:
            order_health_msg = ""
            order_status_color = "green"
            
            # --- STATUS CONSISTENCY FIX ---
            # A bot expects orders on the exchange if its current_step > 0.
            # Scanning bots (current_step == 0) may have 0 or 1 (the Entry Limit).
            active_bots = df_pos[df_pos['is_active'] == 1]
            total_orders = len(market_orders)
            
            # --- REALITY SYNC: Per-Bot Order Validation ---
            try:
                # 🚀 TRUE PHYSICAL VERIFICATION:
                # Count actual open orders directly from CCXT, ignoring the stale SQLite DB.
                # Map physical order counts by matching API returned prefix strings.
                physical_order_counts = {}
                for o in market_orders:
                    cid = str(o.get('clientOrderId') or '')
                    if cid.startswith('CQB_'):
                        try:
                            # format: CQB_{bot_id}_...
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
                bots_pos_limit = []  # Bots that hit exchange position cap — NOT a mismatch
                for _, bot_row in active_bots.iterrows():
                    bid = int(bot_row['id'])
                    actual_physical = physical_order_counts.get(bid, 0)
                    is_pos_capped = _pos_limit_flags.get(bid, False)
                    
                    # SMART ORDER VALIDATION:
                    # Parse config to know max_steps. If current_step < max_steps, we expect TWO orders (TP + Grid).
                    # If current_step >= max_steps, we expect ONE order (TP).
                    # If current_step == 0 (SCANNING), we expect AT MOST 1 order.
                    try:
                        cfg = json.loads(bot_row.get('config', '{}'))
                        max_steps = int(cfg.get('max_steps', 10))
                        c_step = int(bot_row.get('current_step', 0))
                        
                        if c_step == 0:
                            expected_for_bot = min(1, actual_physical)
                        else:
                            expected_for_bot = 1 if c_step >= max_steps else 2
                        
                        # Only genuinely IN-TRADE bots can be "missing" baseline orders.
                        # A bot with c_step > 0 but total_invested <= 0 is a zombie state (post-reset race condition)
                        # — do NOT alarm on it, as it has no real position and no orders are expected.
                        # Fix 4: also check total_invested > 0 to suppress false positives for zombie bots.
                        bot_invested = float(bot_row.get('total_invested', 0) or 0)
                        if actual_physical == 0 and c_step > 0 and bot_invested > 0:
                            if is_pos_capped:
                                # Bot hit exchange cap — holding position, waiting for TP.
                                # This is EXPECTED behaviour — do not alarm.
                                bots_pos_limit.append(f"{bot_row['name']} (0/{expected_for_bot})")
                            else:
                                bots_with_missing_orders.append(f"{bot_row['name']} (0/{expected_for_bot})")
                        elif actual_physical < expected_for_bot and c_step > 0 and bot_invested > 0:
                            if is_pos_capped:
                                # Has TP but no grid — expected when pos cap hit
                                bots_pos_limit.append(f"{bot_row['name']} ({actual_physical}/{expected_for_bot})")
                            else:
                                # Usually means TP placed but Grid is blocked (e.g. Max Position Limit Reached) -> Yellow
                                bots_with_partial_orders.append(f"{bot_row['name']} ({actual_physical}/{expected_for_bot})")
                    except Exception:
                        # Fallback heuristic
                        if actual_physical == 0 and int(bot_row.get('current_step', 0)) > 0:
                            bots_with_missing_orders.append(bot_row['name'])
                
                # Accurately compute expected total including scanning bots entry dynamics
                expected_total = sum(
                    min(1, physical_order_counts.get(int(row['id']), 0)) if int(row.get('current_step', 0)) == 0 
                    else (1 if int(row.get('current_step', 0)) >= int(json.loads(row.get('config', '{}')).get('max_steps', 10)) else 2) 
                    for _, row in active_bots.iterrows()
                )
            except Exception as e:
                expected_total = total_orders # Fallback
                bots_with_missing_orders = []
                bots_pos_limit = []
            
            if bots_with_missing_orders:
                order_health_msg = f"⚠️ MISSING CRITICAL ORDERS: {', '.join(bots_with_missing_orders)} have 0 open limit orders!"
                order_status_color = "red"
            elif bots_with_partial_orders:
                order_health_msg = f"⚠️ MISSING GRIDS (Max Pos Limit?): {', '.join(bots_with_partial_orders)} missing expected grid ladders."
                order_status_color = "orange"
                # Downgrade visually to yellow for bots experiencing partial grid errors
                for pd_idx, bot_row in df_pos.iterrows():
                    for name_str in bots_with_partial_orders:
                        if bot_row['name'] in name_str and '🔴 IN TRADE' in str(bot_row['status']):
                            df_pos.at[pd_idx, 'status'] = str(bot_row['status']).replace('🔴', '🟡')
            elif bots_pos_limit:
                # All missing grids are accounted for by the exchange position cap — this is healthy behaviour.
                order_health_msg = f"🚫 POS LIMIT: {', '.join(bots_pos_limit)} at exchange max notional. Holding position, waiting for TP."
                order_status_color = "green"  # Not an error — deliberate cap
            elif total_orders < expected_total:
                order_health_msg = f"⚠️ EXCHANGE LAG: Found {total_orders}, Expected {expected_total} (Syncing...)."
                order_status_color = "orange" # Warning but not critical mismatch
            elif total_orders > expected_total:
                 order_health_msg = f"⚠️ STRAY ORDERS: Found {total_orders}, Expected only {expected_total}."
                 order_status_color = "red"
            else:
                order_health_msg = f"✅ ORDERS SYNCED: {total_orders} active orders."

            # --- SYSTEM MISMATCH / RECOVERY SHORTCUT ---
            has_mismatch = len(mismatched_pairs) > 0
            if has_mismatch:
                st.error("🚨 DATABASE DESYNC: Binance physical exchange positions drastically differ from the bots' internal ledgers.")

            # Display Metrics
            col1, col2 = st.columns(2)
            with col1:
                st.metric("Net Exposure (Virtual USD)", f"${virtual_net_usd:,.2f}")
            with col2:
                st.metric("Exchange Net (Physical USD)", f"${physical_net_usd:,.2f}", delta=f"{physical_net_usd-virtual_net_usd:,.2f}")

                # MASTER STATUS INDICATOR
                
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
                    st.success(f"✅ **SYSTEM HEALTHY**: Contracts and orders are perfectly aligned. {order_health_msg}")
                elif is_startup_grace and order_status_color == "red":
                    st.warning(f"🟡 **SYSTEM STARTUP**: Waiting for initial sync/orders (Grace Period)...")
                    st.caption(f"Reason: {order_health_msg}")
                else:
                    st.error(f"🚨 **SYSTEM MISMATCH DETECTED**")
                    if mismatched_pairs:
                        # Don't show generic virtual_net_usd if it's just a position issue
                        for row_mp in mismatched_pairs:
                            mp_pair, mp_virt, mp_phys, mp_diff = row_mp[0], row_mp[1], row_mp[2], row_mp[3]
                            mp_vqty = row_mp[4] if len(row_mp) > 4 else None
                            mp_pqty = row_mp[5] if len(row_mp) > 5 else None
                            mp_dqty = row_mp[6] if len(row_mp) > 6 else None
                            mp_price = row_mp[7] if len(row_mp) > 7 else None
                            # Format qty display
                            qty_str = ""
                            if mp_vqty is not None and mp_pqty is not None:
                                qty_str = f" | Qty: system={mp_vqty:+.4f} exchange={mp_pqty:+.4f} diff={mp_dqty:.4f}"
                            # Check if any bot in this pair is at the exchange position cap.
                            # If so, the difference is EXPECTED — not an error or a partial fill.
                            _pair_root = mp_pair.split(' ')[0]  # e.g. 'BTC/USDC' from 'BTC/USDC NET'
                            _pair_pos_capped = any(
                                _pos_limit_flags.get(int(row['id']), False)
                                for _, row in active_bots.iterrows()
                                if str(row.get('pair', '')).startswith(_pair_root.split('/')[0])
                            )
                            if _pair_pos_capped:
                                # Truthful label — the exchange capped the position at this size
                                st.info(f"   🚫 **{mp_pair}**: System ${mp_virt:,.2f} vs Exchange ${mp_phys:,.2f} (Diff: ${mp_diff:,.2f}){qty_str} — *POS LIMIT: Exchange capped. Holding position until TP.*")
                            elif mp_dqty is not None and mp_dqty > 0.001:
                                # Real quantity gap — show qty mismatch prominently
                                st.warning(f"   ⚠️ **{mp_pair}**: System ${mp_virt:,.2f} vs Exchange ${mp_phys:,.2f} (Diff: ${mp_diff:,.2f}){qty_str}")
                            else:
                                # USD difference only (price basis / mark-to-market) — no real qty gap
                                st.write(f"   • **{mp_pair}**: System ${mp_virt:,.2f} vs Exchange ${mp_phys:,.2f} (Diff: ${mp_diff:,.2f}){qty_str} *(price basis only)*")
                    else:
                        st.write(f"**Position State**: Perfect Quantity Match")
                        
                    if order_health_msg:
                        st.write(f"**Order Health**: {order_health_msg}")

                # --- MANUAL LINK RECOVERY TOOL (Always available if mismatch exists) ---
                if has_mismatch:
                    st.divider()
                    st.markdown("### 🧙‍♂️ Manual Link Recovery Tool")
                    st.caption("Accurately restore bot links for manual trades or disconnected assets. These discrepancies are REAL measurements comparing the Database Virtual Contracts vs Binance's Physical Contracts.")
                    
                    # 1. Detect Rogue Positions via Reconciler (Explicitly)
                    reconciler = StateReconciler(exchanges={'future': get_exchange_instance('future'), 'spot': get_exchange_instance('spot')})
                    bot_states = reconciler.get_bot_states()
                    success, all_positions = reconciler.fetch_all_exchange_positions()
                    all_pairs = list(set([b.pair for b in bot_states]))
                    # Filter only pairs with mismatches to save time (stripping the " NET" suffix)
                    m_pairs = [mp[0].split(' ')[0] for mp in mismatched_pairs]
                    all_orders_recon = reconciler.fetch_all_exchange_orders(m_pairs)
                    
                    recon_results = reconciler.resolve_net_mismatch(bot_states, all_positions, all_orders_recon, force_adoption=False)
                    rogue_results = [r for r in recon_results if r.action_taken in (ReconciliationAction.ROGUE_POSITION, ReconciliationAction.REQUIRE_MANUAL) or r.requires_manual_intervention]

                    # Pre-calculate active bots available for adoption
                    # Derive candidates from df_pos which has 12+ columns
                    candidates_df = df_pos[df_pos['status'].str.contains('SCANNING|IN TRADE', case=False, na=False)]
                    scanning_bot_options = {f"{row['name']} (#{row['id']}) [{row['pair']}] - {row['status']}": row['id'] for _, row in candidates_df.iterrows()}

                    if rogue_results:
                        for idx, rogue in enumerate(rogue_results):
                            with st.expander(f"🔴 Rogue Position: {rogue.pair} (Issue #{idx+1})", expanded=True):
                                st.write(f"**Details:** {rogue.details}")
                                st.info("Resolution Options: Adopt mathematically if this is a known bot gap, or market close to flatten the stray physical quantity.")
                                
                                res_col1, res_col2 = st.columns(2)
                                
                                with res_col1:
                                    if scanning_bot_options:
                                        st.markdown("**(Bypass) Force Direct Math Adoption:**")
                                        selected_target_force = st.selectbox("Link to Bot without Proof:", list(scanning_bot_options.keys()), key=f"adopt_force_sel_{rogue.pair}_{idx}")
                                        if st.button("🔗 Adopt (Force Synchronize)", key=f"adopt_force_btn_{rogue.pair}_{idx}", type="primary"):
                                            pair_norm = _norm(rogue.pair)
                                            pos_data = all_positions.get(pair_norm, [])
                                            if pos_data:
                                                total_size = sum(p.size for p in pos_data)
                                                avg_entry = sum(p.size * p.entry_price for p in pos_data) / total_size if total_size > 0 else 0
                                                side = pos_data[0].side
                                                bot_id_ad = scanning_bot_options[selected_target_force]
                                                
                                                success, msg = import_position_from_exchange(bot_id_ad, rogue.pair, total_size, avg_entry, side)
                                                if success:
                                                    st.success(f"✅ Bot #{bot_id_ad} has aggressively adopted the mathematical gap!")
                                                    time.sleep(1)
                                                    st.rerun()
                                                else:
                                                    st.error(f"❌ Force Adoption Failed: {msg}")
                                    else:
                                        st.warning("No 'Scanning' bots available to adopt this position.")
                                
                                with res_col2:
                                    st.markdown("**Terminate Physical Footprint:**")
                                    if st.button("🛑 Market Close", key=f"close_btn_{rogue.pair}_{idx}"):
                                        try:
                                            ex = get_exchange_instance('future')
                                            pair_norm = _norm(rogue.pair)
                                            pos_data = all_positions.get(pair_norm, [])
                                            if pos_data:
                                                for p in pos_data:
                                                    close_side = 'sell' if p.side.upper() == 'LONG' else 'buy'
                                                    ex.create_order(p.symbol, 'market', close_side, p.size)
                                                st.success(f"✅ Market close orders sent!")
                                                if f"forensic_trades_{rogue.pair}_{idx}" in st.session_state: del st.session_state[f"forensic_trades_{rogue.pair}_{idx}"]
                                                time.sleep(1)
                                                st.rerun()
                                        except Exception as e:
                                            st.error(f"Failed to close: {e}")

                                st.divider()
                                st.caption("Advanced: Forensic Search (Link Specific Fills)")
                                # Forensic Search
                                if st.button(f"🔍 Scan Recent Fills ({rogue.pair})", key=f"forensic_btn_{rogue.pair}_{idx}"):
                                     ex = get_exchange_instance('future')
                                     trades = ex.fetch_my_trades(rogue.pair, limit=10)
                                     if trades:
                                         st.session_state[f"forensic_trades_{rogue.pair}_{idx}"] = trades
                                     else:
                                         st.warning("No recent fills found on exchange for this pair.")
                                
                                if f"forensic_trades_{rogue.pair}_{idx}" in st.session_state:
                                     trades = st.session_state[f"forensic_trades_{rogue.pair}_{idx}"]
                                     # Let user select a trade
                                     trade_options = {
                                         f"{t['side'].upper()} {t['amount']} @ {t['price']} (ID:{t['orderId']})": t 
                                         for t in trades
                                     }
                                     selected_trade_label = st.selectbox("Select Evidence Fill:", list(trade_options.keys()), key=f"trade_sel_{rogue.pair}_{idx}")
                                     selected_trade = trade_options[selected_trade_label]
                                     st.info("Forensic Fill isolated. Contact developer to implement specific signature re-linking.")
                    
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
                        b_m = cfg['mode_boll']
                        triggers.append("BOLL(Outside)")
                    if cfg.get('mode_stoch'):
                        s_m = int(cfg['mode_stoch'] or 0)
                        triggers.append(f"Stoch({'Oversold' if s_m==1 else 'Overbought'})")
                    
                    # 3. Patterns and others
                    for i in range(1, 5):
                        if cfg.get(f'pat_{i}_mode'):
                            p_m = int(cfg[f'pat_{i}_mode'] or 0)
                            p_c = int(cfg.get(f'pat_{i}_count', 1) or 1)
                            p_s = cfg.get(f'pat_{i}_source', 'Price')
                            triggers.append(f"{p_s}Pat({p_c}x {'Up' if p_m==1 else 'Dn'})")

                    desc_trigger = " + ".join(triggers) if triggers else "N/A"

                    ee_status = ""
                    # 🚀 STATUS INDEPENDENCE: Don't rely on string matching '🔴 IN TRADE' 
                    # as pandas apply order might not have finalized the column yet.
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
                                # Derive the % decay relative to the original spread
                                direction_up = row.get('direction', 'LONG').upper() == 'LONG'
                                orig_spread = abs(tp_p - avg_p)
                                decayed_spread = abs(decayed_tp - avg_p)
                                if orig_spread > 0:
                                    ee_pc = (1 - decayed_spread / orig_spread) * 100
                                    ee_pc = max(0.0, min(ee_pc, 100.0) if not cfg.get('EEAllowLoss', False) else ee_pc)
                                    ee_status = f" [EE: -{ee_pc:.1f}% → TP {decayed_tp:,.4f}]"
                        except Exception:
                            # Fallback: simple interval-based display only
                            duration_mins = (_t.time() - row.get('basket_start_time', _t.time())) / 60.0
                            interval_mins = float(cfg.get('DecayIntervalMins', 60.0))
                            decay_per_interval = float(cfg.get('DecayPercentPerInterval', 0.0))
                            if decay_per_interval > 0 and interval_mins > 0:
                                ee_pc = (duration_mins / interval_mins) * decay_per_interval
                                if not cfg.get('EEAllowLoss', False):
                                    ee_pc = min(ee_pc, 100.0)
                                if ee_pc > 0:
                                    ee_status = f" [EE: -{ee_pc:.1f}%]"
                    hedge_status = ""
                    if is_in_trade and cfg.get('UseStepHedge', False) and int(row.get('current_step', 0)) >= int(cfg.get('HedgeStartStep', 99)):
                        hedge_status = " 🛡️ [HEDGED]"

                    if is_in_trade:
                        res['Trigger'] = f"In Trade{ee_status}{hedge_status} ({desc_trigger})"
                    else:
                        res['Trigger'] = desc_trigger
                    
                    # 2. Active Orders
                    try:
                        bot_id = int(row['id'])
                    except (ValueError, TypeError):
                        bot_id = row['id']
                        
                    # Filter market_orders (deduplicated)
                    my_orders = [o for o in market_orders if str(o.get('clientOrderId') or '').startswith(f"CQB_{bot_id}_")]

                    if my_orders:
                        detailed = []
                        is_hedged = False
                        for o in my_orders:
                            cid = o.get('clientOrderId', '')
                            price_val = float(o.get('price', 0.0) or 0.0)
                            if 'TP' in cid: 
                                detailed.append('TP')
                                res['TP_Price'] = price_val
                            elif 'GRID' in cid: 
                                detailed.append('GRID')
                                res['Grid_Price'] = price_val
                                res['Grid_Amount'] = float(o.get('origQty', o.get('amount', 0.0)) or 0.0)
                            elif 'HEDGE' in cid:
                                detailed.append('HEDGE')
                                is_hedged = True
                            elif 'ENTRY' in cid: detailed.append('ENTRY')
                            else: detailed.append('LIMIT')
                        
                        count_str = f"{len(my_orders)} " + (f"({', '.join(detailed)})" if detailed else "")
                        res['Orders'] = count_str
                        if is_hedged:
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
                if x <= 0: return "-"
                if x < 1.0:    return f"${x:.4f}"   # sub-dollar assets like SUI, DOGE
                if x < 10.0:   return f"${x:.3f}"   # low-dollar assets
                return f"${x:,.2f}"                 # normal assets
            df_pos['Active TP'] = info_df['TP_Price'].apply(format_tp_price)
            df_pos['Next Grid'] = info_df.apply(
                lambda row: f"{row['Grid_Amount']} @ {format_tp_price(row['Grid_Price'])}" if row.get('Grid_Price', 0) > 0 else "-",
                axis=1
            )
            
            # --- PERFORMANCE MATRIX (Enterprise Batch View) ---
            # Calculate dense metrics for 20+ bots
            st.markdown("### ⚡ Batch Performance Matrix")
            try:
                matrix_df = df_pos.copy()
                
                # 1. PnL Estimate Column
                def est_profit(row):
                    if row['total_invested'] > 0 and row['avg_entry_price'] > 0 and row['target_tp_price'] > 0:
                        ee_full_decay = False
                        if 'Trigger Condition' in row and isinstance(row['Trigger Condition'], str):
                            if '-100.0%]' in row['Trigger Condition'] or '-100%]' in row['Trigger Condition']:
                                ee_full_decay = True
                        if ee_full_decay:
                            return "$0.00 (Break-Even)"
                        
                        qty = row['total_invested'] / row['avg_entry_price']
                        if row['direction'] == 'LONG':
                            profit = (row['target_tp_price'] - row['avg_entry_price']) * qty
                        else:
                            profit = (row['avg_entry_price'] - row['target_tp_price']) * qty
                        try:
                            # ROI% = profit / notional invested (position size, not margin)
                            roi_pct = (profit / row['total_invested']) * 100
                            # ROE% = profit / margin (notional / leverage) — matches Binance UI
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
                cols_matrix = ['name', 'pair', 'direction', 'current_step', 'total_invested', 'Active TP', 'Next Grid', 'Expected Profit', 'Time in Trade', 'status']
                matrix_df = matrix_df[[c for c in cols_matrix if c in matrix_df.columns]]
                
                matrix_df['total_invested'] = matrix_df['total_invested'].apply(lambda x: f"${x:,.2f}" if x > 0 else "-")
                # Expected Profit already formatted above
                
                st.dataframe(matrix_df, width="stretch")
            except Exception as e:
                st.warning(f"Failed to render Batch Matrix: {e}")
                
            st.divider()
            
            if not df_pos.empty:
                scanning_bots = df_pos[df_pos['status'].str.contains('SCANNING', na=False)]
                if not scanning_bots.empty:
                    st.markdown("#### 🎯 Entry Trigger Proximity (Scanning Bots)")
                    st.caption("Shows how close each scanning bot is to its entry signal thresholds.")
                    
                    for _, sbot in scanning_bots.iterrows():
                        try:
                            # Fetch config directly from DB — it's not in df_pos
                            conn_trig = get_connection()
                            _row = conn_trig.execute("SELECT config FROM bots WHERE id=?", (sbot['id'],)).fetchone()
                            conn_trig.close()
                            conf = json.loads(_row[0]) if _row else {}
                            pair_s = sbot['pair']
                            mkt_type = conf.get('market_type', 'future')
                            
                            # Fetch quick OHLCV using the cached fetcher (no exchange_interface needed)
                            _ohlcv = fetch_ohlcv_cached(mkt_type, pair_s, '15m')
                            if not _ohlcv or len(_ohlcv) < 20:
                                continue
                                
                            _df = pd.DataFrame(_ohlcv, columns=['ts','open','high','low','close','vol'])
                            _close = _df['close']
                            
                            indicators = []
                            
                            # RSI
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
                            
                            # CCI
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

                            # Stochastic
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

                            # Price Threshold
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
                            
                            # Bollinger Bands
                            mode_boll = int(conf.get('mode_boll', 0))
                            if mode_boll > 0:
                                from engine.indicators import bollinger_bands
                                boll_period = int(conf.get('boll_period', conf.get('bollinger_length', 20)))
                                boll_dev = float(conf.get('boll_dev', conf.get('bollinger_std', 2.0)))
                                upper, middle, lower = bollinger_bands(_close, boll_period, boll_dev)
                                curr_p = float(_close.iloc[-1])
                                b_up = float(upper.iloc[-1])
                                b_dn = float(lower.iloc[-1])

                                if mode_boll == 1:  # Outside Lower — price below lower band
                                    triggered = curr_p < b_dn
                                    dist = abs(curr_p - b_dn) / curr_p * 100
                                    badge = "✅ Triggered" if triggered else ("🔥 Near" if dist <= 1 else ("👀 In Sight" if dist <= 3 else "🌑 Not Ready"))
                                    indicators.append(f"BOLL: Outside Lower (Dist: {dist:.2f}%) — {badge}")
                                elif mode_boll == 2:  # Outside Upper — price above upper band
                                    triggered = curr_p > b_up
                                    dist = abs(curr_p - b_up) / curr_p * 100
                                    badge = "✅ Triggered" if triggered else ("🔥 Near" if dist <= 1 else ("👀 In Sight" if dist <= 3 else "🌑 Not Ready"))
                                    indicators.append(f"BOLL: Outside Upper (Dist: {dist:.2f}%) — {badge}")
                                    
                            # ATR Percent/Expansion
                            mode_atrp = int(conf.get('mode_atrp', 0))
                            if mode_atrp > 0:
                                indicators.append(f"ATR % Active (Target > {conf.get('atrp_level', 0)}%)")
                            mode_atre = int(conf.get('mode_atre', 0))
                            if mode_atre > 0:
                                indicators.append(f"ATR Exp Active (Mult {conf.get('atre_mult', 0)})")
                            
                            # Patterns
                            patterns = []
                            if int(conf.get('pat_1_mode', 0)) > 0: patterns.append("Pat 1")
                            if int(conf.get('pat_2_mode', 0)) > 0: patterns.append("Pat 2")
                            if int(conf.get('pat_3_mode', 0)) > 0: patterns.append("Pat 3")
                            if int(conf.get('pat_4_mode', 0)) > 0: patterns.append("Pat 4")
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

    # --- Auto-Refresh Upgrade ---
    if auto_refresh and not wizard_active:
        from streamlit_autorefresh import st_autorefresh
        from datetime import datetime
        # 🚀 UI PERFORMANCE BATCHING: Now that rendering is async/cached, we can safely refresh every 15s
        refresh_count = st_autorefresh(interval=15000, limit=None, key="monitor_autorefresh")
        
        # When the auto-refresh triggers a rerun, the counter increments.
        # We explicitly clear the cache to mirror the "Refresh Now" button's behavior.
        if "last_autorefresh_count" not in st.session_state:
            st.session_state.last_autorefresh_count = refresh_count
        elif refresh_count != st.session_state.last_autorefresh_count:
            st.session_state.last_autorefresh_count = refresh_count
            st.cache_data.clear()

        st.caption(f"⏱️ Last Updated: {datetime.now().strftime('%H:%M:%S')} (Auto-refreshes every 15s)")
    elif wizard_active:
        st.warning(
            "⏸ **Auto-Refresh Paused** — Reconciler wizard is active. "
            "Refreshing now would wipe your in-progress recovery work. "
            "Complete or dismiss the wizard to resume automatic updates."
        )
    else:
        st.caption("ℹ️ Tip: Auto-Refresh is OFF. Toggle it in the sidebar for real-time updates.")
