"""Dead-man checker (operability session, 2026-09-11).

Standalone process for Task Scheduler / cron: exits 1 and pushes ONE
alert when the engine heartbeat is stale. Push-only — this never
restarts the engine (operator doctrine).

Schedule example (Task Scheduler, every 5 min):
  py scripts/check_heartbeat.py
Set HEARTBEAT_STALE_SECONDS to override the 300s default.
"""
import os
import sys
import logging

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import ops  # noqa: E402
from engine.alerting import get_router  # noqa: E402

if __name__ == "__main__":
    stale_seconds = int(os.getenv("HEARTBEAT_STALE_SECONDS", 300))
    if ops.check_heartbeat(stale_seconds=stale_seconds):
        get_router().push(logging.LogRecord(
            "ops.heartbeat", logging.CRITICAL, __file__, 1,
            "🫀 [DEAD-MAN] Engine heartbeat is STALE "
            f"(>{stale_seconds}s) — engine may be down. Manual check required.",
            None, None))
        sys.exit(1)
    sys.exit(0)
