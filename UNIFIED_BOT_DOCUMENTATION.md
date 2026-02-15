# Crypto Quant Bot: Unified Documentation

**Version:** 1.0.0 (Fundamental Multi-Bot Isolation)  
**Status:** Production Ready, High Stability

This document provides a unified, comprehensive overview of the Crypto Quant Bot, its architecture, setup, and operational best practices. It consolidates the key information from over 20 separate markdown files.

---

## 1. High-Level Overview

The Crypto Quant Bot is a professional-grade, automated cryptocurrency trading platform designed for high precision, robust risk management, and live market resilience.

### 1.1. Key Features

-   **Advanced Strategy Engine:** Utilizes an 11-trigger "confluence" system, requiring multiple technical indicators (RSI, CCI, Bollinger Bands), price patterns, and volatility filters to align before executing a trade.
-   **Institutional-Grade Safety:**
    -   **Global Circuit Breaker:** A master "kill switch" monitors total account equity and halts all trading if a critical drawdown (e.g., 50%) is detected.
    -   **State Recovery & Self-Healing:** The system automatically synchronizes its internal database with the exchange on startup, detecting and resolving discrepancies like "ghost trades" (positions closed while the bot was offline).
    -   **Pre-emptive Validation:** Every order is checked against the exchange's live rules (minimum notional, quantity, step size) *before* being sent, preventing API rejections and potential bans.
-   **Multi-Bot Architecture (Virtual Position Manager):** The bot's core architectural feature, allowing multiple independent trading strategies (bots) to run on the **same trading pair** simultaneously without interfering with each other.
-   **Professional UI:** A Streamlit-based dashboard provides a comprehensive interface for:
    -   **Live Monitoring:** Real-time view of trades, positions, and logs.
    -   **Bot Creation:** A wizard for configuring and deploying new strategies.
    -   **Bot Management:** Editing and controlling existing bots.
    -   **Advanced Analytics:** A performance dashboard with equity curves, win rate, profit factor, and trade history export.

---

## 2. Core Architecture: The Virtual Position Manager

The bot has evolved from a simple "one bot per pair" model to a sophisticated multi-bot system. Understanding this architecture is critical for operating and developing the bot correctly.

### 2.1. The Problem Solved

In a simple trading bot, if you have two strategies on the same pair (e.g., Bot A is LONG 0.1 BTC, Bot B is SHORT 0.1 BTC), the net position on the exchange is 0. A simple bot would see the zero position, assume its trades were closed, and incorrectly reset itself—a "ghost trade." The Virtual Position Manager solves this.

### 2.2. Core Principles

1.  **The Database is the Source of Truth:** The bot's internal `trades` table is the absolute source of truth for its position. The aggregate net position shown on the exchange is considered **irrelevant** for determining an individual bot's status.
2.  **Order Isolation via `clientOrderId`:** Every order sent to the exchange is tagged with a unique, deterministic prefix: `CQB_{bot_id}_`. For example, `CQB_42_TP_0` is the Take Profit order for Bot 42. This allows the system to distinguish which bot owns which order.
3.  **Bot-Specific Logic:** A bot determines its own state by looking for *its own* orders on the exchange.
    -   `cancel_orders_by_bot_id()` is used to safely cancel only one bot's orders.
    -   **Crucial Rule:** Global `cancel_all_orders()` calls are forbidden in standard bot logic as they would wipe out other bots' orders.

### 2.3. Multi-Bot Virtual Positioning (Final Release 2026-02-12)

The architecture has achieved its final form: **True Virtual Position Independence.**

- **No Ownership System:** The "Owner/Passenger" model has been entirely purged from the codebase. There are no "owners" or "passengers"—only independent bots.
- **Independent Management:** Each bot manages its own entry, take-profit, and grid orders using its unique `CQB_{bot_id}_` prefix. 
- **Database as Source of Truth:** A bot's trade status is determined exclusively by its record in the `trades` table and the presence of its specific orders on the exchange.
- **Aggregate Position Reconciliation:** The `reconciler.py` handles One-Way mode by summing all virtual positions on a pair to verify against the physical exchange position, ensuring stability even when multiple bots trade the same side.

---

## 3. Configuration & Setup

### 3.1. API Keys (CRITICAL UPDATE)
**Binance Testnet/Sandbox for Futures is DEPRECATED by CCXT.**
To run this bot, you **MUST** use valid Binance Futures API keys (Mainnet). 
- Ensure `DEMO_TRADING=False` in your `.env` file.
- Use `DRY_RUN=True` to test logic without placing real orders.

### 3.2. Installation Steps

```bash
# 1. Clone the repository
git clone https://github.com/your-repo/Crypto_Quant_Bot.git
cd Crypto_Quant_Bot

# 2. Create and activate a virtual environment (recommended)
python -m venv venv
# On Windows:
venv\Scripts\activate
# On Linux/Mac:
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
# Create a .env file from the example
cp .env.example .env

# Edit the .env file with your Binance API keys and settings
# nano .env
```

### 3.3. Environment Configuration (`.env`)

Your `.env` file must contain:

```ini
# Your Binance API Key and Secret
BINANCE_API_KEY=your_key_here
# Your Binance API Secret
BINANCE_API_SECRET=your_secret_here

# Set to False for live trading
DRY_RUN=True

# Set to True to use the Binance Testnet (DEPRECATED for Futures)
TESTNET=False

# The global circuit breaker limit (e.g., 50.0 for 50%)
GLOBAL_STOP_LOSS_PCT=50.0

# The master switch for placing live orders
TRADING_ENABLED=True
```

### 3.4. Running the Application

The bot consists of two main components: the UI and the trading engine. The UI provides a control panel for the engine.

```bash
# Start the Streamlit UI
streamlit run ui/app.py

# Once the UI is running, navigate to http://localhost:8501
# Use the sidebar controls to start and stop the trading engine.
```

---

## 4. Developer's Guide & Best Practices

### 4.1. The Golden Rule of Multi-Bot

**Never use `cancel_all_orders(pair)`. Always use `cancel_orders_by_bot_id(bot_id, pair)`.** The former will cause catastrophic interference between bots; the latter is the foundation of the Virtual Position Manager.

### 4.2. Recent Stability & Bug Fixes (Updated: 2026-02-12)

The bot has recently undergone a fundamental stabilization phase to ensure multi-bot isolation.

-   **Order Isolation (2026-02-12):** Fixed critical collisions in `MarketMaker` logic and `OrderManager` where global cancellation calls were wiping out orders from other bots.
-   **Aggregate Position Math (2026-02-12):** Reconciler now sums virtual positions for One-Way mode validation.
-   **WebSocket Handler (2026-02-12):** Fixed a `KeyError` in `ws_event_handlers.py` where database results were being accessed as lists instead of dictionaries.
-   **Database Integrity (2026-02-12):** Fixed "Ghost Fix Loop" by ensuring `basket_start_time` is correctly initialized in the `trades` table.
-   **Exchange Limits (2026-02-12):** Increased default `base_size` to $150 to ensure orders always clear the exchange's "Min Notional" requirements.

### 4.3. Troubleshooting

-   **UI Won't Start:** Check if the port (usually 8501) is in use.
-   **P/L Shows But No Exchange Position:** State mismatch. Restart the engine to trigger re-sync.
-   **Bots Auto-Resetting to IDLE:** Check `basket_start_time` in the `trades` table. If it's `0`, the "Ghost Fix" will trigger a reset. This was addressed in the v1.0.0 fix.
-   **Orders Rejected (Min Notional):** Ensure `base_size` is at least $150 (especially on USDC testnet).

---

## 5. Changelog Summary

### Version 1.0.0 (2026-02-12)
**Fundamental Multi-Bot Isolation**
- **Scoped Cancellations:** Replaced all `cancel_all_orders` with `cancel_orders_by_bot_id` in core engine.
- **Aggregate Reconciliation:** `reconciler.py` now correctly handles shared positions in One-Way mode.
- **WebSocket Fix:** Corrected dictionary access in real-time event handlers.
- **Basket Timestamp Fix:** Ensured active trades have valid start times to prevent premature auto-healing resets.
- **Min Notional Safety:** Increased default order size to clear exchange hurdles.

### Version 0.9.1 (2026-02-11)
**Major Update: True Virtual Positions**
- **Removed Ownership Blocking:** Completely eliminated `try_atomic_claim_ownership_before_entry()`.
- **Fixed Database Schema:** Updated `active_positions` table for multi-bot primary keys.
- **Reconciler Decoupling:** Removed ownership state dependencies.

---
This unified document was last updated on 2026-02-12.
