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

    # --- ATR Planning Foundation (Foundation of Parameters) ---
    with st.expander("📊 ATR Planning Foundation", expanded=True):
        st.write("Use these values to baseline your Grid Range and First Entry Price.")
        try:
            from engine.strategies.mql4_strategy import MQL4Strategy
            # Fetch 1D data to get enough history for various TFs
            ohlcv_f = exchange.fetch_ohlcv(pair, timeframe='1h', limit=500)
            df_f = pd.DataFrame(ohlcv_f, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df_f['timestamp'] = pd.to_datetime(df_f['timestamp'], unit='ms')
            
            temp_strat_f = MQL4Strategy()
            atr_data = temp_strat_f.get_atr_foundation(df_f)
            
            # Display metrics
            m_cols = st.columns(4)
            for i, tf in enumerate(['4h', '1d', '3d', '5d']):
                with m_cols[i]:
                    st.metric(f"ATR ({tf})", f"{atr_data[tf]['atr']:.4f}")
                    move_p = atr_data[tf]['move_pct']
                    color = "normal" if abs(move_p) < 100 else "inverse"
                    st.caption(f"Range Pos: **{move_p:+.1f}%**")
                    st.caption(f"Vol %-tile: {atr_data[tf]['percentile']:.0f}%")
        except Exception as e:
            st.warning(f"Could not load ATR Foundation: {e}")

    # --- Configuration Sections ---
    # Wrap everything in a form to fix StreamlitAPIException and allow clean submission
    with st.form("deploy_bot_form"):
        st.subheader("General Settings")
        col1, col2 = st.columns(2)
        with col1:
            name = st.text_input("Bot Name", placeholder="e.g., Scalper_USDC_01")
            direction = st.selectbox("Direction", ["LONG", "SHORT"])
            strategy_type = st.selectbox("Strategy Logic", ["MQL4", "Classic"], help="MQL4: Uses the 11-trigger confluence system below. Classic: Simple RSI/CCI thresholds.")
            # Global/Base Timeframe (Execution speed)
            timeframe = st.selectbox("Execution Timeframe", ["1m", "5m", "15m", "1h", "4h", "1d"], index=1, help="Scanning frequency. 1m scans for entries every minute.")
        
        with col2:
            base_size = st.number_input("Base Order Size ($USDC)", min_value=1.0, step=10.0, value=10.0)
            martingale_multiplier = st.number_input("Martingale Multiplier", min_value=1.0, step=0.1, value=1.5)
            # Projection Table for Sizing
            from engine.strategies.mql4_strategy import MQL4Strategy
            temp_strat = MQL4Strategy(params={'base_size': base_size, 'martingale_multiplier': martingale_multiplier, 'direction': direction})
            
            try:
                current_price = df_f['close'].iloc[-1]
                # Try to get ATR for visual projection if available
                p_atr = atr_data.get(timeframe, {}).get('atr', 10.0)
                projections = temp_strat.calculate_projections(base_price=current_price, current_atr=p_atr)
                
                with st.expander("🔍 Risk Projection & Math Summary ($USDC)", expanded=False):
                    st.caption(f"Simulated Martingale Grid based on current price: **{current_price:,.2f}**")
                    proj_df = pd.DataFrame(projections)
                    
                    # Update display columns for clarity
                    proj_df.columns = ["Step", "Grid Price", "Order ($)", "Total Inv. ($)", "TP Price", "Hedge Size", "Is Hedge"]
                    st.table(proj_df)
                    
                    # Hedge Summary Instruction
                    hedge_steps = [p for p in projections if p['is_hedge']]
                    if hedge_steps:
                        h1 = hedge_steps[0]
                        st.info(f"🛡️ **Hedge Summary**: At Step {h1['step']} (Price: {h1['price']}), a hedge of **${h1['hedge_size_usdc']}** activates.")
                    else:
                        st.warning("⚠️ No Hedge configured.")
            except Exception as e:
                st.error(f"Projection Error: {e}")

            rsi_limit = st.slider("RSI Limit (for Classic)", 0, 100, 30, help="Only used if Strategy Logic is 'Classic'.")

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
            
            for p_idx in range(1, 5, 2): # 2 cols x 2 rows
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
                config['mode_atrp'] = st.selectbox("Market State", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Below (Quiet)", 2: "Above (Extreme)"}[x], help="Compares current ATR to the last 100 candles. 0-20% is very quiet; 80-100% is high-vol spike.", key="create_mode_atrp")
                a_col1, a_col2 = st.columns(2)
                config['atrp_level'] = a_col1.number_input("Lookback Level %", value=50.0, key="create_atrp_level")
                config['atrp_tf'] = a_col2.selectbox("ATR TF (T10)", ["15m","1h","4h","1d"], index=1, key="create_atrp_tf")

            st.divider()
            st.markdown("**Trigger 11: ATR Expansion (Current Move vs Range)**")
            e_col1, e_col2, e_col3 = st.columns(3)
            with e_col1:
                config['mode_atre'] = st.selectbox("Expansion Move", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Move Up >= X%", 2: "Move Down >= X%"}[x], help="Checks if high/low has moved X% of the ATR from the open of THIS candle.", key="create_mode_atre")
            with e_col2:
                config['atre_level'] = st.number_input("Target % of ATR", value=100.0, key="create_atre_level")
            with e_col3:
                config['atre_tf'] = st.selectbox("TF to Watch (T11)", ["1h","4h","1d"], index=0, key="create_atre_tf")
            
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
            
            st.divider()
            st.subheader("Advanced Re-entry & Cooldown")
            st.caption("Post-Exit behavior. Distance or Time based re-engagement.")
            r1, r2, r3 = st.columns(3)
            with r1:
                config['reentry_cooldown_mins'] = st.number_input("Cooldown (Mins)", value=0.0, help="Wait X minutes after exit before entering again.")
            with r2:
                config['reentry_distance_pct'] = st.number_input("Re-entry Dist (%)", value=0.0, help="Price must move X% away from exit price before re-entering.")
            with r3:
                config['post_exit_stop'] = st.checkbox("Stop After Cycle", value=False, help="Automatically stops the bot after one successful Take Profit.")

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

