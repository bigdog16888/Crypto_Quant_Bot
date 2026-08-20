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


_WIPE_ORDERS_SQL = """
    UPDATE bot_orders
    SET status = 'reset_cleared',
        updated_at = ?,
        wipe_proof_source = ?,
        wipe_proof_snapshot = ?
    WHERE (bot_id = ? AND status NOT IN ('auto_closed', 'reset_cleared'))
          -- Also sweep cancelled rows that had partial fills (they survive normal
          -- filters and get counted as active sells in get_pair_virtual_net,
          -- causing sign flips)
          OR (bot_id = ? AND status IN ('cancelled','canceled') AND filled_amount > 0)
"""


def _apply_wipe_internal(bot_id: int, now: int, source_value: str, snapshot_json: str):
    """INV-31: single writer for the bot_orders reset, executed via
    WriteQueue().put_and_wait(...) on the WriteQueue worker thread.

    Uses the worker's own thread-local connection (never a caller cursor — cursors
    cannot cross threads). If the worker connection is already inside a transaction
    (nested wrapped call), the write joins it and the outer owner commits; otherwise
    it commits standalone. Never closes the connection — mirrors the
    _credit_fill_internal pattern (commit, never close).
    """
    from engine.database import get_connection
    conn = get_connection()
    nested = getattr(conn, 'in_transaction', False)
    conn.cursor().execute(
        _WIPE_ORDERS_SQL,
        (now, source_value, snapshot_json, bot_id, bot_id),
    )
    if not nested:
        conn.commit()


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
    INV-31: the read-only safety gate runs on the caller's thread (using the caller's
    cursor); only the single bot_orders write is routed through the WriteQueue.
    """
    now = now_ts or int(time.time())

    # 1. Fetch current exchange state
    snapshot = fetch_exchange_position_fn(symbol)

    # 2. Safety Gate: If this isn't an explicit destruction, check pair-level drift.
    if not allow_nonzero_wipe:
        from engine.database import get_pair_virtual_net
        current_virtual_net = get_pair_virtual_net(symbol)

        # Get signed_open_qty for this bot
        from engine.parity_gates import get_bot_signed_contribution
        signed_open_qty = get_bot_signed_contribution(bot_id)

        if snapshot.source == WipeProofSource.CACHED_SNAP or (snapshot.source == WipeProofSource.LIVE_FETCH and snapshot.side == 'FLAT'):
            from engine.exchange_interface import normalize_symbol
            norm_pair = normalize_symbol(symbol)
            rows = cursor.execute(
                "SELECT side, size FROM active_positions WHERE pair = ?",
                (norm_pair,)
            ).fetchall()
            if rows:
                physical_net = 0.0
                for side, size in rows:
                    qty_val = float(size or 0)
                    if side.upper() == 'LONG':
                        physical_net += qty_val
                    elif side.upper() == 'SHORT':
                        physical_net -= qty_val
            else:
                physical_net = 0.0
        else:
            physical_net = snapshot.qty if snapshot.side == 'LONG' else (-snapshot.qty if snapshot.side == 'SHORT' else 0.0)

        virtual_net_after_wipe = current_virtual_net - signed_open_qty
        drift_after_wipe = abs(virtual_net_after_wipe - physical_net)
        drift_before_wipe = abs(current_virtual_net - physical_net)

        if drift_after_wipe > drift_before_wipe + tolerance:
            raise WipeBlockedError(
                f"WIPE BLOCKED: Bot {bot_id} ({symbol}) has virtual signed open_qty={signed_open_qty:.8f}. "
                f"Wiping this bot would increase pair-level drift from {drift_before_wipe:.8f} to {drift_after_wipe:.8f} "
                f"(physical net is {physical_net:.8f}, virtual net before wipe is {current_virtual_net:.8f})."
            )

    # 3. Apply the wipe logic (single write path).
    #
    # INV-31: the bot_orders write routes through the WriteQueue singleton so it
    # executes on the single writer thread. EXCEPTION — external-cursor pattern
    # (same rationale as commit 592e131 "safe_wipe_bot: always use
    # _reset_bot_after_tp_internal with existing cursor (avoid WriteQueue
    # deadlock)"): if the caller's cursor is already inside an open transaction,
    # queueing would DEADLOCK — the worker's separate connection cannot acquire
    # the write lock the caller's transaction holds while the caller blocks in
    # put_and_wait (30s TimeoutError). Both current callers reach this point with
    # an open transaction (_reset_bot_after_tp_internal writes before calling;
    # reconcile_with_db uses BEGIN IMMEDIATE), so the write executes inline on the
    # caller's cursor, serialized by the caller's transaction — identical to the
    # pre-refactor semantics. When no transaction is open (standalone/future
    # callers, or any chain already running on the WriteQueue worker), the write
    # is queued and runs on the worker thread.
    caller_conn = cursor.connection
    if getattr(caller_conn, 'in_transaction', False):
        cursor.execute(
            _WIPE_ORDERS_SQL,
            (now, snapshot.source.value, snapshot.to_json(), bot_id, bot_id),
        )
        result = None
    else:
        from engine.write_queue import WriteQueue
        result = WriteQueue().put_and_wait(
            _apply_wipe_internal,
            bot_id,
            now,
            snapshot.source.value,
            snapshot.to_json(),
        )

    logger.info(f"🛡️ [WIPE-PROOF] Bot {bot_id} ledger reset with proof: {snapshot.side} {snapshot.qty:.8f} (source: {snapshot.source.value})")
    return result
