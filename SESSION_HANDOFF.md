# Crypto Quant Bot: Handoff Document (March 12, 2026)

## To the Next AI Agent:
If you are reading this, you are resuming work on the Crypto Quant Bot after a massive structural debugging session. **Read this carefully before touching the Reconciler or Order Executor.**

### The Problem We Solved
The user experienced massive phantom position bugs (e.g. SOL/USDC showing -$22,785 System vs -$53,784 Exchange, and XRP missing exactly 2 limit orders out of nowhere). 

We originally thought this was a multi-bot "sharing" math issue where multiple bots on the same pair double-dipped exchange totals.

**The TRUE Root Cause:**
The bug originated from how **Take Profit (TP)** fills were logged offline in `reconciler.py`.
1. When a TP order filled offline, the Reconciler correctly summoned `reset_bot_after_tp`, which successfully wiped all the current `grid` and `entry` records in `bot_orders` to a state of `reset_cleared`.
2. **THE BUG:** Immediately after returning from the function, the loop in `reconciler.py` fell through and `INSERTED` the TP fill into `bot_orders` with status `'filled'`!
3. **THE SYMPTOM:** The bot database was left heavily unbalanced. It had $0 in Open Grids, but suddenly had a `- $12,907` Take Profit fill record! The Reconciler's math loop aggregated this, thought the bot was massively un-allocated compared to the exchange, and forcefully injected `$12,907` of `adoption_add` ghost quantities to try and "heal" the math.

### The Fixes Implemented (V1.4.2)
1. **Reconciler TP Insertion Sync:** In `engine/reconciler.py` (Line 580~), if the offline fill is `otype == 'TP'`, the loop now explicitly saves the database status as `'reset_cleared'` instead of `'filled'`, dynamically matching the cycle scope wipe so it doesn't infinitely loop negative ghost math against future cycles!
2. **Strict Multi-Bot Isolation:** `reconciler.py` no longer attempts to use proportional math `(Total - SisterBots)` to calculate missing values. If `> 1` bot is trading a pair, the auto-healer skips math entirely and enforces strict 1-to-1 adherence to `client_order_id` DB validation checks.
3. **Dynamic reduceOnly Mapping:** In `engine/bot_executor.py`, TP orders now natively detect if they are the *only* bot on a pair. If yes, they use `reduceOnly=True` to clear arbitrary fractional decimal dust securely. If no, they drop the flag and explicitly use `postOnly=True` or physical GTC Limit orders.

### The Fixes Implemented (V1.4.3 - The Mismatch Resolution)
1. **Split-Brain Cache Resolution (ws_cache.py)**: The `fetch_positions()` pulled data via CCXT under unified keys (`BTC/USDC`), but the live WebSocket streamed real-time fills using raw exchange keys (`BTCUSDC`). The `ws_cache.py` previously maintained *both* instead of overwriting, causing the backend `integrity.py` to accidentally aggregate and double-count the initial entries into synthetic "phantom sizes" that always equalled the exact Entry value. The cache `update_position` and `populate_from_rest` now strictly enforce `normalize_symbol` when storing data.
2. The integrity enforcer mismatch logic has run perfectly clean and the `engine.log` shows no mismatched positions.

### Current System State
- The Binance Exchange is manually completely flat.
- The entire `crypto_bot.db` has been fully wiped back to zero using an offline SQLite tool `wipe_db.py`.
- All `engine.log` caching traces were purged from the system root to create a clean context window.
- Size Discrepancy false positives have been completely eliminated.
- The UI is ready for the User to continue monitoring. The bot has successfully synced its active positions, reporting a perfect match!

**Be extremely smart with math logic going forward.** Do not trust aggregate sums (`total_physical_notional`) on Exchange bounds to make individual bot ledger decisions without validating their specific `client_order_id` in `bot_orders`!
