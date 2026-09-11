"""Alert routing (operability session, 2026-09-11).

CRITICAL/ERROR log records route to a push channel (Telegram) and a DB
notifications row. Contracts (tests/test_operability.py):
  - ERROR and above only; INFO/WARNING never push.
  - Per-key dedup within ALERT_DEDUP_WINDOW: the 2026-09-10 live evidence
    was 24 [CANCEL-ESCALATION] CRITICALs in 30 min on ONE order — one push
    per order per window, never per log line. Distinct orders each push.
  - No token configured -> hard-off: ZERO network calls, but the DB
    notification row still lands (the UI channel stays usable).
  - A broken channel must NEVER raise into the logging path.
Alerting is push-only. Nothing here restarts or otherwise acts on the
engine (operator doctrine: restarts only from a fully-tested branch).
"""
import re
import time
import logging

import requests

from config.settings import config

logger = logging.getLogger("engine.alerting")

_ORDER_KEY_RE = re.compile(r"Order (\d+)")


def _alert_key(message: str) -> str:
    """Dedup key: the exchange order id when present, else message text."""
    m = _ORDER_KEY_RE.search(message)
    if m:
        return f"order:{m.group(1)}"
    return f"msg:{message[:100]}"


class AlertRouter(logging.Handler):
    """logging.Handler that pushes ERROR+ records, deduped per key."""

    def __init__(self, token=None, chat_id=None, dedup_window=None):
        super().__init__(level=logging.ERROR)
        self.token = token if token is not None else config.ALERT_TELEGRAM_TOKEN
        self.chat_id = chat_id if chat_id is not None else config.ALERT_TELEGRAM_CHAT_ID
        self.dedup_window = int(
            dedup_window if dedup_window is not None
            else getattr(config, "ALERT_DEDUP_WINDOW", 3600)
        )
        self._last_push = {}

    # -- logging.Handler plumbing -------------------------------------
    def emit(self, record):
        self.push(record)

    # -- core ----------------------------------------------------------
    def push(self, record):
        try:
            if record.levelno < logging.ERROR:
                return
            msg = record.getMessage()
            key = _alert_key(msg)
            now = time.time()
            if now - self._last_push.get(key, 0.0) < self.dedup_window:
                return
            self._last_push[key] = now
            if self.token and self.chat_id:
                self._send_telegram(msg)
            self._db_notify(msg)
        except Exception:
            # Never propagate channel failures into the logging path.
            pass

    def _send_telegram(self, msg: str):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        requests.post(url, json={"chat_id": self.chat_id, "text": msg[:4000]},
                      timeout=10)

    def _db_notify(self, msg: str):
        from engine.database import get_connection
        conn = get_connection()
        conn.execute(
            "INSERT INTO notifications (timestamp, type, message, bot_id, is_read) "
            "VALUES (?, 'alert', ?, NULL, 0)",
            (int(time.time()), msg[:500]),
        )
        conn.commit()


_router = None


def get_router() -> AlertRouter:
    """Singleton router configured from env (ALERT_TELEGRAM_TOKEN/CHAT_ID)."""
    global _router
    if _router is None:
        _router = AlertRouter()
    return _router
