# Crypto Bot Crash Investigation - Session Context

## Current Investigation Status: ✅ FIXED (2026-01-20)

### Problem Statement (RESOLVED)
**Original Issue**: "Showing 5 running bots, but in open orders there's only 2"

### Root Cause Identified & Fixed
- All 5 BTC bots violated First-Claim Policy by acting as owners simultaneously
- Only the first bot to enter should be OWNER; others should be PASSENGERS
- Solution: New `engine/ownership.py` system with complete state machine

### Solution Implemented

#### 1. New Ownership State Machine (`engine/ownership.py`)
```
States: IDLE → PENDING_ENTRY → OWNER/PASSENGER → CLOSED → IDLE
Events: ENTRY_SIGNAL, ENTRY_FILLED, OWNER_CLAIMED, PASSENGER_JOINED, 
        TP_HIT, STOP_HIT, MANUAL_CLOSE, OWNER_GONE, COOLDOWN_COMPLETE
```

#### 2. Key Functions Added
| Function | Purpose |
|----------|---------|
| `check_first_claim_policy()` | Enforces only one owner per pair |
| `claim_ownership()` | Called when entry order fills |
| `become_passenger()` | Other bots become passengers |
| `handle_position_closed()` | Resets ALL bots when position closes |
| `check_owner_failover()` | Promotes oldest passenger if owner crashes |
| `reconcile_pair()` | Runs each cycle to fix ownership issues |

#### 3. Database Tables Added
```sql
-- bot_ownership_state: Current ownership per bot
CREATE TABLE bot_ownership_state (
    bot_id PRIMARY KEY, state, is_owner, pair, position_size,
    avg_entry_price, target_tp_price, basket_start_time,
    entry_order_id, tp_order_id, owner_id, last_updated
)

-- bot_ownership_history: Complete audit trail
CREATE TABLE bot_ownership_history (
    id, bot_id, pair, previous_state, new_state, event, details, timestamp
)

-- active_positions: Exchange position tracking
CREATE TABLE active_positions (pair PRIMARY KEY, side, size, entry_price, ...)
```

### Current Bot Status (After Fix)
```
BTC/USDC:
  🏆 OWNER: Bot 32 (btc) - $186.60 @ $90973.50, TP: $89608.90
  👥 PASSENGERS: Bots 33, 34, 36, 39 (monitoring only, no orders)
  
ETH/USDC:
  🏆 OWNER: Bot 5 (long eth) - $99.00 @ $3186.48, TP: $3234.28
  
BNB/USDC:
  🏆 OWNER: Bot 6 (long bnb) - $83.97 @ $928.19, TP: $942.11
```

### Files Modified
| File | Changes |
|------|---------|
| `engine/ownership.py` | NEW - Complete ownership system |
| `engine/runner.py` | Integrated ownership system, First-Claim enforcement |
| `config/settings.py` | Added STABLECOINS import |
| `docs/SYSTEM_ARCHITECTURE.md` | Updated with new ownership documentation |

### Scenarios Now Handled
| Scenario | Behavior |
|----------|----------|
| **First-Claim Policy** | Only first bot becomes OWNER; others become PASSENGERS |
| **Owner TP Finishes** | ALL bots (owner + passengers) reset to CLOSED |
| **Owner Crashes** | Oldest PASSENGER auto-promoted to new OWNER |
| **Owner Manual Close** | All passengers detect and reset |
| **Re-entry After Close** | Proper cooldown, new ownership claim |

### Critical Code Paths (Updated)

#### Path 1: Entry Order Filled
```
runner.py:_finalize_entry()
  → claim_ownership(bot_id, bot_name, pair, entry_order_id, ...)
  → check_first_claim_policy() → OWNER or PASSENGER
  → update_ownership_state()
  → record_ownership_event()  # Audit trail
```

#### Path 2: Order Management
```
runner.py:execute_mission('maintain_orders')
  → check_first_claim_policy()
  → If PASSENGER: Skip TP order placement (owner handles it)
  → If OWNER: Place/manage TP orders normally
```

#### Path 3: Position Close
```
handle_position_closed(bot_id, pair, 'tp_hit')
  → Find ALL bots tracking this pair
  → Reset each to CLOSED state
  → Record event for each bot
```

#### Path 4: Cycle Reconciliation
```
runner.py:run_cycle() → _reconcile_ownership()
  → get_all_active_ownerships()
  → reconcile_pair(pair, exchange_position_exists)
  → check_owner_failover() if needed
  → cleanup_stale_ownerships()
```

### Session Resume Checklist
When resuming after computer restart:

```bash
# 1. Navigate to project
cd C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot

# 2. Restore Python environment
.\venv\Scripts\activate  # or source venv/bin/activate

# 3. Check current state
python test_ownership_system.py

# 4. Start the bot
python engine/runner.py

# 5. Monitor logs
tail -f logs/bot.log  # Linux/Mac
Get-Content logs/bot.log -Wait  # PowerShell
```

### Quick Diagnostic Commands
```bash
# Check ownership status
python test_ownership_system.py

# Check database state
python load_context.py --diagnostic

# View ownership history
python -c "
from engine.ownership import get_ownership_history, get_all_active_ownerships
for po in get_all_active_ownerships():
    print(f'{po.pair}: Owner={po.owner.bot_id if po.owner else None}')
"

# Run reconciliation manually
python -c "
from engine.ownership import reconcile_pair, get_all_active_ownerships
for po in get_all_active_ownerships():
    result = reconcile_pair(po.pair, po.exchange_position_exists)
    print(result)
"
```

### Multi-Machine Setup
Each machine needs:
1. **Same code**: Clone repository
2. **Different .env**: Machine-specific API keys (see SETUP_GUIDE.md)
3. **Shared database**: OR separate databases per machine

See `SETUP_GUIDE.md` for cross-machine deployment instructions.

---
*Last Updated: 2026-01-20 16:10*
*System Status: OPERATIONAL*
