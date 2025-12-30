# 🤖 Professional Multi-Bot Crypto Trading System

A professional-grade quantitative trading platform built for extreme precision and robust risk management.

## 🚀 Key Features

### 🎯 8-Trigger Entry Confluence
- **Multi-Switch Logic**: Combine up to 8 triggers (4 indicators + 4 pattern slots). A trade only opens if **ALL** enabled switches align.
- **Granular Indicators**: CCI, RSI, Bollinger Bands, and Stochastic with explicit "Above/Below Level" modes.
- **Consecutive Pattern Slots**: 4 independent slots to detect candle patterns (e.g., "Wait for 3 consecutive red candles") across any timeframe.

### 🛡️ Advanced Risk Management
- **Automated Hedge Executor**: Automatically opens hedge positions to lock net exposure when the Martingale grid depth or drawdown limits are reached.
- **Realistic Projections**: Real-time risk calculator including **0.15% Fee & Slippage simulation** for accurate capital assessment.
- **Accelerated Early Exit**: Rescue logic that reduces the profit target by 30% every 15 minutes to exit stale trades at Break Even.

### 📊 Professional UI & Monitoring
- **Interactive Plotly Charts**: Live visualization of Entry, TP, and Next Order levels.
- **Isolated UI Keys**: 100% stable Streamlit interface using `create_` and `edit_` key isolation to prevent widget clashing.
- **Emergency Killswitch**: Double-confirmation "Liquidate" command for immediate market closure of all positions.

## 🛠️ Technical Stack
- **Frontend**: Streamlit (Rich Dark Aesthetic)
- **Engine**: Python / CCXT (Binance Architecture)
- **Analytics**: Pandas, Pandas-TA, Plotly
- **Database**: SQLite (State-aware trade management)

## 🚦 Getting Started
1. Configure your Binance API in the sidebar.
2. Use the **Bot Creator** to build an 8-trigger confluence bot.
3. Review the **Risk Projection** before deploying.
4. Monitor live performance in the **Live Monitor** tab.

---
*Developed for High-Precision Quantitative Trading.*
