# Quick Investigation Checklist

## Pre-Flight (Before Any Investigation)
- [ ] Read `CRASH_INVESTIGATION_CONTEXT.md` if exists
- [ ] Check memory usage before starting
- [ ] Use `grep` instead of `read` for large files when possible
- [ ] Run one agent at a time instead of parallel

## The Issue (FIXED 2026-01-20)
> "5 running bots, but only 2 open orders on exchange"

**Root Cause**: First-Claim Policy was not enforced - all 5 BTC bots acted as owners.

**Solution**: New `engine/ownership.py` system with complete state machine.

## Quick System Status Check
```bash
# Navigate to project
cd C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot

# Check ownership status (quickest way to see bot states)
python test_ownership_system.py

# Check database diagnostic
python load_context.py --diagnostic

# View current ownership per pair
python -c "
from engine.ownership import get_all_active_ownerships
for po in get_all_active_ownerships():
    owner = po.owner.bot_id if po.owner else 'None'
    passengers = len(po.passengers)
    print(f'{po.pair}: Owner={owner}, Passengers={passengers}')
"
```

## Ownership System Quick Reference

### States
| State | Meaning | Can Place Orders? |
|-------|---------|-------------------|
| `IDLE` | No position | Yes (entry) |
| `PENDING_ENTRY` | Entry order placed | No |
| `OWNER` | Has position, manages it | Yes (TP/Grid) |
| `PASSENGER` | Monitors owner only | No |
| `PENDING_TP` | TP order placed | No |
| `CLOSED` | Position closed | No |

### Key Commands
```bash
# Check if a bot can claim ownership
python -c "
from engine.ownership import check_first_claim_policy
can, owner, msg = check_first_claim_policy(BOT_ID, 'PAIR')
print(msg)
"

# Force reconciliation
python -c "
from engine.ownership import reconcile_pair, get_all_active_ownerships
for po in get_all_active_ownerships():
    result = reconcile_pair(po.pair, po.exchange_position_exists)
    print(f'{po.pair}: {result}')
"

# View ownership history
python -c "
from engine.ownership import get_ownership_history
history = get_ownership_history(BOT_ID, limit=20)
for h in history:
    print(f'{h.event}: {h.previous_state} -> {h.new_state} | {h.details[:50]}')
"
```

## Common Issues & Solutions

### 1. Bot Not Placing TP Orders
**Symptom**: Bot has position but no TP order
**Check**:
```python
from engine.ownership import get_ownership_state, get_pair_ownership
state = get_ownership_state(BOT_ID)
print(f'State: {state.state.value}, Is Owner: {state.is_owner}')
pair_state = get_pair_ownership(state.pair)
print(f'Pair owner: {pair_state.owner.bot_id if pair_state.owner else None}')
```
**Likely Cause**: Bot is a PASSENGER - only OWNER places orders!

### 2. All Bots Acting as Owners
**Symptom**: Multiple bots on same pair all trying to place orders
**Fix**: Run the ownership fix script
```bash
python test_ownership_system.py
```

### 3. Owner Gone / Crashed
**Symptom**: No owner for a pair that has position
**Check**:
```bash
python -c "
from engine.ownership import get_pair_ownership, check_owner_failover
po = get_pair_ownership('BTC/USDC')
print(f'Owner: {po.owner}')
if not po.owner:
    new_owner = check_owner_failover('BTC/USDC')
    print(f'Failover: Bot {new_owner}')
"
```

### 4. Orphan Position (No Bot Tracking)
**Symptom**: Position exists on exchange but no bot claims it
**Check**:
```bash
python -c "
from engine.ownership import reconcile_pair, get_all_active_ownerships
for po in get_all_active_ownerships():
    if not po.owner and not po.passengers and po.exchange_position_exists:
        print(f'ORPHAN: {po.pair}')
"
```

## Files Reference
| File | Purpose |
|------|---------|
| `engine/ownership.py` | Complete ownership state machine |
| `engine/runner.py` | Entry/exit logic with ownership checks |
| `CRASH_INVESTIGATION_CONTEXT.md` | Detailed investigation notes |
| `SETUP_GUIDE.md` | Multi-machine deployment guide |

## Don't Forget
- [ ] Document findings in `CRASH_INVESTIGATION_CONTEXT.md`
- [ ] Update this checklist with new findings
- [ ] Push changes to GitHub for cross-machine sync
