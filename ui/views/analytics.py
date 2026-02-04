
import streamlit as st
import pandas as pd
import plotly.express as px
from engine.database import get_connection
from engine.metrics import export_trade_history

def render_analytics_view():
    st.header("📈 Performance Analytics")
    
    # --- Data Fetching ---
    try:
        conn = get_connection()
        query = """
            SELECT 
                th.timestamp, 
                b.name as bot_name, 
                th.symbol, 
                th.action, 
                th.price, 
                th.amount, 
                th.pnl,
                th.bot_id
            FROM trade_history th
            LEFT JOIN bots b ON th.bot_id = b.id
            WHERE th.pnl != 0  -- Only count profit realizing trades (TP/Stop)
            ORDER BY th.timestamp ASC
        """
        df = pd.read_sql_query(query, conn)
        
        # Also fetch raw CSV for export (includes all actions)
        csv_data = export_trade_history(format='csv')
        
    except Exception as e:
        st.error(f"Error fetching data: {e}")
        return

    # --- Toolbar ---
    col1, col2 = st.columns([3, 1])
    with col2:
        if csv_data:
            st.download_button(
                label="📥 Download Trade History (CSV)",
                data=csv_data,
                file_name="trade_history.csv",
                mime="text/csv",
            )

    if df.empty:
        st.info("No closed trades with PnL found yet. Run some bots!")
        return

    # --- Preprocessing ---
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')
    
    # Ensure pnl is numeric (handle any string values from DB)
    df['pnl'] = pd.to_numeric(df['pnl'], errors='coerce').fillna(0.0)
    
    df['cumulative_pnl'] = df['pnl'].cumsum()
    
    # --- KPI Cards ---
    total_trades = len(df)
    total_pnl = df['pnl'].sum()
    wins = df[df['pnl'] > 0]
    losses = df[df['pnl'] <= 0]
    win_rate = (len(wins) / total_trades) * 100 if total_trades > 0 else 0.0
    
    gross_profit = wins['pnl'].sum()
    gross_loss = abs(losses['pnl'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    avg_win = wins['pnl'].mean() if not wins.empty else 0.0
    avg_loss = losses['pnl'].mean() if not losses.empty else 0.0
    
    kpi1, kpi2, kpi3, kpi4 = st.columns(4)
    kpi1.metric("Net Profit", f"${total_pnl:.2f}")
    kpi2.metric("Win Rate", f"{win_rate:.1f}%", f"{len(wins)}W / {len(losses)}L")
    kpi3.metric("Profit Factor", f"{profit_factor:.2f}")
    kpi4.metric("Total Trades", str(total_trades))

    # --- Charts ---
    st.subheader("Equity Curve (Realized PnL)")
    fig_equity = px.line(df, x='datetime', y='cumulative_pnl', markers=True, title='Account Growth')
    st.plotly_chart(fig_equity, width='stretch')
    
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        st.subheader("PnL Distribution")
        fig_hist = px.histogram(df, x="pnl", nbins=20, title="Profit/Loss Distribution")
        st.plotly_chart(fig_hist, width='stretch')
        
    with col_c2:
        st.subheader("Performance by Bot")
        bot_perf = df.groupby('bot_name')['pnl'].sum().reset_index()
        fig_bar = px.bar(bot_perf, x='bot_name', y='pnl', color='pnl', title="Net Profit by Bot")
        st.plotly_chart(fig_bar, width='stretch')

    # --- Detailed Table ---
    with st.expander("📄 Recent Trade Log"):
        st.dataframe(df[['datetime','bot_name','symbol','action','pnl']].sort_values('datetime', ascending=False), width='stretch')
