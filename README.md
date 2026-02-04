# 🤖 Professional Multi-Bot Crypto Trading System

**Version 0.9.0** - Advanced Analytics & Risk Management Release

A professional-grade quantitative trading platform built for extreme precision, robust risk management, and live market resilience.

## 🚀 Key Features

### 📊 Phase 10: Advanced Features (New in v0.9.0)

#### Strategy Enhancements
- **Multi-Timeframe Trend Analysis**: Confirm trend across multiple timeframes before entry
- **Volatility-Based Position Sizing**: Automatically adjust lot sizes based on ATR (lower size in high volatility)
- **Correlation Filtering**: Avoid correlated pairs to reduce portfolio risk
- **Enhanced Indicators**: RSI, EMA, ATR, CCI, Correlation analysis

#### Risk Management
- **Daily Loss Limits**: Automatically pause trading if daily loss threshold is exceeded
- **Drawdown Protection**: Partial position closing when unrealized loss exceeds configured percentage
- **Portfolio Heatmap**: Visual risk distribution across all active positions
- **Real-time Risk Metrics**: Win Rate, Profit Factor, Expectancy calculations

#### Analytics Dashboard
- **Performance Metrics**: Comprehensive view of Win Rate, Profit Factor, Total PnL
- **Equity Curve**: Visual representation of account growth over time
- **Trade History Export**: Download complete trade journal as CSV for external analysis
- **Per-Bot Performance**: Breakdown of profitability by individual bot

### 🛡️ Institutional-Grade Safety
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
- **4-Page Navigation**: Live Monitor, Bot Creator, Bot Manager, Analytics

## 🔄 Multi-Bot Support

**Multiple bots can trade the same pair/direction safely!**

Each bot tracks its own exchange order IDs:
```
Exchange shows: 1 TP order @ 94000
DB tracks:      Bot A → Order ID 12345
                Bot B → Order ID 67890

When Bot A cancels "its" TP → Only Order ID 12345 is cancelled
Bot B's order (67890) is UNTOUCHED ✓
```

**What Gets Tracked:**
- Entry order IDs
- TP order IDs  
- Grid order IDs

**Display Shows:**
- Which bot owns each order
- Bot vs Manual order breakdown
- Total positions with per-bot composition

## 🎛️ Default Configuration

| Parameter | Default | Notes |
|-----------|---------|-------|
| Leverage | 20x | Futures only, spot is 1x |
| Martingale Multiplier | 1.8 | Safety factor per step |
| Take Profit | 1.5% | Percentage mode |
| ATR Grid Factor | 1.1 | Dynamic grid spacing |
| ATR Timeframe | 1h | Base for grid calculation |
| Max Drawdown | 0% (disabled) | Trigger partial close |
| Daily Loss Limit | 0% (disabled) | Pause trading for the day |

## 🛠️ Technical Stack
- **Engine**: Python / CCXT (Robust Runner with Circuit Breakers)
- **Frontend**: Streamlit (Professional Light Theme, Isolated Keys)
- **Database**: SQLite (State-aware, Sync-capable)
- **Analytics**: Pandas, Plotly, Prometheus Metrics
- **Indicators**: Custom TA library with ATR-based Volatility Analysis
- **WebSocket**: Real-time updates on port 8765

## 🚦 Getting Started

### 1. Installation
```bash
# Clone the repository
git clone https://github.com/yourusername/Crypto_Quant_Bot.git
cd Crypto_Quant_Bot

# Install dependencies
pip install -r requirements.txt
```

### 2. Configuration
Create a `.env` file based on `.env.example`:
```ini
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
DRY_RUN=True  # Set to False for Live Trading
TESTNET=False # Set to True for Binance Testnet
GLOBAL_STOP_LOSS_PCT=50.0
```

### 3. Run the Platform
```bash
# Start the UI
streamlit run ui/app.py

# The engine will auto-start from the UI sidebar
# Or manually: python -m engine.runner
```

### 4. Workflow
1. **Configure API**: Enter your Binance API credentials in the sidebar (or use `.env`).
2. **Start Engine**: Click "▶️ Start Monitoring" in the sidebar.
3. **Create Bot**: Use the **Bot Creator** to build a strategy (e.g., "RSI Dip Buyer + Volatility Filter").
4. **Analyze Risk**: Check the **ATR Planning Foundation** and **Risk Projection** tables.
5. **Deploy**: Activate the bot. The **Runner** handles validation, safety, and execution.
6. **Monitor**: Watch the **Live Monitor** for active trades, positions, and logs.
7. **Analyze**: Review performance in the **Analytics** tab.

## 📁 Project Structure

```
Crypto_Quant_Bot/
├── config/              # Configuration files
│   ├── settings.py      # Global settings
│   └── strategies.json  # Strategy definitions
├── engine/              # Core trading engine
│   ├── runner.py        # Main bot executor
│   ├── bot_executor.py  # Individual bot logic
│   ├── exchange_interface.py  # CCXT wrapper
│   ├── database.py      # SQLite operations
│   ├── metrics.py       # Prometheus metrics & export
│   ├── indicators.py    # Technical indicators
│   └── strategies/      # Strategy implementations
├── ui/                  # Streamlit frontend
│   ├── app.py           # Main entry point
│   └── views/           # Page components
│       ├── monitor.py   # Live monitoring
│       ├── bot_creator.py  # Bot creation wizard
│       ├── bot_manager.py  # Bot editing/management
│       └── analytics.py    # Performance analytics
├── tests/               # Test suite
├── tools/               # Utility scripts
├── .env.example         # Environment template
├── requirements.txt     # Python dependencies
└── README.md            # This file
```

## 📚 Documentation

- **[CHANGELOG.md](CHANGELOG.md)** - Detailed version history
- **[SETUP_GUIDE.md](SETUP_GUIDE.md)** - Comprehensive setup instructions
- **[AI_INSTRUCTIONS.md](AI_INSTRUCTIONS.md)** - Development guidelines

## 🐛 Troubleshooting

### App Won't Start
```bash
# Check if port 8501 is already in use
netstat -ano | findstr :8501  # Windows
lsof -i :8501                 # Linux/Mac

# Kill existing process and restart
streamlit run ui/app.py
```

### P/L Shows But No Exchange Position
This is a **state mismatch** between DB and Exchange. Run:
```bash
python -m engine.runner  # Restart triggers sync
```

### ATR Values Look Wrong
3d and 5d ATR are calculated using √n scaling:
- 3d ATR = 1d ATR × 1.732
- 5d ATR = 1d ATR × 2.236

### Engine Not Responding
Check the engine logs:
```bash
# View recent logs
tail -n 100 engine.log  # Linux/Mac
Get-Content engine.log -Tail 100  # Windows PowerShell
```

## 🔒 Security Notes

- **Never commit `.env` file** - It contains your API keys
- **Use Testnet first** - Set `TESTNET=True` for testing
- **Start with DRY_RUN** - Set `DRY_RUN=True` to simulate orders
- **API Permissions**: Only enable "Futures Trading" and "Read" permissions (no withdrawals)

## 🎯 Roadmap

- [ ] Backtesting engine integration
- [ ] Telegram notifications
- [ ] Multi-exchange support (Bybit, OKX)
- [ ] Advanced order types (Trailing Stop, OCO)
- [ ] Machine learning signal integration

---

**v0.9.0** - Advanced Analytics & Risk Management Release  
*Built with precision. Deployed with confidence.*
