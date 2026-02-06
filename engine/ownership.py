"""
Ownership Management System for Multi-Bot Position Management

Handles the complete lifecycle of position ownership including:
- First-Claim Policy for entry
- Owner tracking and failover
- Passenger monitoring and state management
- Complete audit trail for all ownership transitions

Ownership States:
    IDLE: Bot has no position, waiting for entry signal
    PENDING_ENTRY: Entry order placed, waiting for fill
    OWNER: Has position and manages it (places TP/grid orders)
    PASSENGER: Monitoring owner's position, no orders placed
    PENDING_TP: TP order placed, waiting for hit
    CLOSED: Position closed, in cooldown period

Ownership Transition Events:
    ENTRY_SIGNAL: Entry signal received
    ENTRY_FILLED: Entry order was filled
    OWNER_CLAIMED: Bot became position owner
    PASSENGER_JOINED: Bot joined as passenger
    TP_HIT: Take profit was hit
    STOP_HIT: Stop loss was hit
    MANUAL_CLOSE: Position manually closed
    OWNER_GONE: Owner disappeared/crashed
    COOLDOWN_COMPLETE: Re-entry cooldown finished
"""

import logging
import time
import sqlite3
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from pathlib import Path

logger = logging.getLogger("OwnershipManager")

# Database path
DB_PATH = Path(__file__).parent.parent / "crypto_bot.db"


class OwnershipState(Enum):
    """Bot ownership states"""
    IDLE = "idle"
    PENDING_ENTRY = "pending_entry"
    OWNER = "owner"
    PASSENGER = "passenger"
    PENDING_TP = "pending_tp"
    CLOSED = "closed"


class OwnershipEvent(Enum):
    """Events that trigger ownership transitions"""
    ENTRY_SIGNAL = "entry_signal"
    ENTRY_FILLED = "entry_filled"
    OWNER_CLAIMED = "owner_claimed"
    PASSENGER_JOINED = "passenger_joined"
    TP_HIT = "tp_hit"
    STOP_HIT = "stop_hit"
    MANUAL_CLOSE = "manual_close"
    OWNER_GONE = "owner_gone"
    COOLDOWN_COMPLETE = "cooldown_complete"
    RECONCILE = "reconcile"


@dataclass
class OwnershipRecord:
    """Complete ownership record for audit trail"""
    id: int
    bot_id: int
    pair: str
    previous_state: str
    new_state: str
    event: str
    details: str
    timestamp: float
    created_at: str


@dataclass
class BotOwnership:
    """Current ownership status of a bot"""
    bot_id: int
    bot_name: str
    pair: str
    state: OwnershipState
    is_owner: bool
    position_size: float
    avg_entry_price: float
    target_tp_price: float
    basket_start_time: float
    entry_order_id: Optional[str]
    tp_order_id: Optional[str]
    last_updated: float


@dataclass
class PairOwnership:
    """Complete ownership status for a trading pair"""
    pair: str
    owner: Optional[BotOwnership]
    passengers: List[BotOwnership]
    total_position_size: float
    exchange_position_exists: bool


def get_connection() -> sqlite3.Connection:
    """Get database connection"""
    return sqlite3.connect(str(DB_PATH))


def init_ownership_tables():
    """Initialize ownership tracking tables"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Ownership state table (current state per bot)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_ownership_state (
            bot_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'idle',
            is_owner INTEGER NOT NULL DEFAULT 0,
            pair TEXT,
            position_size REAL DEFAULT 0,
            avg_entry_price REAL DEFAULT 0,
            target_tp_price REAL DEFAULT 0,
            basket_start_time INTEGER DEFAULT 0,
            entry_order_id TEXT,
            tp_order_id TEXT,
            owner_id INTEGER,
            last_updated INTEGER,
            FOREIGN KEY (bot_id) REFERENCES bots(id)
        )
    ''')
    
    # Ownership history table (audit trail)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_ownership_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER NOT NULL,
            pair TEXT,
            previous_state TEXT,
            new_state TEXT NOT NULL,
            event TEXT NOT NULL,
            details TEXT,
            timestamp INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    ''')
    
    # Active positions table (what's on the exchange)
    # Re-create to ensure schema update (safe as it's just a cache)
    cursor.execute('DROP TABLE IF EXISTS active_positions')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS active_positions (
            pair TEXT,
            side TEXT,
            size REAL NOT NULL DEFAULT 0,
            entry_price REAL DEFAULT 0,
            owner_bot_id INTEGER,
            owner_start_time INTEGER,
            last_checked INTEGER,
            last_updated INTEGER DEFAULT (datetime('now')),
            PRIMARY KEY (pair, side)
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("Ownership tables initialized")


def record_ownership_event(
    bot_id: int,
    pair: str,
    previous_state: OwnershipState | None,
    new_state: OwnershipState,
    event: OwnershipEvent,
    details: str = ""
):
    """Record an ownership transition in the audit trail"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO bot_ownership_history 
        (bot_id, pair, previous_state, new_state, event, details, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        bot_id, pair, 
        previous_state.value if previous_state else None,
        new_state.value, 
        event.value, 
        details,
        int(time.time())
    ))
    
    conn.commit()
    conn.close()


def update_ownership_state(
    bot_id: int,
    pair: str,
    state: OwnershipState,
    is_owner: bool,
    position_size: float = 0,
    avg_entry_price: float = 0,
    target_tp_price: float = 0,
    basket_start_time: float = 0,
    entry_order_id: Optional[str] = None,
    tp_order_id: Optional[str] = None,
    owner_id: Optional[int] = None
):
    """Update the current ownership state for a bot"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT OR REPLACE INTO bot_ownership_state 
        (bot_id, state, is_owner, pair, position_size, avg_entry_price, 
         target_tp_price, basket_start_time, entry_order_id, tp_order_id, 
         owner_id, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        bot_id, state.value, 1 if is_owner else 0, pair,
        position_size, avg_entry_price, target_tp_price,
        int(basket_start_time) if basket_start_time else 0,
        entry_order_id, tp_order_id, owner_id, int(time.time())
    ))
    
    conn.commit()
    conn.close()


def get_ownership_state(bot_id: int) -> Optional[BotOwnership]:
    """Get the current ownership state for a bot"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT bot_id, pair, state, is_owner, position_size, avg_entry_price,
               target_tp_price, basket_start_time, entry_order_id, tp_order_id, last_updated
        FROM bot_ownership_state WHERE bot_id = ?
    ''', (bot_id,))
    
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return None
    
    return BotOwnership(
        bot_id=row[0],
        bot_name="",  # Will need to join with bots table if needed
        pair=row[1],
        state=OwnershipState(row[2]),
        is_owner=bool(row[3]),
        position_size=row[4],
        avg_entry_price=row[5],
        target_tp_price=row[6],
        basket_start_time=row[7],
        entry_order_id=row[8],
        tp_order_id=row[9],
        last_updated=row[10]
    )


def get_pair_ownership(pair: str) -> PairOwnership:
    """Get complete ownership status for a trading pair"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get owner
    cursor.execute('''
        SELECT bos.bot_id, bos.state, bos.is_owner, bos.position_size,
               bos.avg_entry_price, bos.target_tp_price, bos.basket_start_time,
               bos.entry_order_id, bos.tp_order_id, bos.last_updated,
               b.name
        FROM bot_ownership_state bos
        JOIN bots b ON bos.bot_id = b.id
        WHERE bos.pair = ? AND bos.is_owner = 1 AND bos.state IN ('owner', 'pending_tp')
    ''', (pair,))
    
    owner_row = cursor.fetchone()
    owner = None
    if owner_row:
        owner = BotOwnership(
            bot_id=owner_row[0],
            bot_name=owner_row[10],
            pair=pair,
            state=OwnershipState(owner_row[1]),
            is_owner=bool(owner_row[2]),
            position_size=owner_row[3],
            avg_entry_price=owner_row[4],
            target_tp_price=owner_row[5],
            basket_start_time=owner_row[6],
            entry_order_id=owner_row[7],
            tp_order_id=owner_row[8],
            last_updated=owner_row[9]
        )
    
    # Get passengers
    cursor.execute('''
        SELECT bos.bot_id, bos.state, bos.is_owner, bos.position_size,
               bos.avg_entry_price, bos.target_tp_price, bos.basket_start_time,
               bos.entry_order_id, bos.tp_order_id, bos.last_updated,
               b.name
        FROM bot_ownership_state bos
        JOIN bots b ON bos.bot_id = b.id
        WHERE bos.pair = ? AND bos.state = 'passenger'
        ORDER BY bos.basket_start_time ASC
    ''', (pair,))
    
    passengers = []
    for row in cursor.fetchall():
        passengers.append(BotOwnership(
            bot_id=row[0],
            bot_name=row[10],
            pair=pair,
            state=OwnershipState(row[1]),
            is_owner=bool(row[2]),
            position_size=row[3],
            avg_entry_price=row[4],
            target_tp_price=row[5],
            basket_start_time=row[6],
            entry_order_id=row[7],
            tp_order_id=row[8],
            last_updated=row[9]
        ))
    
    conn.close()
    
    # Calculate total position
    total_size = (owner.position_size if owner else 0) + sum(p.position_size for p in passengers)
    
    return PairOwnership(
        pair=pair,
        owner=owner,
        passengers=passengers,
        total_position_size=total_size,
        exchange_position_exists=total_size > 0
    )


def check_first_claim_policy(bot_id: int, pair: str) -> tuple[bool, Optional[int], str]:
    """
    Check if a bot can claim ownership of a pair.
    
    Returns:
        (can_claim, existing_owner_id, message)
        - If can_claim is True: This bot can become owner
        - If can_claim is False: existing_owner_id is the current owner
    """
    """
    Check if a bot can claim ownership of a pair.
    
    Returns:
        (can_claim, existing_owner_id, message)
        - If can_claim is True: This bot can become owner
        - If can_claim is False: existing_owner_id is the current owner
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Check if ANY bot currently owns this pair
    cursor.execute('''
        SELECT bot_id, is_owner FROM bot_ownership_state 
        WHERE pair = ? AND is_owner = 1 AND state IN ('owner', 'pending_tp')
    ''', (pair,))
    
    row = cursor.fetchone()
    conn.close()
    
    # If no owner exists, this bot can claim it
    if not row:
        return True, None, "No existing owner"
        
    existing_owner_id = row[0]
    
    # If THIS bot is already the owner, it can proceed
    if existing_owner_id == bot_id:
        return True, bot_id, "Already owner"
        
    # Otherwise, another bot owns it -> BLOCK
    return False, existing_owner_id, f"Pair owned by Bot {existing_owner_id}"

    # Legacy strict locking logic (disabled)
    # conn = get_connection()
    # cursor = conn.cursor()
    # ...


def claim_ownership(
    bot_id: int,
    bot_name: str,
    pair: str,
    entry_order_id: str,
    entry_price: float,
    amount_usd: float,
    tp_price: float
) -> tuple[bool, str]:
    """
    Claim ownership after entry order is filled.
    
    This should be called when the entry order is confirmed filled.
    """
    can_claim, existing_owner_id, message = check_first_claim_policy(bot_id, pair)
    
    if not can_claim:
        # Another bot owns this pair - become passenger instead
        become_passenger(
            bot_id, bot_name, pair,
            entry_order_id, entry_price, amount_usd, tp_price,
            existing_owner_id or 0  # Ensure we pass an int
        )
        return False, f"Became passenger (Owner: Bot {existing_owner_id})"
    
    # Claim ownership
    previous_state = get_ownership_state(bot_id)
    
    update_ownership_state(
        bot_id=bot_id,
        pair=pair,
        state=OwnershipState.OWNER,
        is_owner=True,
        position_size=amount_usd,
        avg_entry_price=entry_price,
        target_tp_price=tp_price,
        basket_start_time=time.time(),
        entry_order_id=entry_order_id,
        owner_id=bot_id
    )
    
    record_ownership_event(
        bot_id=bot_id,
        pair=pair,
        previous_state=previous_state.state if previous_state else None,
        new_state=OwnershipState.OWNER,
        event=OwnershipEvent.OWNER_CLAIMED,
        details=f"Claimed ownership of {pair} at ${entry_price}"
    )
    
    logger.info(f"✅ Bot {bot_id} ({bot_name}) claimed OWNERSHIP of {pair}")
    return True, f"Claimed ownership of {pair}"


def become_passenger(
    bot_id: int,
    bot_name: str,
    pair: str,
    entry_order_id: str,
    entry_price: float,
    amount_usd: float,
    tp_price: float,
    owner_id: int | None
):
    """Become a passenger (monitoring owner, not placing orders)"""
    previous_state = get_ownership_state(bot_id)
    
    update_ownership_state(
        bot_id=bot_id,
        pair=pair,
        state=OwnershipState.PASSENGER,
        is_owner=False,
        position_size=amount_usd,
        avg_entry_price=entry_price,
        target_tp_price=tp_price,
        basket_start_time=time.time(),
        entry_order_id=entry_order_id,
        owner_id=owner_id
    )
    
    record_ownership_event(
        bot_id=bot_id,
        pair=pair,
        previous_state=previous_state.state if previous_state else None,
        new_state=OwnershipState.PASSENGER,
        event=OwnershipEvent.PASSENGER_JOINED,
        details=f"Became passenger monitoring Bot {owner_id} on {pair}"
    )
    
    logger.info(f"👀 Bot {bot_id} ({bot_name}) became PASSENGER of {pair} (Owner: Bot {owner_id})")


def handle_position_closed(
    bot_id: int,
    pair: str,
    close_type: str,
    exit_price: float = 0
):
    """
    Handle position closure (TP hit, stop hit, or manual close).
    
    This resets the owner and all passengers on the pair.
    """
    current_state = get_ownership_state(bot_id)
    
    if not current_state or current_state.state not in [OwnershipState.OWNER, OwnershipState.PASSENGER]:
        return
    
    # Get all bots on this pair
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT bot_id FROM bot_ownership_state
        WHERE pair = ? AND state IN ('owner', 'passenger', 'pending_tp')
    ''', (pair,))
    
    bots_on_pair = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    # Reset all bots on this pair
    event = OwnershipEvent(close_type) if close_type in [e.value for e in OwnershipEvent] else OwnershipEvent.MANUAL_CLOSE
    
    for bot in bots_on_pair:
        bot_state = get_ownership_state(bot)
        if not bot_state:
            continue
            
        update_ownership_state(
            bot_id=bot,
            pair=pair,
            state=OwnershipState.CLOSED,
            is_owner=False,
            position_size=0,
            avg_entry_price=0,
            target_tp_price=0,
            basket_start_time=0,
            entry_order_id=None,
            tp_order_id=None,
            owner_id=None
        )
        
        record_ownership_event(
            bot_id=bot,
            pair=pair,
            previous_state=bot_state.state,
            new_state=OwnershipState.CLOSED,
            event=event,
            details=f"Position closed via {close_type} at ${exit_price}"
        )
    
    logger.info(f"🔄 All bots on {pair} reset to CLOSED state ({close_type})")


def check_owner_failover(pair: str) -> Optional[int]:
    """
    Check if owner has disappeared and promote the oldest passenger.
    
    Returns the new owner bot_id if failover happened, None otherwise.
    """
    pair_ownership = get_pair_ownership(pair)
    
    if pair_ownership.owner:
        # Check if owner is still active and in trade
        owner_state = get_ownership_state(pair_ownership.owner.bot_id)
        if owner_state and owner_state.state in [OwnershipState.OWNER, OwnershipState.PASSENGER]:
            # Owner still exists, no failover needed
            return None
    
    # Owner gone - promote oldest passenger
    if not pair_ownership.passengers:
        logger.warning(f"⚠️ Owner of {pair} gone but no passengers to promote!")
        return None
    
    # Get oldest passenger (earliest basket_start_time)
    new_owner = min(pair_ownership.passengers, key=lambda p: p.basket_start_time)
    
    # Promote to owner
    previous_state = get_ownership_state(new_owner.bot_id)
    
    update_ownership_state(
        bot_id=new_owner.bot_id,
        pair=pair,
        state=OwnershipState.OWNER,
        is_owner=True,
        position_size=new_owner.position_size,
        avg_entry_price=new_owner.avg_entry_price,
        target_tp_price=new_owner.target_tp_price,
        basket_start_time=new_owner.basket_start_time,
        entry_order_id=new_owner.entry_order_id,
        tp_order_id=None,  # Will need to place new TP order
        owner_id=new_owner.bot_id
    )
    
    record_ownership_event(
        bot_id=new_owner.bot_id,
        pair=pair,
        previous_state=previous_state.state if previous_state else None,
        new_state=OwnershipState.OWNER,
        event=OwnershipEvent.OWNER_GONE,
        details=f"Promoted to owner after previous owner disappeared"
    )
    
    logger.info(f"🔄 Bot {new_owner.bot_id} ({new_owner.bot_name}) PROMOTED to owner of {pair} (failover)")
    return new_owner.bot_id


def reconcile_pair(pair: str, exchange_position_exists: bool) -> Dict[str, Any]:
    """
    Complete reconciliation for a trading pair.
    
    Checks ownership consistency, handles failover, and updates state.
    """
    result = {
        "pair": pair,
        "actions_taken": [],
        "new_owner": None,
        "warnings": []
    }
    
    pair_ownership = get_pair_ownership(pair)
    
    if not exchange_position_exists:
        # No position on exchange - all bots should be CLOSED
        if pair_ownership.owner or pair_ownership.passengers:
            for bot in [pair_ownership.owner] + pair_ownership.passengers:
                if bot:
                    current = get_ownership_state(bot.bot_id)
                    if current and current.state not in [OwnershipState.CLOSED, OwnershipState.IDLE]:
                        handle_position_closed(bot.bot_id, pair, "reconcile")
                        result["actions_taken"].append(f"Reset Bot {bot.bot_id} to CLOSED")
        
        return result
    
    # Position exists on exchange
    if not pair_ownership.owner and not pair_ownership.passengers:
        # Position exists but no bots tracking - orphan position
        result["warnings"].append(f"Orphan position on {pair} - no bot tracking!")
        logger.warning(f"⚠️ Orphan position on {pair}")
        return result
    
    # Check for owner failover
    new_owner_id = check_owner_failover(pair)
    if new_owner_id:
        result["new_owner"] = new_owner_id
        result["actions_taken"].append(f"Promoted Bot {new_owner_id} to owner")
    
    return result


def get_ownership_history(bot_id: int, limit: int = 50) -> List[OwnershipRecord]:
    """Get ownership history for a bot"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, bot_id, pair, previous_state, new_state, event, details, timestamp, created_at
        FROM bot_ownership_history
        WHERE bot_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
    ''', (bot_id, limit))
    
    records = []
    for row in cursor.fetchall():
        records.append(OwnershipRecord(
            id=row[0],
            bot_id=row[1],
            pair=row[2],
            previous_state=row[3],
            new_state=row[4],
            event=row[5],
            details=row[6],
            timestamp=row[7],
            created_at=row[8]
        ))
    
    conn.close()
    return records


def get_all_active_ownerships() -> List[PairOwnership]:
    """Get all active ownership states across all pairs"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT DISTINCT pair FROM bot_ownership_state WHERE state IN (?, ?, ?)',
                   ('owner', 'passenger', 'pending_tp'))
    
    pairs = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    return [get_pair_ownership(pair) for pair in pairs]


def cleanup_stale_ownerships(max_age_seconds: float = 3600):
    """Clean up ownership records older than max_age for bots that are no longer active"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cutoff = time.time() - max_age_seconds
    
    cursor.execute('''
        UPDATE bot_ownership_state
        SET state = 'idle', is_owner = 0, position_size = 0, 
            entry_order_id = NULL, tp_order_id = NULL, owner_id = NULL
        WHERE last_updated < ? AND state IN ('owner', 'passenger', 'pending_tp')
        AND bot_id NOT IN (SELECT id FROM bots WHERE is_active = 1)
    ''', (cutoff,))
    
    count = cursor.rowcount
    conn.commit()
    conn.close()
    
    if count > 0:
        logger.info(f"Cleaned up {count} stale ownership records")
    
    return count


if __name__ == "__main__":
    # Initialize tables
    init_ownership_tables()
    
    # Test: Show all active ownerships
    print("\n=== ACTIVE OWNERSHIP STATUS ===")
    active = get_all_active_ownerships()
    
    for po in active:
        print(f"\n{po.pair}:")
        if po.owner:
            print(f"  OWNER: Bot {po.owner.bot_id} ({po.owner.bot_name}) - {po.owner.state.value}")
        else:
            print("  OWNER: None")
        
        for p in po.passengers:
            print(f"  PASSENGER: Bot {p.bot_id} ({p.bot_name}) - {p.state.value}")
    
    # Show ownership history for BTC bots
    print("\n=== OWNERSHIP HISTORY (BTC bots) ===")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT DISTINCT bot_id FROM bot_ownership_state WHERE pair LIKE 'BTC%'
    ''')
    btc_bots = [r[0] for r in cursor.fetchall()]
    conn.close()
    
    for bot_id in btc_bots[:5]:  # Show first 5
        print(f"\nBot {bot_id}:")
        history = get_ownership_history(bot_id, limit=10)
        for h in history:
            print(f"  {h.timestamp} | {h.event}: {h.new_state} | {h.details[:50]}")
