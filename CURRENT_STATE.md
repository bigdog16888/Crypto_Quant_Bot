# Crypto Quant Bot - Project State & Handover

**Date:** January 12, 2026
**Current Version:** v0.5 (Functional Alpha)

## 🚀 How to Run
1.  Double-click `run_bot.bat` in the project folder.
2.  Access UI at `http://localhost:8501`.

---

## ✅ Recent Completed Features
1.  **Bot Creator & Configuration**
    *   **Take Profit Modes**: Added support for both **Dollar Target ($)** and **Percentage (%)**.
    *   **Trading Pairs**: Searchable dropdown supporting `USDT` and `USDC` pairs.
    *   **Hedge Logic**: Fixed visibility in Risk Projection (shows "Is Hedge" correctly).
    *   **Grid Logic**: Toggle between **ATR Dynamic Grid** (with Factor) and **Fixed Price Step**.

2.  **Bot Manager (Editing)**
    *   **Full Parity**: Now supports editing ALL settings of active bots without deleting them.
    *   **Live Updates**: Can switch TP mode or Grid logic on the fly.
    *   **Calculators**: Embedded Risk Projection in the Editor to verify math before saving.

3.  **Core Engine**
    *   **Universal Paths**: Fixed `engine.pid` / `engine.log` paths to work in any directory (Home/Work).
    *   **Stability**: Fixed crashes related to `UnboundLocalError`, missing imports (`time`), and logic duplication.
    *   **Emergency Controls**: "Stop Monitoring" and "Force Kill" buttons are fully functional.

---

## 📂 Key File Structure
*   `ui/views/bot_creator.py`: Main form for deploying new bots. **Fixed** initialization scope issues.
*   `ui/views/bot_manager.py`: Interface for editing/deleting active bots. **Upgraded** to include TP/Grid editing.
*   `engine/runner.py`: Main loop. Handles `process_bot`, signals, and order execution.
*   `engine/strategies/mql4_strategy.py`: Logic core. Calculates Grid steps, TP prices, and Indicators (MQL4 port).
*   `engine/manager.py`: Trade lifecycle (TP hit detection, Grid step trigger, Hedge trigger).
*   `config/settings.py`: Centralized configuration (Paths, API Keys).

---

## 📝 Todo / Next Steps
*   **Manual Testing**: User is currently verifying if signals trigger correctly in live/testnet mode.
*   **Verify Live Trades**: Check if `USDC` orders execute properly on Binance Futures/Spot.
*   **UI Polish**: Ensure the "Portfolio" view in Live Monitor correctly sums up PnL for multiple bots.

## 💡 Notes for Resume
*   If restarting context, load this file first to understand where the codebase stands.
*   The "Red Screen" errors in Streamlit have been resolved by strictly initializing variables (`config = {}`) before use.
