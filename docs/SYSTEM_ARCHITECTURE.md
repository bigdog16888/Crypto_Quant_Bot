# System Architecture & Operations Guide

## Table of Contents
1. [Multi-Bot Position Management](#multi-bot-position-management)
2. [Ownership State Machine](#ownership-state-machine)
3. [System Persistence](#system-persistence)
4. [State Recovery](#state-recovery)
5. [Troubleshooting](#troubleshooting)

---

## Multi-Bot Position Management

### The Problem
When multiple bots trade the same pair (e.g., 5 bots all on BTC/USDT), the exchange returns **one aggregate position**, not separate positions per bot:

```
Exchange View:
  Position: 1.5 BTC LONG @ $43,250 (combined position)

Your Bots:
  Bot A: Think they own 0.5 BTC
  Bot B: Think they own 0.5 BTC  
  Bot C: Think they own 0.5 BTC
  Bot D: Think they own 0.5 BTC (waiting to enter)
  Bot E: Think they own 0.5 BTC (waiting to enter)
```

### The Solution: First-Claim Ownership Policy

The system uses **Order ID tracking** and a complete **Ownership State Machine** to manage multi-bot positions:

1. **Order IDs**: Every order placed by a bot is tracked in `bot_orders` table
2. **First Claim**: The bot that places the **first entry order** on a pair becomes the "owner"
3. **Passengers**: Other bots on the same pair become "passengers" - they monitor but don't manage the position
4. **Ownership Transfer**: Only the owner can modify/close the position

```
Ownership Hierarchy:
  ┌─────────────────────────────────────────┐
  │         BTC/USDT Position               │
  │         1.5 BTC LONG @ $43,250          │
  └─────────────────────────────────────────┘
                    │
        ┌───────────┴───────────┐
        │                       │
   ┌────▼────┐            ┌─────▼─────┐
   │ BOT A   │            │ Bot B     │
   │ OWNER   │            │ PASSENGER │
   │ 0.5 BTC │            │ 0.5 BTC   │
   └─────────┘            └───────────┘
        │
   Manages position
   Places/closes orders
   Others follow passively
```

### How It Works

1. **Bot A enters first**:
   - Places entry order → Order ID saved to `bot_orders` table
   - Order fills → Bot A calls `claim_ownership()`
   - Bot A becomes OWNER, DB updated: `total_invested`, `avg_entry_price`, etc.
   - Ownership recorded in `bot_ownership_state` table

2. **Bot B sees position**:
   - Calls `check_first_claim_policy()` → Bot A already owns!
   - Bot B becomes PASSENGER via `become_passenger()`
   - Bot B only monitors, doesn't place orders

3. **Position Closure**:
   - When OWNER closes position (TP hit or manual)
   - `handle_position_closed()` called
   - ALL bots (owner + passengers) reset to CLOSED
   - Next entry restarts ownership cycle

---

## Ownership State Machine

### States
| State | Meaning | Can Place Orders? |
|-------|---------|-------------------|
| `IDLE` | No position, waiting for entry signal | Yes (entry) |
| `PENDING_ENTRY` | Entry order placed, waiting for fill | No |
| `OWNER` | Has position, manages it (places TP/grid) | Yes (TP/Grid) |
| `PASSENGER` | Monitors owner's position only | No |
| `PENDING_TP` | TP order placed, waiting for hit | No |
| `CLOSED` | Position closed, in cooldown | No |

### State Transitions

```
┌─────────┐     ENTRY_SIGNAL      ┌───────────────┐
│  IDLE   │────────────────────▶  │ PENDING_ENTRY │
└─────────┘                       └───────────────┘
      │                                 │
      │ ENTRY_FILLED                    │ ENTRY_FILLED
      ▼                                 ▼
┌─────────┐     OWNER_CLAIMED    ┌───────────────┐
│ CLOSED  │◀─────────────────────│    OWNER      │◀─────┐
└─────────┘                       └───────────────┘      │
      ▲                                 │               │
      │                                 │ PASSENGER     │ OWNER_GONE
      │                                 ▼               │
      │                         ┌───────────────┐       │
      └─────────────────────────│  PASSENGER    │───────┘
            COOLDOWN_COMPLETE   └───────────────┘
                                      │
                                      │ TP_HIT/STOP_HIT/MANUAL_CLOSE
                                      ▼
                               ┌───────────────┐
                               │    CLOSED     │
                               └───────────────┘
```

### Events
| Event | Triggered When |
|-------|----------------|
| `ENTRY_SIGNAL` | Strategy gives entry signal |
| `ENTRY_FILLED` | Entry order is filled on exchange |
| `OWNER_CLAIMED` | First bot to enter claims ownership |
| `PASSENGER_JOINED` | Bot joins as passenger (another owns pair) |
| `TP_HIT` | Take profit order is hit |
| `STOP_HIT` | Stop loss is hit |
| `MANUAL_CLOSE` | User manually closes position |
| `OWNER_GONE` | Owner disappears/crashes (failover) |
| `COOLDOWN_COMPLETE` | Re-entry cooldown period ends |

### Owner Failover
If the OWNER crashes or disappears:
1. `check_owner_failover()` is called during reconciliation
2. Oldest PASSENGER (by `basket_start_time`) is promoted to new OWNER
3. New owner must place new TP order (old one was on crashed bot)

### Database Tables

```sql
-- Current ownership state per bot
CREATE TABLE bot_ownership_state (
    bot_id PRIMARY KEY,
    state TEXT NOT NULL,           -- IDLE, OWNER, PASSENGER, CLOSED, etc.
    is_owner INTEGER,              -- 1 if owner, 0 if passenger
    pair TEXT,
    position_size REAL,
    avg_entry_price REAL,
    target_tp_price REAL,
    basket_start_time INTEGER,
    entry_order_id TEXT,
    tp_order_id TEXT,
    owner_id INTEGER,              -- For passengers: who owns the pair
    last_updated INTEGER
);

-- Complete audit trail
CREATE TABLE bot_ownership_history (
    id PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER,
    pair TEXT,
    previous_state TEXT,
    new_state TEXT,
    event TEXT,
    details TEXT,
    timestamp INTEGER
);
```

### Key Functions

```python
from engine.ownership import (
    # Ownership checks
    check_first_claim_policy(bot_id, pair) -> (can_claim, existing_owner_id, message)
    
    # State transitions
    claim_ownership(bot_id, name, pair, entry_order_id, entry_price, amount, tp_price)
    become_passenger(bot_id, name, pair, entry_order_id, entry_price, amount, tp_price, owner_id)
    handle_position_closed(bot_id, pair, close_type, exit_price)
    
    # Reconciliation
    check_owner_failover(pair) -> new_owner_id or None
    reconcile_pair(pair, exchange_position_exists) -> dict with actions taken
    get_pair_ownership(pair) -> PairOwnership object
    get_ownership_state(bot_id) -> BotOwnership object
    
    # History
    get_ownership_history(bot_id, limit) -> list of OwnershipRecord
    get_all_active_ownerships() -> list of PairOwnership
)

from engine.runner import (
    # Called in _finalize_entry() after entry fills
    _finalize_entry(bot_id, name, pair, side, amount, fill_price, order_id)
        → Calls claim_ownership() or become_passenger()
    
    # Called each cycle
    _reconcile_ownership()
        → Calls reconcile_pair() for all active pairs
        → Handles owner failover
        → Cleans up stale ownership records
)
```

---

## System Persistence

### The Problem
The bot runs as a Python process on your local computer. If you:
- Turn off your computer
- Log out of Windows
- Close the terminal
- Computer crashes

...the bot **stops running**.

### The Solution: Windows Service (NSSM)

We use **NSSM (Non-Sucking Service Manager)** to run the bot as a Windows Service:

```
Benefits:
✅ Auto-starts when Windows boots (even before you log in)
✅ Auto-restarts if the bot crashes
✅ Runs in background - no console window
✅ Managed like other Windows services
✅ Logs are automatically rotated
```

### Installation

1. **Install NSSM** (once):
   ```
   # Option A: Chocolatey (recommended)
   choco install nssm

   # Option B: Download manually
   # Download from https://nssm.cc/download
   # Extract to C:\nssm\nssm.exe
   ```

2. **Install the bot as a service**:
   ```bash
   cd C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot
   python service_manager.py install
   ```

3. **Choose startup mode**:
   - `Manual` (default): You start the service when you want
   - `Auto`: Service starts automatically with Windows

### Service Management

```bash
# Start the service
net start CryptoQuantBot

# Stop the service
net stop CryptoQuantBot

# Check status
sc query CryptoQuantBot

# View logs
python service_manager.py logs

# Edit service settings (GUI)
nssm edit CryptoQuantBot

# Remove the service
python service_manager.py remove
```

### What Happens When...

| Event | With Service | Without Service |
|-------|-------------|-----------------|
| Computer boots | ✅ Bot auto-starts | ❌ Bot doesn't run |
| You log out | ✅ Bot continues | ❌ Bot stops |
| Power outage | ✅ Bot auto-restarts | ❌ Bot stops |
| Bot crashes | ✅ Auto-restarts | ❌ Stays stopped |
| Windows update | ✅ Auto-restarts | ❌ Stops |

---

## State Recovery

### The Recovery Process

When the bot starts (after shutdown or crash), it performs **comprehensive state reconciliation**:

```
Startup Sequence:
┌─────────────────────────────────────────────────────────┐
│ 1. FETCH EXCHANGE STATE                                 │
│    - Get all positions from exchange                    │
│    - Get all open orders from exchange                  │
│    → This is the "source of truth"                      │
└─────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 2. FETCH DATABASE STATE                                 │
│    - Get all bot states from SQLite                     │
│    - Get all tracked order IDs                          │
│    - Check trade history for entry confirmations        │
└─────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 3. MATCH ORDERS TO BOTS                                 │
│    - Match exchange orders to bots via Order ID         │
│    - If order ID matches → bot owns that order          │
│    - If no match → orphan order                         │
└─────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 4. RECONCILE STATES                                     │
│    - Detect mismatches                                   │
│    - Take appropriate recovery actions                  │
│    - Log all actions for audit                          │
└─────────────────────────────────────────────────────────┘
```

### Reconciliation Scenarios

#### Scenario A: Bot thinks IN TRADE, Exchange has NO position
```
Cause: Position closed while bot was offline (TP hit or manual close)

Action:
  1. Check if entry was confirmed in trade_history
  2. If YES: Mark as TP hit, calculate PnL, reset to IDLE
  3. If NO: Mark as "ghost trade", reset to IDLE
```

#### Scenario B: Bot thinks IDLE, Exchange HAS position
```
Cause: Position opened while bot was offline (manual trade or other bot)

Action:
  1. Check if this bot's Order ID matches exchange orders
  2. If YES (this bot owns it): Claim position, continue trading
  3. If NO (another bot owns it): Become passenger, monitor only
  4. If NO match at all: Flag as ORPHAN, require manual review
```

#### Scenario C: Both in trade - verify ownership
```
Cause: Normal state

Action:
  1. Verify Order ID ownership
  2. If OWNER: Continue normal operation
  3. If PASSENGER: Monitor only, don't place orders
```

#### Scenario D: Orphan position detected
```
Cause: Position on exchange but no bot claims it

Action:
  1. Log critical warning
  2. Require manual intervention
  3. Options:
     a) Manually close position on exchange
     b) Create new bot to claim position
     c) Delete orphan if from old test
```

### Manual Recovery Commands

```bash
# Run reconciliation manually
cd C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot
python -c "from engine.reconciliation import sync_all_bots; sync_all_bots()"

# Check for orphan positions
python -c "from engine.reconciliation import get_orphan_positions; print(get_orphan_positions())"

# Reset a ghost trade (clears corrupted state)
# Use the cleanup script
python cleanup_ghost_trades.py
```

---

## Troubleshooting

### Common Issues

#### "CRITICAL Mismatch: DB says IDLE, but Exchange has ACTIVE POSITION"

**Meaning**: There's a position on exchange that your bot doesn't know about.

**Solutions**:
1. **If manual trade**: Close it manually on exchange, then reset bot
2. **If from another bot**: This bot will become a passenger
3. **If unexpected**: Run reconciliation to investigate

```bash
# View current state
python -c "
from engine.reconciliation import sync_all_bots
results = sync_all_bots()
for r in results:
    print(f'{r.bot_name}: {r.position_owner.value} - {r.details}')
"
```

#### "Ghost Trade" warnings

**Meaning**: Bot database shows invested money but no entry confirmation.

**Solution**:
```bash
# Reset the corrupted bot
# Find bot ID from the warning message
# Then manually reset:
sqlite3 crypto_bot.db
UPDATE trades SET current_step=0, total_invested=0, avg_entry_price=0, target_tp_price=0 WHERE bot_id=X;
```

#### Position size doesn't match

**Meaning**: Bot thinks it has 0.5 BTC but exchange shows 1.5 BTC.

**Cause**: Multiple bots on same pair, combined position.

**Solution**: This is expected! Use First-Caim policy:
- First bot to enter owns the position
- Other bots become passengers
- Position size is correct (combined)

### Log Files

```
logs/
├── engine.log          # Main trading engine logs
├── service_stdout.log  # Service output
├── service_stderr.log  # Service errors
└── reconciliation_*.log # Reconciliation events
```

### Monitoring Commands

```bash
# Check service status
sc query CryptoQuantBot

# View recent logs
python service_manager.py logs

# Check exchange sync status
# Look at engine.log for "StateSync" entries

# View all bot states
python -c "
from engine.database import get_all_bots, get_bot_status
for bot in get_all_bots():
    print(f'Bot {bot[1]}: {bot[2]} - Invested: ${bot[5]}')
"
```

---

## Best Practices

### For Multiple Bots on Same Pair

1. **Coordinate entry timing**: If you want a specific bot to own the position, start that bot first
2. **Similar configurations**: Bots on same pair should have similar settings to avoid conflicts
3. **Monitor ownership**: Check reconciliation logs to understand who owns what

### For System Reliability

1. **Use Windows Service**: Always run the bot as a service for production trading
2. **Monitor logs**: Check logs regularly, especially after restarts
3. **Regular reconciliation**: Run `sync_all_bots()` periodically if not using service
4. **Test first**: Always test new strategies in DRY_RUN mode

### For Safety

1. **Emergency controls**: Use the web UI's emergency close button
2. **Position limits**: Set reasonable `base_size` and `max_steps`
3. **Circuit breaker**: Monitor for drawdown warnings
4. **Manual review**: Review orphan positions promptly

---

## Architecture Summary

```
┌────────────────────────────────────────────────────────────────────────┐
│                    CRYPTO QUANT BOT SYSTEM                             │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  ┌──────────────────┐         ┌──────────────────────────────────┐    │
│  │   WINDOWS        │         │          EXCHANGE               │    │
│  │   SERVICE        │◄───────►│   (Single Source of Truth)      │    │
│  │   (NSSM)         │  API    │   - Positions                   │    │
│  │   - Auto-start   │         │   - Open Orders                 │    │
│  │   - Auto-restart │         │   - Balances                    │    │
│  └────────┬─────────┘         └──────────────────────────────────┘    │
│           │                                                         │
│           ▼                                                         │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │                    BOT RUNNER                                │    │
│  │   - Main loop                                               │    │
│  │   - Bot processing                                          │    │
│  │   - Order management                                        │    │
│  └────────┬────────────────┬──────────────────────────────────┘    │
│           │                │                                       │
│           ▼                ▼                                       │
│  ┌────────────────┐  ┌──────────────────────────────────────┐      │
│  │ RECONCILIATION │  │           DATABASE                   │      │
│  │ (reconciliation│  │   (SQLite)                           │      │
│  │  .py)          │  │   - bots table                       │      │
│  │  - State sync  │  │   - trades table                     │      │
│  │  - Position    │  │   - bot_orders table  ◄── Order IDs │      │
│  │    ownership   │  │   - trade_history table              │      │
│  └────────────────┘  └──────────────────────────────────────┘      │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Quick Reference

| Command | Purpose |
|---------|---------|
| `python service_manager.py install` | Install as Windows service |
| `python service_manager.py start` | Start the service |
| `python service_manager.py stop` | Stop the service |
| `python service_manager.py logs` | View recent logs |
| `net start CryptoQuantBot` | Start service (Windows) |
| `net stop CryptoQuantBot` | Stop service (Windows) |
