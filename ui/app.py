# Streamlit Main App Entry Point
# Updated for force reload
import streamlit as st

import os
import sys
import subprocess
from dotenv import load_dotenv

# Add root to sys.path to ensure module resolution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import init_db
from ui.views.monitor import render_monitor_view
from ui.views.bot_creator import render_bot_creator_view
from ui.views.bot_manager import render_bot_manager_view

# Load environment variables
load_dotenv()

# Page configuration
st.set_page_config(
    page_title="Crypto Quant Bot",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Initialize Database
init_db()

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
    
    if st.button("Apply Settings", use_container_width=True):
        st.success("Settings saved locally for this session.")

    st.divider()
    st.header("🚀 Engine Control")
    
    PID_FILE = "engine.pid"
    
    def is_engine_running():
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, "r") as f:
                    pid = int(f.read().strip())
                # Check existance
                os.kill(pid, 0)
                return True, pid
            except Exception:
                return False, None
        return False, None

    running, pid = is_engine_running()
    
    if running:
        st.success(f"Running (PID: {pid})")
        if st.button("Stop Engine", type="primary"):
            try:
                # Force kill on Windows using taskkill
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
                if os.path.exists(PID_FILE): os.remove(PID_FILE)
                st.rerun()
            except Exception as e:
                st.error(f"Stop failed: {e}")
    else:
        st.warning("Engine Stopped")
        if st.button("Start Engine"):
            try:
                # Launch independent process with NEW_CONSOLE to avoid killing Streamlit on stop
                CREATE_NEW_CONSOLE = 0x00000010
                process = subprocess.Popen([sys.executable, "engine/runner.py"], creationflags=CREATE_NEW_CONSOLE)
                with open(PID_FILE, "w") as f:
                    f.write(str(process.pid))
                st.rerun()
            except Exception as e:
                st.error(f"Start failed: {e}")

# Main Area - Tabs
st.title("🤖 Multi-Bot Crypto Trading System")

tab1, tab2, tab3 = st.tabs(["📊 Live Monitor", "🛠️ Bot Creator", "⚙️ Bot Manager"])

with tab1:
    render_monitor_view()

with tab2:
    render_bot_creator_view()

with tab3:
    render_bot_manager_view()
