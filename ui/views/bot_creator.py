import streamlit as st
import pandas as pd
import numpy as np
import sys
import os

# Ensure engine can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine.database import add_bot
from engine.exchange_interface import ExchangeInterface

def render_bot_creator_view():
    st.header("🏗️ Strategy & Bot Creator")
    st.write("Configure and launch new trading bots here.")
    
    # Dynamic Market Selection
    col_m1, col_m2, col_m3 = st.columns(3)
    with col_m1:
        market_type = st.selectbox("Market Type", ["Spot", "Futures (Swap)"], index=0)
        mode_id = 'spot' if market_type == "Spot" else 'future'
        
        # Testnet Toggle
        from config.settings import config as global_config
        is_testnet = st.checkbox("Use Testnet", value=global_config.TESTNET)
        if is_testnet:
             st.caption("⚠️ TESTNET MODE ACTIVE")
             
    with col_m2:
        quote_asset = st.selectbox("Quote Asset", ["USDT", "USDC"])
    with col_m3:
        # Fetch symbols dynamically based on selection
        try:
            exchange = ExchangeInterface(market_type=mode_id)
            # Ensure markets are loaded before querying symbols
            exchange.exchange.load_markets()
            available_pairs = exchange.get_available_symbols(quote_asset=quote_asset)
            if not available_pairs:
                st.warning("No pairs found. Check connection or API keys.")
                available_pairs = [f"BTC/{quote_asset}", f"ETH/{quote_asset}"] # Fallback
        except Exception as e:
            st.error(f"Error fetching symbols: {e}")
            available_pairs = [f"BTC/{quote_asset}"]
            exchange = None # Initialize to avoid UnboundLocalError

        pair = st.selectbox("Trading Pair", available_pairs)

    # --- ATR Planning Foundation (Foundation of Parameters) ---
    df_f = None
    atr_data = {}
    current_price = 0.0 
    p_atr = 10.0        
    
    with st.expander("📊 ATR Planning Foundation", expanded=True):
        st.write("Use these values to baseline your Grid Range and First Entry Price.")
        try:
            if exchange:
                from engine.strategies.mql4_strategy import MQL4Strategy
                # Fetch 1D data to get enough history for various TFs
                ohlcv_f = exchange.fetch_ohlcv(pair, timeframe='1h', limit=500)
                if ohlcv_f and isinstance(ohlcv_f, list):
                    df_f = pd.DataFrame(ohlcv_f, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df_f['timestamp'] = pd.to_datetime(df_f['timestamp'], unit='ms')
                    
                    temp_strat_f = MQL4Strategy()
                    atr_data = temp_strat_f.get_atr_foundation(df_f)
                    
                    # Display metrics
                    m_cols = st.columns(4)
                    for i, tf in enumerate(['4h', '1d', '3d', '5d']):
                        with m_cols[i]:
                            if tf in atr_data:
                                st.metric(f"ATR ({tf})", f"{atr_data[tf]['atr']:.4f}")
                                move_p = atr_data[tf]['move_pct']
                                st.caption(f"Range Pos: **{move_p:+.1f}%**")
                                st.caption(f"Vol %-tile: {atr_data[tf]['percentile']:.0f}%")
                else:
                    st.warning("No OHLCV data returned from exchange.")
        except Exception as e:
            st.warning(f"Could not load ATR Foundation: {e}")

    # --- Configuration Sections ---
    # Initialize config dictionary early to avoid UnboundLocalError
    config = {}
    
    with st.form("deploy_bot_form"):
        st.subheader("General Settings")
        col1, col2 = st.columns(2)
        with col1:
            name = st.text_input("Bot Name", placeholder="e.g., Scalper_USDC_01")
            direction = st.selectbox("Direction", ["LONG", "SHORT"])
            strategy_type = st.selectbox("Strategy Logic", ["MQL4", "Market Maker"], help="MQL4: 11-Trigger Confluence. MM: Spread-based Market Making.")
            timeframe = st.selectbox("Execution Timeframe", ["1m", "5m", "15m", "1h", "4h", "1d"], index=1, help="Scanning frequency.")
        
            with col2:
                base_size = st.number_input("Base Order Size ($USDC)", min_value=1.0, step=10.0, value=10.0)
                martingale_multiplier = st.number_input("Martingale Multiplier", min_value=1.0, step=0.1, value=1.5)
                
                # --- NEW: Take Profit Input with Selection ---
                st.markdown("**Take Profit Logic**")
                tp_type = st.radio("TP Mode", ["Dollar Target ($)", "Percentage (%)"], index=0, horizontal=True)
                
                if tp_type == "Dollar Target ($)":
                    take_profit_base = st.number_input("Take Profit Target ($USDC)", min_value=1.0, step=1.0, value=10.0, help="Dollar Profit Target per Cycle")
                    config['TakeProfitBase'] = take_profit_base
                    config['TakeProfitType'] = 'USD'
                    # Explicitly define for scope safety
                    take_profit_pct = 0.0 
                else:
                    take_profit_pct = st.number_input("Take Profit Target (%)", min_value=0.1, step=0.1, value=1.0, help="Percentage Profit Target per Cycle")
                    config['TakeProfitPct'] = take_profit_pct
                    config['TakeProfitType'] = 'Percent'
                    # Explicitly define for scope safety
                    take_profit_base = 0.0

                # Projection Table for Sizing
                from engine.strategies.mql4_strategy import MQL4Strategy
                
                # Pass params for projection
                proj_params = {
                    'base_size': base_size, 
                    'martingale_multiplier': martingale_multiplier, 
                    'direction': direction,
                    'UseHedge': False # Will update below
                }
                
                if tp_type == "Dollar Target ($)":
                    proj_params['TakeProfitBase'] = take_profit_base
                else:
                    proj_params['TakeProfitPct'] = take_profit_pct
                    
                temp_strat = MQL4Strategy(params=proj_params)
        
        try:
            if df_f is not None and not df_f.empty:
                current_price = df_f['close'].iloc[-1]
                p_atr = atr_data.get(timeframe, {}).get('atr', 10.0)
                projections = temp_strat.calculate_projections(base_price=current_price, current_atr=p_atr)
                
                with st.expander("🔍 Risk Projection & Math Summary ($USDC)", expanded=False):
                    st.caption(f"Simulated Martingale Grid based on current price: **{current_price:,.2f}**")
                    proj_df = pd.DataFrame(projections)
                    
                    if not proj_df.empty:
                        proj_df.columns = ["Step", "Grid Price", "Order ($)", "Total Inv. ($)", "TP Price", "Hedge Size", "Is Hedge"]
                        st.table(proj_df)
                    
                    hedge_steps = [p for p in projections if p['is_hedge']]
                    if hedge_steps:
                        h1 = hedge_steps[0]
                        st.info(f"🛡️ **Hedge Summary**: At Step {h1['step']} (Price: {h1['price']}), a hedge of **${h1['hedge_size_usdc']}** activates.")
                    else:
                        st.warning("⚠️ No Hedge configured.")
            else:
                st.info("Market data unavailable for projection.")
        except Exception as e:
            st.error(f"Projection Error: {e}")

        rsi_limit = st.slider("RSI Limit (for Classic)", 0, 100, 30, help="Only used if Strategy Logic is 'Classic'.")

        st.divider()
        # config = {} # REMOVED: Re-initialization cleared previous values

        if strategy_type == "Market Maker":
            with st.expander("Market Maker Configuration", expanded=True):
                mm_c1, mm_c2 = st.columns(2)
                with mm_c1:
                    config['spread_pct'] = st.number_input("Target Spread (%)", value=0.2, step=0.01)
                    config['skew_factor'] = st.number_input("Inventory Skew Factor", value=0.0, step=1.0, help="Shift price per unit of inventory.")
                with mm_c2:
                    config['order_size'] = base_size 
                    config['max_inventory'] = st.number_input("Max Inventory (Units)", value=1.0)
                    config['reprice_threshold'] = st.number_input("Reprice Threshold (%)", value=0.1)

        elif strategy_type == "MQL4":
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
            
            for p_idx in range(1, 5, 2): 
                pc1, pc2 = st.columns(2)
                for i, col in enumerate([pc1, pc2]):
                    idx = p_idx + i
                    if idx > 4: continue
                    with col:
                        st.markdown(f"**Pattern Slot {idx}**")
                        c_p1, c_p2, c_p3, c_p4 = st.columns(4)
                        config[f'pat_{idx}_mode'] = c_p1.selectbox(f"Type ##{idx}", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Up", 2: "Down"}[x], key=f"create_p_mode_{idx}")
                        config[f'pat_{idx}_source'] = c_p2.selectbox(f"Source ##{idx}", ["Price", "RSI", "CCI"], index=0, key=f"create_p_src_{idx}")
                        config[f'pat_{idx}_tf'] = c_p3.selectbox(f"TF ##{idx}", ["1m","5m","15m","1h","4h","1d"], index=1, key=f"create_p_tf_{idx}")
                        config[f'pat_{idx}_count'] = c_p4.number_input(f"Count ##{idx}", min_value=1, value=3, key=f"create_p_count_{idx}")

            st.divider()
            st.markdown("### 3. Price & Volatility Triggers")
            v_col1, v_col2 = st.columns(2)
            with v_col1:
                st.markdown("**Trigger 9: Price Threshold**")
                config['mode_price'] = st.selectbox("Price Switch", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Above", 2: "Below"}[x], key="create_mode_price")
                config['price_threshold'] = st.number_input("Threshold Price", value=0.0, key="create_price_threshold")
            with v_col2:
                st.markdown("**Trigger 10: Volatility Relative Percentile**")
                config['mode_atrp'] = st.selectbox("Market State", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Below (Quiet)", 2: "Above (Extreme)"}[x], key="create_mode_atrp")
                a_col1, a_col2 = st.columns(2)
                config['atrp_level'] = a_col1.number_input("Lookback Level %", value=50.0, key="create_atrp_level")
                config['atrp_tf'] = a_col2.selectbox("ATR TF (T10)", ["15m","1h","4h","1d"], index=1, key="create_atrp_tf")

            st.divider()
            st.markdown("**Trigger 11: ATR Expansion (Current Move vs Range)**")
            e_col1, e_col2, e_col3 = st.columns(3)
            with e_col1:
                config['mode_atre'] = st.selectbox("Expansion Move", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Move Up >= X%", 2: "Move Down >= X%"}[x], key="create_mode_atre")
            with e_col2:
                config['atre_level'] = st.number_input("Target % of ATR", value=100.0, key="create_atre_level")
            with e_col3:
                config['atre_tf'] = st.selectbox("TF to Watch (T11)", ["1h","4h","1d"], index=0, key="create_atre_tf")
            
            temp_strat.params.update(config)
            if df_f is not None and not df_f.empty and current_price > 0:
               projections = temp_strat.calculate_projections(base_price=current_price, current_atr=p_atr)
            
            st.divider()

        with st.expander("Risk Management (Martingale & Grid)", expanded=False):
            st.subheader("Grid Logic")
            config['UseATRGrid'] = st.checkbox("Use ATR Dynamic Grid", value=True, help="If OFF, uses fixed 'Base Grid' distance.")
            
            g_col1, g_col2 = st.columns(2)
            with g_col1:
                config['ATRGridFactor'] = st.number_input("ATR Grid Factor", value=1.0, step=0.1, help="Multiplier for ATR. < 1.0 = tighter grid.")
            with g_col2:
                config['base_grid'] = st.number_input("Fixed Grid Step (Price)", value=100.0, step=10.0, help="Used if ATR Grid is OFF. Absolute price change.")
            
        with st.expander("Trade Management (Exit & Hedge)", expanded=False):
            st.subheader("Accelerated Early Exit (Smart Decay)")
            config['UseEarlyExit'] = st.checkbox("Enable Early Exit", value=True)
            col_ee1, col_ee2 = st.columns(2)
            with col_ee1:
                config['DecayIntervalMins'] = st.number_input("Decay Interval (Mins)", value=15.0)
            with col_ee2:
                config['DecayPercentPerInterval'] = st.number_input("Reduction (%) per Interval", value=30.0)
            
            st.subheader("Moving Profit")
            config['MaximizeProfit'] = st.checkbox("Use Moving Profit Target", value=False)
            config['ProfitSet'] = st.slider("Profit Set % (Lock in)", 0.1, 0.9, 0.5)
            
            st.divider()
            st.subheader("Advanced Re-entry & Cooldown")
            r1, r2, r3 = st.columns(3)
            with r1:
                config['reentry_cooldown_mins'] = st.number_input("Cooldown (Mins)", value=0.0)
            with r2:
                config['reentry_distance_pct'] = st.number_input("Re-entry Dist (%)", value=0.0)
            with r3:
                config['post_exit_stop'] = st.checkbox("Stop After Cycle", value=False)

            st.subheader("Hedging")
            use_hedge = st.checkbox("Use Hedging", value=False)
            config['UseHedge'] = use_hedge
            config['HedgeStartStep'] = st.number_input("Hedge Start Step (1-10)", min_value=1, max_value=10, value=7)
            config['HedgeStart'] = st.number_input("Hedge Start (DD%)", value=20.0)

            # Update Temp Strat for Projection if Hedging is toggled
            if use_hedge:
                 temp_strat.params['UseHedge'] = True
                 temp_strat.params['HedgeStartStep'] = config['HedgeStartStep']
                 # Re-run projection to show hedge
                 if df_f is not None and not df_f.empty and current_price > 0:
                     projections = temp_strat.calculate_projections(base_price=current_price, current_atr=p_atr)

        submitted = st.form_submit_button("Deploy Bot", type="primary")
    
    if submitted:
        if not name:
            st.error("Bot Name is required.")
        else:
            config['timeframe'] = timeframe
            strat_id = "MarketMaker" if strategy_type == "Market Maker" else "MQL4"
            config['market_type'] = 'spot' if mode_id == 'spot' else 'futures'
            
            # Explicitly mapping all required positional arguments as keywords
            # matches engine\database.py definition
            bot_id = add_bot(
                name=name,
                pair=pair,
                direction=direction,
                rsi_limit=rsi_limit,
                martingale_multiplier=martingale_multiplier,
                base_size=base_size,
                strategy_type=strat_id,
                config_dict=config
            )

            if bot_id:
                st.success(f"Bot '{name}' deployed successfully! (ID: {bot_id})")
                st.info(f"Deployed on {market_type} - {pair} using {strategy_type} ({timeframe})")
            else:
                st.error(f"Failed to deploy bot. Name '{name}' might already exist.")
