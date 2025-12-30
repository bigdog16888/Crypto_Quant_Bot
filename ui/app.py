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

    engine_running, pid = is_engine_running()
    
    if not engine_running:
        if st.button("🚀 Start Monitoring", use_container_width=True):
            # Start engine logic...
            CREATE_NEW_CONSOLE = 0x00000010 # For Windows to run detached
            process = subprocess.Popen([sys.executable, "engine/runner.py"], creationflags=CREATE_NEW_CONSOLE)
            with open("engine.pid", "w") as f:
                f.write(str(process.pid))
            st.success("Monitoring service started.")
            st.rerun()
    else:
        st.success(f"Monitoring Running (PID: {pid})")
        if st.button("🛑 Stop Monitoring", use_container_width=True):
            with open("engine.stop", "w") as f:
                f.write("stop")
            st.warning("Stop signal sent. Waiting for graceful shutdown...")
            st.rerun()
        
        if st.button("🔥 Force Kill Monitoring", use_container_width=True, type="secondary"):
            # Attempt to kill by PID first
            try:
                os.kill(pid, 9) # SIGKILL
            except OSError:
                pass # Process might already be dead
            
            # Fallback for Windows or if PID kill fails
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            else:
                subprocess.run(["kill", "-9", str(pid)], capture_output=True)

            if os.path.exists("engine.pid"): os.remove("engine.pid")
            if os.path.exists("engine.stop"): os.remove("engine.stop")
            st.error("Engine force-killed.")
            st.rerun()

    st.divider()
    
    # Emergency Close All
    if 'show_emergency_confirm' not in st.session_state:
        st.session_state['show_emergency_confirm'] = False

    if st.button("🚨 EMERGENCY: CLOSE ALL", use_container_width=True, type="primary"):
        st.session_state['show_emergency_confirm'] = True

    if st.session_state.get('show_emergency_confirm'):
        st.error("!!! WARNING !!! This will MARKET CLOSE all positions and CANCEL all orders immediately.")
        st.warning("Type **'liquidate'** below to enable the button.")
        conf_input = st.text_input("Confirmation", placeholder="liquidate", key="sidebar_liquidation_conf")
        
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            confirm_disabled = (conf_input.lower().strip() != "liquidate")
            if st.button("✅ YES, CLOSE EVERYTHING", use_container_width=True, disabled=confirm_disabled):
                # Trigger Emergency Signal
                with open("engine.emergency", "w") as f:
                    f.write("emergency")
                st.session_state['show_emergency_confirm'] = False
                st.error("🚨 Emergency Liquidation Signal Sent! 🚨")
                st.rerun()
        with col_c2:
            if st.button("❌ CANCEL", use_container_width=True):
                st.session_state['show_emergency_confirm'] = False
                st.rerun()

# Main Area - Tabs
st.title("🤖 Multi-Bot Crypto Trading System")

tab1, tab2, tab3 = st.tabs(["📊 Live Monitor", "🛠️ Bot Creator", "⚙️ Bot Manager"])

with tab1:
    render_monitor_view()

with tab2:
    render_bot_creator_view()

with tab3:
    render_bot_manager_view()
