# Follow-up Ticket: Structural Risk Analysis & Prevention for Hedged Pairs (INV-38)

## 📌 Context & Problem Statement
During the investigation of **INV-35** (SUI 0.5 dust lock), we identified a structural catch-22 pattern inherent in hedged bot topologies under Binance One-Way Netting mode:
1. Sibling bots (e.g., LONG and SHORT bots on the same pair) operate independently in the database ledger (`bot_orders` / `trades`).
2. When one direction (e.g., LONG) finishes its TP-ladder with an arithmetic dust remainder (due to rounding across partial fills), it attempts a fallback market close.
3. If the opposite-side sibling bot (e.g., SHORT) holds a larger position, the net physical position on the exchange is SHORT.
4. Because the net physical position is SHORT, the exchange rejects the LONG bot's `SELL` order with `reduceOnly=True` (error `-2022`).
5. When the bot retries without `reduceOnly=True`, the exchange rejects it because the quantity is below the minimum notional limit (~$5).
6. The bot is left in a silent "no-orders" trade state with an uncloseable position.

---

## ⚠️ High-Risk Pairs (Hedge Topology)
Our live database audit identified the following active pairs with opposing-direction sibling bots that are susceptible to this same lockup on high-volatility days:

| Pair | Active Sibling Bots | Risk Level |
| :--- | :--- | :--- |
| **SUIUSDC** | `sui long` (LONG) & `short sui` (SHORT) | **Triggered (SUI Bot 10018)** |
| **BTCUSDC** | `long btc price` (LONG) & `short btc` (SHORT) | **High** |
| **ETHUSDC** | `eth` (SHORT), `long eth` (LONG), `eth_hedge` (LONG) | **High** (3 active directions) |
| **SOLUSDC** | `sol` (LONG) & `short sol` (SHORT) | **High** |

---

## 🛠️ Proposed Prevention (INV-37 Integration)
To prevent these rounding remainders from accumulating, the next phase of development (**INV-37**) should implement live exchange-attributed position calculations:
1. **Live Position Attribution**:
   In `maintain_orders()`, when sizing the final TP/close orders, calculate the quantity using `get_bot_attributed_exchange_qty(bot_id, exchange, conn)` instead of local database arithmetic (`trades.open_qty - filled`).
2. **Formula**:
   $$Attributed\_Qty = Exchange\_Net\_Qty - \sum (Other\_Bots\_Open\_Qty)$$
3. **Execution**:
   By aligning the final exit size with the live exchange reality, any rounding differences introduced during partial fills are dynamically corrected in the last replacement order, preventing the residual dust from forming.

---

## 📋 Actions Required in INV-37
- [ ] Implement `get_bot_attributed_exchange_qty` in `engine/oneway_netting.py`.
- [ ] Update `bot_executor.py` to use exchange-attributed sizing for the final step of the TP ladder.
- [ ] Ensure that a bot never places a close order for a size larger than its exchange-attributed position.
- [ ] Add integration tests simulating partial fills across multiple cycles on a hedged pair (e.g., mock BTCUSDC) and verifying no dust is left.
