"""Rate-limit weight governance (operability session, 2026-09-11).

Live-measured basis: docs/RATE_LIMIT_WEIGHT_MAP.md — real FAPI allows
2400 REQUEST_WEIGHT/min (demo allows 6000, i.e. the engine has never run
against the real ceiling). Every _raw_request response's
X-MBX-USED-WEIGHT-1M header feeds the singleton `governor`, which:
  - tracks the last observed 1-minute weight usage,
  - exposes should_throttle() for callers to defer non-critical work,
  - logs ONE [RATE-GOVERNOR] alert per sustained-high episode (3
    consecutive responses at/above the soft threshold), never per line.
"""
import logging

from config.settings import config

logger = logging.getLogger("engine.rate_governor")


class RateGovernor:
    def __init__(self, limit=None, soft_pct=None):
        self.limit = int(limit if limit is not None
                         else getattr(config, "RATE_LIMIT_WEIGHT_CAP", 2400))
        self.soft_pct = float(soft_pct if soft_pct is not None
                              else getattr(config, "RATE_LIMIT_SOFT_PCT", 0.8))
        self.last_weight = 0
        self._consecutive_hot = 0

    def note_weight(self, weight: int) -> None:
        """Record the exchange-reported 1-minute weight usage from one response."""
        self.last_weight = int(weight)
        if self.last_weight >= self.limit * self.soft_pct:
            self._consecutive_hot += 1
            if self._consecutive_hot == 3:
                # Fire exactly once per sustained episode; re-arms only after
                # usage cools back below the soft threshold.
                logger.warning(
                    f"🚨 [RATE-GOVERNOR] Used weight {self.last_weight}/{self.limit} "
                    f"(>= {self.soft_pct:.0%} soft threshold) on 3 consecutive responses — "
                    f"sustained usage is high. Defer non-critical REST work."
                )
        else:
            self._consecutive_hot = 0

    def should_throttle(self) -> bool:
        """True when the last observed usage is at/above the soft threshold."""
        return self.last_weight >= self.limit * self.soft_pct

    def reset(self) -> None:
        self.last_weight = 0
        self._consecutive_hot = 0


# Singleton fed by ExchangeInterface._raw_request on every response.
governor = RateGovernor()
