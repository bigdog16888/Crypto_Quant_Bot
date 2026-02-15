# Crypto Quant Bot - Session Recovery Notes
# Created: 2026-02-09 15:20 (Taiwan Time)
# Purpose: Track state for crash recovery

## SESSION CONTEXT

We were fixing bugs with the crypto quant bot. Key issues discovered:

### 1. MTF (Multi-Timeframe) Confluence Filter
- **Location**: `engine/strategies/martingale_strategy.py` lines 137-139, 210-216, 740-768
- **Issue**: MTF trend filter was blocking bot triggers
- **Current State**: ALL bots have `UseMTFConfluence=False` (already disabled)
- **TODO**: Add UI toggle for MTF in bot creation/management

### 2. Database/Exchange State Mismatch
- **Bot 43 (long btc price)**:
  - Exchange: LONG 0.002 BTC @ $70,475
  - DB trades: step=0, invested=$180, avg=$70,475
  - DB ownership: state="closed" (WRONG!)
  - DB bots: status="IN TRADE"
  - Open Orders: NONE (should have TP order!)
  
- **Bot 44 (gold long)**:
  - Exchange: LONG 0.011 XAU @ $5,026
  - DB trades: No record!
  - DB ownership: state="owner", owner_id=44
  
### 3. Fixes Applied This Session
1. Created diagnostic scripts: `tools/diagnostic_mtf.py`, `tools/quick_exchange.py`
2. Identified state inconsistencies
3. TODO: Sync ownership state with trades table

### 4. Previous Session Fixes (from FIXES_SUMMARY.md)
1. `update_martingale_step()` UPSERT logic - DONE
2. Position locks reverted - DONE  
3. iStochastic wrapper added - DONE
4. log_trade() parameter order - DONE

## NEXT STEPS
1. Sync Bot 43 ownership state to "owner"
2. Place TP order for Bot 43's position
3. Create trade record for Bot 44's XAU position
4. Add MTF toggle to UI
5. Run verification tests

## QUICK RECOVERY COMMANDS

```bash
# Check current state
cd C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot
python tools\diagnostic_mtf.py
python tools\quick_exchange.py

# Start bot
python main.py
```
