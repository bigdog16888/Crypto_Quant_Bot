# 🤖 Crypto Quant Bot (v5.3.7)

A professional-grade, multi-bot algorithmic trading system for **Binance Futures (USDC)**. Each bot maintains independent virtual accounting while the engine enforces **proof-only reconciliation** against the exchange one-way net position.

## 🌟 Key Features

* **Proof-Only Reconciliation:** Virtual positions derive only from `bot_orders` fills via `credit_fill()`. Parity is exact in quantity space (default tolerance 0.002).
* **Independent Accounting:** Each bot tracks its own fills; pair-level net is the signed sum — no proportional cross-reduction.
* **Authoritative Health State:** `engine/health.py` computes netting, orphans, and header metrics once per cycle with a 120 s startup grace period (no false alarms on boot).
* **Manual-Proof Safety Gates:** Fills credit through manual-proof gates; missing-TP, double-close orphan detection, and `REQUIRE_MANUAL_PROOF` states protect in-trade bots from silent corruption.
* **Orphan Detection (INV-32):** Unowned exchange positions surface as human-in-the-loop adoption alerts — never auto-adopted.
* **Hedge Child Lifecycle (INV-29 / INV-30):** Parent/child hedge bots with BE-TP, per-step catch-up reconciliation, cascade timeouts, and ghost detection.
* **Async Write Queue (INV-31):** Non-blocking SQLite writes keep the WebSocket listener responsive.
* **Streamlit Dashboard:** Native `@st.fragment` auto-refresh (Header 30 s, Bot Grid 15 s) with live parity display.
* **One-Way Mode:** Binance account is **one-way** — never send `positionSide`.
* **Single-Instance Guard:** `SocketLock` enforces one engine process per machine per API key — a second instance refuses to start.

---

## 🏗️ Engine Architecture

The trading engine lives in `engine/`. The orchestration entrypoint is `engine/runner/` (a Python package):

| Module | Responsibility |
|--------|---------------|
| `engine/runner/__init__.py` | `BotRunner` class — wires the mixins below together |
| `engine/runner/startup.py` | `StartupMixin` — `startup_sync`, global-wipe detection, ghost sweep, preflight |
| `engine/runner/cycle_loop.py` | `CycleLoopMixin` — per-bot execution loop, `ThreadPoolExecutor` dispatch |
| `engine/runner/websocket_lifecycle.py` | `WebSocketLifecycleMixin` — WS connect/reconnect/health-check |
| `engine/runner/shutdown.py` | `ShutdownMixin` + `SocketLock` — stop signal, fast-shutdown, lock release |

Supporting engines: `bot_executor.py` (per-bot order logic), `ledger.py` (fill ledger / `credit_fill`), `reconciler.py` (exchange↔ledger parity), `oneway_netting.py`, `parity_gates.py`, `database.py`, `exchange_interface.py`.

See **[CODEBASE_GUIDE.md](CODEBASE_GUIDE.md)** for the authoritative module map and invariants.

---

## 🚀 Quick Start

### 1. Prerequisites
* Python 3.10+
* Binance Futures account (Testnet/Demo or Mainnet)
* API key and secret

### 2. Installation
```bash
git clone <repo_url>
cd Crypto_Quant_Bot
pip install -r requirements.txt
```

### 3. Configuration
Copy `.env.example` → `.env` and set your keys. The bot supports separate Mainnet and Testnet credentials:
```ini
# Mainnet
BINANCE_API_KEY=your_mainnet_key
BINANCE_API_SECRET=your_mainnet_secret
# Testnet (used when TESTNET=True)
BINANCE_TESTNET_API_KEY=your_testnet_key
BINANCE_TESTNET_API_SECRET=your_testnet_secret

TESTNET=True
DEMO_TRADING=True
TRADING_ENABLED=True
DRY_RUN=False
MARKET_TYPE=future
ALLOWED_SYMBOLS=BTC/USDT,SOL/USDC
```

> ⚠️ `.env` and `*.db` are **git-ignored** — they are never committed. Copy them manually to any other machine where you run the bot.

### 4. Running

**Combined (recommended):**
```bash
create_backup.bat          # optional — snapshot before first run
run_stack.bat              # engine + dashboard
```

**Separate:**
```bash
run_bot.bat                # trading engine
streamlit run ui/app.py    # dashboard
```

---

## 🖥️ Dashboard

| Tab | Purpose |
|-----|---------|
| **Live Monitor** | Parity status, bot grid, orphan alerts, auto-refresh |
| **Bot Creator** | Launch Martingale / Grid / Magic Hour strategies |
| **Bot Manager** | Edit, stop, delete bots |
| **Analytics** | Historical performance |

Toggle **Auto-Refresh** only when actively monitoring — parallel fetching keeps it fast.

---

## 🔧 Operator Tools

| Tool | Command |
|------|---------|
| Bot state diagnostic | `python check_state.py` |
| Live parity check | `python scripts/diag_live_state.py` |
| One-shot ledger heal | `python scripts/run_startup_heal.py` |
| DB alignment / restore | `python scripts/align_db.py`, `scripts/restore_db.py` |
| Mismatch runbook | `docs/OPERATOR_MISMATCH_RUNBOOK.md` |
| Version backup | `create_backup.bat` |

---

## 📚 Documentation

| Document | Purpose |
|----------|---------|
| **[CODEBASE_GUIDE.md](CODEBASE_GUIDE.md)** | Authoritative guide for agents and developers (module map, invariants, changelog) |
| **[docs/archive/ARCHITECTURE_v3.5.md](docs/archive/ARCHITECTURE_v3.5.md)** | Proof-ledger architecture (historical) |
| **[docs/CHANGELOG.md](docs/CHANGELOG.md)** | Version history |
| **[docs/adr/](docs/adr/)** | Architecture decision records |
| **[TEST_SCENARIOS.md](TEST_SCENARIOS.md)** | Manual test scenarios |

---

## ⚠️ Disclaimer
This software is for educational purposes. Cryptocurrency trading involves high risk. The authors are not responsible for financial losses.