import streamlit as st
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Page configuration
st.set_page_config(
    page_title="Crypto Quant Bot",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom Styling (Rich Aesthetics)
st.markdown("""
    <style>
    .main {
        background-color: #0e1117;
    }
    .stTabs [data-baseweb="tab-list"] {
        gap: 24px;
    }
    .stTabs [data-baseweb="tab"] {
        height: 50px;
        white-space: pre-wrap;
        background-color: #161b22;
        border-radius: 4px 4px 0px 0px;
        gap: 1px;
        padding-top: 10px;
        padding-bottom: 10px;
        color: #c9d1d9;
    }
    .stTabs [aria-selected="true"] {
        background-color: #21262d;
        color: #58a6ff;
    }
    </style>
    """, unsafe_allow_html=True)

# Sidebar - Global Settings
with st.sidebar:
    st.header("⚙️ Global Settings")
    st.divider()
    
    st.subheader("API Configuration")
    api_key = st.text_input("Binance API Key", value=os.getenv("BINANCE_API_KEY", ""), type="password")
    api_secret = st.text_input("Binance API Secret", value=os.getenv("BINANCE_API_SECRET", ""), type="password")
    
    st.divider()
    
    st.subheader("Strategy Parameters")
    trading_pair = st.selectbox("Trading Pair", ["BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT"], index=0)
    timeframe = st.selectbox("Timeframe", ["1m", "5m", "15m", "1h", "4h", "1d"], index=2)
    
    st.divider()
    
    if st.button("Apply Settings", use_container_width=True):
        st.success("Settings saved locally for this session.")

# Main Area - Tabs
st.title("🤖 Multi-Bot Crypto Trading System")

tab1, tab2 = st.tabs(["📊 Live Monitor", "🛠️ Bot Creator"])

with tab1:
    st.header("Live Market Monitor")
    st.info("Visualizing live data from " + trading_pair + " on " + timeframe + " timeframe.")
    # Placeholder for future live charts
    st.empty()

with tab2:
    st.header("Bot Creator & Configuration")
    st.write("Configure and launch new trading bots here.")
    
    col1, col2 = st.columns(2)
    with col1:
        st.text_input("Bot Name", placeholder="e.g., Scalper_BTC_01")
        st.selectbox("Strategy Type", ["MACD Crossover", "RSI Mean Reversion", "Bollinger Band Breakout"])
    
    with col2:
        st.number_input("Initial Investment (USDT)", min_value=10.0, step=10.0, value=100.0)
        st.slider("Risk Tolerance (%)", 1, 10, 2)
        
    if st.button("Deploy Bot", type="primary"):
        st.warning("Bot deployment logic will be implemented in the next phase.")
