#!/usr/bin/env python3
"""
One-shot startup heal — NO trading loop, NO new orders.

Run this when you only have Streamlit open and need the same fixes as
engine/runner.py startup_sync without clicking "Start Monitoring".

Usage (from project root):
    python scripts/run_startup_heal.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()


def main():
    print("=" * 60)
    print("CRYPTO QUANT BOT — STARTUP HEAL (no trading loop)")
    print("=" * 60)

    from engine.database import (
        init_db,
        heal_inflated_filled_amounts,
        consolidate_duplicate_bot_orders,
        verify_filled_orders_against_exchange,
        audit_pair_ledger_vs_exchange,
        sync_trades_from_orders,
        get_pair_virtual_net,
        get_connection,
    )
    from engine.exchange_interface import ExchangeInterface, normalize_symbol

    init_db()

    print("\n[1/5] Cap inflated filled_amount rows...")
    n = heal_inflated_filled_amounts()
    print(f"      healed {n} row(s)")

    print("\n[2/5] Consolidate duplicate client_order_id rows...")
    n = consolidate_duplicate_bot_orders()
    print(f"      merged {n} group(s)")

    print("\n[3/5] Connect exchange + verify fills vs Binance...")
    ex = ExchangeInterface()
    n = verify_filled_orders_against_exchange(ex)
    print(f"      updated {n} order(s) from exchange")

    print("\n[4/5] Offline fill reconstruction (may take 1-2 min)...")
    try:
        from engine.reconciler import StateReconciler
        rec = StateReconciler(exchanges={"future": ex})
        stats = rec.reconstruct_offline_fills(since_hours=48)
        print(f"      {stats}")
    except Exception as e:
        print(f"      WARN: reconstruct_offline_fills failed: {e}")

    print("\n[5/5] Seal all active bots from proof ledger...")
    from engine.ledger import seal_all_active_bots
    corrected = seal_all_active_bots()
    print(f"      sealed; {corrected} bot(s) corrected")

    conn = get_connection()
    for bid_row in conn.execute("SELECT id FROM bots WHERE is_active=1"):
        sync_trades_from_orders(bid_row[0])

    print("\n" + "=" * 60)
    print("PAIR PARITY (virtual vs live exchange)")
    print("=" * 60)
    mismatches = audit_pair_ledger_vs_exchange(ex)
    pairs = conn.execute(
        "SELECT DISTINCT pair FROM bots WHERE is_active=1"
    ).fetchall()
    for (pair,) in pairs:
        norm = normalize_symbol(pair).upper()
        v = get_pair_virtual_net(pair)
        phys = 0.0
        for pos in ex.fetch_positions() or []:
            if normalize_symbol(pos.get("symbol", "")).upper() == norm:
                phys += float(pos.get("contracts", 0) or pos.get("size", 0) or 0)
        ok = abs(v - phys) < 0.001
        flag = "OK" if ok else "MISMATCH"
        print(f"  [{flag}] {norm}: virtual={v:+.4f}  exchange={phys:+.4f}  delta={phys - v:+.4f}")

    if mismatches:
        print("\nACTION REQUIRED before Start Monitoring:")
        print("  These pairs still disagree after proof-based heal.")
        for pair, v, ph, d in mismatches:
            print(f"    - {pair}: ledger {v:.4f} vs exchange {ph:.4f} (delta {d:.4f})")
        print("\n  LINK/SOL ~2x gap = exchange holds size not in current-cycle proof.")
        print("  SUI gap = ledger over-counts; need TP/close fills from exchange history.")
        print("  Options: run engine/runner.py for full reconciler, or manual flatten on Binance.")
    else:
        print("\nAll pairs match. Safe to Start Monitoring.")

    print("\nDone. Refresh Streamlit Pre-Flight Sync.")


if __name__ == "__main__":
    main()
