# 🤖 Crypto Quant Bot (v4.3.8)

A professional-grade, multi-bot algorithmic trading system for **Binance Futures (USDC)**. Each bot maintains independent virtual accounting while the engine enforces **proof-only reconciliation** against the exchange one-way net position.

## 🌟 Key Features (v4.3.8)

* **Proof-Only Reconciliation (v3.5+):** Virtual positions derive only from `bot_orders` fills via `credit_fill()`. Parity is exact in quantity space (default tolerance 0.002).
* **Independent Accounting (v4.3.0 / ADR-006):** Each bot tracks its own fills; pair-level net is the signed sum — no proportional cross-reduction.
* **Authoritative Health State (v4.3.8):** `engine/health.py` computes netting, orphans, and header metrics once per cycle with a 120 s startup grace period (no false alarms on boot).
* **INV-34 / INV-36 Safety:** Fills credit through manual-proof gates; missing-TP and double-close orphan detection protect in-trade bots.
* **INV-32 Orphan Detection:** Unowned exchange positions surface as human-in-the-loop adoption alerts — never auto-adopted.
* **Hedge Child Lifecycle (INV-29):** Parent/child hedge bots with BE-TP, cascade timeouts, and ghost detection.
* **Async Write Queue (INV-31):** Non-blocking SQLite writes keep the WebSocket listener responsive.
* **Streamlit Dashboard:** Native `@st.fragment` auto-refresh (Header 30 s, Bot Grid 15 s) with live parity display.
* **One-Way Mode:** Binance account is **one-way** — never send `positionSide`.

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
Copy `.env.example` → `.env` and set:
```ini
TESTNET=True
DEMO_TRADING=True
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
MARKET_TYPE=future
```

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
| Mismatch runbook | `docs/OPERATOR_MISMATCH_RUNBOOK.md` |
| Version backup | `create_backup.bat` |

---

## 📚 Documentation

| Document | Purpose |
|----------|---------|
| **[CODEBASE_GUIDE.md](CODEBASE_GUIDE.md)** | Authoritative guide for agents and developers |
| **[docs/ARCHITECTURE_v3.5.md](docs/ARCHITECTURE_v3.5.md)** | Proof-ledger architecture |
| **[docs/CHANGELOG.md](docs/CHANGELOG.md)** | Version history |
| **[docs/adr/](docs/adr/)** | Architecture decision records |
| **[TEST_SCENARIOS.md](TEST_SCENARIOS.md)** | Manual test scenarios |

---

## ⚠️ Disclaimer
This software is for educational purposes. Cryptocurrency trading involves high risk. The authors are not responsible for financial losses.
