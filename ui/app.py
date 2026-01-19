# Streamlit Main App Entry Point
# Updated for force reload
import streamlit as st
import time
import os
import sys
import subprocess
from dotenv import load_dotenv, set_key, find_dotenv

# Add root to sys.path to ensure module resolution
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT_DIR)

from engine.database import init_db
from config.settings import config
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

# Custom Styling (Professional Aesthetics)
st.markdown("""
    <style>
    .main {
        background-color: #0d1117;
    }
    .stTabs [data-baseweb="tab-list"] {
        gap: 24px;
        background-color: #161b22;
        padding: 10px 20px 0px 20px;
        border-radius: 8px 8px 0px 0px;
    }
    .stTabs [data-baseweb="tab"] {
        height: 50px;
        white-space: pre-wrap;
        background-color: transparent;
        border-radius: 4px 4px 0px 0px;
        color: #8b949e;
        border: none;
    }
    .stTabs [aria-selected="true"] {
        background-color: #0d1117;
        color: #58a6ff;
        border-bottom: 2px solid #58a6ff;
    }
    div[data-testid="stMetricValue"] {
        font-size: 1.8rem;
        color: #f0f6fc;
    }
    div[data-testid="stMetricDelta"] {
        font-size: 0.9rem;
    }
    .stButton>button {
        border-radius: 6px;
    }
    </style>
    """, unsafe_allow_html=True)


# Sidebar - Global Settings
with st.sidebar:
    st.header("⚙️ Global Settings")
    st.divider()
    
    st.subheader("API Configuration")
    
    # Locate .env file robustly
    dotenv_path = find_dotenv()
    if not dotenv_path:
        dotenv_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
    
    # Reload env to ensure fresh read
    load_dotenv(dotenv_path, override=True)

    # Use config as primary, os.getenv as fallback
    current_key = config.API_KEY if config.API_KEY else os.getenv("BINANCE_API_KEY", "")
    current_secret = config.API_SECRET if config.API_SECRET else os.getenv("BINANCE_API_SECRET", "")

    api_key = st.text_input("Binance API Key", value=current_key, type="password")
    api_secret = st.text_input("Binance API Secret", value=current_secret, type="password")
    
    st.divider()
    
    if st.button("Apply Settings"):
        if api_key and api_secret:
            try:
                # 1. Update .env file on disk
                set_key(dotenv_path, "BINANCE_API_KEY", api_key)
                set_key(dotenv_path, "BINANCE_API_SECRET", api_secret)
                
                # 2. Update current process environment
                os.environ["BINANCE_API_KEY"] = api_key
                os.environ["BINANCE_API_SECRET"] = api_secret
                
                # 3. Update global config object immediately (for UI components)
                config.API_KEY = api_key
                config.API_SECRET = api_secret
                
                st.success("✅ Credentials Saved!")
                
                # 4. Check if engine is running and warn
                if os.path.exists(config.PATHS["PID_FILE"]):
                    st.warning("⚠️ Engine is running! Please RESTART Monitoring below to apply changes.")
                    
            except Exception as e:
                st.error(f"Failed to save settings: {e}")
        else:
            st.error("❌ Key and Secret required.")

    st.divider()
    st.header("🛠️ Engine Control")
    
    PID_FILE = config.PATHS["PID_FILE"]
    STOP_FILE = config.PATHS["STOP_FILE"]
    EMERGENCY_FILE = config.PATHS["EMERGENCY_FILE"]
    
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
        if st.button("▶️ Start Monitoring"):
            # Start engine logic...
            CREATE_NEW_CONSOLE = 0x00000010 # For Windows to run detached
            runner_path = os.path.join(ROOT_DIR, "engine", "runner.py")
            process = subprocess.Popen([sys.executable, runner_path], creationflags=CREATE_NEW_CONSOLE)
            with open(PID_FILE, "w") as f:
                f.write(str(process.pid))
            st.success("Monitoring service started. Refreshing page...")
            time.sleep(2) # Give it a moment
            st.rerun()
    else:
        st.success(f"Monitoring Running (PID: {pid})")
        if st.button("🛑 Stop Monitoring"):
            with open(STOP_FILE, "w") as f:
                f.write("stop")
            st.warning("Stop signal sent. Waiting for shutdown...")
            
            # Simple spin-wait for feedback
            for _ in range(10):
                if not os.path.exists(PID_FILE):
                    break
                time.sleep(1)
                
            st.success("Stopped!")
            st.rerun()
        
        if st.button("💀 Force Kill Monitoring", type="secondary"):
            if pid is None:
                st.error("Cannot kill: PID not found.")
                st.rerun()
                
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

            if os.path.exists(PID_FILE): os.remove(PID_FILE)
            if os.path.exists(STOP_FILE): os.remove(STOP_FILE)
            st.error("Engine force-killed.")
            st.rerun()

    st.divider()
    
    # Emergency Close All
    if 'show_emergency_confirm' not in st.session_state:
        st.session_state['show_emergency_confirm'] = False

    if st.button("🚨 EMERGENCY: CLOSE ALL", type="primary"):
        st.session_state['show_emergency_confirm'] = True

    if st.session_state.get('show_emergency_confirm'):
        st.error("!!! WARNING !!! This will MARKET CLOSE all positions and CANCEL all orders immediately.")
        st.warning("Type **'liquidate'** below to enable the button.")
        conf_input = st.text_input("Confirmation", placeholder="liquidate", key="sidebar_liquidation_conf")
        
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            confirm_disabled = (conf_input.lower().strip() != "liquidate")
            if st.button("🔥YES, CLOSE EVERYTHING", disabled=confirm_disabled):
                # Trigger Emergency Signal
                with open(EMERGENCY_FILE, "w") as f:
                    f.write("emergency")
                st.session_state['show_emergency_confirm'] = False
                st.error("🚨 Emergency Liquidation Signal Sent! 🚨")
                st.rerun()
        with col_c2:
            if st.button("🔙CANCEL"):
                st.session_state['show_emergency_confirm'] = False
                st.rerun()

# Main Area - Tabs
st.title("🤖 Multi-Bot Crypto Trading System")

# ========== TESTNET/SAFETY WARNING BANNER ==========
if config.TESTNET:
    st.warning("⚠️ **TESTNET MODE ACTIVE** - Trading on Binance Futures Testnet. No real funds at risk.")
elif config.DRY_RUN:
    st.info("🧪 **DRY RUN MODE** - Orders are simulated, not sent to exchange.")
else:
    st.error("🔴 **LIVE TRADING MODE** - Real funds at risk! Be careful.")
# ===================================================

tab1, tab2, tab3 = st.tabs(["📊 Live Monitor", "🏗️ Bot Creator", "🛠️ Bot Manager"])

with tab1:
    render_monitor_view()

with tab2:
    render_bot_creator_view()

with tab3:
    render_bot_manager_view()
