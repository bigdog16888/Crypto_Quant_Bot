import streamlit as st
import time
import pandas as pd
import plotly.graph_objects as go
import ccxt
from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection

from config.settings import config

def render_monitor_view():
    st.header("📊 Live Market Monitor")
    
    # --- Command Center (Health Dashboard) ---
    st.markdown("""
    <style>
    .metric-card {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 8px;
        padding: 15px;
        text-align: center;
    }
    .metric-value { font-size: 1.8em; font-weight: bold; color: #f0f6fc; }
    .metric-label { font-size: 0.9em; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; }
    .status-ok { color: #3fb950; font-weight: bold; }
    .status-warn { color: #d29922; font-weight: bold; }
    </style>
    """, unsafe_allow_html=True)
    
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
                ex_global = ExchangeInterface()
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

        # 4. Fetch Wallet Balance
        wallet_balance = 0.0
        total_equity = 0.0
        try:
            ex_bal = ExchangeInterface()
            bal_data = ex_bal.fetch_balance()
            # Assuming USDT/USDC quoted
            # total = free + used
            # For simplicity, getting 'total' of the main quote asset (USDT or USDC)
            # Or usually 'total' in USDT equivalent if exchange provides it
            if 'USDT' in bal_data['total']:
                wallet_balance = float(bal_data['total']['USDT'])
            elif 'USDC' in bal_data['total']:
                wallet_balance = float(bal_data['total']['USDC'])
            else:
                # Fallback to total free + used if total dict not standard
                wallet_balance = 0.0 
            
            total_equity = wallet_balance + global_pnl_usd # Balance usually includes realized, PnL is unrealized
        except Exception:
            wallet_balance = 0.0
            total_equity = 0.0
        
        conn.close()
    except Exception:
        active_count = 0
        total_invested_db = 0.0
        global_pnl_usd = 0.0
        wallet_balance = 0.0
        total_equity = 0.0

    # Display Metrics Grid (Top Command Center)
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.markdown(f"""<div class="metric-card"><div class="metric-label">Wallet Balance</div><div class="metric-value">${wallet_balance:,.2f}</div></div>""", unsafe_allow_html=True)
    with m2:
        st.markdown(f"""<div class="metric-card"><div class="metric-label">Active Exposure</div><div class="metric-value">${total_invested_db:,.2f}</div></div>""", unsafe_allow_html=True)
    with m3:
        # Global PnL
        color = "#3fb950" if global_pnl_usd >= 0 else "#f85149"
        sign = "+" if global_pnl_usd >= 0 else ""
        st.markdown(f"""<div class="metric-card"><div class="metric-label">Global PnL (Unrealized)</div><div class="metric-value" style="color:{color}">{sign}${global_pnl_usd:,.2f}</div></div>""", unsafe_allow_html=True)
    with m4:
        # Equity
        st.markdown(f"""<div class="metric-card"><div class="metric-label">Est. Total Equity</div><div class="metric-value">${total_equity:,.2f}</div></div>""", unsafe_allow_html=True)

    st.divider()
    
    # --- Activity Stream (Ticker) ---
    try:
        conn = get_connection()
        # Fetch last 5 logs or trades
        # Assuming we have a logs table or we use trades table for significant events
        # Let's use trades table for now: (action, symbol, price, timestamp)
        # Note: You might need to add a 'timestamp' column to trades if not present or use ID as proxy
        # Fallback to just static ticker if no logs table yet
        
        # Mocking ticker for now until log table is robust
        st.markdown("""
        <div style="background-color: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 10px; margin-bottom: 20px; white-space: nowrap; overflow: hidden;">
            <span style="color: #8b949e; font-size: 0.8em; margin-right: 10px;">LATEST ACTIVITY:</span>
            <span style="font-family: monospace; color: #58a6ff;">🚀 System Initialized</span> <span style="color: #30363d;"> | </span>
            <span style="font-family: monospace; color: #3fb950;">💰 BTC/USDT Entry @ $42,000</span> <span style="color: #30363d;"> | </span>
            <span style="font-family: monospace; color: #d29922;">⚖️ ETH/USDT Rebalanced</span>
        </div>
        """, unsafe_allow_html=True)
        conn.close()
    except Exception:
        pass

    # --- Control Bar ---
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        # Use config for symbols
        symbol = st.selectbox("Select Symbol", config.ALLOWED_SYMBOLS, key="monitor_symbol")
    with col2:
        timeframe = st.selectbox("Timeframe", ["1m", "5m", "15m", "30m", "1h", "4h", "1d"], index=4, key="monitor_tf")
    with col3:
        st.write("") # Spacer
        if st.button("🔄 Refresh"):
            st.session_state.last_refresh = time.time()
    
    # --- Fetch Data ---
    try:
        # Initialize exchange (defaulting to spot for monitor for now)
        try:
            exchange = ExchangeInterface() 
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
                template="plotly_dark",
                height=600,
                xaxis_rangeslider_visible=True, # Interactive Zoom
                margin=dict(l=10, r=10, t=40, b=10)
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

        else:
            st.warning("No data received from exchange. Check your API connection or symbol.")
            
    except Exception as e:
        st.error(f"Error loading monitor: {e}")

    st.divider()
    
    # --- 🆕 Portfolio Overview Section ---
    st.subheader("📋 Active Positions (All Bots)")
    try:
        conn = get_connection()
        # Fetch all active bots with trade info
        query_all = """
            SELECT b.id, b.name, b.pair, b.direction, b.strategy_type, b.config, t.current_step, t.total_invested, t.avg_entry_price, t.target_tp_price 
            FROM bots b
            LEFT JOIN trades t ON b.id = t.bot_id
            WHERE b.is_active = 1
        """
        # Load into list of dicts for manual processing first
        cursor = conn.cursor()
        cursor.execute(query_all)
        rows = cursor.fetchall()
        conn.close()
        
        if rows:
            import json
            # 1. Gather all unique symbols to batch fetch prices (Optimization)
            # rows = [(id, name, pair, dir, strat, config, step, inv, entry, tp), ...]
            unique_symbols = list(set([r[2] for r in rows if r[2]]))
            
            # 2. Fetch all tickers in one go (or efficient loop)
            current_prices = {}
            if unique_symbols:
                try:
                    # Use shared exchange instance if possible, or new one
                    ex = ExchangeInterface()
                    ex.exchange.load_markets()
                    for sym in unique_symbols:
                        ticker = ex.exchange.fetch_ticker(sym)
                        current_prices[sym] = ticker['last']
                except Exception as e:
                    st.warning(f"Price fetch warning: {e}")

            # 3. Build DataFrame with calculated P/L
            processed_data = []
            for r in rows:
                # r: id, name, pair, direction, strat, config, step, invested, entry, tp
                name, pair, direction, strat_type, config_json, step, invested, entry, tp = r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9]
                
                # --- PARSE STRATEGY SUMMARY ---
                strat_summary = strat_type
                try:
                    c = json.loads(config_json) if config_json else {}
                    triggers = []
                    
                    if strat_type == "MQL4":
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
                            strat_summary = "MQL4 (No Triggers)"
                            
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
                
                # Logic to handle "Scanning" vs "Active" state
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
                    
                    # Format numbers for display if needed, but st.dataframe handles floats well
                else:
                    # IDLE / SCANNING
                    entry = None # Will display as empty/gray in dataframe or custom string if converted
                    tp = None
                    invested = None
                    pnl_str = "⏳ Scanning..."

                processed_data.append({
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
                    "Strategy / Triggers": st.column_config.TextColumn(width="medium"),
                    "Invested": st.column_config.NumberColumn(format="$%.2f"),
                    "Entry": st.column_config.NumberColumn(format="$%.4f"),
                    "Target TP": st.column_config.NumberColumn(format="$%.4f"),
                    "Current Price": st.column_config.NumberColumn(format="$%.4f"),
                },
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("No active bots running.")
            
    except Exception as e:
        st.error(f"Could not load portfolio: {e}")

    st.caption(f"Visualizing live data for **{symbol}** via CCXT.")
