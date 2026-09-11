"""ETH orphan close — DRY-RUN mode (2026-09-04, Option A).

Shows the EXACT order the wrapper would place. No order is placed when
DRY_RUN=true (default). Read-only exchange GETs only.

Usage:
    py -3.10 scripts/close_eth_orphan.py            # dry-run (default)
    py -3.10 scripts/close_eth_orphan.py --execute  # real close (after operator GO)
"""
import sys
import time

sys.path.insert(0, ".")

from engine.exchange_interface import ExchangeInterface  # noqa: E402
from engine.parity_gates import _repair_client_order_id  # noqa: E402

PAIR = "ETH/USDC:USDC"


def main():
    dry = "--execute" not in sys.argv

    ex = ExchangeInterface()

    # FRESH position read (not the UI's stale frame)
    positions = ex.fetch_positions()
    eth_pos = None
    for p in positions:
        if "ETH" in str(p.get("symbol", "")):
            amt = float(p.get("contracts") or 0)
            if abs(amt) > 0:
                eth_pos = p
                break

    print("=" * 60)
    if not eth_pos:
        print("NO ETH POSITION ON EXCHANGE — nothing to close.")
        print("=" * 60)
        return 0

    qty = float(eth_pos.get("contracts"))
    entry = float(eth_pos.get("entryPrice") or 0)
    upnl = float(eth_pos.get("unrealizedPnl") or 0)
    side = "sell" if qty > 0 else "buy"
    close_qty = abs(qty)

    # Step-size rounding (same as the engine's own close paths)
    try:
        prec = ex.get_symbol_precision(PAIR)
        step = float(prec.get("amount_step", prec.get("step_size", 0)) or 0)
        if step > 0:
            rounded = ex.round_to_step(close_qty, step)
            if rounded != close_qty:
                print(f"[step-round] {close_qty} -> {rounded} (step {step})")
            close_qty = rounded
    except Exception as e:
        print(f"[step-round skipped: {e}]")

    client_id = _repair_client_order_id("CQB_ORPH", PAIR)

    print(f"DRY RUN: {dry}")
    print(f"Position : {eth_pos.get('symbol')} {qty:+.4f} (entry {entry:.2f}, uPnL {upnl:+.2f})")
    print(f"Order    : {PAIR} MARKET {side.upper()} {close_qty:.4f}")
    print(f"Params   : reduceOnly=True, clientOrderId={client_id}")
    print(f"Intent   : close the 0.901 ETH orphan (operator Option A, "
          f"approved 2026-09-04)")
    print("=" * 60)

    if dry:
        print("NO ORDER PLACED (dry-run). Re-run with --execute after GO.")
        return 0

    # ---- EXECUTE (only reached with --execute) ----
    print(f"[{time.strftime('%H:%M:%S')}] placing close order...")
    order = ex.create_order(
        symbol=PAIR,
        type="market",
        side=side,
        amount=close_qty,
        price=None,
        params={
            "reduceOnly": True,
            "clientOrderId": client_id,
            "human_approved": True,
        },
    )
    oid = order.get("id")
    print(f"placed: order_id={oid} status={order.get('status')} "
          f"filled={order.get('filled')} avg={order.get('average')}")

    # Poll fetch_order until closed (the button skips this; we don't)
    for i in range(30):
        time.sleep(2)
        try:
            o = ex.fetch_order(oid, PAIR)
        except Exception as e:
            print(f"poll {i}: fetch_order error {e}")
            continue
        st, fl, avg = o.get("status"), o.get("filled") or 0, o.get("average")
        print(f"poll {i}: status={st} filled={fl} avg={avg}")
        if st == "closed":
            print(f"✅ FILL CONFIRMED: {fl} {PAIR} @ {avg} (order {oid})")
            break
        if st in ("canceled", "rejected", "expired"):
            print(f"❌ terminal non-fill: {st} — STOP, report, do not retry.")
            return 1

    # Fresh post-close position read
    positions = ex.fetch_positions()
    eth_after = [p for p in positions if "ETH" in str(p.get("symbol", ""))
                 and abs(float(p.get("contracts") or 0)) > 0]
    if eth_after:
        print(f"⚠️ ETH STILL OPEN: {eth_after} — report, do not retry.")
        return 1
    print("✅ EXCHANGE FLAT: no ETH position remains.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
