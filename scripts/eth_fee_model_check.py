"""Fee model check — read-only. Answers: (1) is USDC zero-fee maker-only?
(2) did the orphan close pay 0.045% because it was a MARKET (taker) order?
Evidence: exchange fee schedule + commission rates on actual fills."""
import sys, json, datetime
sys.path.insert(0, ".")
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface()

print("=== 1) EXCHANGE FEE SCHEDULE (fetch_trading_fees) ===")
try:
    fees = ex.fetch_trading_fees(["ETH/USDC:USDC"])
    print(json.dumps(fees, indent=1, default=str)[:3000])
except Exception as e:
    print("fetch_trading_fees err:", type(e).__name__, e)
    # fallback: markets
    try:
        mkts = ex.fetch_markets()
        for m in mkts:
            if "USDC" in str(m.get("symbol", "")) and "ETH" in str(m.get("symbol", "")):
                print("market:", m.get("symbol"), "maker:", m.get("maker"), "taker:", m.get("taker"))
    except Exception as e2:
        print("markets err:", e2)

print()
print("=== 2) WHICH USD-STABLE PAIRS EXIST (USDC vs USD1) ===")
try:
    mkts = ex.fetch_markets()
    stables = {}
    for m in mkts:
        s = str(m.get("symbol", ""))
        for tag in ("USDC", "USD1", "USDT"):
            if s.endswith(f":{tag}"):
                stables.setdefault(tag, []).append(s)
    for tag, syms in stables.items():
        print(f"{tag}: {len(syms)} pairs; e.g. {syms[:6]}")
except Exception as e:
    print("markets err:", e)

print()
print("=== 3) COMMISSION RATES ON ACTUAL FILLS ===")
# The orphan close (MARKET/taker): trade 108108218
print("close (MARKET sell, taker): commission=1.01535221 cost=2256.33826 -> rate = %.6f%%" % (1.01535221 / 2256.33826 * 100))

# Earlier fills: TP buys @2405.95 (limit GTX postOnly = MAKER), trade ids 107418582-107418585
# and DUST market sells 107418577/107418578 (taker), from income records.
try:
    inc = ex.fetch_income(symbol="ETH/USDC:USDC")
    if isinstance(inc, str):
        inc = json.loads(inc)
    want = {"107418582", "107418583", "107418584", "107418585", "107418577", "107418578",
            "107418579", "107418580", "107418581", "108108218"}
    for e in inc:
        tid = str(e.get("tradeId", ""))
        if tid in want and e.get("incomeType") == "COMMISSION":
            ts = datetime.datetime.fromtimestamp(int(e.get("time", 0)) / 1000).strftime("%m-%d %H:%M")
            print(f"tradeId={tid} {ts} COMMISSION={e.get('income')} (see income REALIZED_PNL entry for notional)")
except Exception as e:
    print("income err:", e)

# Notionals for those trades (from the earlier 48h pull):
notional = {
    "107418577": 7.2186, "107418578": 7.21857,          # DUST sells (market)
    "107418579": 91.43522, "107418580": 24.061,          # FLATTEN sells (market)
    "107418581": 7271.02266,                             # FLATTEN sell (market)
    "107418582": 24.0595, "107418583": 31.27735,         # TP buys @2405.95 (limit maker)
    "107418584": 28.8714, "107418585": 1044.1823,        # TP buys (maker)
    "108108218": 2256.33826,                             # orphan close (market)
}
print()
print("=== 4) RATE TABLE (commission / notional) ===")
commissions = {
    "107418577": 0.00324837, "107418578": 0.00324835,
    "108108218": 1.01535221,
}
print("pre-filled from earlier income pull (DUST + close); TP buys pending from section 3 output")
for tid, comm in commissions.items():
    n = notional.get(tid)
    if n:
        print(f"tradeId={tid} comm={comm} notional={n} -> {comm / n * 100:.4f}%")
