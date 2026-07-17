# SESSION_LOG.md — factual session records (no reflection, just the facts)

---

## 2026-07-17 (session)

### Verified today (method)
- fetch_ticker fix (commit 35caa9c, branch fix/exchangeinterface-fetch_ticker): 6 call sites resolved; smoke + targeted tests PASS; FULL suite later run = 455 passed / 2 failed (env test_ghost_clearing.py, pre-existing).
- A7 deflate fix (commit 72ba4be, branch fix/a7-reset-cleared-deflate): 5 rows terminal-statused reset_cleared; verified.
- Model chain selected via 3-incident benchmark (SOL CID / ADR-006 / runner __init__ mixin): all 3 candidates 3/3. DEFAULT=nano-omni-reasoning:free, FALLBACK=ultra:free, DELEGATION=laguna-m.1:free. Config.yaml default applied live.
- check_model_health.py (Level 1, commit 1156c7a) + session_start_check.py (commit 8beae95) + tests/test_session_start_check.py (4 PASS, commit 9dd7b8d): verified ad-hoc + pytest.
- Windows Task Scheduler task CQB_SessionStartCheck registered (DAILY 09:05, StartWhenAvailable=TRUE), verified present + dry-run exit=0 (commit 0838a4f).
- SocketLock shutdown-reason logging (commit e50ec20, branch fix/socketlock-shutdown-reason): additive log line, verified both reason paths + compile.

### Still open
- auth2015: -2015 on fapiPrivateGetIncome. NARROWED to income/read-permission scope disabled on API key (NOT IP, NOT invalid key — fetch_positions/fetch_ticker/fetch_my_trades all succeed same IP+key). Bot NEVER calls it in production. PARKED — operator's call on Binance key permission. Non-blocking.
- (2) UI reconnecting-state change (bot_manager.py red/reconnecting logic): NOT started — hermes to provide a written plan first; no code until approved.
- Hermes memory store: had a fail-loop 2026-07-17 (junk entry cleaned attempt 2); 8 valid entries, 2,080/2,200. No open memory item.

### New diagnostic facts learned (so next session doesn't re-diagnose from scratch)
- UI red = ERROR_STOP log action (bot_manager.py:210-234), NOT a transient DNS retry. WS retry (websocket_handler.py:200-246) is UNCAPPED (5s/10s infinite loop, no backoff escalation, no circuit-breaker). So a sustained DNS drop keeps the process alive but shows red because live exchange data can't be fetched.
- Hermes cron jobs = InProcessCronScheduler (60s in-process ticker), NOT Windows Task Scheduler. One-time jobs silently never fire if laptop off — session-start date-check + OS Task Scheduler are the real nets.
- 2026-07-17 engine monitoring drops (SocketLock release gaps at 08:21/09:22/12:10/13:18): root cause = intermittent DNS/network loss to demo-fapi.binance.com (getaddrinfo failed cascade at 08:21), NOT the scheduler task/deploys/cron. The CQB_SessionStartCheck 09:05 task does not touch the engine process.
- OpenRouter live free-tier: 20 :free models; hy3:free EXPIRED 2026-07-21; laguna-m.1:free expires 2026-07-28 (our DELEGATION). nano-omni-reasoning:free purpose-built reasoning, fastest (0.60s median), was NOT in cached config.

### Branches open (PR-ready)
- fix/a7-reset-cleared-deflate (72ba4be)
- fix/exchangeinterface-fetch_ticker (35caa9c)
- fix/model-health-check (1156c7a / 8beae95 / 9dd7b8d / 0838a4f)
- fix/socketlock-shutdown-reason (e50ec20)
