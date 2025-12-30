import streamlit as st
import pandas as pd
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
            base_size = st.number_input("Base Order Size ($USDC)", min_value=1.0, step=10.0, value=10.0)
            martingale_multiplier = st.number_input("Martingale Multiplier", min_value=1.0, step=0.1, value=1.5)
            # Projection Table for Sizing
            from engine.strategies.mql4_strategy import MQL4Strategy
            temp_strat = MQL4Strategy(params={'base_size': base_size, 'martingale_multiplier': martingale_multiplier})
            projections = temp_strat.calculate_projections(steps=10)
            
            with st.expander("🔍 Risk Projection ($USDC)", expanded=False):
                st.caption("Martingale Growth & Total Investment")
                proj_df = pd.DataFrame(projections)
                proj_df.columns = ["Step", "Order Size ($)", "Total Invested ($)", "Hedge Req ($)"]
                st.table(proj_df)

            rsi_limit = st.slider("RSI Limit (for Classic)", 0, 100, 30)

        st.divider()
        config = {}

        with st.expander("Entry Triggers (Multi-Switch Confluence)", expanded=True):
            st.caption("ALL enabled switches below must align for an entry. Each works on its own timeframe.")
            
            st.markdown("### 1. Indicators")
            i_col1, i_col2, i_col3, i_col4 = st.columns(4)
            with i_col1: 
                config['mode_cci'] = st.selectbox("CCI Switch", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Above Level", 2: "Below Level"}[x], key="create_mode_cci")
                config['cci_level'] = st.number_input("CCI Level", value=100, key="create_cci_lvl")
                config['cci_tf'] = st.selectbox("CCI TF", ["1m","5m","15m","1h","4h","1d"], index=2, key="create_cci_tf")
            with i_col2: 
                config['mode_boll'] = st.selectbox("Boll Switch", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Outside Lower", 2: "Outside Upper"}[x], key="create_mode_boll")
                config['boll_tf'] = st.selectbox("Boll TF", ["1m","5m","15m","1h","4h","1d"], index=2, key="create_bb_tf")
            with i_col3: 
                config['mode_stoch'] = st.selectbox("Stoch Switch", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Oversold (DN)", 2: "Overbought (UP)"}[x], key="create_mode_stoch")
                config['stoch_tf'] = st.selectbox("Stoch TF", ["1m","5m","15m","1h","4h","1d"], index=2, key="create_stoch_tf")
            with i_col4: 
                config['mode_rsi'] = st.selectbox("RSI Switch", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Below Level", 2: "Above Level"}[x], key="create_mode_rsi")
                config['rsi_level'] = st.number_input("RSI Level", value=30, key="create_rsi_lvl")
                config['rsi_tf'] = st.selectbox("RSI TF", ["1m","15m","1h"], index=1, key="create_rsi_tf")

            st.divider()
            st.markdown("### 2. Consecutive Pattern Slots")
            st.caption("Entries will wait for X consecutive green/red candles on specified TFs.")
            
            for p_idx in range(1, 4, 2): # 2 rows of 2
                pc1, pc2 = st.columns(2)
                for i, col in enumerate([pc1, pc2]):
                    idx = p_idx + i
                    with col:
                        st.markdown(f"**Slot {idx}**")
                        c_p1, c_p2, c_p3 = st.columns(3)
                        config[f'pat_{idx}_mode'] = c_p1.selectbox(f"Type ##{idx}", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Consec. Up", 2: "Consec. Down"}[x], key=f"create_p_mode_{idx}")
                        config[f'pat_{idx}_tf'] = c_p2.selectbox(f"TF ##{idx}", ["1m","5m","15m","1h","4h","1d"], index=1, key=f"create_p_tf_{idx}")
                        config[f'pat_{idx}_count'] = c_p3.number_input(f"Count ##{idx}", min_value=1, value=3, key=f"create_p_count_{idx}")
            
            # Use current config for refined projection
            temp_strat.params.update(config)
            projections = temp_strat.calculate_projections(steps=10)
            
            st.divider()
            # Legacy Indicator Parameters removed in favor of 8-trigger system


        with st.expander("Risk Management (Martingale & Grid)", expanded=False):
            st.subheader("Dynamic Grid (ATR)")
            config['UseATRGrid'] = st.checkbox("Use ATR Dynamic Grid", value=True)
            config['ATRGridFactor'] = st.number_input("ATR Grid Factor", value=1.0)
            
        with st.expander("Trade Management (Exit & Hedge)", expanded=False):
            st.subheader("Accelerated Early Exit (Smart Decay)")
            config['UseEarlyExit'] = st.checkbox("Enable Early Exit", value=True, help="Moves TP target closer to Break Even over time to exit stale trades safely.")
            col_ee1, col_ee2 = st.columns(2)
            with col_ee1:
                config['DecayIntervalMins'] = st.number_input("Decay Interval (Mins)", value=15.0, help="How often (in minutes) the profit target is reduced.")
            with col_ee2:
                config['DecayPercentPerInterval'] = st.number_input("Reduction (%) per Interval", value=30.0, help="What percentage of the current profit target to cut per interval.")
            
            st.subheader("Moving Profit")
            config['MaximizeProfit'] = st.checkbox("Use Moving Profit Target", value=False)
            config['ProfitSet'] = st.slider("Profit Set % (Lock in)", 0.1, 0.9, 0.5)
            
            st.subheader("Hedging")
            config['UseHedge'] = st.checkbox("Use Hedging", value=False, help="Locks drawdown by opening an opposite trade of equal size when grid depth is reached.")
            config['HedgeStartStep'] = st.number_input("Hedge Start Step (1-10)", min_value=1, max_value=10, value=7, help="Which Martingale step triggers the hedge trade.")
            config['HedgeStart'] = st.number_input("Hedge Start (DD%)", value=20.0, help="Alternative trigger: Drawdown percentage to start hedging.")

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

