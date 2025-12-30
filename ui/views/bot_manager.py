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
                    exchange = ExchangeInterface(market_type='spot') # Dynamic type preferred in future
                    curr_price = exchange.get_last_price(pair)
                    
                    # Get config for grid logic
                    raw_params = get_bot_params(b_id)
                    params = json.loads(raw_params[7]) if raw_params[7] else {}
                    strat = MQL4Strategy(name=name, params=params)
                    
                    # Fetch minimal OHLCV for ATR grid if needed
                    market_data = pd.DataFrame() # Placeholder, ATR needs data
                    if params.get('UseATRGrid'):
                        ohlcv = exchange.fetch_ohlcv(pair, timeframe='1h', limit=50)
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
        new_rsi = col7.number_input("RSI Limit", value=float(rsi_limit), key=f"edit_rsi_{bot_id}")

        # Risk Projection in Edit Mode
        temp_strat = MQL4Strategy(params={'base_size': new_base, 'martingale_multiplier': new_mm})
        temp_strat.params.update(config_dict)
        projections = temp_strat.calculate_projections(steps=10)
        with st.expander("🔍 Risk Projection ($USDC)", expanded=False):
            st.caption("Investment Growth & Hedging Thresholds")
            proj_df = pd.DataFrame(projections)
            proj_df.columns = ["Step", "Order Size ($)", "Total Invested ($)", "Hedge Req ($)"]
            st.table(proj_df)

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

        st.markdown("#### Pattern Slots")
        for p_idx in range(1, 4, 2):
            pc1, pc2 = st.columns(2)
            for i, col in enumerate([pc1, pc2]):
                idx = p_idx + i
                with col:
                    c_p1, c_p2, c_p3 = st.columns(3)
                    config_dict[f'pat_{idx}_mode'] = c_p1.selectbox(f"Type ##{idx}", [0, 1, 2], index=int(config_dict.get(f'pat_{idx}_mode', 0)), format_func=lambda x: {0: "OFF", 1: "Consec. Up", 2: "Consec. Down"}[x], key=f"edit_p_mode_{idx}")
                    config_dict[f'pat_{idx}_tf'] = c_p2.selectbox(f"TF ##{idx}", ["1m","5m","15m","1h","4h","1d"], index=1, key=f"edit_p_tf_{idx}")
                    config_dict[f'pat_{idx}_count'] = c_p3.number_input(f"Count ##{idx}", min_value=1, value=int(config_dict.get(f'pat_{idx}_count', 3)), key=f"edit_p_count_{idx}")

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
        
        config_dict['UseEarlyExit'] = use_ee
        
        config_dict['UseEarlyExit'] = use_ee
        config_dict['DecayIntervalMins'] = decay_interval
        config_dict['DecayPercentPerInterval'] = decay_pct
        config_dict['HedgeStartStep'] = hedge_step

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
