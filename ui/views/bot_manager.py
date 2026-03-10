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

    for bot in bots:

        # Current get_all_bots returns: b.id, b.name, b.pair, b.is_active, b.strategy_type, t.total_invested, t.current_step, last_error, last_error_time
        # Robust unpacking to handle stale module versions
        b_id, name, pair, is_active, strat_type, total_invested, step = bot[:7]
        last_error = bot[7] if len(bot) > 7 else None
        last_error_time = bot[8] if len(bot) > 8 else None
        
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
                    strat = MartingaleStrategy(params=params)
                    
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
                    # Show valid error for debugging, but abbr if too long
                    err_msg = str(e)[:20] + "..." if len(str(e)) > 20 else str(e)
                    row_cols[5].caption(f"Err: {err_msg}")
                    logger.error(f"Error calculating NO for {name}: {e}")
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

            # --- PARTIAL FILL CHECK (Yellow Light) ---
            is_partial = False
            if in_trade:
                try:
                    conn = get_connection()
                    cursor = conn.cursor()
                    # Check if any open order has filled_amount > 0
                    cursor.execute("SELECT COUNT(*) FROM bot_orders WHERE bot_id = ? AND status = 'open' AND filled_amount > 0", (b_id,))
                    is_partial = cursor.fetchone()[0] > 0
                except:
                    pass
            
            if error_reason:
                status_text = 'ERROR'
                pulse_color = "#cf222e"  # Red
                state_label = "ERROR"
            elif in_trade:
                if is_partial:
                    status_text = 'PARTIAL FILL'
                    pulse_color = "#d29921"  # Yellow
                    state_label = f"PARTIAL (S{step})"
                else:
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
            
            if last_error:
                 st.markdown(f"<div style='color: #cf222e; font-size: 0.8em; margin-top: 5px;'>⚠️ {last_error}</div>", unsafe_allow_html=True)
            
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
                            
                    # Danger Zone: Panic Close
                    st.markdown("---")
                    st.markdown("**🚨 Emergency Panic Close**")
                    panic_confirm = st.checkbox(f"⚠️ Confirm instant MARKET close for 100% of {name}'s position.", key=f"confirm_panic_{b_id}")
                    if st.button("🚨 PANIC CLOSE & FLATTEN", key=f"panic_all_{b_id}", type="primary", disabled=not panic_confirm, help="Instantly market-sells entire position and resets bot state."):
                        result = close_position(b_id, close_pct=100.0, reason="PANIC FLAT from UI")
                        if result['success']:
                            st.success(f"✅ PANIC CLOSED {name}. PnL: ${result.get('pnl', 0):.2f}")
                            st.rerun()
                        else:
                            st.error(f"❌ Panic Failed: {result.get('error')}")
                    
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
        raw_quote = pair.split('/')[1]
        current_quote = raw_quote.split(':')[0] if ':' in raw_quote else raw_quote

    # --- Market Configuration (Per-Bot) ---
    # KEPT OUTSIDE FORM for dynamic updates
    st.markdown("#### 🌐 Market Configuration")
    mcol1, mcol2, mcol3 = st.columns(3)
    
    with mcol1:
        # Load market type from config, default to 'future' if missing
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
        # Load quote asset from pair string or config
        # Pair format: BASE/QUOTE or BASE/QUOTE:QUOTE
        inferred_quote = "USDT"
        if pair and '/' in pair:
            raw_quote = pair.split('/')[1]
            inferred_quote = raw_quote.split(':')[0] if ':' in raw_quote else raw_quote
            
        quote_options = ["USDT", "USDC"]
        quote_idx = quote_options.index(inferred_quote) if inferred_quote in quote_options else 0
        new_quote = st.selectbox(
            "Quote Asset",
            quote_options,
            index=quote_idx,
            key=f"edit_quote_{bot_id}"
        )
    
    with mcol3:
        # Fetch available pairs dynamically
        try:
            # Use cached instance for connection
            edit_exchange = get_exchange_instance(new_market_type)
            available_pairs = edit_exchange.get_available_symbols(quote_asset=new_quote)
            if not available_pairs:
                available_pairs = [f"BTC/{new_quote}", f"ETH/{new_quote}"]
        except Exception as e:
            st.warning(f"Could not fetch pairs: {e}")
            available_pairs = [f"BTC/{new_quote}", f"ETH/{new_quote}", f"SOL/{new_quote}"]
        
        # Smart Index Match
        pair_idx = 0
        current_pair_normalized = pair.strip().upper()
        
        # Try exact match
        if current_pair_normalized in available_pairs:
            pair_idx = available_pairs.index(current_pair_normalized)
        # Try matching Base asset (e.g. switching USDT -> USDC)
        elif '/' in current_pair_normalized:
            base = current_pair_normalized.split('/')[0]
            candidate = f"{base}/{new_quote}"
            if candidate in available_pairs:
                pair_idx = available_pairs.index(candidate)
        
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
    with st.form(key=f"edit_bot_form_{bot_id}"):
        
        col1, col2 = st.columns(2)
        with col1:
            new_name = st.text_input("Bot Name", value=name)
            new_direction = st.selectbox("Direction", ["LONG", "SHORT"], index=["LONG", "SHORT"].index(direction))
            config_dict['leverage'] = st.number_input("Leverage", min_value=1, max_value=100, value=int(config_dict.get('leverage', 1)))

        with col2:
            stTypes = ["Martingale", "MagicHour", "MarketMaker"]
            curr_strat = strategy_type if strategy_type in stTypes else "Martingale"
            new_strat = st.selectbox("Strategy Type", stTypes, index=stTypes.index(curr_strat))
            new_max_steps = st.number_input("Max Steps", min_value=1, max_value=50, value=int(config_dict.get('max_steps', 10)))

        st.markdown("#### ⚙️ Order Calculation")
        col5, col6 = st.columns(2)
        # Safe Min Calculation
        min_safe_usd = 5.0
        if new_pair:
            try:
                # Use cached instance
                exchange = get_exchange_instance(new_market_type)
                # Check min cost for pair
                market = exchange.get_market_structure(new_pair)
                if market:
                    # Limits
                    min_cost = market.get('cost_min', 5.0)
                    min_safe_usd = max(5.0, min_cost * 1.1) # 10% buffer
            except:
                pass

        # [FEATURE PARITY] Use Min Size Checkbox
        use_min = config_dict.get('use_min_size', False)
        
        with col5:
            new_base = st.number_input(
                f"Order Size ($USDC) [Safe Min: ${min_safe_usd:.2f}]", 
                min_value=0.0,  # Allow 0 to let user type, but validate below
                step=1.0, 
                value=max(float(base_size), min_safe_usd),
                help=f"Minimum calculated based on exchange limits + rounding. Lower values will be rejected by Binance."
            )
            new_use_min = st.checkbox("Auto-Size (Min Qty)", value=use_min, key=f"edit_use_min_{bot_id}")
            config_dict['use_min_size'] = new_use_min
            
            if new_base < min_safe_usd and not new_use_min:
                 st.caption(f"⚠️ Low Size (Min: ${min_safe_usd:.2f})")

        with col6:
            new_mm = st.number_input("Martingale Multiplier", value=float(martingale_multiplier), step=0.1)
            
        st.markdown("#### 🎯 Take Profit & Trailing")
        tp_col1, tp_col2, tp_col3, tp_col4 = st.columns(4)
        with tp_col1:
            tp_types_ui = ["Fixed", "Percentage"]
            db_tp_type = config_dict.get('TakeProfitType', 'Fixed')
            curr_tp = "Percentage" if db_tp_type == "Percent" else "Fixed"
            
            selected_tp_ui = st.selectbox("Take Profit Type", tp_types_ui, index=tp_types_ui.index(curr_tp), key=f"edit_tp_type_{bot_id}")
            # Map back to DB expected string format
            config_dict['TakeProfitType'] = "Percent" if selected_tp_ui == "Percentage" else "Fixed"
            
        with tp_col2:
            # Load from correct base/pct field depending on current selection
            if selected_tp_ui == "Percentage":
                initial_val = float(config_dict.get('TakeProfitPct', 1.5))
                step_val = 0.1
            else:
                initial_val = float(config_dict.get('TakeProfitBase', 15.0))
                step_val = 1.0
                
            new_tp_val = st.number_input("TP Target Value", value=initial_val, step=step_val, key=f"edit_tp_val_{bot_id}")
            
            # Save into correct field
            if selected_tp_ui == "Percentage":
                config_dict['TakeProfitPct'] = new_tp_val
            else:
                config_dict['TakeProfitBase'] = new_tp_val
            
        with tp_col3:
            config_dict['ProfitSet'] = st.number_input("Trailing Distance (%)", 
                value=float(config_dict.get('ProfitSet', 0.5)), 
                step=0.1, 
                help="Distance from peak price to trigger trailing stop.")
                
        with tp_col4:
            st.markdown("<br>", unsafe_allow_html=True)
            config_dict['MaximizeProfit'] = st.checkbox("Enable Trailing", 
                value=bool(config_dict.get('MaximizeProfit', False)), 
                help="If checked, uses Trailing Stop. If unchecked, uses fixed Take Profit.")
        
        # [Grid Settings]
        st.markdown("#### 📐 Grid & Martingale Settings")
        with st.expander("Grid Spacing & Safety", expanded=True):
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
                
                # ATR Mode logic (dynamic vs locked)
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

                # Martingale Spacing
                curr_mult = float(config_dict.get('GridMultiplier', 1.0))
                is_exp_enabled = abs(curr_mult - 1.0) > 0.001
                use_grid_mult = st.checkbox("Enable Exponential Spacing", value=is_exp_enabled)
                
                if use_grid_mult:
                     val_to_show = curr_mult if is_exp_enabled else 1.1
                     config_dict['GridMultiplier'] = st.number_input("Spacing Multiplier", value=val_to_show, step=0.05, min_value=0.1)
                else:
                     config_dict['GridMultiplier'] = 1.0

            st.divider()
            st.markdown("##### 🎯 Advanced Step-Based Rules")
            grid_rules = config_dict.get('GridStepRules', [])
            if grid_rules:
                st.info(f"✅ Active Rules: {len(grid_rules)}")
            else:
                 st.caption("No custom step rules defined.")

        # Math Projection in Editor
        try:
            # Fetch current price for projection
            # Use cached fetch
            ohlcv = fetch_ohlcv_cached(new_market_type, new_pair if new_pair else "BTC/USDT", '1m')
            if ohlcv and len(ohlcv) > 0:
                curr_p = float(ohlcv[-1][4]) 
            else:
                curr_p = 40000.0
            
            # Merge params for strategy calculation
            combined_params = config_dict.copy()
            combined_params.update({
                'base_size': new_base, 
                'martingale_multiplier': new_mm, 
                'direction': new_direction, 
                'max_steps': new_max_steps
            })
            
            # Calculate Logic (Simplified for View)
            temp_strat = MartingaleStrategy(params=combined_params)
            
            # ATR Context
            atr_timeframe = config_dict.get('ATR_Timeframe', '1h')
            p_atr = curr_p * 0.01 # Default fallback
            try:
                 ohlcv_atr = fetch_ohlcv_cached(new_market_type, new_pair if new_pair else "BTC/USDT", atr_timeframe)
                 if ohlcv_atr:
                    df_atr = pd.DataFrame(ohlcv_atr, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    tr1 = df_atr['high'] - df_atr['low']
                    tr2 = (df_atr['high'] - df_atr['close'].shift()).abs()
                    tr3 = (df_atr['low'] - df_atr['close'].shift()).abs()
                    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                    p_atr = float(true_range.iloc[-14:].mean())
            except:
                 pass

            projections = temp_strat.calculate_projections(base_price=curr_p, current_atr=p_atr)
            
            with st.expander("🔍 Editor Risk Projection & Math Summary", expanded=False):
                st.caption("ℹ️ Projections update after saving changes.")
                st.caption(f"Simulated levels starting at: **{curr_p:,.2f}** (ATR: {p_atr:.2f})")
                
                proj_df = pd.DataFrame(projections)
                if not proj_df.empty:
                    proj_df.columns = [
                        "Step", "Grid Price", "Order ($)", "Total Inv. ($)", 
                        "Avg Price", "TP Price", "Is Hedge", "Hedge Size"
                    ]
                    dt_view = proj_df[[
                        "Step", "Grid Price", "Avg Price", "TP Price", 
                        "Order ($)", "Total Inv. ($)", "Is Hedge", "Hedge Size"
                    ]]
                    st.table(dt_view)
                        
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
                config_dict['mode_cci'] = st.selectbox("CCI Switch", [0, 1, 2], index=int(config_dict.get('mode_cci', 0)), format_func=lambda x: {0: "OFF", 1: "Above", 2: "Below"}[x],
                    help="Commodity Channel Index. Mode 1 (Above) = bullish momentum; Mode 2 (Below) = oversold/pullback entry.")
                config_dict['cci_level'] = st.number_input("CCI Level", value=float(config_dict.get('cci_level', 100)),
                    help="Typically oscillates between -200 and +200. Oversold entry: Below -100. Overbought: Above +100.")
                config_dict['cci_tf'] = st.selectbox("CCI TF", ["1m","5m","15m","1h","4h","1d"], index=["1m","5m","15m","1h","4h","1d"].index(config_dict.get('cci_tf', "15m")))
            with t_col2:
                config_dict['mode_boll'] = st.selectbox("Boll Switch", [0, 1, 2], index=int(config_dict.get('mode_boll', 0)), format_func=lambda x: {0: "OFF", 1: "Outside Lower", 2: "Outside Upper"}[x])
                config_dict['boll_tf'] = st.selectbox("Boll TF", ["1m","5m","15m","1h","4h","1d"], index=["1m","5m","15m","1h","4h","1d"].index(config_dict.get('boll_tf', "15m")))
            with t_col3:
                config_dict['mode_stoch'] = st.selectbox("Stoch Switch", [0, 1, 2], index=int(config_dict.get('mode_stoch', 0)), format_func=lambda x: {0: "OFF", 1: "Oversold", 2: "Overbought"}[x],
                    help="Stochastic Oscillator. Range: 0–100. Oversold = below 20; Overbought = above 80.")
                config_dict['stoch_tf'] = st.selectbox("Stoch TF", ["1m","5m","15m","1h","4h","1d"], index=["1m","5m","15m","1h","4h","1d"].index(config_dict.get('stoch_tf', "15m")))
            with t_col4:
                config_dict['mode_rsi'] = st.selectbox("RSI Switch", [0, 1, 2], index=int(config_dict.get('mode_rsi', 0)), format_func=lambda x: {0: "OFF", 1: "Below", 2: "Above"}[x],
                    help="Relative Strength Index. Range: 0–100. Mode 1 (Below) = oversold entry. Typical level: 30. Mode 2 (Above) = overbought entry. Typical level: 70.")
                config_dict['rsi_level'] = st.number_input("RSI Level", value=float(config_dict.get('rsi_level', 30)), min_value=0.0, max_value=100.0,
                    help="RSI range is 0–100. Classic thresholds: 30 = oversold (LONG entry), 70 = overbought (SHORT entry).")
                config_dict['rsi_tf'] = st.selectbox("RSI TF", ["1m","5m","15m","1h","4h","1d"], index=["1m","5m","15m","1h","4h","1d"].index(config_dict.get('rsi_tf', "15m")))

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
                config_dict['atrp_tf'] = pa2.selectbox("ATR TF", ["1m","5m","15m","1h","4h","1d"], index=3, key=f"atrp_tf_select_{bot_id}")


            st.markdown("#### Trigger 11: ATR Expansion (Current Move vs Range)")
            e_col1, e_col2, e_col3 = st.columns(3)
            with e_col1:
                config_dict['mode_atre'] = st.selectbox("Expansion Move", [0, 1, 2], index=int(config_dict.get('mode_atre', 0)), format_func=lambda x: {0: "OFF", 1: "Move Up >= X%", 2: "Move Down >= X%"}[x], help="Move from open as % of ATR.")
            with e_col2:
                config_dict['atre_level'] = st.number_input("Target % of ATR", value=float(config_dict.get('atre_level', 100.0)))
            with e_col3:
                config_dict['atre_tf'] = st.selectbox("TF to Watch (T11)", ["1m","5m","15m","1h","4h","1d"], index=3)

            st.markdown("#### Pattern Slots (Consecutive Triggers)")
            # 🚀 FIXED: Flattened the nested columns to give the SelectBoxes more horizontal breathing room
            for p_idx in range(1, 4):
                st.caption(f"**Trigger Pattern {p_idx}**")
                c_p1, c_p2, c_p3, c_p4 = st.columns([1.5, 1.5, 1, 1])
                config_dict[f'pat_{p_idx}_mode'] = c_p1.selectbox(f"Type ##{p_idx}", [0, 1, 2], index=int(config_dict.get(f'pat_{p_idx}_mode', 0)), format_func=lambda x: {0: "OFF", 1: "Consecutive Up", 2: "Consecutive Down"}[x])
                config_dict[f'pat_{p_idx}_source'] = c_p2.selectbox(f"Source ##{p_idx}", ["Price", "RSI", "CCI"], index=["Price", "RSI", "CCI"].index(config_dict.get(f'pat_{p_idx}_source', "Price")))
                config_dict[f'pat_{p_idx}_tf'] = c_p3.selectbox(f"TF ##{p_idx}", ["1m","5m","15m","1h","4h","1d"], index=["1m","5m","15m","1h","4h","1d"].index(config_dict.get(f'pat_{p_idx}_tf', "5m")))
                config_dict[f'pat_{p_idx}_count'] = c_p4.number_input(f"Count ##{p_idx}", min_value=1, value=int(config_dict.get(f'pat_{p_idx}_count', 3)))

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
        col_ee1, col_ee2, col_ee3, col_ee4, col_ee5 = st.columns(5)
        with col_ee1:
            use_ee = st.checkbox("Use Early Exit", value=config_dict.get('UseEarlyExit', False), help="Moves TP target closer to Break Even over time to exit stale trades safely.")
        with col_ee2:
            ee_start_hours = st.number_input("Start After (Hours)", value=float(config_dict.get('EEStartHours', 2.0)), min_value=0.0, step=0.5,
                help="Hours after entry before decay begins.")
        with col_ee3:
            decay_interval = st.number_input("Decay Every (Mins)", value=float(config_dict.get('DecayIntervalMins', 15.0)), help="How often (in minutes) the profit target is reduced.")
        with col_ee4:
            decay_pct = st.number_input("TP Reduction (%)", value=float(config_dict.get('DecayPercentPerInterval', 30.0)), help="What percentage of the current profit target to cut per interval.")
        with col_ee5:
            ee_allow_loss = st.checkbox("Allow Loss Exit", value=bool(config_dict.get('EEAllowLoss', False)),
                help="Allow TP to decay past break-even to exit at a small loss.")

        st.markdown("#### Post-Exit Re-entry & Cooldown")
        re1, re2, re3 = st.columns(3)
        with re1:
            reentry_mins = st.number_input("Cooldown (Mins)", value=float(config_dict.get('reentry_cooldown_mins', 0.0)))
        with re2:
            reentry_dist = st.number_input("Re-entry Dist (%)", value=float(config_dict.get('reentry_distance_pct', 0.0)))
        with re3:
            post_stop = st.checkbox("Stop After Cycle", value=bool(config_dict.get('post_exit_stop', False)))

        # EE
        config_dict['UseEarlyExit'] = use_ee
        config_dict['EEStartHours'] = ee_start_hours
        config_dict['DecayIntervalMins'] = decay_interval
        config_dict['DecayPercentPerInterval'] = decay_pct
        config_dict['EEAllowLoss'] = ee_allow_loss
        # Hedge step now in separate row below
        ee_hedge_row = st.columns(3)
        with ee_hedge_row[0]:
            hedge_step = st.number_input("Hedge Step", min_value=1, max_value=10, value=int(config_dict.get('HedgeStartStep', 7)), help="Which Martingale step triggers the hedge trade.")
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
                'EEStartHours': float(config_dict.get('EEStartHours', 2.0)),
                'DecayIntervalMins': float(config_dict.get('DecayIntervalMins', 15.0)),
                'DecayPercentPerInterval': float(config_dict.get('DecayPercentPerInterval', 30.0)),
                'EEAllowLoss': bool(config_dict.get('EEAllowLoss', False)),
                
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
