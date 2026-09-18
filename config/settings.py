import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    VERSION = "5.3.7"  # v5.3.7: Refactored grid grace age check and cached indicators

    ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def __init__(self):
        self.TESTNET = os.getenv("TESTNET", "True").lower() == "true"
        self.FUTURES_ONLY_MODE = os.getenv("FUTURES_ONLY_MODE", "True" if self.TESTNET else "False").lower() == "true"

        if self.TESTNET:
            self.API_KEY = os.getenv("BINANCE_TESTNET_API_KEY", os.getenv("BINANCE_API_KEY", ""))
            self.API_SECRET = os.getenv("BINANCE_TESTNET_API_SECRET", os.getenv("BINANCE_API_SECRET", ""))
        else:
            self.API_KEY = os.getenv("BINANCE_API_KEY", "")
            self.API_SECRET = os.getenv("BINANCE_API_SECRET", "")

        self.DRY_RUN = os.getenv("DRY_RUN", "False").lower() == "true"
        self.TRADING_ENABLED = os.getenv("TRADING_ENABLED", "True").lower() == "true"
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

        self.EXCHANGE_ID = os.getenv("EXCHANGE_ID", "binance").lower()
        self.DEMO_TRADING = os.getenv("DEMO_TRADING", "True").lower() == "true"
        self.MARKET_TYPE = os.getenv("MARKET_TYPE", "future").lower()
        self.ALLOWED_SYMBOLS = os.getenv("ALLOWED_SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,BTC/USDC,ETH/USDC,SOL/USDC").split(",")

        self.MAX_ORDER_USD = float(os.getenv("MAX_ORDER_USD", 10000))

        # ATR Configuration for UI/Strategy
        self.ATR_TIMEFRAME = os.getenv("ATR_TIMEFRAME", "1h")
        self.ATR_PERIODS = int(os.getenv("ATR_PERIODS", 14))

        self.METRICS_PORT = int(os.getenv("METRICS_PORT", 9090))

        self.GLOBAL_STOP_LOSS_PCT = float(os.getenv("GLOBAL_STOP_LOSS_PCT", 50.0))
        self.MAX_RETRIES = int(os.getenv("MAX_RETRIES", 3))
        self.RETRY_DELAY = int(os.getenv("RETRY_DELAY", 2))
        # 🛡️ P1 silent-cancel-swallow fix (2026-09-10): consecutive failed cancel
        # attempts (neither cancelled nor verifiably gone) before a CRITICAL
        # [CANCEL-ESCALATION] is logged per exchange order id. 3 failures ≈ 30s
        # of looped cancels (maintain cadence ~10s) instead of hours of silence
        # (live evidence: SUI 10018 looped 1127x on 2026-09-09).
        self.CANCEL_STREAK_ESCALATION = int(os.getenv("CANCEL_STREAK_ESCALATION", 3))

        # 🛡️ Operability session (2026-09-11): per-order cancel back-off.
        # Once the failure streak reaches CANCEL_STREAK_ESCALATION, retries
        # for that order back off exponentially (base doubling, capped) so a
        # stuck order cannot hammer the exchange (rate-cap math:
        # docs/RATE_LIMIT_WEIGHT_MAP.md §4 — N stuck orders x (DELETE +
        # verify-GET)/min scales toward the 1200 orders/min cap).
        # The emergency sweep (cancel_orders_by_bot_id) bypasses via force=True.
        self.CANCEL_BACKOFF_BASE_SECONDS = int(os.getenv("CANCEL_BACKOFF_BASE_SECONDS", 5))
        self.CANCEL_BACKOFF_MAX_SECONDS = int(os.getenv("CANCEL_BACKOFF_MAX_SECONDS", 30))
        # 🛡️ Operability: periodic clock re-sync (one-shot at 7a5948c; a
        # headless box drifts and real FAPI rejects >1s skew with -1021).
        self.CLOCK_RESYNC_INTERVAL = int(os.getenv("CLOCK_RESYNC_INTERVAL", 1800))
        # 🛡️ Operability: weight/429 governance (real FAPI cap is 2400/min;
        # demo allowed 6000 — live-measured in docs/RATE_LIMIT_WEIGHT_MAP.md).
        self.RATE_LIMIT_WEIGHT_CAP = int(os.getenv("RATE_LIMIT_WEIGHT_CAP", 2400))
        self.RATE_LIMIT_SOFT_PCT = float(os.getenv("RATE_LIMIT_SOFT_PCT", 0.8))
        # 🛡️ Operability: alert channel (Telegram push; empty token = push
        # disabled, DB notification rows still written). Dedup window per
        # alert key (live evidence: 24 CANCEL-ESCALATION CRITICALs in 30 min
        # on one order — one push per order per window).
        self.ALERT_TELEGRAM_TOKEN = os.getenv("ALERT_TELEGRAM_TOKEN", "")
        self.ALERT_TELEGRAM_CHAT_ID = os.getenv("ALERT_TELEGRAM_CHAT_ID", "")
        self.ALERT_DEDUP_WINDOW = int(os.getenv("ALERT_DEDUP_WINDOW", 3600))

        # 🛡️ O-3: Rolling-window portfolio drawdown breaker
        self.DRAWDOWN_WINDOW_HOURS = float(os.getenv("DRAWDOWN_WINDOW_HOURS", 24))
        self.DRAWDOWN_PCT = float(os.getenv("DRAWDOWN_PCT", 20.0))
        # 🛡️ O-10: Pair-level netting verification
        self.MIN_HEDGE_QTY = float(os.getenv("MIN_HEDGE_QTY", 0.0001))
        self.HEDGE_ENGAGE_TIMEOUT_SECONDS = int(os.getenv("HEDGE_ENGAGE_TIMEOUT_SECONDS", 300))
        self.HEDGE_FAIL_WINDOW_SECONDS = int(os.getenv("HEDGE_FAIL_WINDOW_SECONDS", 86400))
        self.PAIR_NETTING_TOLERANCE = float(os.getenv("PAIR_NETTING_TOLERANCE", "0.002"))

        # 🛡️ HEDGE-LIVE-GUARD Hardening (INV-30) — prevents single-read DB corruption
        # Multi-read corroboration: require N consistent reads within window before acting
        self.HEDGE_LIVE_GUARD_MIN_READS = int(os.getenv("HEDGE_LIVE_GUARD_MIN_READS", "3"))
        self.HEDGE_LIVE_GUARD_READ_WINDOW_SEC = int(os.getenv("HEDGE_LIVE_GUARD_READ_WINDOW_SEC", "10"))
        self.HEDGE_LIVE_GUARD_QTY_TOLERANCE = float(os.getenv("HEDGE_LIVE_GUARD_QTY_TOLERANCE", "0.01"))
        # Startup cooldown: after any connectivity failure at startup, skip hedge-live-guard for N seconds
        self.HEDGE_LIVE_GUARD_STARTUP_COOLDOWN_SEC = int(os.getenv("HEDGE_LIVE_GUARD_STARTUP_COOLDOWN_SEC", "300"))
        # Rate-of-change bound: max position change per minute (fraction, e.g., 0.5 = 50%/min)
        self.HEDGE_LIVE_GUARD_MAX_QTY_CHANGE_PER_MIN = float(os.getenv("HEDGE_LIVE_GUARD_MAX_QTY_CHANGE_PER_MIN", "0.5"))

        # 🛡️ Circuit breaker: original global-equity breaker gated behind this flag (default OFF).
        # Disabled because STARTING_EQUITY is a stale DB constant that false-positives when
        # the live balance drifts from it (testnet resets). O-1 and O-3 run unconditionally.
        self.ENABLE_GLOBAL_EQUITY_BREAKER = os.getenv("ENABLE_GLOBAL_EQUITY_BREAKER", "false").lower() == "true"

        # 🛡️ SAFETY TOGGLE: Allow user to disable auto-cancellation of zombie orders
        # 🛡️ SAFETY TOGGLE: Strict Cleanup (True = Kill Manual Orders, False = Protect Them)
        self.STRICT_CLEANUP = os.getenv("STRICT_CLEANUP", "False").lower() == "true"

        # TESTING MODE (used to bypass/alter production behavior in unit tests)
        self.TESTING_MODE = os.getenv("TESTING_MODE", "False").lower() in ("true", "1")

        # 🛡️ SAFETY TOGGLE: Block autonomous execution in production (requires human approval)
        self.REQUIRE_HUMAN_APPROVAL = os.getenv("REQUIRE_HUMAN_APPROVAL", "False").lower() == "true"

        # 🛡️ SAFETY TOGGLE: Auto-detect and repair global position wipe (e.g. testnet reset)
        self.ENABLE_GLOBAL_WIPE_DETECTION = os.getenv("ENABLE_GLOBAL_WIPE_DETECTION", "True").lower() == "true"

        # Pair parity: max |virtual - exchange| qty before blocking trade / cycle reset
        self.PAIR_PARITY_QTY_TOLERANCE = float(os.getenv("PAIR_PARITY_QTY_TOLERANCE", "0.002"))
        # Forensic/anonymous WS adopt — on by default for testnet/demo (pair-level checkpoint write)
        self.ALLOW_FORENSIC_ADOPT = os.getenv("ALLOW_FORENSIC_ADOPT", "True").lower() == "true"
        # Testnet: when exchange net is 0 but ledger is not, safe-wipe bots (no market order)
        _purge_default = "True" if self.TESTNET else "False"
        self.TESTNET_PURGE_PHANTOM_LEDGER = os.getenv(
            "TESTNET_PURGE_PHANTOM_LEDGER", _purge_default
        ).lower() == "true"
        # When ledger is flat but exchange still holds size, auto repair (adopt with proof or flatten)
        _orphan_default = "False"
        self.AUTO_REPAIR_ORPHAN_EXCHANGE = os.getenv(
            "AUTO_REPAIR_ORPHAN_EXCHANGE", _orphan_default
        ).lower() == "true"
        # One-way: block opposite-direction entry while siblings hold open_qty
        self.ONE_WAY_BLOCK_OPPOSITE_ENTRY = os.getenv(
            "ONE_WAY_BLOCK_OPPOSITE_ENTRY", "True"
        ).lower() == "true"

        # 🛡️ RECONCILER LIVE APPROVAL: Gate live reconciliation operations that can modify state.
        # When False (default): reconcile_all() runs in DRY RUN mode - logs what would be done but makes no changes.
        # When True: Full live reconciliation with DB writes, position adoption, ghost wiping, etc.
        # Set RECONCILER_LIVE_APPROVED=1 in environment to enable.
        self.RECONCILER_LIVE_APPROVED = os.getenv(
            "RECONCILER_LIVE_APPROVED", "False"
        ).lower() in ("true", "1")

        # Circuit Breaker: Max quantity allowed to be adopted/aligned for a single bot per cycle (default 0.5)
        self.MAX_ADOPTION_QTY_PER_CYCLE = float(os.getenv("MAX_ADOPTION_QTY_PER_CYCLE", "0.5"))
        # Max quantity allowed to be automatically trimmed/aligned by OWAY_REPAIR (default 50.0)
        self.MAX_OWAY_REPAIR_QTY = float(os.getenv("MAX_OWAY_REPAIR_QTY", "50.0"))
        # Max USD notional for any auto-repair action (orphan flatten, etc.) — default $5.00
        self.AUTO_REPAIR_MAX_USD = float(os.getenv("AUTO_REPAIR_MAX_USD", "5.0"))

        # ── ADR-005 Phase 3: Proportional Allocation ─────────────────────────────
        # When True: sync_pair_to_exchange() writes trades.open_qty proportionally
        # from exchange net; apply_oneway_entry_cross_reduction() is skipped.
        # When False (default / Stage A): virtual netting active; PA logic runs in
        # parallel observation-only mode and logs [PA-SYNC] lines for validation.
        # v4.1.6: Proportional Allocation model permanently CANCELLED.
        self.PROPORTIONAL_ALLOCATION = False
        # After this many consecutive API failures per pair, set bots to REQUIRE_MANUAL_PROOF.
        self.PA_SYNC_MAX_STALE_CYCLES = int(os.getenv("PA_SYNC_MAX_STALE_CYCLES", "5"))

        # 🛡️ STARTUP EXCLUSION LIST: Explicit bot IDs to skip at startup barrier.
        # These are bots with genuine anomalies that need manual review — they are
        # explicitly named so the CID-verification barrier stays strict for ALL other pairs.
        # Format: comma-separated bot IDs. Default: ETH/LINK frozen bots (Aug 2026 incident).
        # LINK bots (10020, 100320) still frozen — repair incomplete.
        # ETH bots (10011,10021,100002,100316,100321,100325) re-frozen after startup barrier failure.
        _excluded_default = "10011,10021,100002,100316,100321,100325,10020,100320"
        self.STARTUP_EXCLUDED_BOT_IDS = set(
            int(x.strip()) for x in os.getenv("STARTUP_EXCLUDED_BOT_IDS", _excluded_default).split(",")
        )

        # ─────────────────────────────────────────────────────────────────────────
        self.PATHS = {
            "PID_FILE": os.path.join(self.ROOT_DIR, "engine.pid"),
            "STOP_FILE": os.path.join(self.ROOT_DIR, "engine.stop"),
            "EMERGENCY_FILE": os.getenv("EMERGENCY_FILE", os.path.join(self.ROOT_DIR, "engine.emergency")),
            "LOG_FILE": os.path.join(self.ROOT_DIR, "engine.log"),
            "DB_FILE": os.path.join(self.ROOT_DIR, "crypto_bot.db"),
        }

    def is_bot_frozen(self, bot_id: int, bot_status: str = None) -> bool:
        """
        Centralized freeze guard — single source of truth for 'this bot must not trade'.

        Checks both config-driven exclusion (STARTUP_EXCLUDED_BOT_IDS) and
        DB-driven exclusion (REQUIRE_MANUAL_PROOF status).

        Returns True if the bot is frozen and must not place any exchange orders.
        Call at the START of any runtime path that can place real orders.
        """
        # Config-driven exclusion (survives restart, explicit operator intent)
        if bot_id in self.STARTUP_EXCLUDED_BOT_IDS:
            return True

        # DB-driven exclusion (set by barrier/GTR when anomalies detected)
        # Case-insensitive: production writes uppercase, but a lowercase write must
        # NOT silently bypass the freeze (latent bug, fixed 2026-09-15).
        if (bot_status or '').upper() == 'REQUIRE_MANUAL_PROOF':
            return True

        return False


config = Config()