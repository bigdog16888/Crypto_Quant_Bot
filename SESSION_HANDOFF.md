# Session Handoff (Effective 2026-03-20)

## State of the System
The bot is running in **Version 1.4.8**.
The workspace is in a **CLEAN START** state — DB wiped, all bots on Step 0.

## Actions Taken Today
1. **`abs_amount` NameError** (`database.py:1298`): `abs_amount` → `abs(amount)`. Active positions snapshot was silently crashing and rolling back on every cycle. Now fixed.
2. **DUST_CHASER CID > 36 chars** (`bot_executor.py:1491`): Replace `_generate_deterministic_id` with compact `CQB_{bot_id}_DUST_{step}`. Keeps per-bot tracking. Was rejecting with Binance API `-4015` every cycle.
3. **BTC ENTRY permanently BLOCKED** (`bot_executor.py:591`): `phys_net_usd = size * current_price` used signed CCXT size (negative for SHORT). Changed to `abs(size) * price` vs `abs(sib_net_usd)` magnitude comparison. Was blocking all LONG entries when a SHORT sibling was on the exchange.
4. **Min Notional hardcoded** (`exchange_interface.py:570`): Replaced `100 if DEMO else 5` with a per-symbol dynamic fetch from the already-populated `_exchange_info_cache`. Correct for every pair on both Demo and Mainnet.

## Directives for Next Session
1. Restart the engine and confirm:
   - `Active Positions Synced: N` in logs (no more rollbacks from Bug 1)
   - DUST_CHASER orders place without `-4015`
   - BTC LONG bot places entry alongside SHORT sibling without CRITICAL block
   - SOL/ETH TP uses correct per-pair min_notional from exchange
2. Monitor margin — 9+ bots on $5,000 may still hit `Margin is insufficient`. Reduce bot count or add capital if needed.

The system is sealed and ready for restart.
