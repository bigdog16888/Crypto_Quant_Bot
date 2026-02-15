import streamlit as st
import sys
import os

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine.database import get_all_bots, toggle_bot_active, delete_bot, get_bot_params, update_bot, get_bot_status, get_trade_history
from engine.exchange_interface import ExchangeInterface
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.bot_management import (
    close_position, partial_close, set_stop_after_pnl, set_stop_after_time,
    set_manual_close_pct, get_position_summary, check_and_execute_stops
)
import engine.indicators as ta
import pandas as pd
import json

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

def render_bot_manager_view():
    st.header("🤖 Bot Manager")
    st.caption("📊 Manage existing bots: Toggle Status, Edit Settings, or Delete.")

    st.divider()
    
    # Import config for default market type
    from config.settings import config
    
    # Fetch Data
    bots = get_all_bots()
    
    if not bots:
        st.info("No bots found. Go to 'Bot Creator' to deploy one.")
        return
        
    st.markdown("### 📈 Active Inventory")

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

    for bot in bots:

        # Note: update engine/database.py get_all_bots to return these if not already
        # Current get_all_bots returns: b.id, b.name, b.pair, b.is_active, b.strategy_type, t.total_invested, t.current_step
        # We need t.avg_entry_price, t.target_tp_price as well.
        b_id, name, pair, is_active, strat_type, total_invested, step = bot[:7]
        
        # FIX: Handle potential None values from LEFT JOIN if trade record missing
        total_invested = float(total_invested) if total_invested is not None else 0.0
        step = int(step) if step is not None else 0

        # Display Row
        row_cols = st.columns([0.5, 1.5, 1.5, 1.5, 2, 2, 2, 2])
        row_cols[0].write(f"#{b_id}")
        row_cols[1].write(name)
        row_cols[2].write(pair)
        row_cols[3].write(strat_type)
        row_cols[4].write(f"${total_invested:.2f} (S{step})")
        
        # Targets Column
        with row_cols[5]:
            status_data = get_bot_status(b_id)  # Returns dict: {avg_entry_price, target_tp_price, ...}
            if status_data and total_invested > 0:
                be = status_data.get('avg_entry_price', 0)
                tp = status_data.get('target_tp_price', 0)
                
                # Fetch current price for Next Order calc and PnL Badge
                try:
                    # Get bot's market_type from its config
                    raw_params = get_bot_params(b_id)
                    params_config = json.loads(raw_params[7]) if raw_params[7] else {}
                    bot_market_type = params_config.get('market_type', config.MARKET_TYPE)
                    
                    # Create exchange with bot's market type
                    curr_price = fetch_last_price_cached(bot_market_type, pair)
                    
                    # raw_params: name, pair, direction, rsi_limit, mm, base, strat, config_json
                    direction_str = raw_params[2]
                    
                    # PnL Calculation
                    pnl_pct = 0.0
                    if be > 0 and curr_price > 0:
                        if direction_str == "LONG":
                            pnl_pct = (curr_price - be) / be * 100
                        else:
                            pnl_pct = (be - curr_price) / be * 100
                    
                    # Badge Color
                    badge_color = "green" if pnl_pct >= 0 else "red"
                    badge_bg = "#dafbe1" if pnl_pct >= 0 else "#ffebe9"
                    badge_text = "#1a7f37" if pnl_pct >= 0 else "#cf222e"
                    
                    # Render Badge
                    st.markdown(
                        f"""<span style='background-color: {badge_bg}; color: {badge_text}; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.8em;'>{pnl_pct:+.2f}%</span>""", 
                        unsafe_allow_html=True
                    )
                    
                    params = json.loads(raw_params[7]) if raw_params[7] else {}
                    strat = MartingaleStrategy(name=name, params=params)
                    
                    # Fetch minimal OHLCV for ATR grid if needed
                    market_data = pd.DataFrame() # Placeholder, ATR needs data
                    if params.get('UseATRGrid'):
                        # Unify ATR TF selection UI in Manager too
                        # We use the bot's configured ATR TF
                        target_tf = params.get('ATR_Timeframe', '1h')
                        
                        # Hybrid fetch for accurate metrics
                        ohlcv_1h = fetch_ohlcv_cached(bot_market_type, pair, '1h')
                        ohlcv_1d = fetch_ohlcv_cached(bot_market_type, pair, '1d')
                        
                        if ohlcv_1h and ohlcv_1d:
                            df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])  # type: ignore[arg-type]
                            df_1d = pd.DataFrame(ohlcv_1d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])  # type: ignore[arg-type]
                            for dff in [df_1h, df_1d]:
                                dff['timestamp'] = pd.to_datetime(dff['timestamp'], unit='ms')
                            
                            # Determine source based on timeframe
                            market_data = df_1d if 'd' in target_tf else df_1h
                        
                        # FALBACK: If OHLCV failed, try to construct minimal DF from current price
                        if market_data.empty and curr_price > 0:
                             market_data = pd.DataFrame([{'close': curr_price, 'high': curr_price*1.01, 'low': curr_price*0.99}], index=[0])
                    
                    next_order = strat.calculate_next_grid_price(raw_params[2], curr_price, be, step, market_data)
                    
                    # Highlight which TF is being used for the active bot
                    atr_active_tf = params.get('ATR_Timeframe', '1h')
                    row_cols[5].markdown(f"**BE:** {be:,.2f} | **TP:** {tp:,.2f}")
                    row_cols[5].markdown(f"**NO:** `{next_order:,.2f}` (ATR: {atr_active_tf})")

                    
                    if params.get('UseEarlyExit'):
                        # Check decay status if we have last exit info or can infer duration
                        # For simple visual, just show it's enabled
                        row_cols[5].caption("📉 *Decay Enabled*")
                except Exception as e:
                    row_cols[5].caption(f"BE: {be:.2f}")
                    row_cols[5].caption(f"TP: {tp:.2f}")
                    row_cols[5].write("Error loading NO")
            else:
                row_cols[5].write("-")

        # Toggle Status
        with row_cols[6]:
            # Check for error stop
            error_reason = None
            if not is_active:
                last_logs = get_trade_history(b_id, limit=1)
                if last_logs and last_logs[0][3] == 'ERROR_STOP': # action is index 3
                     error_reason = last_logs[0][11] # notes is index 11

            # Determine trading state (IN TRADE vs IDLE)
            in_trade = total_invested > 0
            
            # Status logic:
            # - Active + In Trade = "IN TRADE" (green)
            # - Active + Idle = "SCANNING" (blue)
            # - Paused = "PAUSED" (yellow)
            # - Error = "ERROR" (red)
            
            if error_reason:
                status_text = 'ERROR'
                pulse_color = "#cf222e"  # Red
                state_label = "ERROR"
            elif in_trade:
                status_text = 'IN TRADE'
                pulse_color = "#3fb950"  # Green
                state_label = f"TRADE (S{step})"
            elif is_active:
                status_text = 'Waiting for Signal'
                pulse_color = "#58a6ff"  # Blue
                state_label = "IDLE"
            else:
                status_text = 'PAUSED'
                pulse_color = "#d29921"  # Yellow
                state_label = "PAUSED"

            pulse_anim = """
            <style>
            .blob {
                background: """ + pulse_color + """;
                border-radius: 50%;
                margin: 5px;
                height: 10px;
                width: 10px;
                box-shadow: 0 0 0 0 """ + pulse_color + """;
                transform: scale(1);
                animation: pulse-green 2s infinite;
                display: inline-block;
            }
            @keyframes pulse-green {
                0% { transform: scale(0.95); box-shadow: 0 0 0 0 """ + pulse_color + """70; }
                70% { transform: scale(1); box-shadow: 0 0 0 10px """ + pulse_color + """00; }
                100% { transform: scale(0.95); box-shadow: 0 0 0 0 """ + pulse_color + """00; }
            }
            </style>
            """
            st.markdown(pulse_anim + f"<div style='display:flex;align-items:center;'><div class='blob'></div> {status_text}</div>", unsafe_allow_html=True)
            
            # Show trading state below status
            st.caption(f"State: {state_label}")
            
            if error_reason:
                 st.caption(f"🛑 {error_reason}")
            
            # Simple toggle below visual status
            if st.button("⏯️ Toggle", key=f"btn_toggle_{b_id}", help="Start/Stop Bot"):
                toggle_bot_active(b_id, not bool(is_active))
                st.success(f"✅ Bot {name} status updated!")
                st.rerun()
            
            # Position Management Section (only show if in trade)
            if total_invested > 0:
                with st.expander(f"🎛️ Position Controls for {name}", expanded=False):
                    # Get position summary
                    pos_summary = get_position_summary(b_id)
                    
                    # Display PnL
                    pnl = pos_summary.get('unrealized_pnl', 0)
                    pnl_pct = pos_summary.get('pnl_pct', 0)
                    pnl_color = "green" if pnl >= 0 else "red"
                    st.markdown(f"""
                    **Current PnL:** <span style="color:{pnl_color}">${pnl:,.2f} ({pnl_pct:+.2f}%)</span>
                    """, unsafe_allow_html=True)
                    
                    # Close buttons
                    st.markdown("**🛑 Close Position**")
                    close_cols = st.columns([1, 1, 1])
                    
                    # Full close button
                    if close_cols[0].button("🔴 Close All", key=f"close_all_{b_id}", help="Close 100% of position"):
                        result = close_position(b_id, close_pct=100.0, reason="Manual close from UI")
                        if result['success']:
                            st.success(f"✅ Closed position for {name}. PnL: ${result.get('pnl', 0):.2f}")
                            st.rerun()
                        else:
                            st.error(f"❌ Failed: {result.get('error')}")
                    
                    # Partial close buttons
                    if close_cols[1].button("🟡 50%", key=f"close_50_{b_id}", help="Close 50% of position"):
                        result = partial_close(b_id, pct=50, reason="Partial close 50%")
                        if result['success']:
                            st.success(f"✅ Closed 50% of {name}. PnL: ${result.get('pnl', 0):.2f}")
                            st.rerun()
                        else:
                            st.error(f"❌ Failed: {result.get('error')}")
                    
                    if close_cols[2].button("🟢 25%", key=f"close_25_{b_id}", help="Close 25% of position"):
                        result = partial_close(b_id, pct=25, reason="Partial close 25%")
                        if result['success']:
                            st.success(f"✅ Closed 25% of {name}. PnL: ${result.get('pnl', 0):.2f}")
                            st.rerun()
                        else:
                            st.error(f"❌ Failed: {result.get('error')}")
                    
                    # Stop settings
                    st.markdown("---")
                    st.markdown("**⚙️ Auto-Close Settings**")
                    
                    close_settings = pos_summary.get('close_settings', {})
                    set_cols = st.columns(3)
                    
                    with set_cols[0]:
                        current_pnl_target = close_settings.get('stop_after_pnl', 0)
                        new_pnl_target = st.number_input(
                            "Stop after PnL ($)", 
                            min_value=0.0, 
                            value=float(current_pnl_target),
                            step=5.0,
                            key=f"stop_pnl_{b_id}",
                            help="Close when PnL reaches this amount (0 = disabled)"
                        )
                        if st.button("💾 Save PnL Target", key=f"save_pnl_{b_id}"):
                            if set_stop_after_pnl(b_id, new_pnl_target):
                                st.success("✅ PnL target updated")
                                st.rerun()
                    
                    with set_cols[1]:
                        current_time_limit = close_settings.get('stop_after_time', 0)
                        new_time_limit = st.number_input(
                            "Stop after (hours)", 
                            min_value=0, 
                            value=int(current_time_limit),
                            step=1,
                            key=f"stop_time_{b_id}",
                            help="Close after this many hours in trade (0 = disabled)"
                        )
                        if st.button("💾 Save Time Limit", key=f"save_time_{b_id}"):
                            if set_stop_after_time(b_id, new_time_limit):
                                st.success("✅ Time limit updated")
                                st.rerun()
                    
                    with set_cols[2]:
                        current_manual_pct = close_settings.get('manual_close_pct', 100)
                        new_manual_pct = st.number_input(
                            "Manual Close %", 
                            min_value=10, 
                            max_value=100, 
                            value=int(current_manual_pct),
                            key=f"manual_pct_{b_id}",
                            help="Default % to close when using manual close"
                        )
                        if st.button("💾 Save Close %", key=f"save_close_{b_id}"):
                            if set_manual_close_pct(b_id, new_manual_pct):
                                st.success("✅ Manual close % updated")
                                st.rerun()

        # Actions
        with row_cols[7]:
            col1, col2 = st.columns(2)
            if col1.button("✏️ Edit", key=f"edit_{b_id}", help=f"Edit {name} settings"):
                render_edit_form(b_id)

            if col2.button("🗑️ Delete", key=f"del_{b_id}", help=f"Delete {name}"):
                if delete_bot(b_id):
                    st.success(f"✅ Deleted {name} successfully!")
                    st.rerun()
        
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

    name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config_json = params
    config_dict = json.loads(config_json) if config_json else {}
    
    current_quote = "USDT"
    if pair and '/' in pair:
        current_quote = pair.split('/')[1]


    # --- Market Configuration (Per-Bot) ---
    # KEPT OUTSIDE FORM for dynamic updates
    st.markdown("#### 🌐 Market Configuration")
    mcol1, mcol2, mcol3 = st.columns(3)
    
    with mcol1:
        current_market_type = config_dict.get('market_type', 'future')
        market_options = ["Spot", "Futures (Swap)"]
        market_idx = 0 if current_market_type == 'spot' else 1
        new_market_type_display = st.selectbox(
            "Market Type", 
            market_options, 
            index=market_idx,
            key=f"edit_market_type_{bot_id}"
        )
        new_market_type = 'spot' if new_market_type_display == "Spot" else 'future'
    
    with mcol2:
        quote_options = ["USDT", "USDC"]
        quote_idx = quote_options.index(current_quote) if current_quote in quote_options else 0
        new_quote = st.selectbox(
            "Quote Asset",
            quote_options,
            index=quote_idx,
            key=f"edit_quote_{bot_id}"
        )
    
    with mcol3:
        # Fetch available pairs dynamically
        try:
            # Use cached instance for connection, but we need to ensure markets are loaded
            edit_exchange = get_exchange_instance(new_market_type)
            
            # We can't easily cache the list of symbols per quote without a new wrapper, 
            # but get_available_symbols is relatively fast if markets are loaded.
            # Force load if needed (ExchangeInterface usually loads on init)
            available_pairs = edit_exchange.get_available_symbols(quote_asset=new_quote)
            if not available_pairs:
                available_pairs = [f"BTC/{new_quote}", f"ETH/{new_quote}"]
        except Exception as e:
            st.warning(f"Could not fetch pairs: {e}")
            available_pairs = [f"BTC/{new_quote}", f"ETH/{new_quote}", f"SOL/{new_quote}"]
        
        # Find current pair in list, or default to first
        pair_idx = 0
        # Check if current pair matches the new quote asset
        if pair in available_pairs:
            pair_idx = available_pairs.index(pair)
        elif f"{pair.split('/')[0]}/{new_quote}" in available_pairs:
            # Try to keep same base asset with new quote
            pair_idx = available_pairs.index(f"{pair.split('/')[0]}/{new_quote}")
        
        new_pair = st.selectbox(
            "Trading Pair",
            available_pairs,
            index=pair_idx,
            key=f"edit_pair_{bot_id}_{new_quote}_{new_market_type}"
        )
    
    # Store market_type in config_dict for saving
    config_dict['market_type'] = new_market_type
    
    st.divider()
    
    # --- MAIN FORM ---
    # Wrap all other settings in a form to prevent reload on every change
    with st.form(key=f"edit_bot_form_{bot_id}"):
        col1, col2 = st.columns(2)
        new_name = col1.text_input("Bot Name", value=name)
        new_direction = col2.selectbox("Direction", ["LONG", "SHORT"], index=0 if direction == "LONG" else 1)
        
        # Leverage Editing (Futures Only)
        if new_market_type == 'future':
            current_lev = int(config_dict.get('leverage', 1))
            new_leverage = col2.slider("Leverage (x)", 1, 50, current_lev)
            config_dict['leverage'] = new_leverage
        else:
            config_dict['leverage'] = 1

        col3, col4 = st.columns(2)
        # Strategy type options matching bot_creator
        strat_options = ["Martingale", "MarketMaker", "MagicHour"]
        strat_index = 0
        if strategy_type in strat_options:
            strat_index = strat_options.index(strategy_type)
        new_strat = col3.selectbox("Strategy Type", strat_options, index=strat_index)
        
        col5, col6, col7 = st.columns(3)
        # Safe Min Calculation
        # Safe Min Calculation
        min_safe_usd = 5.0
        if new_pair:
            try:
                # Use cached instance
                exchange = get_exchange_instance(new_market_type)
                # Fetch price for accurate calc
                p_for_calc = fetch_last_price_cached(new_market_type, new_pair)
                if p_for_calc > 0:
                    min_safe_usd = exchange.calculate_safe_min_size(new_pair, p_for_calc)
                else:
                     min_safe_usd = exchange.get_min_order_usd(new_pair)
            except Exception:
                pass
                
        # Ensure min_safe_usd is at least 5.0 as a baseline
        min_safe_usd = max(5.0, min_safe_usd)
                
        new_base = col5.number_input(
            f"Order Size ($USDC) [Safe Min: ${min_safe_usd:.2f}]", 
            min_value=0.0,  # Allow 0 to let user type, but validate below
            step=1.0, 
            value=max(float(base_size), min_safe_usd),
            help=f"Minimum calculated based on exchange limits + rounding. Lower values will be rejected by Binance."
        )
        
        if new_base < min_safe_usd:
            col5.error(f"⚠️ TOO LOW! Min Safe: ${min_safe_usd:.2f}")
            if col5.button("🔧 Auto-Fix Size", key=f"fix_size_{bot_id}"):
                # We can't update the widget directly easily without session state hacks, 
                # but we can update the config value which will reflect on next render?
                # Actually, easier to just let the user type, but show strong error.
                pass

            
        new_mm = col6.number_input("Martingale Multiplier", value=float(martingale_multiplier))
        
        # New Max Steps Input
        new_max_steps = col7.number_input("Max Steps", min_value=1, max_value=30, value=int(config_dict.get('max_steps', 10)))
        config_dict['max_steps'] = new_max_steps

        # Legacy RSI Limit (Hidden/Fixed)
        new_rsi = float(rsi_limit) if rsi_limit else 30.0

        # --- NEW: Take Profit Editing ---
        st.markdown("#### Take Profit Logic")
        curr_tp_type = config_dict.get('TakeProfitType', 'USD')
        # Map USD -> index 0, Percent -> index 1
        tp_type_idx = 0 if curr_tp_type == 'USD' else 1
        
        new_tp_type = st.radio("TP Mode", ["Dollar Target ($)", "Percentage (%)"], index=tp_type_idx, horizontal=True)
        
        if new_tp_type == "Dollar Target ($)":
            new_tp_base = st.number_input("Take Profit Target ($USDC)", min_value=0.1, step=0.1, value=float(config_dict.get('TakeProfitBase', 10.0)))
            config_dict['TakeProfitType'] = 'USD'
            config_dict['TakeProfitBase'] = new_tp_base
        else:
            new_tp_pct = st.number_input("Take Profit Target (%)", min_value=0.01, step=0.01, value=float(config_dict.get('TakeProfitPct', 1.0)), format="%.2f")
            config_dict['TakeProfitType'] = 'Percent'
            config_dict['TakeProfitPct'] = new_tp_pct

        # --- NEW: Risk & Grid Configuration ---
        st.markdown("#### 🛡️ Risk & Grid Configuration")
        with st.expander("Grid Spacing & Safety", expanded=False):
            st.subheader("Grid Spacing Logic")
            
            # Consolidated Grid Logic
            use_atr_grid = st.checkbox("Use Dynamic ATR Grid", value=config_dict.get('UseATRGrid', False), help="If OFF, uses fixed 'Base Grid' distance.")
            config_dict['UseATRGrid'] = use_atr_grid
            
            col_grid_main1, col_grid_main2 = st.columns(2)
            
            with col_grid_main1:
                # ATR Configuration
                st.markdown("##### 📉 ATR Settings")
                current_tf = config_dict.get('ATR_Timeframe', '1h')
                atr_tf_options = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
                atr_tf_idx = atr_tf_options.index(current_tf) if current_tf in atr_tf_options else 4
                
                atr_tf = st.selectbox(
                    "ATR Timeframe", 
                    atr_tf_options, 
                    index=atr_tf_idx, 
                    help="Timeframe used to calculate ATR for grid spacing."
                )
                config_dict['ATR_Timeframe'] = atr_tf
                
                atr_periods = st.number_input(
                    "ATR Periods", 
                    value=int(config_dict.get('ATRPeriods', 14)), 
                    min_value=3, 
                    max_value=240
                )
                config_dict['ATRPeriods'] = atr_periods
                
                atr_mode = st.radio(
                    "ATR Mode",
                    ["dynamic", "locked"],
                    index=0 if config_dict.get('ATRMode', 'dynamic') == 'dynamic' else 1,
                    horizontal=True
                )
                config_dict['ATRMode'] = atr_mode

            with col_grid_main2:
                # Spacing Configuration
                st.markdown("##### 📐 Spacing Settings")
                if use_atr_grid:
                    config_dict['ATRGridFactor'] = st.number_input(
                        "Base Spacing (ATR Multiplier)", 
                        value=float(config_dict.get('ATRGridFactor', 1.0)), 
                        step=0.1
                    )
                    config_dict['base_grid'] = 100.0 # Default hidden
                else:
                    config_dict['base_grid'] = st.number_input(
                        "Fixed Grid Price Step", 
                        value=float(config_dict.get('base_grid', 100.0)), 
                        step=10.0
                    )
                    config_dict['ATRGridFactor'] = 1.0 # Default hidden

                # 2. Martingale Spacing (Exponential Grid)
                # Check if currently enabled (Multiplier != 1.0)
                curr_mult = float(config_dict.get('GridMultiplier', 1.0))
                is_exp_enabled = abs(curr_mult - 1.0) > 0.001
                use_grid_mult = st.checkbox("Enable Exponential Spacing", value=is_exp_enabled)
                
                if use_grid_mult:
                     # Default to 1.1 if enabling for first time (curr_mult was 1.0)
                     val_to_show = curr_mult if is_exp_enabled else 1.1
                     config_dict['GridMultiplier'] = st.number_input("Spacing Multiplier", value=val_to_show, step=0.05, min_value=0.1, help="> 1.0 expands grid. < 1.0 tightens grid.")
                else:
                     config_dict['GridMultiplier'] = 1.0

            st.divider()
            
            # Advanced Rules Section (Consolidated)
            st.markdown("##### 🎯 Advanced Step-Based Rules")
            grid_rules = config_dict.get('GridStepRules', [])
            
            if grid_rules:
                st.info(f"✅ Active Rules: {len(grid_rules)}")
                for rule in grid_rules:
                    r_desc = f"Steps {rule['start']}-{rule['end']}: "
                    if rule['type'] == 'atr': r_desc += f"ATR × {rule['multiplier']}"
                    else: r_desc += f"Fixed ${rule['value']}"
                    st.markdown(f"- {r_desc}")
                st.caption("ℹ️ To modify rules, please re-create the bot or edit the JSON config directly if comfortable.")
            else:
                 st.caption("No custom step rules defined.")

        # Math Projection in Editor
        try:
            # Merge basic params with config_dict to ensure Strategy __init__ sees everything (like UseATRGrid)
            combined_params = config_dict.copy()
            combined_params.update({
                'base_size': new_base, 
                'martingale_multiplier': new_mm, 
                'direction': new_direction, 
                'max_steps': new_max_steps
            })
            
            temp_strat = MartingaleStrategy(params=combined_params)
            
            # Fetch current price for projection using the bot's market type
            # Use cached fetch
            ohlcv = fetch_ohlcv_cached(new_market_type, new_pair if new_pair else "BTC/USDT", '1m')
            # Handle cached result which might be empty
            if ohlcv and len(ohlcv) > 0:
                curr_p = float(ohlcv[-1][4]) # Use last close
            else:
                curr_p = 40000.0
            
            # Pass ATR context for grid
            # Use 'ATR_Timeframe' from config if present
            atr_tf = config_dict.get('ATR_Timeframe', '1h')
            
            # Calculate Real ATR for Projection
            p_atr = 0.0
            atr_timeframe = config_dict.get('ATR_Timeframe', '1h')
            atr_period = int(config_dict.get('ATRPeriods', 14))
            atr_period = min(max(atr_period, 3), 240)  # Clamp to valid range
            
            try:
                # Fetch data at ATR timeframe - Cached
                ohlcv_atr = fetch_ohlcv_cached(new_market_type, new_pair if new_pair else "BTC/USDT", atr_timeframe)
                
                if ohlcv_atr:
                    df_atr = pd.DataFrame(ohlcv_atr, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    # Calculate True Range
                    tr1 = df_atr['high'] - df_atr['low']
                    tr2 = (df_atr['high'] - df_atr['close'].shift()).abs()
                    tr3 = (df_atr['low'] - df_atr['close'].shift()).abs()
                    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                    
                    if len(true_range) >= atr_period:
                        # Average True Range over atr_period
                        p_atr = float(true_range.iloc[-atr_period:].mean())
                    else:
                        p_atr = curr_p * 0.01  # Fallback 1% if not enough data
                else:
                    p_atr = curr_p * 0.01  # Fallback 1% if data fetch fails
            except Exception as e:
                p_atr = curr_p * 0.01
                st.warning(f"ATR Calc Failed: {e}")

            projections = temp_strat.calculate_projections(base_price=curr_p, current_atr=p_atr)
            with st.expander("🔍 Editor Risk Projection & Math Summary", expanded=False):
                st.caption("ℹ️ Projections update after saving changes.")
                # Display Key Metrics
                m1, m2, m3 = st.columns(3)
                m1.metric("Simulated Price", f"${curr_p:,.4f}")
                m2.metric(f"ATR ({atr_tf})", f"{p_atr:.4f}")
                
                grid_dist_pips = p_atr * float(config_dict.get('ATRGridFactor', 1.0)) if config_dict.get('UseATRGrid') else float(config_dict.get('base_grid', 25.0))
                m3.metric("Grid Step Size", f"{grid_dist_pips:.4f}")

                # --- DYNAMIC GRID VISUALIZER ---
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
                    fig.add_hline(y=curr_p, line_dash="solid", line_color="#1f2328", annotation_text="Entry")
                    
                    fig.update_layout(
                        title="Grid Visualizer",
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
                # -------------------------------

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
                        st.info("ℹ️ No Hedge Configured.")
                        
        except Exception as e:
            st.warning(f"Projection skip: {e}")

        # --- Strategy-Specific Configuration Sections ---
        if new_strat == "MagicHour":
            st.markdown("#### 🕰️ Magic Hour Configuration")
            st.info("🎯 **Strategy Goal:** Capture mean reversion after breakout from a specific hourly range.")
            
            # Timezone Selector
            common_tzs = ["Asia/Taipei", "America/New_York", "Europe/London", "Asia/Tokyo", "UTC"]
            curr_tz = config_dict.get('timezone', 'America/New_York')
            tz_idx = common_tzs.index(curr_tz) if curr_tz in common_tzs else 0
            selected_tz = st.selectbox("🌍 Strategy Timezone", common_tzs, index=tz_idx)
            config_dict['timezone'] = selected_tz
            
            mh1, mh2 = st.columns(2)
            with mh1:
                config_dict['magic_hour'] = st.slider(
                    f"🕒 Magic Hour ({selected_tz} 0-23)", 
                    0, 23, 
                    int(config_dict.get('magic_hour', 9)), 
                    help=f"The specific hour that defines the trading range (e.g. 9 = 09:00-10:00 {selected_tz})."
                )
                config_dict['analysis_duration'] = st.slider(
                    "⏳ Analysis Window (Hours)", 
                    1, 6, 
                    int(config_dict.get('analysis_duration', 3)), 
                    help="Duration to monitor for breakouts after the Magic Hour closes."
                )
            with mh2:
                config_dict['stop_loss_ext'] = st.number_input(
                    "🛑 Max Extension (Fade Zone)", 
                    value=float(config_dict.get('stop_loss_ext', 1.0)), 
                    step=0.1, 
                    help="Allowed deviation multiplier. If Price > High + (Range * Extension), we assume strong trend and STOP fading."
                )
                st.success("✅ Target is fixed at **50% Mean Reversion** (Range Midpoint).")
        
        elif new_strat == "MarketMaker":
            st.markdown("#### 📈 Market Maker Configuration")
            st.info("🎯 **Strategy Goal:** High-frequency spread capturing in ranging markets.")
            
            mm_c1, mm_c2 = st.columns(2)
            with mm_c1:
                config_dict['spread_pct'] = st.number_input(
                    "Target Spread (%)", 
                    value=float(config_dict.get('spread_pct', 0.2)), 
                    step=0.01
                )
                config_dict['skew_factor'] = st.number_input(
                    "Inventory Skew Factor", 
                    value=float(config_dict.get('skew_factor', 0.0)), 
                    step=1.0, 
                    help="Shift price per unit of inventory."
                )
            with mm_c2:
                config_dict['max_inventory'] = st.number_input(
                    "Max Inventory (Units)", 
                    value=float(config_dict.get('max_inventory', 1.0))
                )
                config_dict['reprice_threshold'] = st.number_input(
                    "Reprice Threshold (%)", 
                    value=float(config_dict.get('reprice_threshold', 0.1))
                )
        
        # Martingale-specific triggers (only show for Martingale strategy)
        if new_strat == "Martingale":
            st.markdown("#### Entry Triggers (8-Switch Confluence)")
            t_col1, t_col2, t_col3, t_col4 = st.columns(4)
            with t_col1:
                config_dict['mode_cci'] = st.selectbox("CCI Switch", [0, 1, 2], index=int(config_dict.get('mode_cci', 0)), format_func=lambda x: {0: "OFF", 1: "Above", 2: "Below"}[x])
                config_dict['cci_level'] = st.number_input("CCI Level", value=float(config_dict.get('cci_level', 100)))
                config_dict['cci_tf'] = st.selectbox("CCI TF", ["1m","5m","15m","1h","4h","1d"], index=["1m","5m","15m","1h","4h","1d"].index(config_dict.get('cci_tf', "15m")))
            with t_col2:
                config_dict['mode_boll'] = st.selectbox("Boll Switch", [0, 1, 2], index=int(config_dict.get('mode_boll', 0)), format_func=lambda x: {0: "OFF", 1: "Outside Lower", 2: "Outside Upper"}[x])
                config_dict['boll_tf'] = st.selectbox("Boll TF", ["1m","5m","15m","1h","4h","1d"], index=["1m","5m","15m","1h","4h","1d"].index(config_dict.get('boll_tf', "15m")))
            with t_col3:
                config_dict['mode_stoch'] = st.selectbox("Stoch Switch", [0, 1, 2], index=int(config_dict.get('mode_stoch', 0)), format_func=lambda x: {0: "OFF", 1: "Oversold", 2: "Overbought"}[x])
                config_dict['stoch_tf'] = st.selectbox("Stoch TF", ["1m","5m","15m","1h","4h","1d"], index=["1m","5m","15m","1h","4h","1d"].index(config_dict.get('stoch_tf', "15m")))
            with t_col4:
                config_dict['mode_rsi'] = st.selectbox("RSI Switch", [0, 1, 2], index=int(config_dict.get('mode_rsi', 0)), format_func=lambda x: {0: "OFF", 1: "Below", 2: "Above"}[x])
                config_dict['rsi_level'] = st.number_input("RSI Level", value=float(config_dict.get('rsi_level', 30)))
                config_dict['rsi_tf'] = st.selectbox("RSI TF", ["1m","15m","1h"], index=["1m","15m","1h"].index(config_dict.get('rsi_tf', "15m")))

            st.markdown("#### Price & Volatility Triggers (9 & 10)")
            pv_col1, pv_col2 = st.columns(2)
            with pv_col1:
                config_dict['mode_price'] = st.selectbox("Price Switch", [0, 1, 2], index=int(config_dict.get('mode_price', 0)), format_func=lambda x: {0: "OFF", 1: "Above", 2: "Below"}[x])
                config_dict['price_threshold'] = st.number_input("Threshold Price", value=float(config_dict.get('price_threshold', 0.0)))
            with pv_col2:
                st.markdown("**Trigger 10: Market State**")
                config_dict['mode_atrp'] = st.selectbox("Volatility Context", [0, 1, 2], index=int(config_dict.get('mode_atrp', 0)), format_func=lambda x: {0: "OFF", 1: "Below (Quiet)", 2: "Above (Extreme)"}[x], help="Compares current volatility to historical levels.")
                pa1, pa2 = st.columns(2)
                config_dict['atrp_level'] = pa1.number_input("Lookback Level %", value=float(config_dict.get('atrp_level', 50.0)))
                config_dict['atrp_tf'] = pa2.selectbox("ATR TF", ["15m","1h","4h","1d"], index=1, key=f"atrp_tf_select_{bot_id}")


            st.markdown("#### Trigger 11: ATR Expansion (Current Move vs Range)")
            e_col1, e_col2, e_col3 = st.columns(3)
            with e_col1:
                config_dict['mode_atre'] = st.selectbox("Expansion Move", [0, 1, 2], index=int(config_dict.get('mode_atre', 0)), format_func=lambda x: {0: "OFF", 1: "Move Up >= X%", 2: "Move Down >= X%"}[x], help="Move from open as % of ATR.")
            with e_col2:
                config_dict['atre_level'] = st.number_input("Target % of ATR", value=float(config_dict.get('atre_level', 100.0)))
            with e_col3:
                config_dict['atre_tf'] = st.selectbox("TF to Watch (T11)", ["1h","4h","1d"], index=0)

            st.markdown("#### Pattern Slots")
            for p_idx in range(1, 4, 2):
                pc1, pc2 = st.columns(2)
                for i, col in enumerate([pc1, pc2]):
                    idx = p_idx + i
                    with col:
                        c_p1, c_p2, c_p3, c_p4 = st.columns(4)
                        config_dict[f'pat_{idx}_mode'] = c_p1.selectbox(f"Type ##{idx}", [0, 1, 2], index=int(config_dict.get(f'pat_{idx}_mode', 0)), format_func=lambda x: {0: "OFF", 1: "Up", 2: "Down"}[x])
                        config_dict[f'pat_{idx}_source'] = c_p2.selectbox(f"Source ##{idx}", ["Price", "RSI", "CCI"], index=["Price", "RSI", "CCI"].index(config_dict.get(f'pat_{idx}_source', "Price")))
                        config_dict[f'pat_{idx}_tf'] = c_p3.selectbox(f"TF ##{idx}", ["1m","5m","15m","1h","4h","1d"], index=["1m","5m","15m","1h","4h","1d"].index(config_dict.get(f'pat_{idx}_tf', "5m")))
                        config_dict[f'pat_{idx}_count'] = c_p4.number_input(f"Count ##{idx}", min_value=1, value=int(config_dict.get(f'pat_{idx}_count', 3)))

            st.markdown("#### Advanced Filters & MTF Trend")
            af1, af2, af3, af4 = st.columns(4)
            with af1:
                use_mtf = st.checkbox("MTF Trend Filter", value=bool(config_dict.get('UseMTFConfluence', False)))
                config_dict['UseMTFConfluence'] = use_mtf
            with af2:
                config_dict['MTF_Timeframe'] = st.selectbox("MTF Timeframe", ["1h","4h","1d","1w"], index=["1h","4h","1d","1w"].index(config_dict.get('MTF_Timeframe', "4h")), disabled=not use_mtf)
            with af3:
                config_dict['MTF_MA_Period'] = st.number_input("MTF MA Period", value=int(config_dict.get('MTF_MA_Period', 50)), disabled=not use_mtf)
            with af4:
                config_dict['mode_correlation'] = st.selectbox("Correlation", [0, 1, 2], index=int(config_dict.get('mode_correlation', 0)), format_func=lambda x: {0: "OFF", 1: "Positive (>0.7)", 2: "Negative (< -0.7)"}[x])


        st.markdown("#### Risk Management")
        rm1, rm2, rm3 = st.columns(3)
        with rm1:
            config_dict['UseATRGrid'] = st.checkbox("Use ATR Grid", value=config_dict.get('UseATRGrid', True))
            if config_dict['UseATRGrid']:
                atr_tf_risk = st.selectbox(
                    "ATR TF", 
                    ["1m", "5m", "15m", "1h", "4h", "1d"], 
                    index=["1m", "5m", "15m", "1h", "4h", "1d"].index(config_dict.get('ATR_Timeframe', '1h')),
                    key=f"risk_atr_tf_{bot_id}"
                )

                config_dict['ATR_Timeframe'] = atr_tf_risk
        with rm2:
                config_dict['ATRGridFactor'] = st.number_input("ATR Factor", value=float(config_dict.get('ATRGridFactor', 1.0)))
        with rm3:
            config_dict['daily_loss_limit'] = st.number_input("Daily Loss Limit ($)", value=float(config_dict.get('daily_loss_limit', 0.0)), help="Pause bot if daily realized loss exceeds this amount.")

        rm_r1, rm_r2 = st.columns(2)
        with rm_r1:
            config_dict['MaxDrawdownPct'] = st.number_input("Max Drawdown (%)", value=float(config_dict.get('MaxDrawdownPct', 0.0)), help="Trigger partial close if drawdown exceeds this %.")
        with rm_r2:
            if not config_dict.get('UseATRGrid'):
                config_dict['base_grid'] = st.number_input("Fixed Step", value=float(config_dict.get('base_grid', 100.0)))
            else:
                pass
            
            config_dict['UseVolSizing'] = st.checkbox("Volatility Position Sizing", value=config_dict.get('UseVolSizing', False), help="Adjusts lot size based on ATR (High Vol = Smaller Size).")

        st.markdown("#### Advanced Exit & Hedge Settings")
        col_ee1, col_ee2, col_ee3, col_ee4 = st.columns(4)
        with col_ee1:
            use_ee = st.checkbox("Use Early Exit", value=config_dict.get('UseEarlyExit', False), help="Moves TP target closer to Break Even over time to exit stale trades safely.")
        with col_ee2:
            decay_interval = st.number_input("Decay Interval (Mins)", value=float(config_dict.get('DecayIntervalMins', 15.0)), help="How often (in minutes) the profit target is reduced.")
        with col_ee3:
            decay_pct = st.number_input("TP Reduction (%)", value=float(config_dict.get('DecayPercentPerInterval', 30.0)), help="What percentage of the current profit target to cut per interval.")
        with col_ee4:
            hedge_step = st.number_input("Hedge Step", min_value=1, max_value=10, value=int(config_dict.get('HedgeStartStep', 7)), help="Which Martingale step triggers the hedge trade.")
        
        st.markdown("#### Post-Exit Re-entry & Cooldown")
        re1, re2, re3 = st.columns(3)
        with re1:
            reentry_mins = st.number_input("Cooldown (Mins)", value=float(config_dict.get('reentry_cooldown_mins', 0.0)))
        with re2:
            reentry_dist = st.number_input("Re-entry Dist (%)", value=float(config_dict.get('reentry_distance_pct', 0.0)))
        with re3:
            post_stop = st.checkbox("Stop After Cycle", value=bool(config_dict.get('post_exit_stop', False)))

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
        submitted = submit_cols[0].form_submit_button("💾 Save All Changes", type="primary")

    # Handle Submission
    if submitted:
        try:
            # Reconstruct Config from Form Widgets to allow one-click save
            # (Bypassing JSON text area state issues)
            new_conf = config_dict.copy()
            new_conf.update({
                'market_type': new_market_type,
                'leverage': int(config_dict.get('leverage', 1)), # Updated in widget above
                'max_steps': int(config_dict.get('max_steps', 10)),
                
                # Indicators
                'mode_cci': int(config_dict.get('mode_cci', 0)), 
                'cci_level': float(config_dict.get('cci_level', 100)), 
                'cci_tf': config_dict.get('cci_tf', '15m'),
                'mode_boll': int(config_dict.get('mode_boll', 0)), 
                'boll_tf': config_dict.get('boll_tf', '15m'),
                'mode_stoch': int(config_dict.get('mode_stoch', 0)), 
                'stoch_tf': config_dict.get('stoch_tf', '15m'),
                'mode_rsi': int(config_dict.get('mode_rsi', 0)), 
                'rsi_level': float(config_dict.get('rsi_level', 30)), 
                'rsi_tf': config_dict.get('rsi_tf', '15m'),
                
                # Triggers
                'mode_price': int(config_dict.get('mode_price', 0)), 
                'price_threshold': float(config_dict.get('price_threshold', 0.0)),
                'mode_atrp': int(config_dict.get('mode_atrp', 0)), 
                'atrp_level': float(config_dict.get('atrp_level', 50.0)),
                'mode_atre': int(config_dict.get('mode_atre', 0)), 
                'atre_level': float(config_dict.get('atre_level', 100.0)),
                
                # Trigger 12
                'mode_ma': int(config_dict.get('mode_ma', 0)),
                'ma_period': int(config_dict.get('ma_period', 200)),
                'ma_tf': config_dict.get('ma_tf', '1h'),
                'ma_type': config_dict.get('ma_type', 'SMA'),

                # Risk / Grid
                'UseATRGrid': bool(config_dict.get('UseATRGrid', True)),
                'ATRGridFactor': float(config_dict.get('ATRGridFactor', 1.0)),
                'ATR_Timeframe': config_dict.get('ATR_Timeframe', '1h'),
                'ATRMode': config_dict.get('ATRMode', 'dynamic'),
                'base_grid': float(config_dict.get('base_grid', 100.0)),
                'GridMultiplier': float(config_dict.get('GridMultiplier', 1.0)),
                
                # Phase 10
                'UseVolSizing': bool(config_dict.get('UseVolSizing', False)),
                'UseMTFConfluence': bool(config_dict.get('UseMTFConfluence', False)),
                'MTF_Timeframe': config_dict.get('MTF_Timeframe', '4h'),
                'MTF_MA_Period': int(config_dict.get('MTF_MA_Period', 50)),
                'mode_correlation': int(config_dict.get('mode_correlation', 0)),
                
                # Phase 10.2 Risk
                'daily_loss_limit': float(config_dict.get('daily_loss_limit', 0.0)),
                'MaxDrawdownPct': float(config_dict.get('MaxDrawdownPct', 0.0)),
                
                # Exit
                'TakeProfitType': config_dict.get('TakeProfitType', 'USD'),
                'TakeProfitBase': float(config_dict.get('TakeProfitBase', 10.0)),
                'TakeProfitPct': float(config_dict.get('TakeProfitPct', 1.0)),
                'UseEarlyExit': bool(config_dict.get('UseEarlyExit', False)),
                'DecayIntervalMins': float(config_dict.get('DecayIntervalMins', 15.0)),
                'DecayPercentPerInterval': float(config_dict.get('DecayPercentPerInterval', 30.0)),
                
                # Trailing Profit
                'MaximizeProfit': bool(config_dict.get('MaximizeProfit', False)), # Trailing Switch
                'ProfitSet': float(config_dict.get('ProfitSet', 0.5)),           # Trailing Distance
                
                # Hedge
                'UseHedge': bool(config_dict.get('UseHedge', False)),
                'HedgeStartStep': int(config_dict.get('HedgeStartStep', 7)),
                'reentry_cooldown_mins': float(config_dict.get('reentry_cooldown_mins', 0.0)),
                'reentry_distance_pct': float(config_dict.get('reentry_distance_pct', 0.0)),
                'post_exit_stop': bool(config_dict.get('post_exit_stop', False))
            })
            

            # Call Update
            if update_bot(bot_id, new_name, new_pair, new_direction, float(config_dict.get('rsi_level', 30)), new_mm, new_base, new_strat, new_conf):
                st.success("✅ Bot updated successfully!")
                st.rerun()
            else:
                st.error("❌ Failed to update bot.")
        except Exception as e:
            st.error(f"❌ Error Saving: {e}")

    if st.button("❌ Cancel", key=f"btn_cancel_{bot_id}"):
        st.rerun()
