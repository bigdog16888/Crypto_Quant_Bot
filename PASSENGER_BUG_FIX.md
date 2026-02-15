# PASSENGER ORDER PLACEMENT - ANALYSIS REPORT
**Date**: 2026-02-10
**Status**: ✅ FIXED (Vulnerability Closed)

---

## 🛑 The Vulnerability
**Issue**: When a bot is "In Trade" but has **no ownership record** in the database (e.g., after a manual DB edit, migration error, or orphaned trade), the system defaulted `is_owner = True`.
**Consequence**:
1.  Bot A is the real owner.
2.  Bot B is a passenger but its ownership record is missing/corrupted.
3.  Bot B enters `process_bot` loop.
4.  `is_owner` defaults to `True`.
5.  Bot B executes missions (TP/Grid), potentially fighting Bot A.

## 🛡️ The Fix
We implemented a **Fundamental Safety Check** in `engine/bot_executor.py`.

**New Logic**:
If a bot is `IN TRADE` but `ownership` record is missing:
1.  **Check Pair Ownership**: Look up if *anyone else* owns this pair.
2.  **If Owner Exists (and it's not me)**:
    -   Force `is_owner = False`.
    -   Log a warning: `Defaulting to PASSENGER`.
3.  **If No Owner Exists**:
    -   Assume `is_owner = True` (Legacy/Recovery mode).
    -   Log a warning: `Defaulting to OWNER`.

## 📂 Files Modified
- `engine/bot_executor.py`: Implemented the fail-safe logic in `process_bot` and added locking to `execute_entry`.
- `engine/reconciler.py`: Updated `_attempt_adoption` to correctly set ownership state when recovering orphaned trades.
- `engine/ownership.py`: Updated `check_first_claim_policy` to include `PENDING_ENTRY` as a blocking state (preventing race conditions).

## 🚀 Status
The "Passenger Placing Orders" bug is fundamentally fixed. The system now defaults to safety (Passenger) when ambiguity exists.
