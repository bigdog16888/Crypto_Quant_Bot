# 🤖 Crypto Quant Bot (v4.1.4)

A professional-grade, multi-bot algorithmic trading system designed for **Binance Futures (USDT/USDC)**. It features a robust **Virtual Position Manager** that allows multiple bots to trade the same pair independently (e.g., Hedging Long/Short) without conflict.

## 🌟 Key Features

*   **Virtual Position System:** Each bot tracks its own position logic (`trades` table) while the engine reconciles with the exchange.
*   **Fully Autonomous Reconciliation (v4.1.4):** Achieved absolute ledger parity (0.00 drift). The engine relies on cryptographic proof-of-fill (Deterministic ID mapping) and handles complex One-Way mode netting and Hedge-aware ledger recovery.
*   **Ghost-Proof Order Management:** Advanced string-parsing logic for `clientOrderId` eliminates infinite cancel/recreate loops and stale order "ghosting."
*   **Drift-Aware Consensus:** Pair-consensus logic accounts for sibling virtual positions on the same pair, preventing false-positive drift alerts in One-Way accounts.
*   **Atomic State Integrity:** Consolidates snapshots into `BEGIN IMMEDIATE` transaction blocks with fail-safe recovery for TP and Grid placements.
*   **High-Precision Arithmetic & Ledger Mapping:** All calculations use cent-level ($0.01) precision guards. WebSocket order fills map correctly to `pnl` and `cost_usdc` database columns, eliminating numerical noise.
*   **🛡️ [DEDUP-GUARD] Pre-flight Check (v3.1):** Enforces a strict pre-flight state lock in `bot_executor.py` preventing duplicate orders from being placed during runner retry loops if an order is already executing.
*   **💥 Single-Click Close Orphan UI (v3.1):** Dashboard features a visual mismatch-action button allowing users to immediately resolve and flatten orphaned positions on the exchange with single-click precision.
*   **Async DB Write Queue (INV-31):** WebSocket fill events are dispatched to a non-blocking background SQLite write queue worker, keeping the CCXT listener lag-free and preventing DB-lock exceptions.
*   **Hedge Child Lifecycle Integrity (INV-29):** Implements strict lifecycle gates and recovery paths for child hedge orders (TP/SL/BE) to prevent orphaned orders in volatile markets.
*   **Pending Hedge Close & BE-Only States:** Protects virtual positions in volatile conditions using Break-Even only and pending-close states.
*   **Dynamic Fragment UI Refresh:** Dashboard utilizes independent `@st.fragment` zones for Header (30s) and Bot Grid (15s) with live data-sync timestamps — zero full-page flickering.
*   **SocketLock Singleton:** OS-enforced process protection (TCP port 19888) to prevent duplicate runners.
*   **Real-Time UI:** Streamlit dashboard with ghosting-loop fixes, **Auto-Refresh**, Live Charts, Parallel Data Fetching, and Portfolio Heatmaps.
*   **One-Way & Hedge Mode Support:** Fully compatible with Binance's One-Way and Hedge modes.

---

## 🚀 Quick Start

### 1. Prerequisites
*   Python 3.10+
*   Binance Futures Account (Testnet or Mainnet)
*   API Key & Secret

### 2. Installation
```bash
# Clone repository
git clone <repo_url>
cd Crypto_Quant_Bot

# Install dependencies
pip install -r requirements.txt
```

### 3. Configuration
Create a `.env` file in the root directory:
```ini
# --- EXCHANGE SETTINGS ---
# Set both to True for Demo Trading
# Set both to False for Mainnet (Real Money)
TESTNET=True
DEMO_TRADING=True

BINANCE_API_KEY=your_api_key
BINANCE_API_SECRET=your_api_secret

# --- SYSTEM SETTINGS ---
MARKET_TYPE=future
```

### 4. Running the Bot

**Option A: Combined Startup (Recommended)**
Start both the trading engine and dashboard with a single command:
```bash
run_stack.bat
```

**Option B: Separate Startup**
**Step 1: Start the Trading Engine**
This runs the backend logic (orders, websocket, risk management).
```bash
run_bot.bat
# OR
python engine/runner.py
```

**Step 2: Launch the Dashboard**
Open the web interface to monitor and control bots.
```bash
streamlit run ui/app.py
```

---

## 🖥️ UI Dashboard Guide

*   **📊 Live Monitor:**
    *   **Overview Tab:** Global PnL, Total Equity, and Asset Breakdown.
    *   **Live Charts Tab:** Real-time OHLCV charts for active pairs.
    *   **Orders & History Tab:** View **Open Orders** (instantly fetched from DB) and **Recent Activity Log**.
    *   **Auto-Refresh:** Toggle "⚡ Auto-Refresh" for 15s updates.
*   **🏗️ Bot Creator:** Visually configure and launch new strategies (Martingale, Grid, etc.).
*   **🛠️ Bot Manager:** Edit, Stop, or Delete existing bots.
*   **📈 Analytics:** View historical performance, win rates, and equity curves.

---

## 🔧 Architecture & Troubleshooting

See **[CODEBASE_GUIDE.md](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/CODEBASE_GUIDE.md)** for a deep dive into the system architecture, Pair-Consensus mathematical rules, and detailed debugging steps.

### Common Fixes
*   **System Slow?** Enable "Auto-Refresh" in the UI only when needed. The Dashboard now uses **parallel fetching** for speed.
*   **Orders not showing?** The UI prioritizes the **Database** for speed. Click "Force Sync" if you suspect a mismatch.
*   **API Errors?** If using Demo/Testnet, ensure `TESTNET=True` in `.env`. The system uses specific overrides for the `demo-fapi` endpoints to prevent `-2008` and `-2015` errors.

---

## ⚠️ Disclaimer
This software is for educational purposes. Cryptocurrency trading involves high risk. The authors are not responsible for any financial losses incurred while using this bot.
