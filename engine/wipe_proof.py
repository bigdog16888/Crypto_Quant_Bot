import time
import json
import logging
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional, List, Callable, Dict, Any

logger = logging.getLogger("WipeProof")

class WipeProofSource(Enum):
    LIVE_FETCH = "live_fetch"       # Mandatory exchange check
    CACHED_SNAP = "cached_snap"     # Used if we just fetched positions for reconciliation
    LEGACY_WIPE = "legacy_wipe"     # Pre-migration rows
    MANUAL_OVERRIDE = "manual"      # Operator-approved forensic scan

@dataclass
class ExchangePositionSnapshot:
    symbol: str
    qty: float
    side: str  # 'LONG', 'SHORT', 'FLAT'
    fetched_at: int
    source: WipeProofSource

    def to_json(self) -> str:
        data = asdict(self)
        data['source'] = self.source.value
        return json.dumps(data)

    @classmethod
    def from_json(cls, json_str: str) -> Optional['ExchangePositionSnapshot']:
        if not json_str: return None
        try:
            data = json.loads(json_str)
            data['source'] = WipeProofSource(data['source'])
            return cls(**data)
        except Exception:
            return None

class WipeBlockedError(Exception):
    """Raised when a ledger wipe is attempted but the exchange position is not flat."""
    pass

def build_zero_snapshot(symbol: str) -> ExchangePositionSnapshot:
    """Shortcut for when we know the position is already flat (e.g. TP receipt)."""
    return ExchangePositionSnapshot(
        symbol=symbol,
        qty=0.0,
        side="FLAT",
        fetched_at=int(time.time()),
        source=WipeProofSource.LIVE_FETCH
    )

def safe_mark_reset_cleared(
    cursor,
    bot_id: int,
    symbol: str,
    action_label: str,
    fetch_exchange_position_fn: Callable[[str], ExchangePositionSnapshot],
    excluded_carry_labels: List[str],
    now_ts: Optional[int] = None,
    allow_nonzero_wipe: bool = False,
    tolerance: float = 1e-8
):
    """
    V3.3.1: Wipe-Proof Status Update.
    Mandates a physical exchange snapshot before marking orders as 'reset_cleared'.
    """
    now = now_ts or int(time.time())
    
    # 1. Fetch current exchange state
    snapshot = fetch_exchange_position_fn(symbol)
    
    # 2. Safety Gate: If position is NOT flat and this isn't an explicit destruction, BLOCK the wipe.
    if snapshot.qty > tolerance and not allow_nonzero_wipe:
        raise WipeBlockedError(
            f"WIPE BLOCKED: Bot {bot_id} ({symbol}) has live {snapshot.side} position "
            f"({snapshot.qty:.8f}). Ledger wipe would cause phantom drift."
        )

    # 3. Apply the wipe logic (Hedge Preservation Gate remains)
    is_destruction = action_label in excluded_carry_labels
    
    if is_destruction:
        # Clear everything
        cursor.execute("""
            UPDATE bot_orders 
            SET status = 'reset_cleared', 
                updated_at = ?,
                wipe_proof_source = ?,
                wipe_proof_snapshot = ?
            WHERE (bot_id = ? AND status NOT IN ('auto_closed', 'reset_cleared'))
                  -- Also sweep cancelled rows that had partial fills (they survive normal filters
                  -- and get counted as active sells in get_pair_virtual_net, causing sign flips)
                  OR (bot_id = ? AND status IN ('cancelled','canceled') AND filled_amount > 0)
        """, (now, snapshot.source.value, snapshot.to_json(), bot_id, bot_id))
    else:
        # Normal TP: clear entries/TPs/grids but preserve hedges
        cursor.execute("""
            UPDATE bot_orders 
            SET status = 'reset_cleared', 
                updated_at = ?,
                wipe_proof_source = ?,
                wipe_proof_snapshot = ?
            WHERE (bot_id = ? AND status NOT IN ('auto_closed', 'reset_cleared') 
                  AND NOT (order_type LIKE 'hedge%' AND filled_amount > 0))
                  -- Also sweep cancelled rows that had partial fills (same phantom prevention)
                  OR (bot_id = ? AND status IN ('cancelled','canceled') AND filled_amount > 0
                      AND NOT (order_type LIKE 'hedge%' AND filled_amount > 0))
        """, (now, snapshot.source.value, snapshot.to_json(), bot_id, bot_id))

    logger.info(f"🛡️ [WIPE-PROOF] Bot {bot_id} ledger reset with proof: {snapshot.side} {snapshot.qty:.8f} (source: {snapshot.source.value})")
