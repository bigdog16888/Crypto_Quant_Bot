import streamlit as st
import time
import pandas as pd
import plotly.graph_objects as go
import ccxt
import json
from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection, get_bots_by_order_id


import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import config as global_config

# --- Performance Caching Wrappers ---
@st.cache_resource(ttl=3600, show_spinner=False)
def get_exchange_instance(market_type):
    """Singleton provider for ExchangeInterface to reuse connections."""
    return ExchangeInterface(market_type=market_type, validate=False)

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
        return ex.exchange.fetch_positions()
    except Exception: return []

@st.cache_data(ttl=10, show_spinner=False)
def fetch_open_orders_cached(market_type, symbol):
    try:
        ex = get_exchange_instance(market_type)
        return ex.fetch_open_orders(symbol)
    except Exception: return []

@st.cache_data(ttl=30, show_spinner=False)
def fetch_balance_cached(market_type):
    try:
        ex = get_exchange_instance(market_type)
        return ex.fetch_balance()
    except Exception: return {}
# ------------------------------------


def render_monitor_view():
    st.header("📊 Live Market Monitor")
    
    # --- Command Center (Health Dashboard) ---
    # Global CSS in app.py handles .metric-card styling
    
    # Fetch real stats
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
                # Use market type from config (spot vs future)
                ex_global = get_exchange_instance(market_type=global_config.MARKET_TYPE)
                # Optimized fetch
                for sym in active_symbols:
                    ticker = ex_global.exchange.fetch_ticker(sym)
                    price_map[sym] = float(ticker['last'])
            except Exception:
                pass
        
        for trade in active_trades:
            # trade: invested, entry, pair, direction
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
            
            if fut_data and 'info' in fut_data:
                info = fut_data['info']
                # Try standard Total Wallet Balance (Binance specific)
                if 'totalWalletBalance' in info:
                    futures_balance = float(info['totalWalletBalance'])
                # Fallback to USDT total if specific key missing
                elif 'USDT' in fut_data:
                    futures_balance = float(fut_data['USDT'].get('total', 0))
                
                # Extract assets for breakdown
                if 'assets' in info:
                    for asset in info['assets']:
                        wb = float(asset.get('walletBalance', 0))
                        u_pnl = float(asset.get('unrealizedProfit', 0))
                        if wb > 0 or u_pnl != 0:
                            assets_breakdown.append({
                                'Type': 'Futures',
                                'Asset': asset.get('asset'),
                                'Balance': wb,
                                'Unrealized PnL': u_pnl,
                                'Equity': wb + u_pnl
                            })
        except Exception: pass

        # --- B. Fetch Spot Balance ---
        # Get active market types from DB to see if we even need spot
        try:
            conn_mt = get_connection()
            cur_mt = conn_mt.cursor()
            cur_mt.execute("SELECT config FROM bots WHERE is_active = 1")
            active_configs = cur_mt.fetchall()
            conn_mt.close()
            
            needs_spot = False
            for cfg in active_configs:
                try:
                    c_dict = json.loads(cfg[0]) if cfg[0] else {}
                    if c_dict.get('market_type') == 'spot':
                        needs_spot = True
                        break
                except: pass
            
            # Skip spot check if we are strictly in Futures mode (common testnet setup)
            # This prevents -2015 errors when using Futures-only keys
            if needs_spot and global_config.MARKET_TYPE != 'future':
                spot_data = fetch_balance_cached('spot')
                if spot_data and 'total' in spot_data and isinstance(spot_data['total'], dict):
                    for asset, amount in spot_data['total'].items():
                        if amount > 0:
                            val = amount if asset in ['USDT', 'USDC', 'DAI', 'BUSD'] else 0.0
                            if val > 0: spot_balance += val
                            assets_breakdown.append({
                                'Type': 'Spot', 'Asset': asset, 'Balance': amount,
                                'Unrealized PnL': 0.0, 'Equity': val
                            })
        except Exception: pass

        # Total Calculation
        total_equity = futures_balance + spot_balance + global_pnl_usd # Global PnL is active trade PnL from DB logic
        
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


    
    # Display Metrics Grid (Top Command Center)
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.markdown(f"""<div class="metric-card"><div class="metric-label">Total Equity</div><div class="metric-value">${total_equity:,.2f}</div></div>""", unsafe_allow_html=True)
    with m2:
        st.markdown(f"""<div class="metric-card"><div class="metric-label">Futures Balance</div><div class="metric-value">${futures_balance:,.2f}</div></div>""", unsafe_allow_html=True)
    with m3:
        # Global PnL
        color = "#3fb950" if global_pnl_usd >= 0 else "#f85149"
        sign = "+" if global_pnl_usd >= 0 else ""
        st.markdown(f"""<div class="metric-card"><div class="metric-label">Active PnL (Unrealized)</div><div class="metric-value" style="color:{color}">{sign}${global_pnl_usd:,.2f}</div></div>""", unsafe_allow_html=True)
    with m4:
        # Spot or Invested
        st.markdown(f"""<div class="metric-card"><div class="metric-label">Active Exposure</div><div class="metric-value">${total_invested_db:,.2f}</div></div>""", unsafe_allow_html=True)

    # Asset Breakdown Expander
    if assets_breakdown:
        with st.expander("💰 Detailed Asset Breakdown"):
            df_assets = pd.DataFrame(assets_breakdown)
            st.dataframe(
                df_assets, 
                column_config={
                    "Balance": st.column_config.NumberColumn(format="%.4f"),
                    "Unrealized PnL": st.column_config.NumberColumn(format="$%.2f"),
                    "Equity": st.column_config.NumberColumn(format="$%.2f"),
                },
                width='stretch',
                hide_index=True
            )


    st.divider()
    
    # --- 1. System Status Ribbon ---
    # Global CSS in app.py handles .status-ribbon styling
    
    # Simple, high-performance status bar
    try:
        conn_h = get_connection()
        cur_h = conn_h.cursor()
        cur_h.execute("SELECT COUNT(*) FROM bots WHERE is_active = 1")
        act_count = cur_h.fetchone()[0]
        cur_h.execute("SELECT action, symbol, price FROM trade_history ORDER BY id DESC LIMIT 1")
        last_h = cur_h.fetchone()
        
        # Check DB vs Exchange sync status
        sync_status = "synced"
        sync_class = "sync-ok"
        try:
            # Quick check if exchange is accessible
            ex_check = get_exchange_instance(market_type=global_config.MARKET_TYPE)
            _ = ex_check.fetch_ticker(list(global_config.ALLOWED_SYMBOLS)[0]) if global_config.ALLOWED_SYMBOLS else None
        except Exception:
            sync_status = "exchange_lag"
            sync_class = "sync-warn"
        
        conn_h.close()
        
        last_act_str = f"{last_h[0]}: {last_h[1]} @ {last_h[2]:,.2f}" if last_h else "NO RECENT ACTIVITY"
        st.markdown(f"""<div class="status-ribbon">
            <span>CORE ENGINE: <span class="status-ok">ONLINE</span><span class="sync-status {sync_class}">{sync_status.upper()}</span></span>
            <span>ACTIVE BOTS: {act_count}</span>
            <span>LAST ACTION: {last_act_str}</span>
        </div>""", unsafe_allow_html=True)
    except: pass


    # --- Control Bar ---
    # Fetch active bots for focus dropdown
    conn_b = get_connection()
    cur_b = conn_b.cursor()
    cur_b.execute("SELECT id, name, pair FROM bots WHERE is_active = 1")
    active_bots_list = cur_b.fetchall()
    conn_b.close()
    
    bot_options = ["None (Symbol View)"] + [f"{b[1]} ({b[2]})" for b in active_bots_list]

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        c1a, c1b = st.columns(2)
        with c1a:
            selected_bot_str = st.selectbox("Focus Bot", bot_options, index=0, key="monitor_bot_select")
        
        # Determine symbol based on bot selection or fallback
        target_symbol_list = list(global_config.ALLOWED_SYMBOLS)
        selected_bot_id = None
        
        if selected_bot_str != "None (Symbol View)":
            # Extract bot name
            bot_name_sel = selected_bot_str.split(" (")[0]
            for b in active_bots_list:
                if b[1] == bot_name_sel:
                    selected_bot_id = b[0]
                    # Override list to focus on this symbol
                    target_symbol_list = [b[2]] + [s for s in target_symbol_list if s != b[2]]
                    break
        else:
            # Add all active pairs to top
            active_pairs = list(set([b[2] for b in active_bots_list]))
            target_symbol_list = list(dict.fromkeys(active_pairs + target_symbol_list))

        with c1b:
            symbol = st.selectbox("Symbol", target_symbol_list, key="monitor_symbol")

    with col2:
        timeframe = st.selectbox("Timeframe", ["1m", "5m", "15m", "30m", "1h", "4h", "1d"], index=4, key="monitor_tf")
    with col3:
        st.write("") # Spacer
        col3_a, col3_b = st.columns([1, 1])
        with col3_a:
            auto_refresh = st.checkbox("Auto (30s)", value=True, key="monitor_autorefresh")
        with col3_b:
            if st.button("🔄 Refresh"):
                st.cache_data.clear()
                st.rerun()
                
    if auto_refresh:
        # Non-blocking sleep mechanism using session state? 
        # Streamlit execution is top-down. time.sleep blocks rendering.
        # But st.rerun() restarts it.
        # We need a placeholder for countdown?
        # Simple approach: Just sleep at the END of the script? 
        # No, putting it here pauses execution of the REST of the page?
        # Putting it at the very end is safer.
        pass
    
    # --- Fetch Data ---
    try:
        # Initialize exchange with correct market type from config
        try:
            # Use cached fetch for performance
            ohlcv = fetch_ohlcv_cached(global_config.MARKET_TYPE, symbol, timeframe)
        except Exception as e:
            st.error(f"🔌 **API Connection Failed**: {e}")
            st.warning("Please check your API keys in the `.env` file.")
            ohlcv = []
        except Exception as e:
            st.error(f"Exchange Error: {e}")
            ohlcv = []
        
        if ohlcv and len(ohlcv) > 0:
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            # Technical Indicator: 20-period SMA
            df['sma_20'] = df['close'].rolling(window=20).mean()
            
            # --- Create Interactive Plotly Chart ---
            fig = go.Figure()
            
            # Candlestick Trace
            fig.add_trace(go.Candlestick(
                x=df['timestamp'],
                open=df['open'],
                high=df['high'],
                low=df['low'],
                close=df['close'],
                name=symbol
            ))
            
            # SMA Trace
            fig.add_trace(go.Scatter(
                x=df['timestamp'], 
                y=df['sma_20'], 
                name='SMA 20', 
                line=dict(color='orange', width=1.5),
                opacity=0.7
            ))
            
            # --- Visual Overlays for Active Bot ---
            active_bot_data = None
            try:
                conn = get_connection()
                cur = conn.cursor()
                
                # Enhanced query: Get grid_price from bot_orders (subquery)
                if selected_bot_id:
                    query = """
                        SELECT b.name, t.avg_entry_price, t.target_tp_price, t.current_step, b.direction, t.total_invested,
                               (SELECT price FROM bot_orders WHERE bot_id = b.id AND order_type='grid' AND status='open' ORDER BY id DESC LIMIT 1) as grid_price
                        FROM bots b
                        JOIN trades t ON b.id = t.bot_id
                        WHERE b.id = ?
                    """
                    cur.execute(query, (selected_bot_id,))
                else:
                    # Fallback to symbol based (pick highest investment)
                    query = """
                        SELECT b.name, t.avg_entry_price, t.target_tp_price, t.current_step, b.direction, t.total_invested,
                               (SELECT price FROM bot_orders WHERE bot_id = b.id AND order_type='grid' AND status='open' ORDER BY id DESC LIMIT 1) as grid_price
                        FROM bots b
                        JOIN trades t ON b.id = t.bot_id
                        WHERE b.pair = ? AND b.is_active = 1
                        ORDER BY t.total_invested DESC
                        LIMIT 1
                    """
                    cur.execute(query, (symbol,))
                
                active_bot_data = cur.fetchone()
                conn.close()
                
                if active_bot_data:
                    bot_name, entry, tp, step, direction, invested, grid_price = active_bot_data
                    
                    if entry > 0:
                        color_entry = "blue"
                        color_tp = "green" if direction == "LONG" else "red"
                        
                        fig.add_hline(y=entry, line_dash="solid", line_color=color_entry, annotation_text=f"Entry ({bot_name})")
                        if tp > 0:
                             fig.add_hline(y=tp, line_dash="dash", line_color=color_tp, annotation_text="Take Profit")
                        
                        if grid_price and grid_price > 0:
                             fig.add_hline(y=grid_price, line_dash="dot", line_color="gray", annotation_text="Grid (NO)")
                        
                        st.info(f"🤖 **Bot '{bot_name}' active**. Step: {step} | Entry: ${entry:,.2f} | TP: ${tp:,.2f} | NO: ${grid_price if grid_price else 0:,.2f}")
                
            except Exception as e:
                st.warning(f"Could not load bot levels: {e}")

            # Layout Improvements
            fig.update_layout(
                title=f"{symbol} - {timeframe.upper()} Live Chart",
                yaxis_title="Price (USDT)",
                xaxis_title="Time",
                template="plotly_white",
                height=600,
                xaxis_rangeslider_visible=True, # Interactive Zoom
                margin=dict(l=10, r=10, t=40, b=10),
                paper_bgcolor='rgba(0,0,0,0)',
                plot_bgcolor='rgba(0,0,0,0)',
                font=dict(color='#1f2328')
            )
            
            # Range Selector Buttons
            fig.update_xaxes(
                rangeselector=dict(
                    buttons=list([
                        dict(count=1, label="1h", step="hour", stepmode="backward"),
                        dict(count=6, label="6h", step="hour", stepmode="backward"),
                        dict(count=1, label="1d", step="day", stepmode="backward"),
                        dict(step="all")
                    ])
                )
            )
            
            st.plotly_chart(fig, use_container_width=True)
            
            # --- PnL and Stats Area ---
            latest = df.iloc[-1]
            prev_close = df.iloc[-2]['close'] if len(df) > 1 else latest['close']
            
            st.subheader("📊 Performance & Metrics")
            c1, c2, c3, c4 = st.columns(4)
            
            with c1: 
                st.metric("Latest Price", f"{latest['close']:,.2f}", f"{latest['close'] - prev_close:,.2f}")
            
            with c2: 
                st.metric("24h Volume", f"{latest['volume']:,.0f}")
            
            # PnL Calculation
            with c3:
                if active_bot_data and active_bot_data[1] > 0:
                    bot_name, entry, tp, step, direction, invested, grid_price = active_bot_data
                    price_diff = latest['close'] - entry if direction == "LONG" else entry - latest['close']
                    pnl_pct = (price_diff / entry) * 100
                    pnl_usd = (invested * pnl_pct / 100) if invested > 0 else 0.0
                    
                    st.metric("Unrealized PnL", f"${pnl_usd:,.2f}", f"{pnl_pct:.2f}%")
                else:
                    st.metric("Unrealized PnL", "$0.00 (Mock)", "0.00%", delta_color="off")
            
            with c4:
                invested_val = active_bot_data[5] if active_bot_data else 0.0
                st.metric("Total Invested", f"${invested_val:,.2f}")

            # --- 🆕 ATR Planning Foundation Section ---
            st.divider()
            st.subheader("📊 ATR Market Context")
            try:
                # Get ATR configuration from global config
                atr_timeframe = global_config.ATR_TIMEFRAME if hasattr(global_config, 'ATR_TIMEFRAME') else '1h'
                atr_periods = int(getattr(global_config, 'ATR_PERIODS', 14))
                
                # Fetch data at the selected timeframe
                ohlcv_atr = fetch_ohlcv_cached(global_config.MARKET_TYPE, symbol, atr_timeframe)
                
                if ohlcv_atr:
                    df_atr = pd.DataFrame(ohlcv_atr, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df_atr['timestamp'] = pd.to_datetime(df_atr['timestamp'], unit='ms')
                    
                    # Calculate True Range
                    tr1 = df_atr['high'] - df_atr['low']
                    tr2 = (df_atr['high'] - df_atr['close'].shift()).abs()
                    tr3 = (df_atr['low'] - df_atr['close'].shift()).abs()
                    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                    
                    # Calculate ATR as average of True Range over N periods
                    # Allow lookback from 3 to 240 candles
                    valid_periods = min(max(atr_periods, 3), 240)
                    
                    if len(true_range) >= valid_periods:
                        # Current ATR (average of last N candles)
                        current_atr = true_range.iloc[-valid_periods:].mean()
                        
                        # Historical ATR for percentile calculation
                        rolling_atrs = true_range.rolling(window=valid_periods).mean()
                        atr_history = rolling_atrs.dropna()
                        
                        # Percentile calculation
                        if len(atr_history) >= 10:
                            percentile = (atr_history < float(current_atr)).sum() / len(atr_history) * 100
                        else:
                            percentile = 50
                        
                        # Move percentage (from last candle open)
                        last_open = df_atr['open'].iloc[-1]
                        last_close = df_atr['close'].iloc[-1]
                        move_pct = (last_close - last_open) / float(current_atr) * 100
                        
                        atr_data = {
                            'atr': float(current_atr),
                            'move_pct': float(move_pct),
                            'percentile': float(percentile),
                            'timeframe': atr_timeframe,
                            'periods': valid_periods
                        }
                    else:
                        st.warning(f"Not enough data for {atr_timeframe} timeframe. Need {valid_periods} candles.")
                        atr_data = None
                else:
                    st.warning("Could not fetch OHLCV data for ATR calculation.")
                    atr_data = None
                
                # ATR Configuration in expander
                with st.expander("⚙️ ATR Configuration", expanded=False):
                    c1, c2 = st.columns(2)
                    with c1:
                        new_atr_tf = st.selectbox(
                            "ATR Timeframe",
                            ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"],
                            index=["1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"].index(atr_timeframe) if atr_timeframe in ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"] else 4,
                            key="monitor_atr_tf_cfg"
                        )
                    with c2:
                        new_atr_periods = st.slider(
                            "ATR Lookback Period (candles)",
                            min_value=3,
                            max_value=240,
                            value=atr_periods,
                            key="monitor_atr_periods_cfg"
                        )
                    
                    if st.button("Apply ATR Settings"):
                        # Save to config (would need to persist this)
                        st.info(f"ATR Timeframe: {new_atr_tf}, Lookback: {new_atr_periods} candles")
                    
                    st.caption(f"**Formula:** ATR = Average(True Range of last {new_atr_periods} {new_atr_tf} candles)")
                
                # Display current ATR
                st.markdown("**Current ATR Context**")
                if atr_data:
                    mc1, mc2, mc3, mc4 = st.columns(4)
                    with mc1:
                        st.metric(
                            f"ATR ({atr_data['timeframe']})",
                            f"{atr_data['atr']:.6f}"
                        )
                    with mc2:
                        st.metric("Range Position", f"{atr_data['move_pct']:+.1f}%")
                    with mc3:
                        st.metric("Vol Percentile", f"{atr_data['percentile']:.0f}%")
                    with mc4:
                        st.metric("Lookback", f"{atr_data['periods']} candles")
                    
                    # Show where current price is relative to ATR
                    if atr_data['percentile'] > 70:
                        st.info(f"📈 **High Volatility**: Current volatility is in top {100-atr_data['percentile']:.0f}% percentile")
                    elif atr_data['percentile'] < 30:
                        st.info(f"📉 **Low Volatility**: Current volatility is in bottom {atr_data['percentile']:.0f}% percentile")
                    else:
                        st.info(f"➡️ **Normal Volatility**: Current volatility is at {atr_data['percentile']:.0f}% percentile")
                else:
                    st.warning("ATR data unavailable")
                    
            except Exception as e:
                st.warning(f"Could not load ATR Foundation: {e}")


        else:

            st.warning("No data received from exchange. Check your API connection or symbol.")
            
    except Exception as e:
        st.error(f"Error loading monitor: {e}")

    st.divider()
    
    # --- 🆕 Portfolio Overview Section ---
    st.subheader("📋 Active Positions (All Bots)")
    try:
        conn = get_connection()
        # Fetch all bots (Active & Paused) with trade info
        query_all = """
            SELECT b.id, b.name, b.pair, b.direction, b.strategy_type, b.config, t.current_step, t.total_invested, t.avg_entry_price, t.target_tp_price, b.is_active
            FROM bots b
            LEFT JOIN trades t ON b.id = t.bot_id
            -- Show all bots so users can see paused/errored ones too
        """
        # Load into list of dicts for manual processing first
        cursor = conn.cursor()
        cursor.execute(query_all)
        rows = cursor.fetchall()
        
        # FIX: Fetch exchange positions FIRST to ensure sync with DB
        # This creates a unified view of "what the exchange actually has"
        exchange_positions = {}
        exchange_orders = {}
        try:
            ex_futures = get_exchange_instance(market_type='future')
            ex_spot = get_exchange_instance(market_type='spot')
            
            # Fetch all futures positions
            try:
                fut_positions = ex_futures.exchange.fetch_positions()
                for pos in fut_positions:
                    sym = pos.get('symbol')
                    if sym:
                        contracts = float(pos.get('contracts', 0) or 0)
                        if contracts != 0:
                            exchange_positions[sym] = {
                                'side': pos.get('side'),
                                'size': abs(contracts),
                                'entry_price': float(pos.get('entryPrice', 0) or 0),
                                'mark_price': float(pos.get('markPrice', 0) or 0),
                                'unrealized_pnl': float(pos.get('unrealizedPnl', 0) or 0)
                            }
            except Exception as e:
                st.warning(f"Could not fetch futures positions: {e}")
            
            # Fetch all open orders for all active symbols
            active_symbols = set(r[2] for r in rows if r[2] and r[10])  # Only active bots
            for sym in active_symbols:
                try:
                    orders = ex_futures.fetch_open_orders(sym)
                    if orders:
                        exchange_orders[sym] = orders
                except Exception:
                    pass
        except Exception as e:
            st.warning(f"Exchange sync warning: {e}")
        
        conn.close()
        
        if rows:
            import json
            # 1. Gather all unique symbols to batch fetch prices (Optimization)
            # rows = [(id, name, pair, dir, strat, config, step, inv, entry, tp), ...]
            unique_symbols = list(set([r[2] for r in rows if r[2]]))
            
            # 2. Fetch all tickers in one go (or efficient loop)
            # Group symbols by market type from bot config to avoid 'spot' errors on futures keys
            current_prices = {}
            symbols_by_market = {'spot': set(), 'future': set()}
            
            for r in rows:
                # r: id, name, pair, direction, strat, config, step, invested, entry, tp
                # Parse config to get market type
                try:
                    c = json.loads(r[5]) if r[5] else {}
                    m_type = c.get('market_type', global_config.MARKET_TYPE)
                except:
                    m_type = global_config.MARKET_TYPE
                
                # Normalize
                if m_type not in ['spot', 'future']: m_type = 'future'
                
                if r[2]: # pair
                    symbols_by_market[m_type].add(r[2])

            # Fetch for each market type
            for m_type, syms in symbols_by_market.items():
                if not syms: continue
                try:
                    ex = get_exchange_instance(market_type=m_type)
                    # ex.exchange.load_markets() # Handled by init
                    for sym in syms:
                        try:
                            # Use safe fetch
                            ticker = ex.get_last_price(sym)
                            current_prices[sym] = ticker # Store by symbol
                        except Exception:
                            current_prices[sym] = 0.0
                except Exception as e:
                    # Log error but don't crash UI
                    st.warning(f"Price fetch warning ({m_type}): {e}")

            # 3. Build DataFrame with calculated P/L
            processed_data = []
            for r in rows:
                # r: id, name, pair, direction, strat, config, step, invested, entry, tp, is_active
                name, pair, direction, strat_type, config_json, step, invested, entry, tp, is_active = r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10]
                
                # --- PARSE STRATEGY SUMMARY ---
                strat_summary = strat_type
                try:
                    c = json.loads(config_json) if config_json else {}
                    triggers = []
                    
                    if strat_type == "Martingale":
                        if c.get('mode_rsi'): triggers.append(f"RSI({c.get('rsi_tf','?')})")
                        if c.get('mode_cci'): triggers.append(f"CCI({c.get('cci_tf','?')})")
                        if c.get('mode_boll'): triggers.append(f"BB({c.get('boll_tf','?')})")
                        if c.get('mode_stoch'): triggers.append(f"Stoch({c.get('stoch_tf','?')})")
                        if c.get('mode_price'): triggers.append(f"Price")
                        if c.get('mode_atrp'): triggers.append(f"Vol%")
                        
                        # Pattern Slots
                        pat_count = 0
                        for i in range(1, 5):
                             if c.get(f'pat_{i}_mode'): pat_count += 1
                        if pat_count > 0: triggers.append(f"Pat(x{pat_count})")
                        
                        if triggers:
                            strat_summary = f"{', '.join(triggers)}"
                        else:
                            strat_summary = "Martingale (No Triggers)"
                            
                    elif strat_type == "Market Maker":
                        spread = c.get('spread_pct', '?')
                        strat_summary = f"MM (Spr: {spread}%)"
                        
                    elif strat_type == "Magic Hour":
                        hour = c.get('magic_hour', '?')
                        strat_summary = f"Magic (Hr: {hour})"
                        
                except:
                    pass
                # ------------------------------

                curr_p = current_prices.get(pair, 0.0)
                pnl_str = "-"
                status_icon = "🟢" if is_active else "🔴"
                
                # Logic to handle "Scanning" vs "Active" state
                if is_active:
                    if entry and entry > 0 and curr_p > 0 and invested > 0:
                        # ACTIVE TRADE
                        if direction == "LONG":
                            pnl_raw = (curr_p - entry) / entry
                        else: # SHORT
                            pnl_raw = (entry - curr_p) / entry
                        
                        pnl_pct = pnl_raw * 100
                        pnl_usd = invested * pnl_raw
                        
                        # Formatting
                        icon = "🟢" if pnl_pct >= 0 else "🔴"
                        pnl_str = f"{icon} {pnl_pct:+.2f}% (${pnl_usd:+.2f})"
                    else:
                        # IDLE / SCANNING
                        entry = None 
                        tp = None
                        invested = None
                        pnl_str = "⏳ Scanning..."
                else:
                    # PAUSED / STOPPED
                    pnl_str = "⛔ Stopped"

                processed_data.append({
                    "Status": "Running" if is_active else "Stopped",
                    "Bot Name": name,
                    "Strategy / Triggers": strat_summary,
                    "Symbol": pair,
                    "Side": direction,
                    "Step": step if step > 0 else "-",
                    "Invested": invested, # None = "missing" value, distinct from 0
                    "Entry": entry,
                    "Target TP": tp,
                    "Current Price": curr_p,
                    "P/L": pnl_str
                })
            
            df_display = pd.DataFrame(processed_data)
            
            st.dataframe(
                df_display,
                column_config={
                    "Status": st.column_config.TextColumn(width="small"),
                    "Strategy / Triggers": st.column_config.TextColumn(width="medium"),
                    "Invested": st.column_config.NumberColumn(format="$%.2f"),
                    "Entry": st.column_config.NumberColumn(format="$%.4f"),
                    "Target TP": st.column_config.NumberColumn(format="$%.4f"),
                    "Current Price": st.column_config.NumberColumn(format="$%.4f"),
                },
                width='stretch',
                hide_index=True
            )
        else:
            st.info("No active bots running.")
            
    except Exception as e:
        st.error(f"Could not load portfolio: {e}")

    st.divider()
    
    # --- 🆕 Open Orders Section ---
    st.subheader("📋 Open Orders (Exchange)")
    try:
        ex_orders = get_exchange_instance(market_type=global_config.MARKET_TYPE)
        
        # Get unique symbols from active bots to check for open orders
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT pair FROM bots WHERE is_active = 1")
        active_pairs = [row[0] for row in cursor.fetchall()]
        all_open_orders = []
        for pair in active_pairs:
            try:
                # Use cached fetch for performance
                orders = fetch_open_orders_cached(global_config.MARKET_TYPE, pair)
                if orders:
                    for o in orders:
                        order_id = o.get('id')
                        
                        # Match order to bot using order ID tracking
                        bot_match = get_bots_by_order_id(order_id) if order_id else []
                        
                        if bot_match:
                            # Get bot name
                            cursor = conn.cursor()
                            cursor.execute('SELECT name FROM bots WHERE id = ?', (bot_match[0]['bot_id'],))
                            bot_result = cursor.fetchone()
                            bot_name = bot_result[0] if bot_result else 'Unknown'
                            bot_type = bot_match[0]['type']
                            label = f"{bot_name} ({bot_type})"
                        else:
                            label = o.get('clientOrderId', '').split('_')[0].upper() if o.get('clientOrderId') else "MANUAL"
                        
                        all_open_orders.append({
                            "Bot": label,
                            "Symbol": o.get('symbol', pair),
                            "Side": o.get('side', '').upper(),
                            "Type": o.get('type', '').upper(),
                            "Price": o.get('price', 0),
                            "Amount": o.get('amount', 0),
                            "Filled": o.get('filled', 0),
                            "Status": o.get('status', 'unknown'),
                            "Order ID": o.get('id', '')[:12] + '...' if o.get('id') else '-'
                        })

            except Exception:
                pass
        
        if all_open_orders:
            df_orders = pd.DataFrame(all_open_orders)
            st.dataframe(
                df_orders, 
                column_config={
                    "Bot": st.column_config.TextColumn(width="medium"),
                    "Price": st.column_config.NumberColumn(format="$%.4f"),
                    "Amount": st.column_config.NumberColumn(format="%.6f"),
                    "Filled": st.column_config.NumberColumn(format="%.6f"),
                },
                width='stretch',
                hide_index=True
            )
            
            # Show breakdown summary
            bot_orders = df_orders[df_orders['Bot'] != 'MANUAL']
            manual_orders = df_orders[df_orders['Bot'] == 'MANUAL']
            
            if not bot_orders.empty:
                st.caption(f"🤖 Bot orders: {len(bot_orders)} | 👤 Manual orders: {len(manual_orders)}")
        else:
            st.info("No open orders on exchange.")
        
        conn.close()
            
    except Exception as e:
        st.warning(f"Could not fetch open orders: {e}")

    st.divider()
    
    # --- 🆕 Open Positions Section (Futures) ---
    if global_config.MARKET_TYPE in ['future', 'swap']:
        st.subheader("📈 Open Positions (Exchange)")
        try:
            ex_positions = get_exchange_instance(market_type=global_config.MARKET_TYPE)
            
            # Fetch active bots to map positions to bots
            # This answers: "what bot what trade, which step"
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT b.pair, b.name, b.strategy_type, t.current_step 
                FROM bots b 
                LEFT JOIN trades t ON b.id = t.bot_id 
                WHERE b.is_active = 1
            """)
            # Create mapping: Symbol -> {Name, Strat, Step}
            bot_map = {}
            for row in cursor.fetchall():
                # row: pair, name, strategy_type, current_step
                if row[0]:
                    bot_map[row[0]] = {
                        'name': row[1], 
                        'strat': row[2], 
                        'step': row[3] if row[3] is not None else 0
                    }
            conn.close()

            # Fetch all positions from exchange (Cached)
            positions = fetch_positions_cached(global_config.MARKET_TYPE)
            
            # Filter to only show positions with non-zero size
            active_positions = []
            for pos in positions:
                contracts = float(pos.get('contracts', 0) or 0)
                notional = float(pos.get('notional', 0) or 0)
                
                if contracts != 0 or notional != 0:
                    symbol = pos.get('symbol', '')
                    side = pos.get('side', 'unknown')
                    entry_price = float(pos.get('entryPrice', 0) or 0)
                    mark_price = float(pos.get('markPrice', 0) or 0)
                    unrealized_pnl = float(pos.get('unrealizedPnl', 0) or 0)
                    leverage = pos.get('leverage', 1)
                    liquidation = float(pos.get('liquidationPrice', 0) or 0)
                    
                    # Format PnL with color
                    pnl_str = f"${unrealized_pnl:+.2f}"
                    
                    # Enrich with Bot Info
                    bot_info = bot_map.get(symbol, {})
                    bot_name = bot_info.get('name', 'Unknown/Manual')
                    bot_step = bot_info.get('step', '-')
                    bot_strat = bot_info.get('strat', '-')

                    active_positions.append({
                        "Bot Name": bot_name,
                        "Step": f"S{bot_step}",
                        "Symbol": symbol,
                        "Side": side.upper() if side else 'UNKNOWN',
                        "Size": abs(contracts),
                        "Notional": f"${abs(notional):.2f}",
                        "Entry": entry_price,
                        "Mark": mark_price,
                        "Liq. Price": liquidation if liquidation > 0 else None,
                        "Leverage": f"{leverage}x",
                        "Unrealized PnL": pnl_str
                    })
            
            if active_positions:
                df_positions = pd.DataFrame(active_positions)
                
                # Reorder columns to put Bot info first
                cols = ["Bot Name", "Step", "Symbol", "Side", "Size", "Entry", "Mark", "Unrealized PnL", "Liq. Price"]
                # Filter to only existing columns (ignoring Notional/Leverage in main view to save space if needed)
                cols = [c for c in cols if c in df_positions.columns]
                
                st.dataframe(
                    df_positions[cols],
                    column_config={
                        "Bot Name": st.column_config.TextColumn("🤖 Bot", width="medium"),
                        "Step": st.column_config.TextColumn("Step", width="small"),
                        "Size": st.column_config.NumberColumn(format="%.4f"),
                        "Entry": st.column_config.NumberColumn(format="$%.4f"),
                        "Mark": st.column_config.NumberColumn(format="$%.4f"),
                        "Liq. Price": st.column_config.NumberColumn(format="$%.2f"),
                    },
                    width='stretch',
                    hide_index=True
                )
                
                st.caption("ℹ️ **Bot**: Validates which bot owns the position. **Step**: Current Martingale step.")
            else:
                st.info("No open positions on exchange.")
                
        except Exception as e:
            st.warning(f"Could not fetch positions: {e}")

        st.divider()
    
    # --- 🆕 Trade History Section ---
    st.subheader("📜 Trade History (Recent)")
    try:
        from engine.database import get_trade_history
        from datetime import datetime
        
        history = get_trade_history(limit=20)
        
        if history:
            history_data = []
            for h in history:
                # h: id, bot_id, bot_name, action, symbol, price, amount, cost_usdc, step, pnl, timestamp, notes
                h_id, bot_id, bot_name, action, symbol, price, amount, cost_usdc, step, pnl, ts, notes = h
                
                # Format timestamp
                try:
                    dt = datetime.fromtimestamp(ts)
                    time_str = dt.strftime("%m/%d %H:%M")
                except:
                    time_str = str(ts)
                
                # Format PnL with color indicator
                pnl_str = f"${pnl:+.2f}" if pnl != 0 else "-"
                
                history_data.append({
                    "Time": time_str,
                    "Bot": bot_name or f"Bot #{bot_id}",
                    "Action": action,
                    "Symbol": symbol,
                    "Price": price,
                    "Amount": amount,
                    "Cost ($)": cost_usdc,
                    "Step": step,
                    "PnL": pnl_str,
                    "Notes": notes or ""
                })
            
            df_history = pd.DataFrame(history_data)
            st.dataframe(
                df_history,
                column_config={
                    "Price": st.column_config.NumberColumn(format="$%.4f"),
                    "Amount": st.column_config.NumberColumn(format="%.6f"),
                    "Cost ($)": st.column_config.NumberColumn(format="$%.2f"),
                },
                width='stretch',
                hide_index=True
            )
        else:
            st.info("No trade history yet. Trades will appear here once bots start executing.")
            
    except Exception as e:
        st.warning(f"Could not load trade history: {e}")

    st.caption(f"Visualizing live data for **{symbol}** via CCXT.")
    
    # Auto-Refresh Logic
    if auto_refresh:
        time.sleep(30)
        st.rerun()
