# Virtual Position System (VPS) Architecture

## Core Philosophy
The Virtual Position System (VPS) is designed to allow multiple independent bots to trade on the same pair (e.g., BTC/USDT) in **One-Way Mode** without interfering with each other's logical state, while maintaining a strict mathematical link to the physical exchange reality.

### 1. The Two Truths
-   **Physical Truth (Exchange)**: The net position size and side on the exchange is the ultimate physical reality. If the exchange says net position is 0, the sum of all bots must mathematically equal 0.
-   **Logical Intent (Database)**: The database tracks the "Intent" of individual bots (e.g., Bot A is at Step 3 of a Long Martingale). This intent is mapped to "Virtual Positions."

### 2. The Verification Contract (Double-Check Both Ways)
Reconciliation is not a secondary cleanup; it is a fundamental verification of every move.
-   **Intent -> Reality**: Every time a bot wants to move (place an order), it must verify that its current database state matches its traceable history on the exchange.
-   **Reality -> Intent**: Periodic reconciliation (Deep Sync) scans the physical reality and *reconstructs* the logical intent by replaying traceable fills.

### 3. Traceability (No "Ghost" Wipes)
-   **No "Make-Up" Trades**: The system never creates artificial trade records to match a position.
-   **No "Blind Deletes"**: If a bot has a position in the DB but 0 on the exchange, the system must **prove** the closure by finding the specific Order ID/Fill in the history before resetting.
-   **Order ID Tie-Back**: Every order created by the VPS uses a deterministic `clientOrderId` (e.g., `CQB_BOTID_TYPE_STEP_TIMESTAMP`). This is the "DNA" that allows the system to trace every fill back to a specific bot and strategy move.

### 4. Robustness Principles
-   **API Reliability**: API components are hardened to handle failures (Timeouts/4xx/5xx) by retrying or entering a safe diagnostic mode, rather than crashing the core event loop.
-   **Source of Truth Hierarchy**:
    1.  **Confirmed Fills**: The primary link between intent and reality.
    2.  **Physical Net Position**: The final constraint on total exposure.
    3.  **Bot Database**: The local cache of strategy state.

## Operational Flow
1.  **Cycle Start**: Runner fetches "Physical Truth" (Net Position) and "Recent History."
2.  **Trace**: `StateReconciler` parses history. If it finds a fill for `CQB_1_GRID_3`, it *must* update Bot 1 to Step 3.
3.  **Cross-Verify**: Sum of Virtual Positions (Bot 1 + Bot 2 + ...) is compared to Physical Net.
4.  **Resolve**: If a deviation exists, the system performs a "Deep Audit" of history to find missing fills rather than simply resetting.
