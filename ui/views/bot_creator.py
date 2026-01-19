import streamlit as st
import pandas as pd
import sys
import os

# Ensure engine can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine.database import add_bot
from engine.exchange_interface import ExchangeInterface

def render_bot_creator_view():
    st.header("🏗️ Strategy & Bot Creator")
    st.caption("Configure and launch new trading bots with advanced martingale and confluence logic.")
    
    st.divider()
    
    # Dynamic Market Selection
    st.subheader("🌐 Market Configuration")
    col_m1, col_m2, col_m3 = st.columns(3)
    with col_m1:
        from config.settings import config as global_config
        market_type = st.selectbox(
            "Market Type",
            ["Spot", "Futures (Swap)"],
            index=0 if global_config.MARKET_TYPE == 'spot' else 1,
            help="Choose Spot (for USDC pairs) or Futures (for USDT pairs)"
        )
        mode_id = 'spot' if market_type == "Spot" else 'future'

        # Store in session for consistency with bot_manager
        st.session_state['market_type'] = market_type

        # Testnet Toggle
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

            # Store market type in session for bot_manager consistency
            st.session_state['market_type'] = mode_id
            available_pairs = exchange.get_available_symbols(quote_asset=quote_asset)
            if not available_pairs:
                st.warning("No pairs found. Check connection or API keys.")
                available_pairs = [f"BTC/{quote_asset}", f"ETH/{quote_asset}"] # Fallback
        except Exception as e:
            st.error(f"Error fetching symbols: {e}")
            available_pairs = [f"BTC/{quote_asset}"]
            exchange = None # Initialize to avoid UnboundLocalError
        
        # Refresh Button for Pairs
        if st.button("🔄 Refresh Pairs", help="Force reload of market pairs"):
             try:
                 temp_ex = ExchangeInterface(market_type=mode_id)
                 temp_ex.exchange.load_markets(reload=True)
                 st.success("Markets reloaded!")
                 st.rerun()
             except Exception as e:
                 st.error(f"Reload failed: {e}")

        pair = st.selectbox("Trading Pair", available_pairs)

        # Dynamic Min Order Calculation
        min_order_usd = 5.0
        if exchange and pair:
            try:
                min_order_usd = exchange.get_min_order_usd(pair)
            except Exception as e:
                pass # Fallback to default


    # --- ATR Planning Foundation (Foundation of Parameters) ---
    df_f = None
    atr_data = {}
    current_price = 0.0 
    p_atr = 10.0        
    
    # --- Configuration Sections ---
    # Initialize config dictionary VERY early to avoid UnboundLocalError
    # This dictionary stores all user selections for the new bot
    bot_config = {}
    
    with st.expander("📊 ATR Planning Foundation (Market Context)", expanded=True):
        st.info("💡 Use these live values to baseline your Grid Range and First Entry Price.")
        try:
            if exchange:
                from engine.strategies.martingale_strategy import MartingaleStrategy
                # Fetch data for foundation
                # Hybrid Fetch: 1h for 4h/1d, 1d for 3d/5d
                ohlcv_1h = exchange.fetch_ohlcv(pair, timeframe='1h', limit=500)
                ohlcv_1d = exchange.fetch_ohlcv(pair, timeframe='1d', limit=100)
                
                if ohlcv_1h and ohlcv_1d:
                    df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df_1d = pd.DataFrame(ohlcv_1d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    for df_temp in [df_1h, df_1d]:
                        df_temp['timestamp'] = pd.to_datetime(df_temp['timestamp'], unit='ms')
                    
                    temp_strat_f = MartingaleStrategy()
                    atr_data = {}
                    # 4h from 1h data
                    res_4h = temp_strat_f.get_atr_foundation(df_1h)
                    if '4h' in res_4h: atr_data['4h'] = res_4h['4h']
                    # 1d, 3d, 5d from 1d data
                    res_daily = temp_strat_f.get_atr_foundation(df_1d)
                    for tf in ['1d', '3d', '5d']:
                        if tf in res_daily: atr_data[tf] = res_daily[tf]
                    
                    # Store selected ATR TF in config
                    st.markdown("**Grid ATR Context**")
                    atr_tf_options = ["1m", "5m", "15m", "1h", "4h", "1d"]
                    selected_atr_tf = st.selectbox("ATR Timeframe for Grid", atr_tf_options, index=3, key="bot_creator_atr_tf")
                    bot_config['ATR_Timeframe'] = selected_atr_tf
                    
                    # Display metrics
                    foundation_tfs = ['4h', '1d', '3d', '5d']
                    m_cols = st.columns(len(foundation_tfs))
                    for i, tf in enumerate(foundation_tfs):
                        with m_cols[i]:
                            if tf in atr_data:
                                # Highlight if matches current grid TF
                                label = f"ATR ({tf})"
                                if tf == selected_atr_tf: label = f"🎯 **ATR ({tf})**"
                                st.metric(label, f"{atr_data[tf]['atr']:.4f}")
                                move_p = atr_data[tf]['move_pct']
                                st.caption(f"Range Pos: **{move_p:+.1f}%**")
                                st.caption(f"Vol %-tile: {atr_data[tf]['percentile']:.0f}%")

                else:
                    st.warning("No OHLCV data returned from exchange.")
        except Exception as e:
            st.warning(f"Could not load ATR Foundation: {e}")

    # --- Configuration Sections ---
    # bot_config is initialized above in the expander
    
    st.divider()
    
    with st.form("deploy_bot_form"):
        st.subheader("⚙️ General Settings")

        
        # --- Visual Strategy Selector (Cards) ---
        st.markdown("### 🧠 Select Strategy Logic")
        
        # Custom CSS for Strategy Cards
        st.markdown("""
        <style>
        .strat-card {
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 15px;
            background-color: #161b22;
            height: 100%;
            text-align: center;
        }
        .strat-icon { font-size: 2em; margin-bottom: 10px; }
        .strat-title { font-weight: bold; font-size: 1.1em; color: #58a6ff; margin-bottom: 5px; }
        .strat-desc { font-size: 0.9em; color: #8b949e; }
        </style>
        """, unsafe_allow_html=True)
        
        strat_col1, strat_col2, strat_col3 = st.columns(3)
        
        # This is a visual trick; the actual selection is via radio button below, but we format it nicely
        with strat_col1:
            st.markdown("""
            <div class="strat-card">
                <div class="strat-icon">🛡️</div>
                <div class="strat-title">Martingale Grid</div>
                <div class="strat-desc">Confluence of RSI, CCI, Bollinger. Best for conservative entries.</div>
            </div>
            """, unsafe_allow_html=True)
            
        with strat_col2:
            st.markdown("""
            <div class="strat-card">
                <div class="strat-icon">📈</div>
                <div class="strat-title">Market Maker</div>
                <div class="strat-desc">High-frequency spread capturing. Best for ranging markets.</div>
            </div>
            """, unsafe_allow_html=True)
            
        with strat_col3:
            st.markdown("""
            <div class="strat-card">
                <div class="strat-icon">🕰️</div>
                <div class="strat-title">Magic Hour</div>
                <div class="strat-desc">Session breakout & mean reversion. Time-based statistical edge.</div>
            </div>
            """, unsafe_allow_html=True)
            
        strategy_type = st.radio(
            "Select Strategy Logic",
            ["Martingale", "Market Maker", "Magic Hour"],
            label_visibility="collapsed",
            horizontal=True
        )
        
        st.divider()
        
        col1, col2 = st.columns(2)
        with col1:
            name = st.text_input("Bot Name", placeholder="e.g., Scalper_USDC_01")
            direction = st.selectbox("Direction", ["LONG", "SHORT"])
            # strategy_type removed from here, moved up
            timeframe = st.selectbox("Execution Timeframe", ["1m", "5m", "15m", "1h", "4h", "1d"], index=0, help="Scanning frequency.")
        
            # Leverage Input (Only for Futures)
            if mode_id == 'future':
                leverage = st.slider("Leverage (x)", min_value=1, max_value=50, value=20, help="Leverage multiplier. Ensure your account allows this level.")
                bot_config['leverage'] = leverage
            else:
                bot_config['leverage'] = 1 # Spot is always 1x


        with col2:
            base_size = st.number_input(f"Base Order Size (Min: ${min_order_usd:.2f})", min_value=min_order_usd, step=1.0, value=max(10.0, min_order_usd))
            if base_size < min_order_usd:
                    st.warning(f"Order size below minimum ${min_order_usd:.2f}")

            martingale_multiplier = st.number_input("Martingale Multiplier", min_value=1.0, step=0.1, value=1.8)

            max_steps = st.number_input("Max Martingale Steps", min_value=1, max_value=20, value=10, help="Maximum number of safety orders (DCA layers).")
            bot_config['max_steps'] = max_steps
            
            # --- NEW: Order Chasing Config ---
            st.markdown("**Order Execution**")
            chase_str = st.text_input("Chase Intervals (sec)", value="10, 5, 2", help="Seconds to wait for each limit order retry before moving to market.")
            try:
                bot_config['chase_intervals'] = [int(x.strip()) for x in chase_str.split(',')]
            except:
                bot_config['chase_intervals'] = [10, 5, 2]

                
            # --- NEW: Take Profit Input with Selection ---
            st.markdown("**Take Profit Logic**")
            tp_type = st.radio("TP Mode", ["Dollar Target ($)", "Percentage (%)"], index=0, horizontal=True)
            
            if tp_type == "Dollar Target ($)":
                take_profit_base = st.number_input("Take Profit Target ($USDC)", min_value=0.1, step=0.1, value=0.1, help="Dollar Profit Target per Cycle. Note: $10 profit on a $10 trade = 100% gain!")
                bot_config['TakeProfitBase'] = take_profit_base
                bot_config['TakeProfitType'] = 'USD'
                # Explicitly define for scope safety
                take_profit_pct = 0.0 
            else:
                st.caption("✅ Low percentage values (e.g. 0.1%) are allowed.")
                take_profit_pct = st.number_input("Take Profit Target (%) 🎯", min_value=0.01, step=0.01, value=1.5, format="%.2f", key="tp_pct_fixed_v5", help="Percentage Profit Target per Cycle")

                bot_config['TakeProfitPct'] = take_profit_pct
                bot_config['TakeProfitType'] = 'Percent'
                # Explicitly define for scope safety
                take_profit_base = 0.0

            # Projection Table for Sizing
            from engine.strategies.martingale_strategy import MartingaleStrategy
            
            # Pass params for projection
            proj_params = {
                'base_size': base_size, 
                'martingale_multiplier': martingale_multiplier, 
                'max_steps': max_steps,
                'direction': direction,
                'UseHedge': False # Will update below
            }
            
            if tp_type == "Dollar Target ($)":
                proj_params['TakeProfitBase'] = take_profit_base
            else:
                proj_params['TakeProfitPct'] = take_profit_pct
                
            temp_strat = MartingaleStrategy(params=proj_params)
        
            # Projection Logic Moved to Bottom to capture all configs


        # rsi_limit = st.slider("RSI Limit (for Classic)", 0, 100, 30, help="Only used if Strategy Logic is 'Classic'.")
        rsi_limit = 30 # Default/Legacy value hidden from UI
        
        st.divider()
        # config = {} # REMOVED: Re-initialization cleared previous values

        if strategy_type == "Market Maker":
            with st.expander("📈 Market Maker Configuration", expanded=True):
                mm_c1, mm_c2 = st.columns(2)
                with mm_c1:
                    bot_config['spread_pct'] = st.number_input("Target Spread (%)", value=0.2, step=0.01)
                    bot_config['skew_factor'] = st.number_input("Inventory Skew Factor", value=0.0, step=1.0, help="Shift price per unit of inventory.")
                with mm_c2:
                    bot_config['order_size'] = base_size 
                    bot_config['max_inventory'] = st.number_input("Max Inventory (Units)", value=1.0)
                    bot_config['reprice_threshold'] = st.number_input("Reprice Threshold (%)", value=0.1)

        elif strategy_type == "Magic Hour":
            with st.expander("🕰️ Magic Hour Configuration", expanded=True):
                st.info("🎯 **Strategy Goal:** Capture mean reversion after breakout from a specific hourly range.")
                
                # Timezone Selector
                common_tzs = ["Asia/Taipei", "America/New_York", "Europe/London", "Asia/Tokyo", "UTC"]
                selected_tz = st.selectbox("🌍 Strategy Timezone", common_tzs, index=0)
                bot_config['timezone'] = selected_tz
                
                mh1, mh2 = st.columns(2)
                with mh1:
                    bot_config['magic_hour'] = st.slider(f"🕒 Magic Hour ({selected_tz} 0-23)", 0, 23, 9, help=f"The specific hour that defines the trading range (e.g. 9 = 09:00-10:00 {selected_tz}).")
                    bot_config['analysis_duration'] = st.slider("⏳ Analysis Window (Hours)", 1, 6, 3, help="Duration to monitor for breakouts after the Magic Hour closes.")
                with mh2:
                    bot_config['stop_loss_ext'] = st.number_input("🛑 Max Extension (Fade Zone)", value=1.0, step=0.1, help="Allowed deviation multiplier. If Price > High + (Range * Extension), we assume strong trend and STOP fading.")
                    st.success(f"✅ Target is fixed at **50% Mean Reversion** (Range Midpoint).")

        elif strategy_type == "Martingale":
            with st.expander("Entry Triggers (Multi-Switch Confluence)", expanded=True):
                st.caption("ALL enabled switches below must align for an entry. Each works on its own timeframe.")
                st.markdown("### 1. Indicators")
            i_col1, i_col2, i_col3, i_col4 = st.columns(4)
            with i_col1: 
                bot_config['mode_cci'] = st.selectbox("CCI Switch", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Above Level", 2: "Below Level"}[x], key="create_mode_cci")
                bot_config['cci_level'] = st.number_input("CCI Level", value=100, key="create_cci_lvl")
                bot_config['cci_tf'] = st.selectbox("CCI TF", ["1m","5m","15m","1h","4h","1d"], index=2, key="create_cci_tf")
            with i_col2: 
                bot_config['mode_boll'] = st.selectbox("Boll Switch", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Outside Lower", 2: "Outside Upper"}[x], key="create_mode_boll")
                bot_config['boll_tf'] = st.selectbox("Boll TF", ["1m","5m","15m","1h","4h","1d"], index=2, key="create_bb_tf")
            with i_col3: 
                bot_config['mode_stoch'] = st.selectbox("Stoch Switch", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Oversold (DN)", 2: "Overbought (UP)"}[x], key="create_mode_stoch")
                bot_config['stoch_tf'] = st.selectbox("Stoch TF", ["1m","5m","15m","1h","4h","1d"], index=2, key="create_stoch_tf")
            with i_col4: 
                bot_config['mode_rsi'] = st.selectbox("RSI Switch", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Below Level", 2: "Above Level"}[x], key="create_mode_rsi")
                bot_config['rsi_level'] = st.number_input("RSI Level", value=30, key="create_rsi_lvl")
                bot_config['rsi_tf'] = st.selectbox("RSI TF", ["1m","15m","1h"], index=1, key="create_rsi_tf")

            st.divider()
            st.markdown("### 📊 2. Consecutive Pattern Slots")
            st.caption("📈 Entries will wait for X consecutive green/red candles on specified TFs.")
            
            for p_idx in range(1, 5, 2): 
                pc1, pc2 = st.columns(2)
                for i, col in enumerate([pc1, pc2]):
                    idx = p_idx + i
                    if idx > 4: continue
                    with col:
                        st.markdown(f"**Pattern Slot {idx}**")
                        c_p1, c_p2, c_p3, c_p4 = st.columns(4)
                        bot_config[f'pat_{idx}_mode'] = c_p1.selectbox(f"Type ##{idx}", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Up", 2: "Down"}[x], key=f"create_p_mode_{idx}")
                        bot_config[f'pat_{idx}_source'] = c_p2.selectbox(f"Source ##{idx}", ["Price", "RSI", "CCI"], index=0, key=f"create_p_src_{idx}")
                        bot_config[f'pat_{idx}_tf'] = c_p3.selectbox(f"TF ##{idx}", ["1m","5m","15m","1h","4h","1d"], index=1, key=f"create_p_tf_{idx}")
                        bot_config[f'pat_{idx}_count'] = c_p4.number_input(f"Count ##{idx}", min_value=1, value=3, key=f"create_p_count_{idx}")

            st.divider()
            st.markdown("### 3. Price & Volatility Triggers")
            v_col1, v_col2 = st.columns(2)
            with v_col1:
                st.markdown("**Trigger 9: Price Threshold**")
                bot_config['mode_price'] = st.selectbox("Price Switch", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Above", 2: "Below"}[x], key="create_mode_price")
                bot_config['price_threshold'] = st.number_input("Threshold Price", value=0.0, key="create_price_threshold")
            with v_col2:
                st.markdown("**Trigger 10: Volatility Relative Percentile**")
                bot_config['mode_atrp'] = st.selectbox("Market State", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Below (Quiet)", 2: "Above (Extreme)"}[x], key="create_mode_atrp")
                a_col1, a_col2 = st.columns(2)
                bot_config['atrp_level'] = a_col1.number_input("Lookback Level %", value=50.0, key="create_atrp_level")
                bot_config['atrp_tf'] = a_col2.selectbox("ATR TF (T10)", ["15m","1h","4h","1d"], index=1, key="create_atrp_tf")

            st.divider()
            st.markdown("**Trigger 11: ATR Expansion (Current Move vs Range)**")
            e_col1, e_col2, e_col3 = st.columns(3)
            with e_col1:
                bot_config['mode_atre'] = st.selectbox("Expansion Move", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Move Up >= X%", 2: "Move Down >= X%"}[x], key="create_mode_atre")
            with e_col2:
                bot_config['atre_level'] = st.number_input("Target % of ATR", value=100.0, key="create_atre_level")
            with e_col3:
                bot_config['atre_tf'] = st.selectbox("TF to Watch (T11)", ["1h","4h","1d"], index=0, key="create_atre_tf")
            
            temp_strat.params.update(bot_config)

            if df_f is not None and not df_f.empty and current_price > 0:
               projections = temp_strat.calculate_projections(base_price=current_price, current_atr=p_atr)
            
            st.divider()

        with st.expander("Risk Management (Martingale & Grid)", expanded=False):
            st.subheader("Grid Logic")
            bot_config['UseATRGrid'] = st.checkbox("Use ATR Dynamic Grid", value=True, help="If OFF, uses fixed 'Base Grid' distance.")
            
            g_col1, g_col2 = st.columns(2)
            with g_col1:
                bot_config['ATRGridFactor'] = st.number_input("ATR Grid Factor", value=1.1, step=0.1, help="Multiplier for ATR. < 1.0 = tighter grid.")

            with g_col2:
                if not bot_config['UseATRGrid']:
                     bot_config['base_grid'] = st.number_input("Fixed Grid Step (Price)", value=100.0, step=10.0, help="Used if ATR Grid is OFF. Absolute price change.")
                else:
                     bot_config['base_grid'] = 100.0 # Default hidden
            
            # --- New ATR Timeframe Selector ---
            if bot_config['UseATRGrid']:
                bot_config['ATR_Timeframe'] = st.selectbox("ATR Timeframe for Grid", ["1m", "5m", "15m", "1h", "4h", "1d"], index=3, help="Timeframe used to calculate ATR for grid spacing. Lower timeframe = tighter grid.")
            # ----------------------------------
            
        with st.expander("Trade Management (Exit & Hedge)", expanded=False):
            st.subheader("Accelerated Early Exit (Smart Decay)")
            bot_config['UseEarlyExit'] = st.checkbox("Enable Early Exit", value=True)
            col_ee1, col_ee2 = st.columns(2)
            with col_ee1:
                bot_config['DecayIntervalMins'] = st.number_input("Decay Interval (Mins)", value=15.0)
            with col_ee2:
                bot_config['DecayPercentPerInterval'] = st.number_input("Reduction (%) per Interval", value=30.0)
            
            st.subheader("Moving Profit")
            bot_config['MaximizeProfit'] = st.checkbox("Use Moving Profit Target", value=False)
            bot_config['ProfitSet'] = st.slider("Profit Set % (Lock in)", 0.1, 0.9, 0.5)
            
            st.divider()
            st.subheader("Advanced Re-entry & Cooldown")
            r1, r2, r3 = st.columns(3)
            with r1:
                bot_config['reentry_cooldown_mins'] = st.number_input("Cooldown (Mins)", value=0.0)
            with r2:
                bot_config['reentry_distance_pct'] = st.number_input("Re-entry Dist (%)", value=0.0)
            with r3:
                bot_config['post_exit_stop'] = st.checkbox("Stop After Cycle", value=False)

            st.subheader("Hedging")
            use_hedge = st.checkbox("Use Hedging", value=False)
            bot_config['UseHedge'] = use_hedge
            bot_config['HedgeStartStep'] = st.number_input("Hedge Start Step (1-10)", min_value=1, max_value=10, value=7)
            bot_config['HedgeStart'] = st.number_input("Hedge Start (DD%)", value=20.0)

            # Update Temp Strat for Projection if Hedging is toggled
            if use_hedge:
                 temp_strat.params['UseHedge'] = True
                 temp_strat.params['HedgeStartStep'] = bot_config['HedgeStartStep']
                 # Re-run projection to show hedge
                 # if df_f is not None and not df_f.empty and current_price > 0:
                 #     projections = temp_strat.calculate_projections(base_price=current_price, current_atr=p_atr)

        st.divider()

        # --- MOVED PROJECTION LOGIC ---
        try:
            if df_f is not None and not df_f.empty:
                current_price = df_f['close'].iloc[-1]
                
                # Determine correct ATR for projection
                proj_tf = timeframe
                if bot_config.get('UseATRGrid'):
                    proj_tf = bot_config.get('ATR_Timeframe', timeframe)
                    # Validation Warning
                    tf_minutes = {'1m': 1, '5m': 5, '15m': 15, '1h': 60, '4h': 240, '1d': 1440}
                    if tf_minutes.get(proj_tf, 0) < tf_minutes.get(timeframe, 0):
                        st.warning(f"⚠️ ATR Timeframe ({proj_tf}) is lower than Execution Timeframe ({timeframe}). This may cause grid calculation errors. Recommended: ATR TF >= Execution TF.")
                
                # Use data from Foundation if available, else default
                p_atr = atr_data.get(proj_tf, {}).get('atr', 10.0)
                
                # Update strat params with full config before calculating
                # temp_strat is already initialized, so we must recreate it or update its internal flags
                # The Strategy class caches 'UseATRGrid' in __init__, so updating params dict is not enough.
                # Re-initializing is safer.
                final_proj_params = proj_params.copy()
                final_proj_params.update(bot_config)
                temp_strat = MartingaleStrategy(params=final_proj_params)

                
                projections = temp_strat.calculate_projections(base_price=current_price, current_atr=p_atr)
                
                with st.expander("🔍 Risk Projection & Math Summary ($USDC)", expanded=True):
                    
                    # --- DYNAMIC GRID PREVIEW CHART ---
                    if projections:
                        import plotly.graph_objects as go
                        
                        proj_df = pd.DataFrame(projections)
                        steps = proj_df['step']
                        prices = proj_df['price']
                        tps = proj_df['tp_price']
                        
                        fig = go.Figure()
                        
                        # Grid Levels
                        fig.add_trace(go.Scatter(x=steps, y=prices, mode='lines+markers', name='Grid Orders', line=dict(color='#58a6ff')))
                        
                        # TP Levels
                        fig.add_trace(go.Scatter(x=steps, y=tps, mode='lines+markers', name='Take Profit', line=dict(color='#3fb950', dash='dash')))
                        
                        # Current Price Line
                        fig.add_hline(y=current_price, line_dash="solid", line_color="white", annotation_text="Current Price")
                        
                        fig.update_layout(
                            title=f"Grid Visualizer (ATR TF: {proj_tf})",
                            xaxis_title="Martingale Step",
                            yaxis_title="Price ($)",
                            template="plotly_dark",
                            height=300,
                            margin=dict(l=10, r=10, t=30, b=10)
                        )
                        st.plotly_chart(fig, width='stretch')
                    # ----------------------------------

                    st.success(f"📈 Simulated Martingale Grid based on current price: **{current_price:,.2f}**")
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
        # ------------------------------

        submitted = st.form_submit_button("Deploy Bot", type="primary")
    
    if submitted:
        if not name:
            st.error("🚨 Bot Name is required.")
        else:
            bot_config['timeframe'] = timeframe
            
            strat_id = "Martingale" # Default
            if strategy_type == "Market Maker":
                strat_id = "MarketMaker"
            elif strategy_type == "Magic Hour":
                strat_id = "MagicHour"
                
            bot_config['market_type'] = 'spot' if mode_id == 'spot' else 'futures'
            
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
                config_dict=bot_config
            )



            if bot_id:
                st.success(f"Bot '{name}' deployed successfully! (ID: {bot_id})")
                st.info(f"Deployed on {market_type} - {pair} using {strategy_type} ({timeframe})")
            else:
                st.error(f"Failed to deploy bot. Name '{name}' might already exist.")
