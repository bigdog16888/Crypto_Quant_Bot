import streamlit as st
import pandas as pd
import sys
import os

# Ensure engine can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine.database import add_bot
from engine.exchange_interface import ExchangeInterface

# --- Performance Caching Wrappers ---
@st.cache_resource(ttl=3600, show_spinner=False)
def get_exchange_instance(market_type):
    """Singleton provider for ExchangeInterface to reuse connections."""
    return ExchangeInterface(market_type=market_type, validate=False)

@st.cache_data(ttl=300, show_spinner=False)
def fetch_symbols_cached(market_type, quote_asset):
    try:
        ex = get_exchange_instance(market_type)
        # load_markets is internal to get_available_symbols but we can force it here if needed
        # get_available_symbols calls _ensure_markets() which calls load_markets()
        return ex.get_available_symbols(quote_asset=quote_asset)
    except Exception: return []

@st.cache_data(ttl=300, show_spinner=False)
def fetch_min_order_cached(market_type, pair):
    try:
        ex = get_exchange_instance(market_type)
        return ex.get_min_order_usd(pair)
    except Exception: return 5.0

@st.cache_data(ttl=60, show_spinner=False)
def fetch_ohlcv_cached(market_type, symbol, timeframe, limit=100):
    try:
        ex = get_exchange_instance(market_type)
        return ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception: return []
# ------------------------------------

def validate_bot_config(name, pair, base_size, martingale_multiplier):
    """Validate bot configuration before submission."""
    errors = []
    warnings = []
    
    # Required field validation
    if not name or len(name.strip()) == 0:
        errors.append("Bot Name is required")
    
    if not pair:
        errors.append("Trading Pair is required")
    
    # Numeric validation
    try:
        if float(base_size) <= 0:
            errors.append("Base Order Size must be greater than 0")
        elif float(base_size) < 5:
            warnings.append(f"Base Order Size ${base_size} is below recommended minimum ($5)")
    except (ValueError, TypeError):
        errors.append("Base Order Size must be a valid number")
    
    try:
        if float(martingale_multiplier) <= 1:
            errors.append("Martingale Multiplier must be greater than 1")
        elif float(martingale_multiplier) > 10:
            warnings.append(f"Martingale Multiplier {martingale_multiplier}x is very aggressive")
    except (ValueError, TypeError):
        errors.append("Martingale Multiplier must be a valid number")
    
    return errors, warnings


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
            # Store market type in session for bot_manager consistency
            st.session_state['market_type'] = mode_id
            available_pairs = fetch_symbols_cached(mode_id, quote_asset)
            
            if not available_pairs:
                st.warning("No pairs found. Check connection or API keys.")
                available_pairs = [f"BTC/{quote_asset}", f"ETH/{quote_asset}"] # Fallback
        except Exception as e:
            st.error(f"Error fetching symbols: {e}")
            available_pairs = [f"BTC/{quote_asset}"]
        
        # Refresh Button for Pairs
        if st.button("🔄 Refresh Pairs", help="Force reload of market pairs"):
             try:
                 fetch_symbols_cached.clear()
                 st.success("Cache cleared! Reloading...")
                 st.rerun()
             except Exception as e:
                 st.error(f"Reload failed: {e}")

        pair = st.selectbox("Trading Pair", available_pairs)

        # Dynamic Min Order Calculation
        min_order_usd = 5.0
        if pair:
            try:
                min_order_usd = fetch_min_order_cached(mode_id, pair)
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
    
    # Initialize default values that were previously set in the removed block
    bot_config['ATR_Timeframe'] = '1h'
    bot_config['ATRPeriods'] = 14
    
    # --- Market Context / Analysis (Visual Only) ---
    # Moved calculation logic to be demand-based or inside the Risk Management section if needed
    p_atr = 10.0
    current_price = 0.0



    # REMOVED STANDALONE FLEXIBLE GRID SECTION
    # Merged into Risk Management below for cleaner UI


    # --- Configuration Sections ---
    # bot_config is initialized above in the expander
    
    st.divider()
    
    with st.form("deploy_bot_form"):
        st.subheader("⚙️ General Settings")

        
        # --- Visual Strategy Selector (Cards) ---
        st.markdown("### 🧠 Select Strategy Logic")
        
        # Global CSS in app.py handles .strat-card styling
        
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
            
            use_min_size = st.checkbox("Use Minimum Quantity (Auto-Size)", value=False, help="If checked, the bot will automatically use the exchange's minimum valid quantity + 5% buffer, overriding the Base Size.")
            bot_config['use_min_size'] = use_min_size
            
            if base_size < min_order_usd and not use_min_size:
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
            
            st.divider()
            st.markdown("**Trigger 12: Moving Average Filter (Trend Bias)**")
            ma_c1, ma_c2, ma_c3, ma_c4 = st.columns(4)
            with ma_c1:
                bot_config['mode_ma'] = st.selectbox("Trend Filter", [0, 1, 2], index=0, format_func=lambda x: {0: "OFF", 1: "Price > MA (Bullish)", 2: "Price < MA (Bearish)"}[x], key="create_mode_ma")
            with ma_c2:
                bot_config['ma_period'] = st.number_input("MA Period", value=200, min_value=1, key="create_ma_period")
            with ma_c3:
                bot_config['ma_tf'] = st.selectbox("MA Timeframe", ["1m","5m","15m","1h","4h","1d"], index=3, key="create_ma_tf", help="Timeframe for the Moving Average (e.g. 1h or 4h for trend).")
            with ma_c4:    
                bot_config['ma_type'] = st.selectbox("MA Type", ["SMA", "EMA"], index=0, key="create_ma_type")
            
            temp_strat.params.update(bot_config)

            if df_f is not None and not df_f.empty and current_price > 0:
               projections = temp_strat.calculate_projections(base_price=current_price, current_atr=p_atr)
            
            st.divider()

        with st.expander("Risk Management (Grid & Safety)", expanded=False):
            st.subheader("Grid Spacing Logic")
            
            # Consolidated Grid Logic
            use_atr_grid = st.checkbox("Use Dynamic ATR Grid", value=True, help="If OFF, uses fixed 'Base Grid' distance for all steps (unless overridden by rules).")
            bot_config['UseATRGrid'] = use_atr_grid
            
            col_grid_main1, col_grid_main2 = st.columns(2)
            
            with col_grid_main1:
                # ATR Configuration
                st.markdown("##### 📉 ATR Settings")
                atr_timeframe = st.selectbox(
                    "ATR Timeframe", 
                    ["1m", "5m", "15m", "30m", "1h", "4h", "1d"], 
                    index=4, 
                    help="Timeframe used to calculate ATR for grid spacing. Lower timeframe = tighter grid."
                )
                bot_config['ATR_Timeframe'] = atr_timeframe
                
                atr_periods = st.number_input("ATR Periods", value=14, min_value=3, max_value=240, help="Number of candles to calculate average range.")
                bot_config['ATRPeriods'] = atr_periods
                
                atr_mode = st.radio(
                    "ATR Mode",
                    ["dynamic", "locked"],
                    index=0,
                    horizontal=True,
                    help="'dynamic': Recalculate ATR every cycle. 'locked': Capture ATR at first entry and keep it constant."
                )
                bot_config['ATRMode'] = atr_mode

            with col_grid_main2:
                # Spacing Configuration
                st.markdown("##### 📐 Spacing Settings")
                
                # 1. Base Spacing Input
                if use_atr_grid:
                    bot_config['ATRGridFactor'] = st.number_input("Base Spacing (ATR Multiplier)", value=1.0, step=0.1, help="Initial grid spacing = ATR × this factor.")
                    bot_config['base_grid'] = 100.0
                else:
                    bot_config['base_grid'] = st.number_input("Base Spacing (Price $)", value=100.0, step=10.0)
                    bot_config['ATRGridFactor'] = 1.0
                
                # 2. Martingale Spacing (Exponential Grid)
                use_grid_mult = st.checkbox("Enable Exponential Spacing (Martingale Grid)", value=False, help="If checked, the grid spacing changes by a multiplier at each step.")
                if use_grid_mult:
                     bot_config['GridMultiplier'] = st.number_input("Spacing Multiplier", value=1.1, step=0.05, min_value=0.1, help="> 1.0 expands grid (classic). < 1.0 tightens grid (aggressive).")
                else:
                     bot_config['GridMultiplier'] = 1.0

            st.divider()
            
            # Advanced Rules Section (Consolidated)
            st.markdown("##### 🎯 Advanced Step-Based Rules")
            if st.checkbox("Enable Advanced Step Rules", value=False, help="Define custom spacing for specific step ranges (e.g., Steps 1-3 tight, Steps 4-10 loose)."):
                
                # Initialize session state for grid rules
                if 'grid_rules' not in st.session_state: st.session_state.grid_rules = []
                
                # Add/Remove rules
                r_col1, r_col2, r_col3 = st.columns([2, 2, 2])
                with r_col1:
                    rule_start = st.number_input("Start Step", min_value=1, max_value=20, value=1, key="rule_start")
                    rule_end = st.number_input("End Step", min_value=1, max_value=20, value=4, key="rule_end")
                with r_col2:
                    rule_type = st.selectbox("Type", ["atr", "fixed"], key="rule_type")
                    if rule_type == 'atr':
                        rule_val = st.number_input("Multiplier", value=1.0, step=0.1, key="rule_val_m")
                    else:
                        rule_val = st.number_input("Spacing ($)", value=100.0, step=10.0, key="rule_val_f")
                with r_col3:
                    st.write("") # Spacer
                    st.write("") # Spacer
                    if st.button("Add Rule", width='stretch'):
                        st.session_state.grid_rules.append({
                            "start": rule_start, "end": rule_end, "type": rule_type, 
                            "multiplier" if rule_type == 'atr' else "value": rule_val
                        })
                        st.rerun()

                # Display existing rules
                if st.session_state.grid_rules:
                    st.markdown("**Active Rules:**")
                    for i, rule in enumerate(st.session_state.grid_rules):
                        r_desc = f"Steps {rule['start']}-{rule['end']}: "
                        if rule['type'] == 'atr': r_desc += f"ATR × {rule.get('multiplier', 1.0)}"
                        else: r_desc += f"Fixed ${rule.get('value', 100)}"
                        
                        rc1, rc2 = st.columns([4,1])
                        with rc1: st.info(r_desc)
                        with rc2: 
                            if st.button("❌", key=f"del_rule_{i}"):
                                st.session_state.grid_rules.pop(i)
                                st.rerun()
                    bot_config['GridStepRules'] = st.session_state.grid_rules
                else:
                    bot_config['GridStepRules'] = []
            else:
                bot_config['GridStepRules'] = []

            
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
            # Optimized Projection Data Fetch (Cached)
            ohlcv_proj = fetch_ohlcv_cached(mode_id, pair if pair else "BTC/USDT", timeframe='1m', limit=1)
            if ohlcv_proj and len(ohlcv_proj) > 0:
                current_price = float(ohlcv_proj[0][4])
            
            if current_price > 0:
                
                # Determine correct ATR for projection
                proj_tf = timeframe
                if bot_config.get('UseATRGrid'):
                    proj_tf = bot_config.get('ATR_Timeframe', timeframe)
                    # Validation Warning
                    tf_minutes = {'1m': 1, '5m': 5, '15m': 15, '1h': 60, '4h': 240, '1d': 1440}
                    if tf_minutes.get(proj_tf, 0) < tf_minutes.get(timeframe, 0):
                        st.warning(f"⚠️ ATR Timeframe ({proj_tf}) is lower than Execution Timeframe ({timeframe}). This may cause grid calculation errors. Recommended: ATR TF >= Execution TF.")
                
                # Use data from Foundation if available, else default
                # Calculated from configured ATR settings
                if p_atr <= 0:
                     p_atr = current_price * 0.01
                
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
                        fig.add_hline(y=current_price, line_dash="solid", line_color="#1f2328", annotation_text="Current Price")
                        
                        fig.update_layout(
                            title=f"Grid Visualizer (ATR TF: {proj_tf})",
                            xaxis_title="Martingale Step",
                            yaxis_title="Price ($)",
                            template="plotly_white",
                            height=300,
                            margin=dict(l=10, r=10, t=30, b=10),
                            paper_bgcolor='rgba(0,0,0,0)',
                            plot_bgcolor='rgba(0,0,0,0)',
                            font=dict(color='#1f2328')
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
        
        # Validation feedback
        errors, warnings = validate_bot_config(name, pair, base_size, martingale_multiplier)
        
        # Display warnings inline
        for warning in warnings:
            st.warning(f"⚠️ {warning}")
        
        submitted = st.form_submit_button(
            "Deploy Bot", 
            type="primary",
            disabled=len(errors) > 0
        )
        
        # Show errors if button is disabled
        if len(errors) > 0:
            for error in errors:
                st.error(f"🚫 {error}")
            if submitted:
                st.info("Please fix the errors above before deploying.")
    
    if submitted and len(errors) == 0:
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
                
                # Navigate to Monitor button
                col_nav1, col_nav2 = st.columns([1, 4])
                with col_nav1:
                    if st.button("📊 View in Monitor", type="primary", use_container_width=True):
                        st.session_state['_nav_to_monitor'] = True
                        st.rerun()
                with col_nav2:
                    st.caption("Go to Live Monitor to see your bot running")
            else:
                st.error(f"Failed to deploy bot. Name '{name}' might already exist.")
