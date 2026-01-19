# Phase 6: Advanced Bot Management & Configuration

## Goal Description
Address user feedback by implementing a "Bot Manager" (to delete/toggle bots) and exposing "Pro" settings (Dollar TP, Hedging) in the UI.

> [!IMPORTANT]
> This phase also involves updating the `runner.py` to **act** on these new settings (Monitoring TP, placing Grid orders), moving beyond simple "Entry" logic.

## Proposed Changes

### 1. Bot Manager UI
#### [NEW] [ui/views/bot_manager.py](file:///d:/Crypto_Quant_Bot/ui/views/bot_manager.py)
- **Table View**: List all bots with status, pair, PNL (simulated).
- **Controls**:
    - `Toggle Active`: Switch 0/1 in DB.
    - `Delete`: Remove bot (and history?) from DB.
    - `Edit Strategy`: (Future scope, maybe just JSON edit for now).

### 2. Advanced Configuration
#### [MODIFY] [ui/views/bot_creator.py](file:///d:/Crypto_Quant_Bot/ui/views/bot_creator.py)
- **Profit Settings**: Add "Take Profit ($)" vs "Take Profit (%)".
- **Hedging**: Add "Hedge Multiplier", "Hedge Max Trades".
- **Grid settings**: Expose "PipStep" explicitly.

### 3. Execution Engine Upgrade (Logic)
#### [MODIFY] [engine/runner.py](file:///d:/Crypto_Quant_Bot/engine/runner.py)
- **Exit Logic**: Check if `current_price` hits `target_tp_price` -> Execute Sell/Close.
- **Grid Logic**: Check if `current_price` drops `grid_dist` -> Execute Martingale Buy.
- **Hedge Logic**: Check if `drawdown` > `hedge_start` -> Open opposite trade.

## Verification Plan
1.  **Manager**: Create 3 bots, Delete 1, Toggle 1 off. Verify DB.
2.  **Config**: Create bot with "$10 Profit Target". Verify JSON in DB.
3.  **Runner**: Simulate price move to TP. Verify "Close" signal in log.
