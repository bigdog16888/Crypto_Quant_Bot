import streamlit as st
import sys
import os

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine.database import get_all_bots, toggle_bot_active, delete_bot, get_bot_params, update_bot, get_bot_status, get_trade_history, get_connection
from engine.exchange_interface import ExchangeInterface
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.bot_management import (
    close_position, partial_close, set_stop_after_pnl, set_stop_after_time,
    set_manual_close_pct, get_position_summary, check_and_execute_stops
)
import engine.indicators as ta
import pandas as pd
import json
import logging

logger = logging.getLogger(__name__)

# --- Caching Wrappers ---
@st.cache_resource(ttl=3600, show_spinner=False)
def get_exchange_instance(market_type):
    """
    Singleton provider for ExchangeInterface to reuse connections.
    """
    return ExchangeInterface(market_type=market_type)

@st.cache_data(ttl=15, show_spinner=False)
def fetch_last_price_cached(market_type, symbol):
    try:
        ex = get_exchange_instance(market_type)
        return ex.get_last_price(symbol)
    except Exception: return 0.0

@st.cache_data(ttl=60, show_spinner=False)
def fetch_ohlcv_cached(market_type, symbol, timeframe):
    try:
        ex = get_exchange_instance(market_type)
        return ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=500)
    except Exception: return []
# ------------------------


def _build_bot_tree(bots):
    """
    Build a parent->children tree from flat bot list.
    Returns: (parents_list, children_map)
    """
    parents = []
    children_map = {}
    
    for bot in bots:
        # Unpack with new fields
        b_id = bot[0]
        name = bot[1]
        pair = bot[2]
        is_active = bot[3]
        strat_type = bot[4]
        total_invested = bot[5]
        step = bot[6]
        last_error = bot[7] if len(bot) > 7 else None
        last_error_time = bot[8] if len(bot) > 8 else None
        db_status = bot[9] if len(bot) > 9 else None
        bot_type = bot[10] if len(bot) > 10 else 'standard'
        parent_bot_id = bot[11] if len(bot) > 11 else None
        hedge_child_bot_id = bot[12] if len(bot) > 12 else None
        direction = bot[13] if len(bot) > 13 else 'LONG'
        hedge_trigger_step = bot[14] if len(bot) > 14 else None
        
        bot_data = {
            'id': b_id,
            'name': name,
            'pair': pair,
            'is_active': is_active,
            'strat_type': strat_type,
            'total_invested': float(total_invested) if total_invested is not None else 0.0,
            'step': int(step) if step is not None else 0,
            'last_error': last_error,
            'last_error_time': last_error_time,
            'db_status': db_status,
            'bot_type': bot_type,
            'parent_bot_id': parent_bot_id,
            'hedge_child_bot_id': hedge_child_bot_id,
            'direction': direction,
            'hedge_trigger_step': hedge_trigger_step,
        }
        
        if bot_type == 'hedge_child':
            # This is a hedge child - map to parent
            if parent_bot_id:
                children_map.setdefault(parent_bot_id, []).append(bot_data)
        else:
            # This is a parent/standard bot
            parents.append(bot_data)
    
    return parents, children_map


def _render_bot_row(bot, config, row_cols, show_hedge_info=False):
    """Render a single bot row (used for both parents and children)."""
    b_id = bot['id']
    name = bot['name']
    pair = bot['pair']
    is_active = bot['is_active']
    strat_type = bot['strat_type']
    total_invested = bot['total_invested']
    step = bot['step']
    last_error = bot['last_error']
    last_error_time = bot['last_error_time']
    db_status = bot['db_status']
    direction = bot['direction']
    hedge_trigger_step = bot['hedge_trigger_step']
    
    is_cleaning = db_status in ['pending_sl', 'stop_loss_triggered']
    
    # Display Row
    row_cols[0].write(f"#{b_id}")
    row_cols[1].write(name)
    row_cols[2].write(pair)
    row_cols[3].write(strat_type)
    row_cols[4].write(f"${total_invested:.2f} (S{step})")
    
    # Hedge info for child bots
    if show_hedge_info:
        row_cols[3].caption(f"↳ Hedge: {direction} @ Step {hedge_trigger_step or '?'}")
    
    # Targets Column
    with row_cols[5]:
        status_data = get_bot_status(b_id)
        if status_data and total_invested > 0:
            be = status_data.get('avg_entry_price', 0)
            tp = status_data.get('target_tp_price', 0)
            
            try:
                raw_params = get_bot_params(b_id)
                params_config = json.loads(raw_params[7]) if raw_params[7] else {}
                bot_market_type = params_config.get('market_type', config.MARKET_TYPE)
                
                curr_price = fetch_last_price_cached(bot_market_type, pair)
                
                direction_str = raw_params[2] if raw_params and len(raw_params) > 2 else direction
                
                pnl_pct = 0.0
                if be > 0 and curr_price > 0:
                    if direction_str == "LONG":
                        pnl_pct = (curr_price - be) / be * 100
                    else:
                        pnl_pct = (be - curr_price) / be * 100
                
                badge_color = "green" if pnl_pct >= 0 else "red"
                badge_bg = "#dafbe1" if pnl_pct >= 0 else "#ffebe9"
                badge_text = "#1a7f37" if pnl_pct >= 0 else "#cf222e"
                
                st.markdown(
                    f"""<span style='background-color: {badge_bg}; color: {badge_text}; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.8em;'>{pnl_pct:+.2f}%</span>""",
                    unsafe_allow_html=True
                )
                
                params = json.loads(raw_params[7]) if raw_params[7] else {}
                strat = MartingaleStrategy(params=params)
                
                market_data = pd.DataFrame()
                if params.get('UseATRGrid'):
                    target_tf = params.get('ATR_Timeframe', '1h')
                    ohlcv_1h = fetch_ohlcv_cached(bot_market_type, pair, '1h')
                    ohlcv_1d = fetch_ohlcv_cached(bot_market_type, pair, '1d')
                    
                    if ohlcv_1h and ohlcv_1d:
                        df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        df_1d = pd.DataFrame(ohlcv_1d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        for dff in [df_1h, df_1d]:
                            dff['timestamp'] = pd.to_datetime(dff['timestamp'], unit='ms')
                        market_data = df_1d if 'd' in target_tf else df_1h
                    if market_data.empty and curr_price > 0:
                        market_data = pd.DataFrame([{'close': curr_price, 'high': curr_price*1.01, 'low': curr_price*0.99}], index=[0])
                
                next_order = strat.calculate_next_grid_price(direction_str, curr_price, be, step, market_data)
                atr_active_tf = params.get('ATR_Timeframe', '1h')
                row_cols[5].markdown(f"**BE:** {be:,.2f} | **TP:** {tp:,.2f}")
                row_cols[5].markdown(f"**NO:** `{next_order:,.2f}` (ATR: {atr_active_tf})")
                
                if params.get('UseEarlyExit'):
                    row_cols[5].caption("📉 *Decay Enabled*")
            except Exception as e:
                row_cols[5].caption(f"BE: {be:.2f}")
                row_cols[5].caption(f"TP: {tp:.2f}")
                err_msg = str(e)[:20] + "..." if len(str(e)) > 20 else str(e)
                row_cols[5].caption(f"Err: {err_msg}")
                logger.error(f"Error calculating NO for {name}: {e}")
        else:
            row_cols[5].write("-")
    
    # Status Column
    with row_cols[6]:
        error_reason = None
        if not is_active:
            last_logs = get_trade_history(b_id, limit=1)
            if last_logs and last_logs[0][3] == 'ERROR_STOP':
                error_reason = last_logs[0][11]
        
        in_trade = total_invested > 0
        
        is_partial = False
        if in_trade:
            try:
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM bot_orders WHERE bot_id = ? AND status = 'open' AND filled_amount > 0", (b_id,))
                is_partial = cursor.fetchone()[0] > 0
            except:
                pass
        
        if is_cleaning:
            status_text = 'MARKET SL (FLUSHING)'
            pulse_color = "#f39c12"
            state_label = "CLEANING"
        elif error_reason:
            status_text = 'ERROR'
            pulse_color = "#cf222e"
            state_label = "ERROR"
        elif in_trade:
            if is_partial:
                status_text = 'PARTIAL FILL'
                pulse_color = "#d29921"
                state_label = f"PARTIAL (S{step})"
            else:
                status_text = 'IN TRADE'
                pulse_color = "#3fb950"
                state_label = f"TRADE (S{step})"
        elif is_active:
            status_text = 'Waiting for Signal'
            pulse_color = "#58a6ff"
            state_label = "IDLE"
        else:
            status_text = 'PAUSED'
            pulse_color = "#d29921"
            state_label = "PAUSED"
        
        pulse_anim = f"""
        <style>
        .blob-{b_id} {{
            background: {pulse_color};
            border-radius: 50%;
            margin: 5px;
            height: 10px;
            width: 10px;
            box-shadow: 0 0 0 0 {pulse_color};
            transform: scale(1);
            animation: pulse-{b_id} 2s infinite;
            display: inline-block;
        }}
        @keyframes pulse-{b_id} {{
            0% {{ transform: scale(0.95); box-shadow: 0 0 0 0 {pulse_color}70; }}
            70% {{ transform: scale(1); box-shadow: 0 0 0 10px {pulse_color}00; }}
            100% {{ transform: scale(0.95); box-shadow: 0 0 0 0 {pulse_color}00; }}
        }}
        </style>
        """
        st.markdown(pulse_anim + f"<div style='display:flex;align-items:center;'><div class='blob-{b_id}'></div> {status_text}</div>", unsafe_allow_html=True)
        
        st.caption(f"State: {state_label}")
        
        if error_reason:
            st.caption(f"🛑 {error_reason}")
        
        if last_error:
            st.markdown(f"<div style='color: #cf222e; font-size: 0.8em; margin-top: 5px;'>⚠️ {last_error}</div>", unsafe_allow_html=True)
        
        # Toggle button
        if st.button("⏯️ Toggle", key=f"btn_toggle_{b_id}", help="Start/Stop Bot"):
            toggle_bot_active(b_id, not bool(is_active))
            st.success(f"✅ Bot {name} status updated!")
            st.rerun()
    
    # Actions Column
    with row_cols[7]:
        col1, col2 = st.columns(2)
        if col1.button("✏️ Edit", key=f"edit_{b_id}", help=f"Edit {name} settings", disabled=show_hedge_info):
            render_edit_form(b_id)
        
        # Disable delete for hedge children shown inline (parent controls it)
        if col2.button("🗑️ Delete", key=f"del_{b_id}", help=f"Delete {name}", disabled=show_hedge_info):
            if delete_bot(b_id):
                st.success(f"✅ Deleted {name} successfully!")
                st.rerun()
            else:
                st.error(f"❌ Delete blocked (active position/orders). See logs.")
    
    # Position Controls (expander below row)
    if total_invested > 0 or is_cleaning:
        with st.expander(f"🎛️ Position Controls for {name}", expanded=False):
            if is_cleaning:
                st.warning("⏳ **Market SL Flush in Progress...**")
                st.markdown("1️⃣ The system is securely clearing all pending exchange orders on Binance.\n2️⃣ Attempting physical `MARKET CLOSE`.\n3️⃣ Releasing local database memory locking.\n\n*Please wait... The Background Engine handles this automatically.*")
            else:
                pos_summary = get_position_summary(b_id)
                pnl = pos_summary.get('unrealized_pnl', 0)
                pnl_pct = pos_summary.get('pnl_pct', 0)
                pnl_color = "green" if pnl >= 0 else "red"
                st.markdown(f"**Current PnL:** <span style='color:{pnl_color}'>${pnl:,.2f} ({pnl_pct:+.2f}%)</span>", unsafe_allow_html=True)
                
                st.markdown("**🛑 Professional Position Exit**")
                close_cols = st.columns([2, 1, 1])
                
                if close_cols[0].button("🟢 Close Position (Limit/Post-Only)", key=f"close_all_{b_id}", help="Close 100% of position using Post-Only Limit orders (Professional/Maker)."):
                    result = close_position(b_id, close_pct=100.0, reason="Manual close (Limit) from UI", order_type='limit')
                    if result['success']:
                        st.success(f"✅ Limit close order placed for {name}.")
                        st.rerun()
                    else:
                        st.error(f"❌ Limit Close Failed: {result.get('error')}")
                
                if close_cols[1].button("🟡 50%", key=f"close_50_{b_id}", help="Close 50% via Limit"):
                    result = close_position(b_id, close_pct=50.0, reason="Partial 50% (Limit)", order_type='limit')
                    if result['success']:
                        st.success("✅ 50% Limit order placed.")
                        st.rerun()
                    else:
                        st.error(f"❌ Failed: {result.get('error')}")
                
                if close_cols[2].button("⚪ 25%", key=f"close_25_{b_id}", help="Close 25% via Limit"):
                    result = close_position(b_id, close_pct=25.0, reason="Partial 25% (Limit)", order_type='limit')
                    if result['success']:
                        st.success("✅ 25% Limit order placed.")
                        st.rerun()
                    else:
                        st.error(f"❌ Failed: {result.get('error')}")
                
                st.markdown("---")
                
                # Panic Market Close
                if st.button("🔴 PANIC: Market Close (Aggressive/Taker)", key=f"panic_close_{b_id}", type="primary", help="IMMEDIATE market close. Uses TAKER orders - expect slippage."):
                    result = close_position(b_id, close_pct=100.0, reason="PANIC CLOSE from UI", order_type='market')
                    if result['success']:
                        st.success("✅ Market close order sent!")
                        st.rerun()
                    else:
                        st.error(f"❌ Market Close Failed: {result.get('error')}")
                
                st.markdown("---")
                
                # Stop Management
                st.markdown("**🛡️ Stop & Risk Automation**")
                stop_cols = st.columns(3)
                
                if stop_cols[0].button("🎯 Set Stop After PnL", key=f"stop_pnl_{b_id}", help="Auto-close when unrealized PnL reaches target"):
                    pnl_target = st.number_input("PnL Target ($)", min_value=0.01, value=10.0, key=f"pnl_target_{b_id}")
                    if st.button("Confirm", key=f"confirm_stop_pnl_{b_id}"):
                        if set_stop_after_pnl(b_id, pnl_target):
                            st.success(f"✅ Stop after PnL ${pnl_target} set.")
                            st.rerun()
                
                if stop_cols[1].button("⏰ Set Stop After Time", key=f"stop_time_{b_id}", help="Auto-close after duration"):
                    mins = st.number_input("Minutes", min_value=1, value=60, key=f"stop_time_mins_{b_id}")
                    if st.button("Confirm", key=f"confirm_stop_time_{b_id}"):
                        if set_stop_after_time(b_id, mins):
                            st.success(f"✅ Stop after {mins} mins set.")
                            st.rerun()
                
                if stop_cols[2].button("📊 Set Manual Close %", key=f"manual_close_{b_id}", help="Set % threshold for manual close"):
                    pct = st.number_input("Close %", min_value=1.0, max_value=100.0, value=100.0, key=f"manual_pct_{b_id}")
                    if st.button("Confirm", key=f"confirm_manual_{b_id}"):
                        if set_manual_close_pct(b_id, pct):
                            st.success(f"✅ Manual close at {pct}% set.")
                            st.rerun()


def render_bot_manager_view():
    st.header("🤖 Bot Manager")
    st.caption("📊 Manage existing bots: Toggle Status, Edit Settings, or Delete. Hedge children are nested under parents.")
    
    # Show Decommissioned checkbox
    show_decommissioned = st.checkbox("Show Decommissioned Bots", value=False, key="show_decommissioned_bots")
    
    st.divider()
    
    # Import config for default market type
    from config.settings import config
    
    # Fetch Data
    bots = get_all_bots(include_decommissioned=show_decommissioned)
    
    if not bots:
        st.info("No bots found. Go to 'Bot Creator' to deploy one.")
        return
    
    # Build parent-child tree
    parents, children_map = _build_bot_tree(bots)
    
    st.markdown("### 📈 Active Inventory")
    
    # Global Controls
    st.markdown("##### 🌍 Global Controls")
    g_cols = st.columns([1, 1, 2])
    with g_cols[0]:
        if st.button("🛑 Set Stop After Cycle (All Active)", key="global_stop_cycle_on"):
            from engine.trading_controls import update_all_bots_stop_cycle
            if update_all_bots_stop_cycle(True):
                st.success("Global Stop After Cycle ENABLED for all active bots.")
                st.rerun()
    with g_cols[1]:
        if st.button("▶️ Clear Stop After Cycle (All Active)", key="global_stop_cycle_off"):
            from engine.trading_controls import update_all_bots_stop_cycle
            if update_all_bots_stop_cycle(False):
                st.success("Global Stop After Cycle DISABLED for all active bots.")
                st.rerun()
    
    st.divider()
    
    # Header Row
    cols = st.columns([0.5, 1.5, 1.5, 1.5, 2, 2, 2, 2])
    cols[0].markdown("**🆔 ID**")
    cols[1].markdown("**🏷️ Name**")
    cols[2].markdown("**💰 Pair**")
    cols[3].markdown("**⚙️ Strat**")
    cols[4].markdown("**💵 Invested**")
    cols[5].markdown("**🎯 Targets (BE/TP/Next)**")
    cols[6].markdown("**📊 Status**")
    cols[7].markdown("**🔧 Action**")
    
    st.divider()
    
    # Render parents with nested children
    for parent in parents:
        b_id = parent['id']
        name = parent['name']
        pair = parent['pair']
        children = children_map.get(b_id, [])
        
        # Parent row
        row_cols = st.columns([0.5, 1.5, 1.5, 1.5, 2, 2, 2, 2])
        _render_bot_row(parent, config, row_cols, show_hedge_info=False)
        
        # Nested children accordion
        if children:
            with st.expander(f"↳ Hedge Children ({len(children)})", expanded=False):
                child_header = st.columns([0.5, 1.5, 1.5, 1.5, 2, 2, 2, 2])
                child_header[0].markdown("**🆔**")
                child_header[1].markdown("**Name**")
                child_header[2].markdown("**Pair**")
                child_header[3].markdown("**Type**")
                child_header[4].markdown("**Invested**")
                child_header[5].markdown("**Targets**")
                child_header[6].markdown("**Status**")
                child_header[7].markdown("**Action**")
                st.divider()
                
                for child in children:
                    child_cols = st.columns([0.5, 1.5, 1.5, 1.5, 2, 2, 2, 2])
                    _render_bot_row(child, config, child_cols, show_hedge_info=True)
        
        st.divider()


@st.dialog("Edit Bot Settings")
def render_edit_form(bot_id):
    from config.settings import config  # Import config for this function
    
    st.markdown("---")
    st.subheader(f"🛠️ Editing Bot #{bot_id}")
    st.caption("⚙️ Modify bot settings and parameters")
    
    params = get_bot_params(bot_id)
    if not params:
        st.error("Could not fetch bot parameters.")
        return
    
    try:
        config_json = params[7]
        config_dict = json.loads(config_json) if config_json else {}
    except Exception as e:
        st.error(f"Failed to parse config: {e}")
        config_dict = {}
    
    # Basic fields
    col1, col2 = st.columns(2)
    with col1:
        new_name = st.text_input("Bot Name", value=params[0], key=f"edit_name_{bot_id}")
    with col2:
        new_pair = st.text_input("Pair", value=params[1], key=f"edit_pair_{bot_id}")
    
    # Config editor
    st.markdown("**Configuration (JSON)**")
    new_config_str = st.text_area("Config JSON", value=json.dumps(config_dict, indent=2), height=300, key=f"edit_config_{bot_id}")
    
    col_save, col_cancel = st.columns(2)
    if col_save.button("💾 Save Changes", type="primary", key=f"save_edit_{bot_id}"):
        try:
            new_config = json.loads(new_config_str)
            update_bot(bot_id, name=new_name, pair=new_pair, config_json=json.dumps(new_config))
            st.success("✅ Bot updated successfully!")
            st.rerun()
        except Exception as e:
            st.error(f"❌ Save failed: {e}")
    
    if col_cancel.button("❌ Cancel", key=f"cancel_edit_{bot_id}"):
        st.rerun()