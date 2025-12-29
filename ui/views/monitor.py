import streamlit as st
import time

def render_monitor_view():
    st.header("Live Market Monitor")
    
    # Simple control bar
    col1, col2 = st.columns([1, 3])
    with col1:
        # Fetch symbols from exchange if possible, else hardcode
        symbol = st.selectbox("Select Symbol", ["BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT"], key="monitor_symbol")
    with col2:
        st.write("") # Spacer
        if st.button("Refresh Data", use_container_width=False):
            st.session_state.last_refresh = time.time()
    
    # Fetch Data
    try:
        from engine.exchange_interface import ExchangeInterface
        import pandas as pd
        import plotly.graph_objects as go
        
        # Initialize exchange (defaulting to spot for monitor for now)
        exchange = ExchangeInterface() 
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=100)
        
        if ohlcv and len(ohlcv) > 0:
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            # Create Plotly Candlestick Chart
            fig = go.Figure(data=[go.Candlestick(
                x=df['timestamp'],
                open=df['open'],
                high=df['high'],
                low=df['low'],
                close=df['close'],
                name=symbol
            )])
            
            # --- Visual Overlays for Active Bot ---
            # Fetch active bot for this symbol
            from engine.database import get_connection
            try:
                conn = get_connection()
                cur = conn.cursor()
                # Get bot + trade info
                query = """
                    SELECT b.name, t.avg_entry_price, t.target_tp_price, t.current_step, b.direction
                    FROM bots b
                    JOIN trades t ON b.id = t.bot_id
                    WHERE b.pair = ? AND b.is_active = 1
                    ORDER BY t.avg_entry_price DESC
                    LIMIT 1
                """
                cur.execute(query, (symbol,))
                active_bot = cur.fetchone()
                conn.close()
                
                if active_bot:
                    bot_name, entry, tp, step, direction = active_bot
                    
                    if entry > 0:
                        color_entry = "blue"
                        color_tp = "green" if direction == "LONG" else "red"
                        
                        fig.add_hline(y=entry, line_dash="solid", line_color=color_entry, annotation_text=f"Entry ({bot_name})")
                        
                        if tp > 0:
                             fig.add_hline(y=tp, line_dash="dash", line_color=color_tp, annotation_text="Take Profit")
                        
                        st.info(f"Bot '{bot_name}' active. Step: {step} | Entry: {entry} | TP: {tp}")
                
            except Exception as e:
                st.warning(f"Could not load bot levels: {e}")

            fig.update_layout(
                title=f"{symbol} - 1H Chart",
                yaxis_title="Price",
                xaxis_title="Time",
                template="plotly_dark",
                height=600,
                xaxis_rangeslider_visible=False
            )
            
            st.plotly_chart(fig, use_container_width=True)
            
            # Display latest price stats
            latest = df.iloc[-1]
            c1, c2, c3 = st.columns(3)
            with c1: st.metric("Latest Price", f"{latest['close']:.2f}", f"{latest['close'] - df.iloc[-2]['close']:.2f}")
            with c2: st.metric("24h Vol", f"{latest['volume']:.0f}")
            with c3: st.caption("Active Bot Levels will appear here when running.")
            
        else:
            st.warning("No data received from exchange.")
            
    except Exception as e:
        st.error(f"Error loading chart: {e}")

    st.info(f"Visualizing live data for **{symbol}**.")
