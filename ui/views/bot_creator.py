import streamlit as st
import sys
import os

# Ensure engine can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine.database import add_bot
from engine.exchange_interface import ExchangeInterface

def render_bot_creator_view():
    st.header("Bot Creator & Configuration")
    st.write("Configure and launch new trading bots here.")
    
    # Dynamic Market Selection
    col_m1, col_m2, col_m3 = st.columns(3)
    with col_m1:
        market_type = st.selectbox("Market Type", ["Spot", "Futures (Swap)"], index=0)
        mode_id = 'spot' if market_type == "Spot" else 'swap'
    with col_m2:
        quote_asset = st.selectbox("Quote Asset", ["USDT", "USDC"])
    with col_m3:
        # Fetch symbols dynamically based on selection
        try:
            exchange = ExchangeInterface(market_type=mode_id)
            available_pairs = exchange.get_available_symbols(quote_asset=quote_asset)
            if not available_pairs:
                available_pairs = [f"BTC/{quote_asset}", f"ETH/{quote_asset}"] # Fallback
        except Exception as e:
            st.error(f"Error fetching symbols: {e}")
            available_pairs = [f"BTC/{quote_asset}"]

        pair = st.selectbox("Trading Pair", available_pairs)

    # --- Configuration Sections ---
    # Wrap everything in a form to fix StreamlitAPIException and allow clean submission
    with st.form("deploy_bot_form"):
        st.subheader("General Settings")
        col1, col2 = st.columns(2)
        with col1:
            name = st.text_input("Bot Name", placeholder="e.g., Scalper_USDC_01")
            direction = st.selectbox("Direction", ["LONG", "SHORT"])
            strategy_type = st.selectbox("Strategy Logic", ["Classic (RSI/CCI/Boll)", "Market Maker (Spread)"])
            # Global/Base Timeframe (Execution speed)
            timeframe = st.selectbox("Execution Timeframe", ["1m", "5m", "15m", "1h", "4h", "1d"], index=1)
        
        with col2:
            base_size = st.number_input("Base Size (USDT/USDC)", min_value=10.0, step=10.0, value=10.0)
            martingale_multiplier = st.number_input("Martingale Multiplier", min_value=1.0, step=0.1, value=1.5)
            rsi_limit = st.slider("RSI Limit (for Classic)", 0, 100, 30)

        st.divider()
        config = {}

        with st.expander("Strategy Settings (Indicators)", expanded=True):
            st.subheader("Entry Logic Switch")
            st.caption("0=Off, 1=Standard (Trend/Level), 2=Reverse (Counter-Trend)")
            
            c1, c2, c3, c4 = st.columns(4)
            with c1: config['cci_entry'] = st.selectbox("CCI Entry", [0, 1, 2], index=0)
            with c2: config['bollinger_entry'] = st.selectbox("Boll Entry", [0, 1, 2], index=0)
            with c3: config['stoch_entry'] = st.selectbox("Stoch Entry", [0, 1, 2], index=0)
            with c4: config['macd_entry'] = st.selectbox("MACD Entry", [0, 1, 2], index=0)
            
            st.divider()
            st.subheader("Indicator Parameters")
            
            # CCI
            c_cci1, c_cci2 = st.columns(2)
            with c_cci1: config['cci_period'] = st.number_input("CCI Period", value=14)
            with c_cci2: config['cci_tf'] = st.selectbox("CCI Timeframe", ["1m","5m","15m","1h","4h","1d"], index=2, key="cci_tf")
            
            # Boll
            c_bb1, c_bb2, c_bb3, c_bb4 = st.columns(4)
            with c_bb1: config['boll_period'] = st.number_input("BB Period", value=20)
            with c_bb2: config['boll_deviation'] = st.number_input("BB Dev", value=2.0)
            with c_bb3: config['boll_distance'] = st.number_input("BB Dist", value=10)
            with c_bb4: config['boll_tf'] = st.selectbox("BB Timeframe", ["1m","5m","15m","1h","4h","1d"], index=2, key="bb_tf")
            
            # Stoch (Phase 3)
            c_st1, c_st2, c_st3, c_st4 = st.columns(4)
            with c_st1: config['stoch_k'] = st.number_input("Stoch K", value=5)
            with c_st2: config['stoch_d'] = st.number_input("Stoch D", value=3)
            with c_st3: config['stoch_slowing'] = st.number_input("Stoch Slow", value=3)
            with c_st4: config['stoch_tf'] = st.selectbox("Stoch Timeframe", ["1m","5m","15m","1h","4h","1d"], index=2, key="stoch_tf")
            
            # MACD (Phase 3)
            c_mac1, c_mac2, c_mac3, c_mac4 = st.columns(4)
            with c_mac1: config['macd_fast'] = st.number_input("MACD Fast", value=12)
            with c_mac2: config['macd_slow'] = st.number_input("MACD Slow", value=26)
            with c_mac3: config['macd_sig'] = st.number_input("MACD Sig", value=9)
            with c_mac4: config['macd_tf'] = st.selectbox("MACD Timeframe", ["1m","5m","15m","1h","4h","1d"], index=3, key="macd_tf")


        with st.expander("Risk Management (Martingale & Grid)", expanded=False):
            st.subheader("Dynamic Grid (ATR)")
            config['UseATRGrid'] = st.checkbox("Use ATR Dynamic Grid", value=True)
            config['ATRGridFactor'] = st.number_input("ATR Grid Factor", value=1.0)
            
        with st.expander("Trade Management (Exit & Hedge)", expanded=False):
            st.subheader("Early Exit (Smart Decay)")
            config['UseEarlyExit'] = st.checkbox("Use Early Exit", value=True)
            config['EEHoursPC'] = st.number_input("Decay % Per Hour", value=0.5)
            config['EEStartHours'] = st.number_input("Start Decay After (Hours)", value=2.0)
            
            st.subheader("Moving Profit")
            config['MaximizeProfit'] = st.checkbox("Use Moving Profit Target", value=False)
            config['ProfitSet'] = st.slider("Profit Set % (Lock in)", 0.1, 0.9, 0.5)
            
            st.subheader("Hedging")
            config['UseHedge'] = st.checkbox("Use Hedging", value=False)
            config['HedgeStart'] = st.number_input("Hedge Start (DD%)", value=20.0)

        submitted = st.form_submit_button("Deploy Bot", type="primary")
    
    if submitted:
        if not name:
            st.error("Bot Name is required.")
        else:
            # Add timeframe to config
            config['timeframe'] = timeframe

            # Map readable strategy name to internal ID
            strat_id = "MQL4" if "Classic" in strategy_type else "MARKET_MAKER"
            
            bot_id = add_bot(name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type=strat_id, config_dict=config)
            if bot_id:
                st.success(f"Bot '{name}' deployed successfully! (ID: {bot_id})")
                st.info(f"Deployed on {market_type} - {pair} using {strategy_type} ({timeframe})")
            else:
                st.error(f"Failed to deploy bot. Name '{name}' might already exist.")

