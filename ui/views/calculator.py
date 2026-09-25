import streamlit as st
import sys
import os

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine.database import get_connection, get_all_bots
from engine.exchange_interface import ExchangeInterface
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.parity_gates import get_exchange_signed_net, qty_tolerance
from engine.health import get_system_health
import pandas as pd
import json
import logging

logger = logging.getLogger(__name__)

@st.cache_data(ttl=15, show_spinner=False)
def fetch_live_equity():
    """Fetch live wallet equity from system health (uses info['assets'] parser for futures)."""
    try:
        ex = ExchangeInterface(market_type='future')
        # Use the same DB path as the engine
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'crypto_bot.db')
        health = get_system_health(db_path, ex, lambda x: x, lambda: 0.002, force_refresh=True)
        header = health.get('header_metrics', {})
        # Prefer total_equity, fallback to futures_balance
        equity = header.get('total_equity') or header.get('futures_balance') or 0.0
        return float(equity)
    except Exception as e:
        logger.warning(f"Could not fetch live equity: {e}")
        return 0.0


@st.cache_data(ttl=30, show_spinner=False)
def fetch_live_positions():
    """Fetch live positions from exchange."""
    try:
        ex = ExchangeInterface(market_type='future')
        pos = ex.fetch_positions()
        return [p for p in pos if abs(float(p.get('contracts') or 0)) > 1e-8]
    except Exception as e:
        logger.warning(f"Could not fetch live positions: {e}")
        return []


def render_calculator_view():
    st.header("🧮 Portfolio Sizing & Risk Calculator")
    st.caption("Interactive risk budgeting, volatility sizing, and Martingale ladder projection using live engine math.")
    
    st.divider()
    
    # --- SECTION 1: Account Risk Budgeting ---
    st.subheader("💰 1. Account Risk Budgeting")
    
    col_bal1, col_bal2, col_bal3 = st.columns(3)
    
    with col_bal1:
        # Live balance from exchange (via system health info['assets'] parser)
        live_equity = fetch_live_equity()
        st.metric("Live Equity (Exchange)", f"${live_equity:,.2f}")
    
    with col_bal2:
        # User can override for planning
        plan_balance = st.number_input(
            "Planning Equity ($)", 
            min_value=0.0, 
            value=live_equity if live_equity > 0 else 10000.0,
            step=100.0,
            help="Override for what-if scenarios"
        )
    
    with col_bal3:
        risk_pct = st.number_input(
            "Max Risk per Pair (%)", 
            min_value=0.1, 
            max_value=50.0, 
            value=5.0,
            step=0.5,
            help="Maximum % of equity to allocate to any single pair"
        )
    
    max_risk_per_pair = plan_balance * (risk_pct / 100.0)
    st.info(f"💡 **Max Capital per Pair:** ${max_risk_per_pair:,.2f} ({risk_pct}% of ${plan_balance:,.2f})")
    
    # Active pairs from DB
    bots = get_all_bots()
    active_pairs = set()
    for bot in bots:
        if bot[3] and bot[9] != 'DECOMMISSIONED':  # is_active and not decommissioned
            active_pairs.add(bot[2])
    
    if active_pairs:
        st.markdown("**Active Pairs & Suggested Allocation:**")
        alloc_cols = st.columns(min(len(active_pairs), 4))
        for i, pair in enumerate(sorted(active_pairs)):
            with alloc_cols[i % 4]:
                st.metric(pair, f"${max_risk_per_pair:,.2f}", f"≤ {risk_pct}%")
    
    st.divider()
    
    # --- SECTION 2: ATR Volatility Sizer ---
    st.subheader("📊 2. ATR Volatility Sizer")
    st.caption("Uses `MartingaleStrategy._apply_volatility_sizing`: scales base size by baseline_ATR(100) / current_ATR(14), clamped 0.2–5.0")
    
    col_vol1, col_vol2, col_vol3 = st.columns(3)
    
    with col_vol1:
        base_size = st.number_input("Base Size ($)", min_value=1.0, value=150.0, step=10.0)
        baseline_atr = st.number_input("Baseline ATR (100-period)", min_value=0.01, value=100.0, step=1.0)
    
    with col_vol2:
        current_atr = st.number_input("Current ATR (14-period)", min_value=0.01, value=80.0, step=1.0)
        atr_period = st.number_input("ATR Period", min_value=3, max_value=240, value=14)
    
    with col_vol3:
        # Live fetch button
        if st.button("🔄 Fetch Live ATR for Pair"):
            pair_input = st.session_state.get('calc_pair', 'BTC/USDC:USDC')
            try:
                ex = ExchangeInterface(market_type='future')
                ohlcv = ex.fetch_ohlcv(pair_input, timeframe='1h', limit=200)
                if ohlcv:
                    df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
                    tr1 = df['h'] - df['l']
                    tr2 = (df['h'] - df['c'].shift()).abs()
                    tr3 = (df['l'] - df['c'].shift()).abs()
                    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                    atr_100 = tr.iloc[-100:].mean()
                    atr_14 = tr.iloc[-14:].mean()
                    st.session_state['live_atr_100'] = float(atr_100)
                    st.session_state['live_atr_14'] = float(atr_14)
                    st.success(f"ATR100: {atr_100:.2f}, ATR14: {atr_14:.2f}")
            except Exception as e:
                st.error(f"Fetch failed: {e}")
        
        # Use live values if available
        if 'live_atr_100' in st.session_state:
            st.caption(f"Live: ATR100={st.session_state['live_atr_100']:.2f}, ATR14={st.session_state['live_atr_14']:.2f}")
            if st.button("Use Live Values"):
                baseline_atr = st.session_state['live_atr_100']
                current_atr = st.session_state['live_atr_14']
                st.rerun()
    
    # Calculate vol multiplier
    if baseline_atr > 0 and current_atr > 0:
        vol_mult = baseline_atr / current_atr
        vol_mult_clamped = max(0.2, min(vol_mult, 5.0))
        adjusted_size = base_size * vol_mult_clamped
        
        col_res1, col_res2, col_res3 = st.columns(3)
        with col_res1:
            st.metric("Raw Vol Multiplier", f"{vol_mult:.3f}x")
        with col_res2:
            st.metric("Clamped Multiplier", f"{vol_mult_clamped:.3f}x", 
                     "HIGH VOL = SMALLER SIZE" if vol_mult_clamped < 1 else "LOW VOL = LARGER SIZE")
        with col_res3:
            st.metric("Adjusted Size", f"${adjusted_size:,.2f}")
        
        # Interpretation
        if vol_mult_clamped == 0.2:
            st.warning("⚠️ **Floor hit** — volatility extremely high, size capped at 20% of base")
        elif vol_mult_clamped == 5.0:
            st.warning("⚠️ **Ceiling hit** — volatility extremely low, size capped at 500% of base")
        elif vol_mult_clamped < 1.0:
            st.info(f"📉 Volatility elevated ({vol_mult_clamped:.1%} of baseline) — reducing size")
        elif vol_mult_clamped > 1.0:
            st.info(f"📈 Volatility compressed ({vol_mult_clamped:.1%} of baseline) — increasing size")
        else:
            st.info("➡️ Volatility at baseline — using base size")
    
    st.divider()
    
    # --- SECTION 3: Martingale Ladder & Liquidation Frontier ---
    st.subheader("📈 3. Martingale Ladder & Liquidation Frontier")
    st.caption("Uses `MartingaleStrategy.calculate_projections`: step-by-step position sizes, total capital, avg entry, TP price, and hedge trigger")
    
    col_lad1, col_lad2, col_lad3, col_lad4 = st.columns(4)
    
    with col_lad1:
        ladder_base = st.number_input("Base Order ($)", min_value=1.0, value=150.0, step=10.0, key="ladder_base")
        ladder_mult = st.number_input("Martingale Multiplier", min_value=1.0, max_value=10.0, value=2.0, step=0.1)
    
    with col_lad2:
        max_steps = st.number_input("Max Steps", min_value=1, max_value=20, value=10)
        direction = st.selectbox("Direction", ["LONG", "SHORT"])
    
    with col_lad3:
        entry_price = st.number_input("Entry Price", min_value=0.01, value=50000.0, step=100.0)
        tp_pct = st.number_input("TP Target (%)", min_value=0.01, max_value=50.0, value=1.0, step=0.1)
    
    with col_lad4:
        use_atr_grid = st.checkbox("Use ATR Grid", value=True)
        if use_atr_grid:
            atr_val = st.number_input("Current ATR", min_value=0.01, value=100.0, step=1.0)
            atr_factor = st.number_input("ATR Grid Factor", min_value=0.1, max_value=10.0, value=1.0, step=0.1)
        else:
            base_grid = st.number_input("Fixed Grid Step ($)", min_value=1.0, value=100.0, step=10.0)
            atr_val = 0
            atr_factor = 0
        
        grid_mult = st.number_input("Grid Multiplier", min_value=1.0, max_value=5.0, value=1.0, step=0.1)
        use_hedge = st.checkbox("Enable Hedge", value=False)
        if use_hedge:
            hedge_step = st.number_input("Hedge Start Step", min_value=1, max_value=20, value=7)
    
    # Build params for projection
    proj_params = {
        'base_size': ladder_base,
        'martingale_multiplier': ladder_mult,
        'max_steps': max_steps,
        'direction': direction,
        'tp_pct': tp_pct,
        'UseATRGrid': use_atr_grid,
        'ATRGridFactor': atr_factor if use_atr_grid else 1.0,
        'base_grid': base_grid if not use_atr_grid else 100.0,
        'GridMultiplier': grid_mult,
        'UseHedge': use_hedge,
        'HedgeStartStep': hedge_step if use_hedge else 7,
    }
    
    try:
        strat = MartingaleStrategy(params=proj_params)
        projections = strat.calculate_projections(base_price=entry_price, current_atr=atr_val)
        
        if projections:
            # Summary metrics
            last = projections[-1]
            total_invested = last['total_invested']
            total_qty = sum(p['order_size_usdc'] / p['price'] for p in projections if p['price'] > 0)
            avg_price = last['avg_price']
            
            # Liquidation estimate (for LONG: price where margin = 0; for SHORT: price where margin = 0)
            # Simplified: maintenance margin ~0.5% for isolated, 0.4% for cross
            # Liquidation price ≈ avg_entry * (1 - 1/leverage + maintenance_margin)
            leverage = 20  # Default
            maint_margin = 0.004  # 0.4% cross
            
            if direction == "LONG":
                liq_price = avg_price * (1 - 1/leverage + maint_margin)
                liq_buffer_pct = (entry_price - liq_price) / entry_price * 100
            else:
                liq_price = avg_price * (1 + 1/leverage - maint_margin)
                liq_buffer_pct = (liq_price - entry_price) / entry_price * 100
            
            col_sum1, col_sum2, col_sum3, col_sum4 = st.columns(4)
            with col_sum1:
                st.metric("Total Capital Required", f"${total_invested:,.2f}")
            with col_sum2:
                st.metric("Avg Entry Price", f"{avg_price:,.2f}")
            with col_sum3:
                st.metric(f"Est. Liq Price (x{leverage})", f"{liq_price:,.2f}")
            with col_sum4:
                st.metric("Buffer to Liq", f"{liq_buffer_pct:.2f}%")
            
            if liq_buffer_pct < 5:
                st.error(f"🚨 **DANGER**: Only {liq_buffer_pct:.1f}% buffer to liquidation!")
            elif liq_buffer_pct < 15:
                st.warning(f"⚠️ **CAUTION**: {liq_buffer_pct:.1f}% buffer — consider reducing max steps")
            else:
                st.success(f"✅ **SAFE**: {liq_buffer_pct:.1f}% buffer to liquidation")
            
            # Capital vs Risk Budget
            if total_invested > max_risk_per_pair:
                st.error(f"🚨 **OVER BUDGET**: ${total_invested:,.2f} > ${max_risk_per_pair:,.2f} (max per pair)")
            else:
                st.success(f"✅ Within risk budget: ${total_invested:,.2f} ≤ ${max_risk_per_pair:,.2f}")
            
            # Projection Table
            st.markdown("**Step-by-Step Projection:**")
            proj_df = pd.DataFrame(projections)
            proj_df.columns = [
                "Step", "Grid Price", "Order Size ($)", "Total Invested ($)", 
                "Avg Price", "TP Price", "Is Hedge", "Hedge Size ($)"
            ]
            # Format for display
            display_df = proj_df[["Step", "Grid Price", "Order Size ($)", "Total Invested ($)", 
                                  "Avg Price", "TP Price", "Is Hedge"]].copy()
            display_df["Grid Price"] = display_df["Grid Price"].apply(lambda x: f"{x:,.2f}")
            display_df["Order Size ($)"] = display_df["Order Size ($)"].apply(lambda x: f"{x:,.2f}")
            display_df["Total Invested ($)"] = display_df["Total Invested ($)"].apply(lambda x: f"{x:,.2f}")
            display_df["Avg Price"] = display_df["Avg Price"].apply(lambda x: f"{x:,.2f}")
            display_df["TP Price"] = display_df["TP Price"].apply(lambda x: f"{x:,.2f}")
            display_df["Is Hedge"] = display_df["Is Hedge"].apply(lambda x: "🛡️ HEDGE" if x else "")
            
            st.dataframe(display_df, width='stretch', hide_index=True)
            
            # Visual: Capital curve
            st.markdown("**Capital Accumulation Curve:**")
            chart_df = pd.DataFrame({
                'Step': [p['step'] for p in projections],
                'Total Invested': [p['total_invested'] for p in projections]
            })
            st.line_chart(chart_df.set_index('Step'))
            
    except Exception as e:
        st.error(f"Projection failed: {e}")
        logger.error(f"Calculator projection error: {e}")
    
    st.divider()
    
    # --- SECTION 4: Live Pair Health Quick Check ---
    st.subheader("🔍 4. Live Pair Health Quick Check")
    st.caption("Real-time tier-1 parity check for active pairs using engine's `get_exchange_signed_net`")
    
    if active_pairs:
        health_data = []
        ex = ExchangeInterface(market_type='future')
        for pair in sorted(active_pairs):
            try:
                physical = get_exchange_signed_net(ex, pair)
                
                # Sum DB open_qty for this pair
                conn = get_connection()
                db_net = conn.execute("""
                    SELECT SUM(CASE WHEN direction='LONG' THEN COALESCE(open_qty,0) ELSE -COALESCE(open_qty,0) END)
                    FROM trades t
                    JOIN bots b ON t.bot_id = b.id
                    WHERE b.pair = ? AND b.is_active = 1 AND b.status != 'DECOMMISSIONED'
                """, (pair,)).fetchone()[0] or 0
                
                tol = qty_tolerance()
                gap = physical - db_net
                status = "🟢 OK" if abs(gap) <= tol else "🔴 MISMATCH"
                
                health_data.append({
                    "Pair": pair,
                    "DB Net": f"{db_net:+.6f}",
                    "Exchange": f"{physical:+.6f}",
                    "Gap": f"{gap:+.6f}",
                    "Status": status
                })
            except Exception as e:
                health_data.append({
                    "Pair": pair,
                    "DB Net": "Error",
                    "Exchange": "Error",
                    "Gap": "Error",
                    "Status": f"❌ {str(e)[:30]}"
                })
        
        if health_data:
            st.dataframe(pd.DataFrame(health_data), width='stretch', hide_index=True)
    else:
        st.info("No active bots found.")
    
    st.divider()
    
    # --- SECTION 5: Quick Kelly Criterion ---
    st.subheader("🎯 5. Kelly Criterion (Reference)")
    st.caption("Full Kelly: f* = (p × b - q) / b where p=win_rate, q=1-p, b=avg_win/avg_loss. **Never bet full Kelly** — use 1/4 to 1/2 Kelly.")
    
    col_k1, col_k2, col_k3 = st.columns(3)
    with col_k1:
        win_rate = st.number_input("Win Rate (%)", min_value=1.0, max_value=99.0, value=55.0, step=1.0) / 100
        avg_win = st.number_input("Avg Win ($)", min_value=0.01, value=100.0, step=10.0)
    with col_k2:
        avg_loss = st.number_input("Avg Loss ($)", min_value=0.01, value=80.0, step=10.0)
        kelly_fraction = st.selectbox("Kelly Fraction", [0.25, 0.33, 0.5, 1.0], index=1, format_func=lambda x: f"{x:.0%} Kelly")
    with col_k3:
        if avg_loss > 0 and 0 < win_rate < 1:
            b = avg_win / avg_loss
            p = win_rate
            q = 1 - p
            full_kelly = (p * b - q) / b
            optimal_f = full_kelly * kelly_fraction
            st.metric("Full Kelly f*", f"{full_kelly:.2%}")
            st.metric(f"Optimal f ({kelly_fraction:.0%} Kelly)", f"{optimal_f:.2%}")
            st.caption(f"b (win/loss ratio) = {b:.2f}")
            if optimal_f > 0.2:
                st.warning("⚠️ High fraction — verify win/loss estimates")
            elif optimal_f < 0:
                st.error("🚨 Negative Kelly — strategy has negative expectancy")
        else:
            st.metric("Full Kelly f*", "N/A")
            st.metric("Optimal f", "N/A")


# Entry point for app.py
if __name__ == "__main__":
    render_calculator_view()