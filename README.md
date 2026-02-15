# 🤖 Crypto Quant Bot (v1.2.0)

A professional-grade, multi-bot algorithmic trading system designed for **Binance Futures (USDT/USDC)**. It features a robust **Virtual Position Manager** that allows multiple bots to trade the same pair independently (e.g., Hedging Long/Short) without conflict.

## 🌟 Key Features

*   **Virtual Position System:** Each bot tracks its own position logic (`trades` table) while the engine reconciles with the exchange.
*   **Hybrid Raw Mode:** Bypasses CCXT limitations on Binance Demo/Testnet by using raw signed requests for critical private data.
*   **Auto-Healing:** The system detects orphans and mismatched states, automatically correcting them.
*   **Real-Time UI:** Streamlit dashboard with **Auto-Refresh**, Live Charts, Parallel Data Fetching, and Portfolio Heatmaps.
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

See `HANDOFF.md` for a deep dive into the system architecture and detailed debugging steps.

### Common Fixes
*   **System Slow?** Enable "Auto-Refresh" in the UI only when needed. The Dashboard now uses **parallel fetching** for speed.
*   **Orders not showing?** The UI prioritizes the **Database** for speed. Click "Force Sync" if you suspect a mismatch.
*   **API Errors?** If using Demo/Testnet, ensure `TESTNET=True` in `.env`. The system uses specific overrides for the `demo-fapi` endpoints to prevent `-2008` and `-2015` errors.

---

## ⚠️ Disclaimer
This software is for educational purposes. Cryptocurrency trading involves high risk. The authors are not responsible for any financial losses incurred while using this bot.
