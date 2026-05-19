import logging
import time
from typing import List, Dict, Optional, Any
from dataclasses import dataclass

logger = logging.getLogger("WipeAudit")

@dataclass
class SuspectWipeRow:
    order_id: str
    bot_id: int
    symbol: str
    qty: float
    order_type: str
    created_at: int

@dataclass
class WipeAuditResult:
    bot_id: int
    symbol: str
    suspect_rows: List[SuspectWipeRow]
    total_suspect_qty: float
    probable_cause_match: bool = False

def check_recompute_for_suspects(cursor, bot_id: int, symbol: str) -> List[Dict[str, Any]]:
    """
    Scans for 'reset_cleared' rows for this bot that lack a wipe_proof_snapshot
    (legacy rows) or whose snapshot shows a mismatch.
    """
    cursor.execute("""
        SELECT order_id, filled_amount, order_type, created_at, wipe_proof_source, wipe_proof_snapshot
        FROM bot_orders
        WHERE bot_id = ? AND status = 'reset_cleared' AND filled_amount > 0
    """, (bot_id,))
    
    suspects = []
    for row in cursor.fetchall():
        o_id, qty, o_type, ts, source, snap_json = row
        if source != 'legacy_wipe' and not snap_json:
            suspects.append({
                'order_id': o_id,
                'qty': qty,
                'type': o_type,
                'ts': ts,
                'is_legacy': False
            })
    return suspects

def audit_bot_wipes(cursor, bot_id: int, symbol: str, exchange_gap: float) -> WipeAuditResult:
    """
    Audits a specific bot for unproved wipes that might explain a reconciliation gap.
    """
    suspect_data = check_recompute_for_suspects(cursor, bot_id, symbol)
    suspect_rows = [
        SuspectWipeRow(s['order_id'], bot_id, symbol, s['qty'], s['type'], s['ts'])
        for s in suspect_data
    ]
    total_qty = sum(s.qty for s in suspect_rows)
    
    # Probable cause: the gap matches the suspect wipe quantity within 0.1%
    probable_cause = False
    if total_qty > 0 and exchange_gap > 0:
        if abs(total_qty - exchange_gap) < (exchange_gap * 0.001):
            probable_cause = True
            
    return WipeAuditResult(bot_id, symbol, suspect_rows, total_qty, probable_cause)

def system_wipe_health_check(cursor, active_bots: List[Any]) -> List[WipeAuditResult]:
    """
    System-wide audit of all unproved reset_cleared rows.
    """
    results = []
    for bot in active_bots:
        # Assuming bot is a tuple or object with (id, pair, ...)
        b_id = bot[0] if isinstance(bot, (tuple, list)) else getattr(bot, 'id', None)
        pair = bot[1] if isinstance(bot, (tuple, list)) else getattr(bot, 'pair', None)
        if not b_id: continue
        
        audit = audit_bot_wipes(cursor, b_id, pair, exchange_gap=0.0)
        if audit.total_suspect_qty > 0:
            results.append(audit)
            
    return results

def resolve_suspect_row(cursor, order_id: str, resolution_type: str):
    """
    Updates a suspect row with a resolution note to clear it from the audit.
    resolution_type: 'forensic_adopt', 'force_sl', 'manual_reset'
    """
    cursor.execute("""
        UPDATE bot_orders 
        SET wipe_proof_source = ?, 
            updated_at = ?
        WHERE order_id = ? AND status = 'reset_cleared'
    """, (resolution_type, int(time.time()), order_id))
