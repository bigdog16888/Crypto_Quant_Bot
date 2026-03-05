# Crypto Quant Bot - Quick Start Guide

## Prerequisites

- **Python 3.10+**
- **Binance API Key** (Spot and/or Futures)
- **Bun** (recommended) or **pip/conda**

## Installation

```bash
# Clone the repository
git clone <your-repo-url>
cd Crypto_Quant_Bot

# Install dependencies (Bun recommended)
bun install

# OR with pip
pip install -r requirements.txt
```

## Configuration

### 1. Environment Variables

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` with your Binance credentials:

```env
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
MARKET_TYPE=spot
TESTNET=false
DRY_RUN=false
```

### 2. Testnet (Recommended for First-Time Users)

Set `TESTNET=true` in `.env` to use Binance Futures Testnet:

```env
TESTNET=true
```

This allows you to test strategies without risking real funds.

## Running the Bot

### Start the Streamlit UI

```bash
# Using Bun
bun run ui/app.py

# OR using Python
python -m streamlit run ui/app.py
```

The UI will open at `http://localhost:8501`

### Start the Engine

1. Open the UI
2. In the sidebar, go to **⚙️ Engine Control**
3. Click **▶️ Start Monitoring**

The engine runs in the background and executes trades based on your bot configurations.

## Creating Your First Bot

1. Navigate to **🏗️ Bot Creator**
2. Configure:
   - Market Type (Spot/Futures)
   - Trading Pair (e.g., BTC/USDT)
   - Strategy (Martingale/Market Maker/Magic Hour)
   - Base Order Size (minimum $5 recommended)
   - Martingale Multiplier (2.0 = double each level)
3. Review projections
4. Click **Deploy Bot**
5. Go to **📊 Live Monitor** to watch your bot

## Bot Strategies

### Martingale (Default)
- Doubles position size after each losing trade
- Best for ranging markets
- Risk: Can accumulate positions quickly

### Market Maker
- Places orders on both sides of the spread
- Profits from spread and rebates
- Requires tight spreads

### Magic Hour
- Time-based strategy
- Enters at specific hours with high volatility

## Risk Management

### Daily Loss Limit
- Bot stops trading when daily loss exceeds threshold
- Includes both realized AND unrealized losses
- Default: 10% of portfolio

### Circuit Breaker
- Automatically closes all positions
- Triggered by:
  - Daily loss limit reached
  - Emergency signal
  - API connection failure

## Testing

Run the test suite:

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_critical_fixes.py -v

# Run with coverage
python -m pytest tests/ --cov=engine --cov-report=html
```

## Troubleshooting

### "No pairs found"
- Check API keys are valid
- Ensure TESTNET setting matches your keys
- Restart the engine after changing credentials

### "Engine not running"
- Click **Start Monitoring** in sidebar
- Check logs in `engine_runner_debug.log`

### Orders not executing
- Check exchange connection status
- Verify API keys have trading permissions
- Check for insufficient balance

## Project Structure

```
Crypto_Quant_Bot/
├── engine/           # Core trading logic
│   ├── bot_executor.py    # Order execution
│   ├── risk_manager.py    # Risk controls
│   ├── strategies/       # Trading strategies
│   └── runner.py         # Engine runner
├── ui/               # Streamlit interface
│   ├── app.py             # Main UI
│   └── views/             # Page views
├── tests/            # Test suite
├── docs/             # Documentation
│   └── adr/              # Architecture Decision Records
└── config/          # Configuration
```

## Key Files

| File | Purpose |
|------|---------|
| `engine/database.py` | Bot state persistence |
| `engine/exchange_interface.py` | Binance API wrapper |
| `ui/views/monitor.py` | Live monitoring dashboard |
| `ui/views/bot_creator.py` | Bot creation form |

## Architecture Highlights

- **Multi-Bot Support**: Run multiple bots simultaneously
- **State Recovery**: Survives restarts with full state restoration
- **Ghost Trade Protection**: Prevents duplicate orders
- **Partial Fill Handling**: Correctly retries remaining order amounts

## Support

- Check logs: `engine_runner_debug.log`
- Run diagnostic scripts: `python tests/check_db_state.py`
- Verify exchange: `python tests/test_exchange_integration.py`

---

**⚠️ LIVE TRADING DISCLAIMER**: This software is for educational purposes only. Use at your own risk. Never trade with funds you cannot afford to lose.
