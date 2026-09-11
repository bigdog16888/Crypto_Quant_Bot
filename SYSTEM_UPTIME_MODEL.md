# SYSTEM_UPTIME_MODEL.md — How this system behaves across uptime gaps

**Written 2026-09-07** after the 67-hour weekend gap (engine died with a user-initiated
Windows reboot Fri 2026-09-04 16:42, machine off until Mon 09-07 07:37, engine
restarted 11:34 by the operator). The recovery from that gap is the reference
evidence for everything below.

## The model, plainly

This system is **not designed to run 24/7**. The laptop sleeps, reboots, and shuts
down — that is expected and normal. The engine has **no OS-level auto-restart**:
when the OS goes down, the engine dies with it (no clean shutdown, SocketLock
released only on process death), and it stays dead until a human clicks
**Start Monitoring** in the Streamlit UI (which spawns `engine/run_engine.py`).

Do not treat "engine found running" or "engine found dead" after a gap as an
incident by itself. Verify state, don't assume either way.

## What keeps working while the engine is dead

**Resting exchange orders are the protection.** TPs and grids are live limit
orders sitting on Binance. During the 67h weekend gap:

- SOL grid `CQB_100001_GRID_29_2` filled 0.18 (short add), SOL TP partial-filled 0.09.
- BNB grid `CQB_10007_GRID_14_3` filled 0.02.
- On restart, `PRE-COMMIT-RESOLVE` restored all 4 fills from exchange truth,
  `SNAP-ALLOCATE` assigned pair nets to single bots, first barrier parity was
  green, new TPs went live within ~10 s of `TRADING MODE ACTIVE`.

So a position is **TP-protected during downtime** (the TP keeps resting on the
exchange and can fill). The weekend proved it end-to-end.

## What does NOT work while the engine is dead

- **No stop-loss on the exchange** — TPs are profit-taking limits; there is no
  resting SL. A large adverse move against an open position runs until the
  engine returns (or margin call). This is the real unattended risk.
- No TP re-pricing (EE-DECAY stepping), no grid maintenance, no martingale
  re-placement, no hedge/INV-30 management, no O-3 circuit breaker, no
  emergency-liquidation path — all of these are engine processes.
- Fills that happen during the gap are queued in exchange history, credited on
  restart by the startup barrier (offline-fill scan + PRE-COMMIT-RESOLVE).

## Safe to leave running unattended

- A machine that stays on with the engine cycling: the engine self-manages TPs,
  grids, freeze-guard, O-3, and reconciliation. Overnight-weekend gaps are
  recovered safely by design — that is exactly what the outage-recovery path
  (offline-fill scan, PRE-COMMIT-RESOLVE, SNAP-ALLOCATE, GTR) is built for.

## NOT safe unattended

- Open positions with no resting TP (should not normally exist — the barrier and
  TP-maintenance replace TPs within seconds of restart; verify anyway).
- Expecting the engine to come back by itself after a reboot — it will not.
  Someone must click Start Monitoring.
- Long gaps on volatile pairs with large positions: no exchange-side stop-loss
  means downside is unbounded while dead. Keep sizes small or close before
  planned long shutdowns.

## Operator checklist after ANY uptime gap (before trusting health)

Run these in order; each has a dated log line or command behind it:

1. **Liveness**: port 19888 LISTENING (`netstat -ano | grep 19888`) + `engine.pid`
   matches + `tail engine.log` timestamp within ~12 s.
2. **Startup completed**: `Startup Sync Complete` AND `TRADING MODE ACTIVE` lines
   present with today's timestamp (a live process without these is not trading).
3. **Outage reconciliation**: `[STARTUP-BARRIER] Outage window: ~Xh` +
   PRE-COMMIT-RESOLVE / OFFLINE-SYNC lines — read WHAT filled during the gap and
   that gaps were zero (`Zero gaps` / parity OK).
4. **PREFLIGHT**: passed with issues count understood (e.g. "In trade, 0 orders"
   self-heals on TP re-place — confirm a TP placement line follows).
5. **Per-pair parity**: `RECON AUDIT [PAIR]` + `GLOBAL-NET-OK` for every active
  pair; PLAUSIBILITY-BLOCK lines only on known flat siblings (pair consensus OK).
6. **Freeze-guard**: ETH/LINK pair-block lines firing (`skipping resolve_net_mismatch`).
7. **Cycling**: `[PERIODIC] cycle N` increments across minutes; Circuit Check
   equity sane and flat-drifting.
8. **O-3 silent**: zero breaker lines today.
9. Only then: trust the UI. (The UI's order table can show `NO ORDERS` transiently
   from cache/render lag — the exchange + engine.log are the source of truth;
   verify with a live `fetch_open_orders` before believing a "missing orders" banner.)

## Known gaps handled by recovery (observed 2026-09-07)

| Gap event | Duration | Recovery result |
|---|---|---|
| User reboot Fri 16:42 → Mon 07:37 boot, 11:34 engine start | ~67h dead | 4 weekend fills restored, parity green, TPs live in <1 min, zero orphans |
| DEPLOY-OUTDATED self-kill (freshness guard) 09-04 14:16 → 15:39 restart | ~2.4h | SOL entry fill restored, TP live, clean |

Both followed the checklist above and ended GREEN.
