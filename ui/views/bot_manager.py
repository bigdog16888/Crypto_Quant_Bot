import streamlit as st
import sys
import os

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine.database import get_all_bots, toggle_bot_active, delete_bot, get_bot_params, update_bot, get_bot_status
from engine.exchange_interface import ExchangeInterface
from engine.strategies.mql4_strategy import MQL4Strategy
import pandas as pd
import json

def render_bot_manager_view():
    st.header("Bot Manager")
    st.caption("Manage existing bots: Toggle Status or Delete.")
    
    # Cache exchange instance to avoid creating new one per bot row
    @st.cache_resource
    def get_shared_exchange():
        try:
            return ExchangeInterface(market_type='spot')
        except Exception:
            return None
    
    shared_exchange = get_shared_exchange()
    
    # Fetch Data
    bots = get_all_bots()
    
    if not bots:
        st.info("No bots found. Go to 'Bot Creator' to deploy one.")
        return
        
    st.markdown("### Active Inventory")
    
    # Header Row
    cols = st.columns([0.5, 1.5, 1.5, 1.5, 2, 2, 2, 2])
    cols[0].markdown("**ID**")
    cols[1].markdown("**Name**")
    cols[2].markdown("**Pair**")
    cols[3].markdown("**Strat**")
    cols[4].markdown("**Invested**")
    cols[5].markdown("**Targets (BE/TP/Next)**")
    cols[6].markdown("**Status**")
    cols[7].markdown("**Action**")
    
    st.divider()

    editing_bot_id = st.session_state.get('editing_bot_id')

    for bot in bots:
        # Note: update engine/database.py get_all_bots to return these if not already
        # Current get_all_bots returns: b.id, b.name, b.pair, b.is_active, b.strategy_type, t.total_invested, t.current_step
        # We need t.avg_entry_price, t.target_tp_price as well.
        b_id, name, pair, is_active, strat_type, total_invested, step = bot[:7]
        
        # Display Row
        row_cols = st.columns([0.5, 1.5, 1.5, 1.5, 2, 2, 2, 2])
        row_cols[0].write(f"#{b_id}")
        row_cols[1].write(name)
        row_cols[2].write(pair)
        row_cols[3].write(strat_type)
        row_cols[4].write(f"${total_invested:.2f} (S{step})")
        
        # Targets Column
        with row_cols[5]:
            status_data = get_bot_status(b_id) # (name, pair, current_step, total_invested, avg_entry_price, target_tp_price)
            if status_data and total_invested > 0:
                be = status_data[4]
                tp = status_data[5]
                
                # Fetch current price for Next Order calc
                try:
                    curr_price = 0.0
                    if shared_exchange:
                        curr_price = shared_exchange.get_last_price(pair)
                    
                    # Get config for grid logic
                    raw_params = get_bot_params(b_id)
                    params = json.loads(raw_params[7]) if raw_params[7] else {}
                    strat = MQL4Strategy(name=name, params=params)
                    
                    # Fetch minimal OHLCV for ATR grid if needed
                    market_data = pd.DataFrame() # Placeholder, ATR needs data
                    if params.get('UseATRGrid') and shared_exchange:
                        ohlcv = shared_exchange.fetch_ohlcv(pair, timeframe='1h', limit=50)
                        market_data = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    next_order = strat.calculate_next_grid_price(raw_params[2], curr_price, be, step, market_data)
                    
                    row_cols[5].caption(f"**BE:** {be:.2f}")
                    row_cols[5].caption(f"**TP:** {tp:.2f}")
                    row_cols[5].caption(f"**NO:** {next_order:.2f}")
                    if params.get('UseEarlyExit'):
                        row_cols[5].caption("📉 *Decay Active*")
                except Exception as e:
                    row_cols[5].caption(f"BE: {be:.2f}")
                    row_cols[5].caption(f"TP: {tp:.2f}")
                    row_cols[5].write("Error loading NO")
            else:
                row_cols[5].write("-")

        # Toggle Status
        with row_cols[6]:
            status_label = "Running" if is_active else "Stopped"
            if st.toggle(status_label, value=bool(is_active), key=f"toggle_{b_id}") != bool(is_active):
                toggle_bot_active(b_id, not bool(is_active))
                st.rerun()
        
        # Actions
        with row_cols[6]:
            col1, col2 = st.columns(2)
            if col1.button("✏️", key=f"edit_{b_id}", help=f"Edit {name}"):
                st.session_state['editing_bot_id'] = b_id
                st.rerun()
                
            if col2.button("🗑️", key=f"del_{b_id}", help=f"Delete {name}"):
                if delete_bot(b_id):
                    st.success(f"Deleted {name}")
                    st.rerun()
        
        st.divider()

        # Edit Form (Appears below or as a modal equivalent)
    if editing_bot_id:
        render_edit_form(editing_bot_id)

def render_edit_form(bot_id):
    st.markdown("---")
    st.subheader(f"🛠️ Editing Bot #{bot_id}")
    
    params = get_bot_params(bot_id)
    if not params:
        st.error("Could not fetch bot parameters.")
        return

    name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config_json = params
    config_dict = json.loads(config_json) if config_json else {}

    with st.form(key="edit_bot_form"):
        col1, col2 = st.columns(2)
        new_name = col1.text_input("Bot Name", value=name, key=f"edit_name_{bot_id}")
        new_pair = col2.text_input("Trading Pair (e.g. BTC/USDT)", value=pair, key=f"edit_pair_{bot_id}")
        
        col3, col4 = st.columns(2)
        new_direction = col3.selectbox("Direction", ["LONG", "SHORT"], index=0 if direction == "LONG" else 1, key=f"edit_dir_{bot_id}")
        new_strat = col4.selectbox("Strategy Type", ["MQL4", "RSI_ONLY"], index=0 if strategy_type == "MQL4" else 1, key=f"edit_strat_type_{bot_id}")
        
        col5, col6, col7 = st.columns(3)
        new_base = col5.number_input("Order Size ($USDC)", value=float(base_size), key=f"edit_base_{bot_id}")
        new_mm = col6.number_input("Martingale Multiplier", value=float(martingale_multiplier), key=f"edit_mm_{bot_id}")
        new_rsi = col7.number_input("RSI Limit (Classic)", value=float(rsi_limit), help="Only for Classic logic.", key=f"edit_rsi_{bot_id}")

        # --- NEW: Take Profit Editing ---
        st.markdown("#### Take Profit Logic")
        curr_tp_type = config_dict.get('TakeProfitType', 'USD')
        # Map USD -> index 0, Percent -> index 1
        tp_type_idx = 0 if curr_tp_type == 'USD' else 1
        
        new_tp_type = st.radio("TP Mode", ["Dollar Target ($)", "Percentage (%)"], index=tp_type_idx, horizontal=True, key=f"edit_tp_type_{bot_id}")
        
        if new_tp_type == "Dollar Target ($)":
            new_tp_base = st.number_input("Take Profit Target ($USDC)", min_value=1.0, step=1.0, value=float(config_dict.get('TakeProfitBase', 10.0)), key=f"edit_tp_base_{bot_id}")
            config_dict['TakeProfitType'] = 'USD'
            config_dict['TakeProfitBase'] = new_tp_base
        else:
            new_tp_pct = st.number_input("Take Profit Target (%)", min_value=0.1, step=0.1, value=float(config_dict.get('TakeProfitPct', 1.0)), key=f"edit_tp_pct_{bot_id}")
            config_dict['TakeProfitType'] = 'Percent'
            config_dict['TakeProfitPct'] = new_tp_pct

        # Math Projection in Editor
        try:
            from engine.exchange_interface import ExchangeInterface
            temp_strat = MQL4Strategy(params={'base_size': new_base, 'martingale_multiplier': new_mm, 'direction': new_direction})
            temp_strat.params.update(config_dict)
            
            # Fetch current price for projection
            exchange = ExchangeInterface()
            # ticker = exchange.exchange.fetch_ticker(pair) # Pair might be modified in input, use current pair for realism
            ohlcv = exchange.fetch_ohlcv(pair if pair else "BTC/USDT", timeframe='1m', limit=1)
            curr_p = ohlcv[0][4] if ohlcv else 40000.0
            
            # Pass ATR context for grid
            # Assume 1h ATR for simplicity in editor projection or add dropdown
            # For now, use a default ATR or calculate
            p_atr = 20.0
            
            projections = temp_strat.calculate_projections(base_price=curr_p, current_atr=p_atr)
            with st.expander("🔍 Editor Risk Projection & Math Summary", expanded=False):
                st.caption(f"Simulated levels starting at: **{curr_p:,.2f}**")
                proj_df = pd.DataFrame(projections)
                proj_df.columns = ["Step", "Grid Price", "Order ($)", "Total Inv. ($)", "TP Price", "Hedge Size", "Is Hedge"]
                st.table(proj_df)
                
                # Hedge Summary
                hedge_steps = [p for p in projections if p['is_hedge']]
                if hedge_steps:
                    h1 = hedge_steps[0]
                    st.info(f"🛡️ **Hedge Summary**: At Step {h1['step']} (Price: {h1['price']}), a hedge of **${h1['hedge_size_usdc']}** activates.")
                else:
                    if config_dict.get('UseHedge'):
                        st.warning("⚠️ Hedge enabled but not triggered in max steps.")
                    else:
                        st.info("No Hedge Configured.")
                        
        except Exception as e:
            st.warning(f"Projection skip: {e}")

        st.markdown("#### Entry Triggers (8-Switch Confluence)")
        t_col1, t_col2, t_col3, t_col4 = st.columns(4)
        with t_col1:
            config_dict['mode_cci'] = st.selectbox("CCI Switch", [0, 1, 2], index=int(config_dict.get('mode_cci', 0)), format_func=lambda x: {0: "OFF", 1: "Above", 2: "Below"}[x], key=f"edit_mode_cci_{bot_id}")
            config_dict['cci_level'] = st.number_input("CCI Level", value=float(config_dict.get('cci_level', 100)), key=f"edit_cci_lvl_{bot_id}")
        with t_col2:
            config_dict['mode_boll'] = st.selectbox("Boll Switch", [0, 1, 2], index=int(config_dict.get('mode_boll', 0)), format_func=lambda x: {0: "OFF", 1: "Outside Lower", 2: "Outside Upper"}[x], key=f"edit_mode_boll_{bot_id}")
        with t_col3:
            config_dict['mode_stoch'] = st.selectbox("Stoch Switch", [0, 1, 2], index=int(config_dict.get('mode_stoch', 0)), format_func=lambda x: {0: "OFF", 1: "Oversold", 2: "Overbought"}[x], key=f"edit_mode_stoch_{bot_id}")
        with t_col4:
            config_dict['mode_rsi'] = st.selectbox("RSI Switch", [0, 1, 2], index=int(config_dict.get('mode_rsi', 0)), format_func=lambda x: {0: "OFF", 1: "Below", 2: "Above"}[x], key=f"edit_mode_rsi_{bot_id}")
            config_dict['rsi_level'] = st.number_input("RSI Level", value=float(config_dict.get('rsi_level', 30)), key=f"edit_rsi_lvl_{bot_id}")

        st.markdown("#### Price & Volatility Triggers (9 & 10)")
        pv_col1, pv_col2 = st.columns(2)
        with pv_col1:
            config_dict['mode_price'] = st.selectbox("Price Switch", [0, 1, 2], index=int(config_dict.get('mode_price', 0)), format_func=lambda x: {0: "OFF", 1: "Above", 2: "Below"}[x], key=f"edit_mode_price_{bot_id}")
            config_dict['price_threshold'] = st.number_input("Threshold Price", value=float(config_dict.get('price_threshold', 0.0)), key=f"edit_price_threshold_{bot_id}")
        with pv_col2:
            st.markdown("**Trigger 10: Market State**")
            config_dict['mode_atrp'] = st.selectbox("Volatility Context", [0, 1, 2], index=int(config_dict.get('mode_atrp', 0)), format_func=lambda x: {0: "OFF", 1: "Below (Quiet)", 2: "Above (Extreme)"}[x], help="Compares current volatility to historical levels.", key=f"edit_mode_atrp_{bot_id}")
            pa1, pa2 = st.columns(2)
            config_dict['atrp_level'] = pa1.number_input("Lookback Level %", value=float(config_dict.get('atrp_level', 50.0)), key=f"edit_atrp_level_{bot_id}")
            config_dict['atrp_tf'] = pa2.selectbox("ATR TF", ["15m","1h","4h","1d"], index=1, key=f"edit_atrp_tf_{bot_id}")

        st.markdown("#### Trigger 11: ATR Expansion (Current Move vs Range)")
        e_col1, e_col2, e_col3 = st.columns(3)
        with e_col1:
            config_dict['mode_atre'] = st.selectbox("Expansion Move", [0, 1, 2], index=int(config_dict.get('mode_atre', 0)), format_func=lambda x: {0: "OFF", 1: "Move Up >= X%", 2: "Move Down >= X%"}[x], help="Move from open as % of ATR.", key=f"edit_mode_atre_{bot_id}")
        with e_col2:
            config_dict['atre_level'] = st.number_input("Target % of ATR", value=float(config_dict.get('atre_level', 100.0)), key=f"edit_atre_level_{bot_id}")
        with e_col3:
            config_dict['atre_tf'] = st.selectbox("TF to Watch (T11)", ["1h","4h","1d"], index=0, key=f"edit_atre_tf_{bot_id}")

        st.markdown("#### Pattern Slots")
        for p_idx in range(1, 4, 2):
            pc1, pc2 = st.columns(2)
            for i, col in enumerate([pc1, pc2]):
                idx = p_idx + i
                with col:
                    c_p1, c_p2, c_p3, c_p4 = st.columns(4)
                    config_dict[f'pat_{idx}_mode'] = c_p1.selectbox(f"Type ##{idx}", [0, 1, 2], index=int(config_dict.get(f'pat_{idx}_mode', 0)), format_func=lambda x: {0: "OFF", 1: "Up", 2: "Down"}[x], key=f"edit_p_mode_{idx}_{bot_id}")
                    config_dict[f'pat_{idx}_source'] = c_p2.selectbox(f"Source ##{idx}", ["Price", "RSI", "CCI"], index=["Price", "RSI", "CCI"].index(config_dict.get(f'pat_{idx}_source', "Price")), key=f"edit_p_src_{idx}_{bot_id}")
                    config_dict[f'pat_{idx}_tf'] = c_p3.selectbox(f"TF ##{idx}", ["1m","5m","15m","1h","4h","1d"], index=["1m","5m","15m","1h","4h","1d"].index(config_dict.get(f'pat_{idx}_tf', "5m")), key=f"edit_p_tf_{idx}_{bot_id}")
                    config_dict[f'pat_{idx}_count'] = c_p4.number_input(f"Count ##{idx}", min_value=1, value=int(config_dict.get(f'pat_{idx}_count', 3)), key=f"edit_p_count_{idx}_{bot_id}")

        st.markdown("#### Risk Management")
        rm1, rm2, rm3 = st.columns(3)
        with rm1:
            config_dict['UseATRGrid'] = st.checkbox("Use ATR Grid", value=config_dict.get('UseATRGrid', True), key=f"edit_atr_grid_{bot_id}")
        with rm2:
            config_dict['ATRGridFactor'] = st.number_input("ATR Factor", value=float(config_dict.get('ATRGridFactor', 1.0)), key=f"edit_atr_fac_{bot_id}")
        with rm3:
            config_dict['base_grid'] = st.number_input("Fixed Step", value=float(config_dict.get('base_grid', 100.0)), key=f"edit_base_grid_{bot_id}")

        st.markdown("#### Advanced Exit & Hedge Settings")
        col_ee1, col_ee2, col_ee3, col_ee4 = st.columns(4)
        with col_ee1:
            use_ee = st.checkbox("Use Early Exit", value=config_dict.get('UseEarlyExit', False), help="Moves TP target closer to Break Even over time to exit stale trades safely.", key=f"edit_use_ee_{bot_id}")
        with col_ee2:
            decay_interval = st.number_input("Decay Interval (Mins)", value=float(config_dict.get('DecayIntervalMins', 15.0)), help="How often (in minutes) the profit target is reduced.", key=f"edit_ee_int_{bot_id}")
        with col_ee3:
            decay_pct = st.number_input("TP Reduction (%)", value=float(config_dict.get('DecayPercentPerInterval', 30.0)), help="What percentage of the current profit target to cut per interval.", key=f"edit_ee_red_{bot_id}")
        with col_ee4:
            hedge_step = st.number_input("Hedge Step", min_value=1, max_value=10, value=int(config_dict.get('HedgeStartStep', 7)), help="Which Martingale step triggers the hedge trade.", key=f"edit_hedge_step_{bot_id}")
        
        st.markdown("#### Post-Exit Re-entry & Cooldown")
        re1, re2, re3 = st.columns(3)
        with re1:
            reentry_mins = st.number_input("Cooldown (Mins)", value=float(config_dict.get('reentry_cooldown_mins', 0.0)), key=f"edit_reentry_mins_{bot_id}")
        with re2:
            reentry_dist = st.number_input("Re-entry Dist (%)", value=float(config_dict.get('reentry_distance_pct', 0.0)), key=f"edit_reentry_dist_{bot_id}")
        with re3:
            post_stop = st.checkbox("Stop After Cycle", value=bool(config_dict.get('post_exit_stop', False)), key=f"edit_post_stop_{bot_id}")

        config_dict['UseEarlyExit'] = use_ee
        config_dict['DecayIntervalMins'] = decay_interval
        config_dict['DecayPercentPerInterval'] = decay_pct
        config_dict['HedgeStartStep'] = hedge_step
        config_dict['reentry_cooldown_mins'] = reentry_mins
        config_dict['reentry_distance_pct'] = reentry_dist
        config_dict['post_exit_stop'] = post_stop

        st.markdown("#### Strategy Parameters (JSON View)")
        new_config_str = st.text_area("JSON View", value=json.dumps(config_dict, indent=4), height=150)

        submit_cols = st.columns([1, 1, 4])
        if submit_cols[0].form_submit_button("💾 Save Changes"):
            try:
                new_config = json.loads(new_config_str)
                if update_bot(bot_id, new_name, new_pair, new_direction, new_rsi, new_mm, new_base, new_strat, new_config):
                    st.success("Bot updated successfully!")
                    st.session_state['editing_bot_id'] = None
                    st.rerun()
                else:
                    st.error("Failed to update bot.")
            except Exception as e:
                st.error(f"Invalid JSON in config: {e}")

        if submit_cols[1].form_submit_button("❌ Cancel"):
            st.session_state['editing_bot_id'] = None
            st.rerun()
