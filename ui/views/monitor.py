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
                # Use market type from config (spot vs future)
                ex_global = ExchangeInterface(market_type=config.MARKET_TYPE)
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
        balance_debug_msg = []

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
            
            balance_debug_msg.append(f"Fut: ${futures_balance:.2f}")
        except Exception as e:
            balance_debug_msg.append(f"Fut Err: {str(e)[:20]}")

        # --- B. Fetch Spot Balance ---
        try:
            # Only try spot if not exclusively futures restricted (optional check)
            ex_spot = ExchangeInterface(market_type='spot')
            # Check if we can fetch (might fail if keys are futures-only)
            spot_data = ex_spot.fetch_balance()
            
            if spot_data:
                # Calculate total spot value in USDT (simplified)
                # We iterate over non-zero balances
                if 'total' in spot_data:
                    for asset, amount in spot_data['total'].items():
                        if amount > 0:
                            # Estimate value in USDT (mock or fetch price?)
                            # For now, just count USDT/USDC directly as 1:1
                            val = 0.0
                            if asset in ['USDT', 'USDC', 'DAI', 'BUSD']:
                                val = amount
                            else:
                                # Try to get price? Too slow for now.
                                # Just log the asset
                                pass 
                            
                            if val > 0:
                                spot_balance += val
                            
                            assets_breakdown.append({
                                'Type': 'Spot',
                                'Asset': asset,
                                'Balance': amount,
                                'Unrealized PnL': 0.0,
                                'Equity': val # Approximate
                            })
            
            balance_debug_msg.append(f"Spot: ${spot_balance:.2f}")
        except Exception as e:
            # Likely auth error if using futures keys
            balance_debug_msg.append("Spot: N/A (Auth)")

        # Total Calculation
        total_equity = futures_balance + spot_balance + global_pnl_usd # Global PnL is active trade PnL from DB logic

        conn.close()
    except Exception as outer_e:
        active_count = 0
        total_invested_db = 0.0
        global_pnl_usd = 0.0
        futures_balance = 0.0
        spot_balance = 0.0
        total_equity = 0.0
        balance_debug_msg = [f"Critical Error: {str(outer_e)}"]
        assets_breakdown = []

    # DEBUG: Show balance debug info (temporary - remove after fixing)
    if balance_debug_msg:
        st.caption(f"🔧 Portfolio Debug: {' | '.join(balance_debug_msg)}")
    
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
        # Initialize exchange with correct market type from config
        try:
            exchange = ExchangeInterface(market_type=config.MARKET_TYPE) 
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
                    m_type = c.get('market_type', config.MARKET_TYPE)
                except:
                    m_type = config.MARKET_TYPE
                
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
        ex_orders = ExchangeInterface(market_type=config.MARKET_TYPE)
        
        # Get unique symbols from active bots to check for open orders
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT pair FROM bots WHERE is_active = 1")
        active_pairs = [row[0] for row in cursor.fetchall()]
        conn.close()
        
        all_open_orders = []
        for pair in active_pairs:
            try:
                orders = ex_orders.fetch_open_orders(pair)
                if orders:
                    for o in orders:
                        all_open_orders.append({
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
                    "Price": st.column_config.NumberColumn(format="$%.4f"),
                    "Amount": st.column_config.NumberColumn(format="%.6f"),
                    "Filled": st.column_config.NumberColumn(format="%.6f"),
                },
                width='stretch',
                hide_index=True
            )
        else:
            st.info("No open orders on exchange.")
            
    except Exception as e:
        st.warning(f"Could not fetch open orders: {e}")

    st.divider()
    
    # --- 🆕 Open Positions Section (Futures) ---
    if config.MARKET_TYPE in ['future', 'swap']:
        st.subheader("📈 Open Positions (Exchange)")
        try:
            ex_positions = ExchangeInterface(market_type=config.MARKET_TYPE)
            
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
