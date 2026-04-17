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
from ui.views.analytics import render_analytics_view

# Load environment variables
load_dotenv()
st.set_page_config(
    page_title="Crypto Quant Bot",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Initialize Database (Cached to prevent re-init issues)
@st.cache_resource
def initialize_database():
    init_db()

initialize_database()

# Custom Styling (Professional Aesthetics)
st.markdown("""
    <style>
    /* --- GLOBAL THEME (LIGHT PROFESSIONAL) --- */
    :root {
        --bg-color: #f6f8fa;
        --card-bg: #ffffff;
        --border-color: #d0d7de;
        --text-primary: #1f2328;
        --text-secondary: #656d76;
        --accent-color: #0969da;
        --success-color: #1a7f37;
        --success-text: #1a7f37;
        --danger-color: #cf222e;
        --danger-text: #cf222e;
        --warning-color: #9a6700;
        --shadow-sm: 0 1px 3px rgba(31, 35, 40, 0.12);
        --shadow-md: 0 3px 6px rgba(140, 149, 159, 0.15);
    }
    
    /* Main App Background */
    .stApp {
        background-color: var(--bg-color);
        color: var(--text-primary);
    }
    
    /* Force opaque background for main container to prevent ghosting */
    .main .block-container {
        background-color: var(--bg-color) !important;
        padding-top: 2rem;
    }
    
    /* Ensure widgets have solid backgrounds */
    .stSelectbox, .stTextInput, .stNumberInput, .stButton, .stTabs {
        background-color: var(--card-bg) !important;
    }
    
    .stApp > header {
        background-color: var(--bg-color) !important;
    }

    [data-testid="stSidebar"] {
        background-color: #f6f8fa;
        border-right: 1px solid var(--border-color);
    }
    
    [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {
         color: var(--text-primary) !important;
    }
    
    /* Headings */
    h1, h2, h3, h4, h5, h6 {
        color: var(--text-primary) !important;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
        font-weight: 600;
    }
    
    /* --- TABS --- */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        background-color: transparent;
        padding-bottom: 0px;
        border-bottom: 1px solid var(--border-color);
    }
    
    .stTabs [data-baseweb="tab"] {
        height: 45px;
        background-color: transparent;
        border-radius: 6px 6px 0px 0px;
        color: var(--text-secondary);
        border: none;
        padding: 0 20px;
        font-weight: 500;
    }
    
    .stTabs [aria-selected="true"] {
        background-color: var(--card-bg);
        color: var(--accent-color);
        border: 1px solid var(--border-color);
        border-bottom: 1px solid var(--card-bg);
        position: relative;
        top: 1px;
    }

    /* --- DATAFRAMES & TABLES --- */
    [data-testid="stDataFrame"] {
        background-color: var(--card-bg);
        border: 1px solid var(--border-color);
        border-radius: 6px;
        padding: 5px;
        box-shadow: var(--shadow-sm);
    }

    [data-testid="stTable"] {
        background-color: var(--card-bg);
        color: var(--text-primary);
    }
    
    /* Force text color in tables */
    div[data-testid="stDataFrame"] div[role="columnheader"] {
        color: var(--text-primary) !important;
        background-color: #f6f8fa !important;
        font-weight: 600;
        border-bottom: 1px solid var(--border-color);
    }
    
    div[data-testid="stDataFrame"] div[role="gridcell"] {
        color: var(--text-primary) !important;
        background-color: var(--card-bg) !important;
    }

    /* --- METRICS --- */
    div[data-testid="stMetricValue"] {
        font-size: 1.8rem;
        color: var(--text-primary) !important;
        font-weight: 600;
    }
    
    div[data-testid="stMetricLabel"] {
        color: var(--text-secondary);
        font-size: 0.9rem;
    }
    
    [data-testid="stMetricDelta"] {
        background-color: rgba(0,0,0,0.03);
        padding: 2px 6px;
        border-radius: 4px;
    }

    /* --- INPUTS --- */
    .stTextInput input, .stSelectbox div[data-baseweb="select"], .stNumberInput input {
        background-color: #ffffff;
        color: var(--text-primary);
        border-color: var(--border-color);
        border-radius: 6px;
    }
    
    .stTextInput input:focus, .stSelectbox div[data-baseweb="select"]:focus-within {
        border-color: var(--accent-color);
        box-shadow: 0 0 0 2px rgba(9, 105, 218, 0.2);
    }
    
    /* Buttons */
    .stButton>button {
        border-radius: 6px;
        font-weight: 600;
        background-color: #f6f8fa;
        color: var(--text-primary);
        border: 1px solid var(--border-color);
        transition: all 0.2s cubic-bezier(0.3, 0, 0.5, 1);
        box-shadow: var(--shadow-sm);
    }
    
    .stButton>button:hover {
        background-color: #f3f4f6;
        border-color: #8c959f;
        box-shadow: var(--shadow-md);
    }
    
    .stButton>button[kind="primary"] {
        background-color: #1f883d;
        color: white;
        border: 1px solid rgba(27, 31, 36, 0.15);
    }
    
    .stButton>button[kind="primary"]:hover {
        background-color: #1a7f37;
    }
    
    .stButton>button[kind="secondary"] {
        color: var(--danger-color);
        border-color: var(--border-color);
    }

    /* --- CUSTOM CLASSES --- */
    
    /* Monitor View: Metric Cards */
    .metric-card {
        background-color: var(--card-bg);
        border: 1px solid var(--border-color);
        border-radius: 8px;
        padding: 24px 20px;
        text-align: center;
        box-shadow: var(--shadow-sm);
        height: 100%;
        display: flex;
        flex-direction: column;
        justify-content: center;
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    
    .metric-card:hover {
        transform: translateY(-2px);
        box-shadow: var(--shadow-md);
    }
    
    .metric-value { 
        font-size: 1.8em; 
        font-weight: 700; 
        color: var(--text-primary);
        margin-top: 8px;
        letter-spacing: -0.5px;
    }
    
    .metric-label { 
        font-size: 0.85em; 
        color: var(--text-secondary); 
        text-transform: uppercase; 
        letter-spacing: 0.5px; 
        font-weight: 600;
    }
    
    .status-ok { color: var(--success-text); font-weight: bold; }
    .status-warn { color: var(--warning-color); font-weight: bold; }
    .status-err { color: var(--danger-text); font-weight: bold; }

    /* Monitor View: Status Ribbon */
    .status-ribbon {
        background-color: #ffffff;
        border-left: 4px solid var(--accent-color);
        padding: 15px 25px;
        margin-bottom: 25px;
        border-radius: 6px;
        font-family: 'SF Mono', 'Segoe UI Mono', 'Roboto Mono', monospace;
        font-size: 0.9rem;
        display: flex;
        justify-content: space-between;
        align-items: center;
        color: var(--text-primary);
        border: 1px solid var(--border-color);
        border-left-width: 4px;
        box-shadow: var(--shadow-sm);
    }
    
    .sync-status {
        font-size: 0.75rem;
        padding: 2px 8px;
        border-radius: 4px;
        margin-left: 10px;
        font-weight: bold;
    }
    
    .sync-ok { background-color: #dafbe1; color: #1a7f37; border: 1px solid rgba(26, 127, 55, 0.2); }
    .sync-warn { background-color: #fff8c5; color: #9a6700; border: 1px solid rgba(154, 103, 0, 0.2); }
    .sync-err { background-color: #ffebe9; color: #cf222e; border: 1px solid rgba(207, 34, 46, 0.2); }
    
    /* Bot Creator: Strategy Cards */
    .strat-card {
        border: 1px solid var(--border-color);
        border-radius: 8px;
        padding: 25px;
        background-color: var(--card-bg);
        height: 100%;
        text-align: center;
        transition: all 0.2s ease;
        box-shadow: var(--shadow-sm);
        cursor: pointer;
    }
    
    .strat-card:hover {
        border-color: var(--accent-color);
        box-shadow: 0 4px 12px rgba(9, 105, 218, 0.15);
        transform: translateY(-2px);
    }
    
    .strat-icon { font-size: 3em; margin-bottom: 15px; }
    .strat-title { font-weight: 700; font-size: 1.25em; color: var(--text-primary); margin-bottom: 8px; }
    .strat-desc { font-size: 0.95em; color: var(--text-secondary); line-height: 1.5; }

    /* Dividers */
    hr {
        margin: 2em 0;
        border: 0;
        border-top: 1px solid var(--border-color);
    }
    
    /* Expander */
    .streamlit-expanderHeader {
        background-color: #ffffff;
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
                # ⚠️ SAFETY GUARD: Only write plain ASCII strings.
                # This prevents MagicMock objects (from tests) or corrupted
                # values from ever reaching the .env file on disk.
                _key_str = str(api_key).strip()
                _sec_str = str(api_secret).strip()
                _is_safe = (
                    len(_key_str) >= 10
                    and len(_sec_str) >= 10
                    and _key_str.isprintable()
                    and _sec_str.isprintable()
                    and "<" not in _key_str  # reject MagicMock repr
                    and "<" not in _sec_str
                )
                if not _is_safe:
                    st.error("❌ Invalid key format — not saved. Check your credentials.")
                else:
                    # 1. Update .env file on disk
                    set_key(dotenv_path, "BINANCE_API_KEY", _key_str)
                    set_key(dotenv_path, "BINANCE_API_SECRET", _sec_str)

                    # 2. Update current process environment
                    os.environ["BINANCE_API_KEY"] = _key_str
                    os.environ["BINANCE_API_SECRET"] = _sec_str

                    # 3. Update global config object immediately (for UI components)
                    config.API_KEY = _key_str
                    config.API_SECRET = _sec_str

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
        """Check if engine is running via SocketLock port OR PID file."""
        # Method 1: Check SocketLock port (authoritative)
        import socket
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.settimeout(0.5)
            test_sock.bind(("127.0.0.1", 19888))
            test_sock.close()
            # Port is FREE → engine is NOT running
        except OSError:
            # Port is BOUND → engine IS running
            # Try to get PID from file for display
            pid = None
            if os.path.exists(PID_FILE):
                try:
                    with open(PID_FILE, "r") as f:
                        pid = int(f.read().strip())
                except Exception:
                    pass
            return True, pid
        
        # Method 2: Fallback to PID file
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, "r") as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)
                return True, pid
            except Exception:
                # Stale PID file — clean it up
                os.remove(PID_FILE)
                return False, None
        return False, None

    engine_running, pid = is_engine_running()
    
    if not engine_running:
        if st.button("▶️ Start Monitoring"):
            # Start engine logic...
            runner_path = os.path.join(ROOT_DIR, "engine", "runner.py")
            
            # Redirect stdout/stderr to DEVNULL. runner.py natively uses its own RotatingFileHandler.
            # Passing a file handle here locks the file on Windows and causes RotatingFileHandler to crash.
            process = subprocess.Popen([sys.executable, runner_path], 
                                       stdout=subprocess.DEVNULL, 
                                       stderr=subprocess.DEVNULL, 
                                       creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
                                       close_fds=True)

            
            with open(PID_FILE, "w") as f:
                f.write(str(process.pid))
            st.success("Monitoring service started. Refreshing page...")
            time.sleep(2) # Give it a moment
            st.rerun()
    else:
        st.success(f"Monitoring Running (PID: {pid})")
        if st.button("🛑 Stop Monitoring"):
            try:
                with open(STOP_FILE, "w") as f:
                    f.write("stop")
                st.info("Stop signal sent. Waiting for graceful shutdown...")

                # More robust waiting logic
                with st.spinner("Waiting for engine to terminate..."):
                    shutdown_success = False
                    for i in range(30):  # Wait up to 30 seconds
                        is_running, _ = is_engine_running()
                        if not is_running:
                            shutdown_success = True
                            break
                        time.sleep(1)
                
                if shutdown_success:
                    st.success("✅ Engine stopped gracefully!")
                    if os.path.exists(PID_FILE): # Cleanup just in case
                        os.remove(PID_FILE)
                else:
                    st.error("Engine did not stop in time. Consider Force Kill.")

                time.sleep(1)
                st.rerun()

            except Exception as e:
                st.error(f"Failed to send stop signal: {e}")
        
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

# Main Area - Navigation
st.title("🤖 Multi-Bot Crypto Trading System")

# ========== TESTNET/SAFETY WARNING BANNER ==========
if config.TESTNET:
    st.warning("⚠️ **TESTNET MODE ACTIVE** - Trading on Binance Futures Testnet. No real funds at risk.")
elif config.DRY_RUN:
    st.info("🧪 **DRY RUN MODE** - Orders are simulated, not sent to exchange.")
else:
    st.error("🔴 **LIVE TRADING MODE** - Real funds at risk! Be careful.")
# ===================================================

# Navigation (Sidebar) to isolate page execution
with st.sidebar:
    st.markdown("---")
    st.subheader("Navigation")
    
    # Handle auto-navigation requests from other views
    nav_index = 0  # Default: Live Monitor
    pages = ["📊 Live Monitor", "🏗️ Bot Creator", "🛠️ Bot Manager", "📈 Analytics"]
    
    if '_nav_to_monitor' in st.session_state and st.session_state['_nav_to_monitor']:
        nav_index = 0
        del st.session_state['_nav_to_monitor']
    elif '_nav_to_manager' in st.session_state and st.session_state['_nav_to_manager']:
        nav_index = 2
        del st.session_state['_nav_to_manager']
    
    selected_page = st.radio(
        "Go to", 
        pages, 
        index=nav_index,
        label_visibility="collapsed"
    )
    
# Render ONLY the selected page
# Use st.empty to ensure the container is wiped clean between renders to prevent ghosting
main_placeholder = st.empty()

with main_placeholder.container():
    if selected_page == "📊 Live Monitor":
        # Clear Bot Manager specific state when leaving
        if 'editing_bot_id' in st.session_state:
            del st.session_state['editing_bot_id']
            
        render_monitor_view()

    elif selected_page == "🏗️ Bot Creator":
        if 'editing_bot_id' in st.session_state:
            del st.session_state['editing_bot_id']
            
        render_bot_creator_view()

    elif selected_page == "🛠️ Bot Manager":
        render_bot_manager_view()

    elif selected_page == "📈 Analytics":
        render_analytics_view()
