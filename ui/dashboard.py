import streamlit as st
import pandas as pd
import time
import os
import sys

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config import config
from engine.exchange_interface import ExchangeInterface

st.set_page_config(page_title="Crypto Quant Bot Dashboard", layout="wide")

st.title("🚀 Crypto Quant Bot Dashboard")

st.sidebar.header("Bot Configuration")
st.sidebar.write(f"**Exchange:** Binance")
st.sidebar.write(f"**Dry Run:** {config.DRY_RUN}")
st.sidebar.write(f"**Allowed Symbols:** {', '.join(config.ALLOWED_SYMBOLS)}")

# Status Overview
col1, col2, col3 = st.columns(3)
col1.metric("Connection", "Connected" if config.API_KEY else "Missing API Keys", delta="Live" if not config.DRY_RUN else "Simulated")
col2.metric("Max Order Safety", f"${config.MAX_ORDER_USD}")
col3.metric("Running Mode", "Dry Run" if config.DRY_RUN else "Live")

st.subheader("Market Monitor")
selected_symbol = st.selectbox("Select Symbol", config.ALLOWED_SYMBOLS)

if st.button("Refresh Data") or 'last_refresh' not in st.session_state:
    st.session_state.last_refresh = time.time()
    # Mock data or real if keys exist
    # Here we would initialize ExchangeInterface and fetch data
    st.info(f"Selected {selected_symbol}. Data fetching logic would go here.")

st.divider()
st.subheader("Recent Logs")
# In a real app, read from logs/bot.log
st.text_area("Live Logs", value="Bot initialized...\nWaiting for signal...", height=200)
