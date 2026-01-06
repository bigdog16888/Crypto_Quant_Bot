# 🤖 Professional Multi-Bot Crypto Trading System (v0.4)

A professional-grade quantitative trading platform built for extreme precision, robust risk management, and live market resilience.

## 🚀 Key Features

### 🛡️ Institutional-Grade Safety (New in v0.4)
- **Circuit Breaker**: Global "Kill Switch" monitors total account equity. If drawdown exceeds 50%, it locks the engine and prevents further losses.
- **Exchange Validation**: Pre-validates every order against live `MinNotional`, `MinQty`, and `StepSize` rules to prevent API bans.
- **State Recovery**: Auto-syncs database with exchange on startup. Detects "Ghost Trades" (TP hit while offline) and "Orphaned Orders".
- **Network Resilience**: Automatic retry logic with exponential backoff for unstable connections.

### 🎯 11-Trigger Entry Confluence
- **Multi-Switch Logic**: Combine up to 11 triggers (Indicators, Patterns, Volatility). A trade only opens if **ALL** enabled switches align.
- **Volatility Awareness**: "Market State" trigger filters entries based on historical volatility percentile (e.g., "Only trade when vol is > 80%").
- **Indicator-Aware Patterns**: Detect consecutive patterns not just on Price, but on RSI or CCI values.

### 📊 Professional UI & Risk Math
- **Automated Hedge Executor**: Locks net exposure via counter-orders when grid depth is reached.
- **Realistic Projections**: Real-time risk calculator including **0.15% Fee & Slippage** simulation.
- **Accelerated Early Exit**: Smart decay logic reduces profit targets over time to exit stale trades at Break Even.

## 🛠️ Technical Stack
- **Engine**: Python / CCXT (Robust Runner with Circuit Breakers)
- **Frontend**: Streamlit (Rich Dark Aesthetic, Isolated Keys)
- **Database**: SQLite (State-aware, Sync-capable)
- **Analytics**: Pandas, Plotly, ATR-based Volatility Analysis

## 🚦 Getting Started

### 1. Installation
```bash
pip install -r requirements.txt
```

### 2. Configuration
Create a `.env` file based on `.env.example`:
```ini
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
DRY_RUN=True  # Set to False for Live Trading
GLOBAL_STOP_LOSS_PCT=50.0
```

### 3. Run the Platform
```bash
streamlit run ui/app.py
```

### 4. Workflow
1.  **Configure API**: Ensure valid keys in `.env`.
2.  **Create Bot**: Use the **Bot Creator** to build a strategy (e.g., "RSI Dip Buyer + Volatility Filter").
3.  **Analyze Risk**: Check the **ATR Planning Foundation** and **Risk Projection** tables.
4.  **Deploy**: Launch the bot. The **Runner** handles validation, safety, and execution.
5.  **Monitor**: Watch the **Live Monitor** for active trades and logs.

---
*v0.4 "Live Readiness" Release - Built for Stability.*
