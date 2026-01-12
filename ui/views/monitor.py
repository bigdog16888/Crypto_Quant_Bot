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
            SELECT b.name, b.pair, b.direction, t.current_step, t.total_invested, t.avg_entry_price, t.target_tp_price 
            FROM bots b
            LEFT JOIN trades t ON b.id = t.bot_id
            WHERE b.is_active = 1
        """
        df_bots = pd.read_sql_query(query_all, conn)
        conn.close()
        
        if not df_bots.empty:
            # Format columns for display
            df_bots['P/L'] = "Calculating..." # Placeholder for now, hard to calc for all without fetch_ticker for each
            st.dataframe(
                df_bots,
                column_config={
                    "name": "Bot Name",
                    "pair": "Symbol",
                    "direction": "Side",
                    "current_step": "Step",
                    "total_invested": st.column_config.NumberColumn("Invested ($)", format="$%.2f"),
                    "avg_entry_price": st.column_config.NumberColumn("Entry Price", format="$%.2f"),
                    "target_tp_price": st.column_config.NumberColumn("Target TP", format="$%.2f"),
                },
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("No active bots running.")
            
    except Exception as e:
        st.error(f"Could not load portfolio: {e}")

    st.caption(f"Visualizing live data for **{symbol}** via CCXT.")
