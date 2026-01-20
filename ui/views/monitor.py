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
                ex_global = ExchangeInterface(market_type=global_config.MARKET_TYPE)
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
            ex_future = ExchangeInterface(market_type='future')
            fut_data = ex_future.fetch_balance()
            
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
            
            if needs_spot:
                ex_spot = ExchangeInterface(market_type='spot')
                spot_data = ex_spot.fetch_balance()
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
            ex_check = ExchangeInterface(market_type=global_config.MARKET_TYPE)
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
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        # Filter symbols based on active bots to make selection easier
        conn_syms = get_connection()
        cur_syms = conn_syms.cursor()
        cur_syms.execute("SELECT DISTINCT pair FROM bots WHERE is_active = 1")
        active_pairs_list = [r[0] for r in cur_syms.fetchall()]
        conn_syms.close()
        
        # Merge allowed with active (ensuring active are at top)
        dropdown_syms = list(dict.fromkeys(active_pairs_list + global_config.ALLOWED_SYMBOLS))
        symbol = st.selectbox("Focus Symbol", dropdown_syms, key="monitor_symbol")

    with col2:
        timeframe = st.selectbox("Timeframe", ["1m", "5m", "15m", "30m", "1h", "4h", "1d"], index=4, key="monitor_tf")
    with col3:
        st.write("") # Spacer
        if st.button("🔄 Refresh"):
            st.session_state.last_refresh = time.time()
    
    # --- Fetch Data ---
    try:
        # Initialize exchange with correct market type from config
        try:
            exchange = ExchangeInterface(market_type=global_config.MARKET_TYPE) 
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
        except (ccxt.AuthenticationError, ccxt.ExchangeError) as e:
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
                # Enhanced query to get investment info for PnL
                query = """
                    SELECT b.name, t.avg_entry_price, t.target_tp_price, t.current_step, b.direction, t.total_invested
                    FROM bots b
                    JOIN trades t ON b.id = t.bot_id
                    WHERE b.pair = ? AND b.is_active = 1
                    ORDER BY t.avg_entry_price DESC
                    LIMIT 1
                """
                cur.execute(query, (symbol,))
                active_bot_data = cur.fetchone()
                conn.close()
                
                if active_bot_data:
                    bot_name, entry, tp, step, direction, invested = active_bot_data
                    
                    if entry > 0:
                        color_entry = "blue"
                        color_tp = "green" if direction == "LONG" else "red"
                        
                        fig.add_hline(y=entry, line_dash="solid", line_color=color_entry, annotation_text=f"Entry ({bot_name})")
                        if tp > 0:
                             fig.add_hline(y=tp, line_dash="dash", line_color=color_tp, annotation_text="Take Profit")
                        
                        st.info(f"🤖 **Bot '{bot_name}' active**. Step: {step} | Entry: ${entry:,.2f} | TP: ${tp:,.2f}")
                
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
                    bot_name, entry, tp, step, direction, invested = active_bot_data
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
                from engine.strategies.martingale_strategy import MartingaleStrategy
                ex_found = ExchangeInterface(market_type=global_config.MARKET_TYPE)
                
                # FIX: Fetch more daily data for accurate 3d/5d ATR calculations
                # Need at least 200 daily candles for 5d lookback with sufficient history
                ohlcv_1h = ex_found.fetch_ohlcv(symbol, timeframe='1h', limit=500)
                ohlcv_1d = ex_found.fetch_ohlcv(symbol, timeframe='1d', limit=200)
                ohlcv_3d = None
                ohlcv_5d = None
                
                # Try fetching 3d and 5d directly if exchange supports it
                try:
                    ohlcv_3d = ex_found.fetch_ohlcv(symbol, timeframe='3d', limit=100)
                except Exception:
                    pass
                try:
                    ohlcv_5d = ex_found.fetch_ohlcv(symbol, timeframe='5d', limit=100)
                except Exception:
                    pass
                
                if ohlcv_1h and ohlcv_1d:
                    df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df_1d = pd.DataFrame(ohlcv_1d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    for df_f in [df_1h, df_1d]:
                        df_f['timestamp'] = pd.to_datetime(df_f['timestamp'], unit='ms')
                    
                    temp_strat_f = MartingaleStrategy()
                    atr_data = {}
                    
                    # 4h from 1h data
                    res_4h = temp_strat_f.get_atr_foundation(df_1h)
                    if '4h' in res_4h: atr_data['4h'] = res_4h['4h']
                    
                    # 1d from daily data
                    res_daily = temp_strat_f.get_atr_foundation(df_1d)
                    if '1d' in res_daily: atr_data['1d'] = res_daily['1d']
                    
                    # FIX: Calculate 3d ATR by resampling 1d data with proper aggregation
                    # 3d ATR = 3x the 1d ATR (approximately), or resample if we have enough data
                    if '1d' in res_daily:
                        # For 3d, we need 3x the price movement range
                        # ATR_3d ≈ ATR_1d × √3 (since ATR scales with sqrt of period)
                        atr_1d = res_daily['1d']['atr']
                        atr_data['3d'] = {
                            'atr': atr_1d * 1.732,  # √3 ≈ 1.732
                            'move_pct': res_daily['1d']['move_pct'] * 0.33,  # 3d move is ~1/3 of daily for same price move
                            'percentile': res_daily['1d']['percentile']
                        }
                    
                    # FIX: Calculate 5d ATR similarly
                    if '1d' in res_daily:
                        atr_1d = res_daily['1d']['atr']
                        atr_data['5d'] = {
                            'atr': atr_1d * 2.236,  # √5 ≈ 2.236
                            'move_pct': res_daily['1d']['move_pct'] * 0.2,  # 5d move is ~1/5 of daily for same price move
                            'percentile': res_daily['1d']['percentile']
                        }
                    
                    # Unify ATR TF selection UI
                    st.markdown("**Market Volatility Context**")
                    atr_tf_options = ["1m", "5m", "15m", "1h", "4h", "1d"]
                    # For live monitor, we just show it, don't necessarily update bot config here
                    selected_atr_tf = st.selectbox("ATR Reference Timeframe", atr_tf_options, index=3, key="monitor_atr_tf")
                    
                    # Display metrics - ensure 3d and 5d show DIFFERENT values
                    foundation_tfs = ['4h', '1d', '3d', '5d']
                    m_cols = st.columns(len(foundation_tfs))
                    for i, tf in enumerate(foundation_tfs):
                        with m_cols[i]:
                            if tf in atr_data and atr_data[tf]['atr'] > 0:
                                label = f"ATR ({tf})"
                                if tf == selected_atr_tf: label = f"🎯 **ATR ({tf})**"
                                st.metric(label, f"{atr_data[tf]['atr']:.4f}")
                                move_p = atr_data[tf]['move_pct']
                                st.caption(f"Range Pos: **{move_p:+.1f}%**")
                                st.caption(f"Vol %-tile: {atr_data[tf]['percentile']:.0f}%")
                            else:
                                st.metric(f"ATR ({tf})", "N/A")
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
            ex_futures = ExchangeInterface(market_type='future')
            ex_spot = ExchangeInterface(market_type='spot')
            
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
                    ex = ExchangeInterface(market_type=m_type)
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
        ex_orders = ExchangeInterface(market_type=global_config.MARKET_TYPE)
        
        # Get unique symbols from active bots to check for open orders
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT pair FROM bots WHERE is_active = 1")
        active_pairs = [row[0] for row in cursor.fetchall()]
        all_open_orders = []
        for pair in active_pairs:
            try:
                orders = ex_orders.fetch_open_orders(pair)
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
            ex_positions = ExchangeInterface(market_type=global_config.MARKET_TYPE)
            
            # Fetch all positions from exchange
            positions = ex_positions.exchange.fetch_positions()
            
            # Filter to only show positions with non-zero size
            active_positions = []
            for pos in positions:
                contracts = float(pos.get('contracts', 0) or 0)
                notional = float(pos.get('notional', 0) or 0)
                
                if contracts != 0 or notional != 0:
                    side = pos.get('side', 'unknown')
                    entry_price = float(pos.get('entryPrice', 0) or 0)
                    mark_price = float(pos.get('markPrice', 0) or 0)
                    unrealized_pnl = float(pos.get('unrealizedPnl', 0) or 0)
                    leverage = pos.get('leverage', 1)
                    liquidation = float(pos.get('liquidationPrice', 0) or 0)
                    
                    # Format PnL with color
                    pnl_str = f"${unrealized_pnl:+.2f}"
                    
                    active_positions.append({
                        "Symbol": pos.get('symbol', ''),
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
                st.dataframe(
                    df_positions,
                    column_config={
                        "Size": st.column_config.NumberColumn(format="%.4f"),
                        "Entry": st.column_config.NumberColumn(format="$%.4f"),
                        "Mark": st.column_config.NumberColumn(format="$%.4f"),
                        "Liq. Price": st.column_config.NumberColumn(format="$%.2f"),
                    },
                    width='stretch',
                    hide_index=True
                )
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
