# 🤖 Professional Multi-Bot Crypto Trading System (v0.4.1)

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

### ⚡ v0.4.1 Fixes & Improvements
- **One Bot Per Pair**: BLOCKED - Cannot deploy multiple bots on same pair/direction. This prevents order conflicts, position mismatches, and reduce-only issues.
- **ATR Timeframe Fix**: 3d and 5d ATR now calculated using √n scaling (more accurate)
- **P/L Sync**: Exchange positions fetched early for unified view with DB state
- **Streamlit API**: Fixed deprecated `column_global_config` → `column_config`
- **Performance**: Analysis documents sequential bot processing bottleneck

## 🔄 Multi-Bot Support (v0.4.1)

**Multiple bots can now trade the same pair/direction safely!**

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

## 🛠️ Technical Stack
- **Engine**: Python / CCXT (Robust Runner with Circuit Breakers)
- **Frontend**: Streamlit (Rich Dark Aesthetic, Isolated Keys)
- **Database**: SQLite (State-aware, Sync-capable)
- **Analytics**: Pandas, Plotly, ATR-based Volatility Analysis
- **Testing**: Playwright (UI & sync verification)

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

## 📚 Documentation

- **[CHANGELOG.md](CHANGELOG.md)** - Detailed list of changes
- **[PERFORMANCE_ANALYSIS.md](PERFORMANCE_ANALYSIS.md)** - Architecture analysis, P/L sync debugging
- **[tests/test_pl_sync.py](tests/test_pl_sync.py)** - Playwright tests for UI & sync verification

## 🐛 Troubleshooting

### P/L Shows But No Exchange Position
This is a **state mismatch** between DB and Exchange. Run:
```bash
python -m engine.runner  # Restart triggers sync
# OR
python cleanup_ghost_trades.py
```

### ATR Values Look Wrong
3d and 5d ATR are now calculated using √n scaling:
- 3d ATR = 1d ATR × 1.732
- 5d ATR = 1d ATR × 2.236

### Slow Bot Processing
The runner processes bots sequentially. See PERFORMANCE_ANALYSIS.md for parallel processing solutions.

---
*v0.4.1 - Sync & ATR Fixes Release*
